import os
import json
from datetime import datetime, timezone
from typing import NotRequired, Protocol, TypedDict, cast

from dateutil import parser as dateparse
from dotenv import load_dotenv
import requests
from ics import Calendar, Event

# Google API
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ------------------
# Load .env
# ------------------
load_dotenv()

VIKUNJA_API_BASE = os.getenv("VIKUNJA_API_BASE", "").rstrip("/")
VIKUNJA_USERNAME = os.getenv("VIKUNJA_USERNAME", "")
VIKUNJA_PASSWORD = os.getenv("VIKUNJA_PASSWORD", "")
VIKUNJA_VERIFY_SSL = os.getenv("VIKUNJA_VERIFY_SSL", "true").lower() == "true"

PROJECT_IDS_RAW = os.getenv("PROJECT_IDS", "").strip()

GOOGLE_CALENDAR_NAME = os.getenv("GOOGLE_CALENDAR_NAME", "Vikunja Tasks")
GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
GOOGLE_TOKEN_FILE = os.getenv("GOOGLE_TOKEN_FILE", "token.json")
STATE_FILE = os.getenv("STATE_FILE", ".state/state.json")
ICS_OUTPUT = os.getenv("ICS_OUTPUT", ".out/vikunja_calendar.ics")
TIMEZONE = os.getenv("TIMEZONE", "UTC")

REMINDER_MINUTES_RAW = os.getenv("REMINDER_MINUTES", "").strip()

SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Parse PROJECT_IDS env ("2,5,9" -> [2,5,9])
PROJECT_IDS: list[int] = []
if PROJECT_IDS_RAW:
    PROJECT_IDS = [int(x) for x in PROJECT_IDS_RAW.split(",") if x.strip().isdigit()]

# Parse reminders
REMINDER_MINUTES: list[int] = []
if REMINDER_MINUTES_RAW:
    REMINDER_MINUTES = [
        int(x)
        for x in REMINDER_MINUTES_RAW.split(",")
        if x.strip().lstrip("-").isdigit()
    ]

# Ensure output folders exist
for path in [STATE_FILE, ICS_OUTPUT]:
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)

# ------------------
# Types
# ------------------
type JsonObject = dict[str, object]


class VikunjaTask(TypedDict):
    id: int
    project_id: int
    title: str
    description: str
    done: bool
    due_date: str
    start_date: str
    updated: str


class EventState(TypedDict):
    event_id: str
    done: bool
    last_updated: str | None


class State(TypedDict):
    events: dict[str, EventState]
    calendar_id: str | None


class EventReminder(TypedDict):
    method: str
    minutes: int


class EventReminders(TypedDict):
    useDefault: bool
    overrides: NotRequired[list[EventReminder]]


class EventDateTime(TypedDict):
    dateTime: str


class EventExtendedProperties(TypedDict):
    private: dict[str, str]


class EventSource(TypedDict):
    title: str
    url: str


class EventBody(TypedDict):
    summary: str
    description: str
    start: EventDateTime
    end: EventDateTime
    extendedProperties: EventExtendedProperties
    source: EventSource
    reminders: EventReminders
    colorId: NotRequired[str]


class CreatedEvent(TypedDict):
    id: str


class CalendarBody(TypedDict):
    summary: str
    timeZone: str


class CalendarListEntry(TypedDict):
    id: str
    summary: NotRequired[str]
    defaultReminders: NotRequired[list[EventReminder]]


class CalendarListPage(TypedDict):
    items: NotRequired[list[CalendarListEntry]]
    nextPageToken: NotRequired[str]


# Minimal, Any-free view of the Google Calendar v3 resource (only what this script uses).
# The official stubs expose `**kwargs: Any` and `dict[str, Any]` fields everywhere.
class Executable[T](Protocol):
    def execute(self) -> T: ...


class CalendarsResource(Protocol):
    def get(self, *, calendarId: str) -> Executable[CalendarBody]: ...
    def insert(self, *, body: CalendarBody) -> Executable[CalendarListEntry]: ...


class CalendarListResource(Protocol):
    def list(self, *, pageToken: str | None = None) -> Executable[CalendarListPage]: ...
    def get(self, *, calendarId: str) -> Executable[CalendarListEntry]: ...
    def update(
        self, *, calendarId: str, body: CalendarListEntry
    ) -> Executable[CalendarListEntry]: ...


class EventsResource(Protocol):
    def insert(
        self, *, calendarId: str, body: EventBody, sendUpdates: str
    ) -> Executable[CreatedEvent]: ...
    def patch(
        self, *, calendarId: str, eventId: str, body: EventBody, sendUpdates: str
    ) -> Executable[CreatedEvent]: ...


class CalendarService(Protocol):
    def calendars(self) -> CalendarsResource: ...
    def calendarList(self) -> CalendarListResource: ...
    def events(self) -> EventsResource: ...


# ------------------
# JSON helpers
# ------------------
def as_object(value: object) -> JsonObject:
    if not isinstance(value, dict):
        raise TypeError(f"Expected a JSON object, got {type(value).__name__}")
    return cast(JsonObject, value)


def as_list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise TypeError(f"Expected a JSON array, got {type(value).__name__}")
    return cast(list[object], value)


def get_str(obj: JsonObject, key: str, default: str = "") -> str:
    value = obj.get(key)
    return value if isinstance(value, str) else default


def get_int(obj: JsonObject, key: str) -> int:
    value = obj.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"Expected integer field {key!r}, got {value!r}")
    return value


def parse_task(raw: object) -> VikunjaTask:
    obj = as_object(raw)
    return {
        "id": get_int(obj, "id"),
        "project_id": get_int(obj, "project_id"),
        "title": get_str(obj, "title", "Untitled"),
        "description": get_str(obj, "description"),
        "done": bool(obj.get("done")),
        "due_date": get_str(obj, "due_date"),
        "start_date": get_str(obj, "start_date"),
        "updated": get_str(obj, "updated"),
    }


def response_json(resp: requests.Response) -> object:
    return cast(object, resp.json())


# ------------------
# State helpers
# ------------------
def load_state() -> State:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return cast(State, json.load(f))
    return {"events": {}, "calendar_id": None}


def save_state(state: State) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ------------------
# Google Calendar helpers
# ------------------
def google_service() -> CalendarService:
    # google.oauth2.credentials ships `py.typed` but its methods are unannotated.
    creds: Credentials | None = None
    if os.path.exists(GOOGLE_TOKEN_FILE):
        creds = cast(
            Credentials,
            Credentials.from_authorized_user_file(GOOGLE_TOKEN_FILE, SCOPES),  # type: ignore[no-untyped-call, misc]
        )
    if not creds or not cast(bool, creds.valid):
        if (
            creds
            and cast(bool, creds.expired)
            and cast(str | None, creds.refresh_token)
        ):
            creds.refresh(Request())  # type: ignore[no-untyped-call]
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                GOOGLE_CREDENTIALS_FILE, SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open(GOOGLE_TOKEN_FILE, "w") as token:
            token.write(cast(str, creds.to_json()))  # type: ignore[no-untyped-call]
    return cast(CalendarService, build("calendar", "v3", credentials=creds))


def ensure_calendar(service: CalendarService, state: State) -> str:
    # Use cached calendar_id if still valid
    cached_id = state["calendar_id"]
    if cached_id:
        try:
            service.calendars().get(calendarId=cached_id).execute()
            return cached_id
        except Exception:
            pass
    # Find by name
    page_token: str | None = None
    while True:
        cals = service.calendarList().list(pageToken=page_token).execute()
        for cal in cals.get("items", []):
            if cal.get("summary") == GOOGLE_CALENDAR_NAME:
                state["calendar_id"] = cal["id"]
                return cal["id"]
        page_token = cals.get("nextPageToken")
        if not page_token:
            break
    # Create new
    new_cal: CalendarBody = {"summary": GOOGLE_CALENDAR_NAME, "timeZone": TIMEZONE}
    created = service.calendars().insert(body=new_cal).execute()
    state["calendar_id"] = created["id"]
    return created["id"]


def set_calendar_default_reminders(
    service: CalendarService, calendar_id: str, minutes_list: list[int]
) -> None:
    overrides: list[EventReminder] = [
        {"method": "popup", "minutes": m} for m in minutes_list
    ]
    entry = service.calendarList().get(calendarId=calendar_id).execute()
    entry["defaultReminders"] = overrides
    service.calendarList().update(calendarId=calendar_id, body=entry).execute()


# ------------------
# Vikunja API
# ------------------
def vikunja_login() -> str:
    """
    POST /login with {"username": "...", "password": "..."}
    Returns a JWT token string.
    """
    if not VIKUNJA_API_BASE or not VIKUNJA_USERNAME or not VIKUNJA_PASSWORD:
        raise SystemExit(
            "VIKUNJA_API_BASE, VIKUNJA_USERNAME and VIKUNJA_PASSWORD must be set in .env"
        )

    login_url = f"{VIKUNJA_API_BASE}/login"
    payload: dict[str, str] = {
        "username": VIKUNJA_USERNAME,
        "password": VIKUNJA_PASSWORD,
    }
    try:
        r = requests.post(
            login_url,
            json=payload,
            timeout=30,
            verify=VIKUNJA_VERIFY_SSL,
        )
        r.raise_for_status()
        data = as_object(response_json(r))
        token = (
            get_str(data, "token")
            or get_str(data, "access_token")
            or get_str(data, "jwt")
        )
        if not token:
            raise RuntimeError(f"Login response missing token field: {data}")
        return token
    except Exception as e:
        raise SystemExit(f"Failed to login to Vikunja: {e}")


def vikunja_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def fetch_projects_map(token: str) -> dict[int, str]:
    url = f"{VIKUNJA_API_BASE}/projects"
    resp = requests.get(
        url, headers=vikunja_headers(token), timeout=30, verify=VIKUNJA_VERIFY_SSL
    )
    resp.raise_for_status()
    projects = [as_object(p) for p in as_list(response_json(resp))]
    return {get_int(p, "id"): get_str(p, "title") for p in projects}


def fetch_tasks(token: str) -> list[VikunjaTask]:
    tasks: list[VikunjaTask] = []
    if PROJECT_IDS:
        for pid in PROJECT_IDS:
            url = f"{VIKUNJA_API_BASE}/projects/{pid}/tasks"
            r = requests.get(
                url,
                headers=vikunja_headers(token),
                timeout=30,
                verify=VIKUNJA_VERIFY_SSL,
            )
            r.raise_for_status()
            items = response_json(r)
            if isinstance(items, dict) and "tasks" in items:
                items = as_object(items)["tasks"]
            tasks.extend(parse_task(item) for item in as_list(items))
    else:
        # Pull all tasks (paginate if supported)
        page = 1
        while True:
            url = f"{VIKUNJA_API_BASE}/tasks/all?page={page}"
            r = requests.get(
                url,
                headers=vikunja_headers(token),
                timeout=30,
                verify=VIKUNJA_VERIFY_SSL,
            )
            r.raise_for_status()
            data = response_json(r)
            items = (
                as_list(data)
                if isinstance(data, list)
                else as_list(as_object(data).get("tasks", []))
            )
            if not items:
                break
            tasks.extend(parse_task(item) for item in items)
            page += 1
    return tasks


# ------------------
# Transform & Sync
# ------------------
def iso_to_utc_dt(iso_str: str) -> datetime | None:
    if not iso_str or iso_str == "0001-01-01T00:00:00Z":
        return None
    dt = dateparse.isoparse(iso_str)
    return dt.astimezone(timezone.utc)


def task_key(task: VikunjaTask) -> str:
    return f"{task['project_id']}:{task['id']}"


def is_overdue(task: VikunjaTask) -> bool:
    due = iso_to_utc_dt(task["due_date"])
    if not due:
        return False
    return (not task["done"]) and (due < datetime.now(timezone.utc))


def build_event_body(task: VikunjaTask, project_name: str) -> EventBody | None:
    title = task["title"]
    desc = task["description"]
    start_dt = iso_to_utc_dt(task["start_date"])
    due_dt = iso_to_utc_dt(task["due_date"])
    updated_dt = iso_to_utc_dt(task["updated"])
    done = task["done"]

    if not due_dt:
        return None

    # If both start and due exist and due > start, use both; else use due as an instant point
    if start_dt and due_dt > start_dt:
        start_iso = start_dt.isoformat()
        end_iso = due_dt.isoformat()
    else:
        start_iso = due_dt.isoformat()
        end_iso = due_dt.isoformat()

    status_prefix = "✅ " if done else ""
    summary = f"{status_prefix}[{project_name}] {title}"

    body: EventBody = {
        "summary": summary,
        "description": desc,
        "start": {"dateTime": start_iso},
        "end": {"dateTime": end_iso},
        "extendedProperties": {
            "private": {
                "vikunja_task_id": str(task["id"]),
                "vikunja_project_id": str(task["project_id"]),
                "vikunja_done": str(done),
                "vikunja_updated": (updated_dt.isoformat() if updated_dt else ""),
            }
        },
        "source": {
            "title": "Vikunja",
            "url": f"{VIKUNJA_API_BASE.replace('/api/v1', '')}/tasks/{task['id']}",
        },
        # Per-event reminders
        "reminders": (
            {
                "useDefault": False,
                "overrides": [
                    {"method": "popup", "minutes": m} for m in REMINDER_MINUTES
                ],
            }
            if REMINDER_MINUTES
            else {"useDefault": True}
        ),
    }

    if done:
        body["colorId"] = "10"  # green
    return body


def tasks_to_ics(tasks: list[VikunjaTask], projects_map: dict[int, str]) -> None:
    cal = Calendar()
    for t in tasks:
        due_dt = iso_to_utc_dt(t["due_date"])
        if not due_dt:
            continue
        project_name = projects_map.get(t["project_id"], f"Project {t['project_id']}")
        e = Event()
        e.name = f"{'✅ ' if t['done'] else ''}[{project_name}] {t['title']}"
        e.description = t["description"]
        start_dt = iso_to_utc_dt(t["start_date"]) or due_dt
        e.begin = start_dt
        e.end = due_dt
        cal.events.add(e)
    with open(ICS_OUTPUT, "w", encoding="utf-8") as f:
        f.writelines(cal)


# ------------------
# Main
# ------------------
def main() -> None:
    # Sanity
    if not VIKUNJA_API_BASE or not VIKUNJA_USERNAME or not VIKUNJA_PASSWORD:
        raise SystemExit(
            "VIKUNJA_API_BASE, VIKUNJA_USERNAME and VIKUNJA_PASSWORD must be set in .env"
        )

    # Login to Vikunja (fresh JWT)
    token = vikunja_login()

    state = load_state()
    service = google_service()
    cal_id = ensure_calendar(service, state)

    # Optional: set calendar default reminders (safety net)
    if REMINDER_MINUTES:
        try:
            set_calendar_default_reminders(service, cal_id, REMINDER_MINUTES)
        except Exception as e:
            print(f"[WARN] Could not set default reminders on calendar: {e}")

    projects_map = fetch_projects_map(token)
    tasks = fetch_tasks(token)

    # Apply your rules:
    # 2.1 if task is done -> upsert (create/update as done)
    # 2.2 if overdue and already created -> skip unless changed; if becomes done -> update
    candidates: list[VikunjaTask] = []

    for t in tasks:
        if not t["due_date"] or t["due_date"] == "0001-01-01T00:00:00Z":
            continue

        key = task_key(t)
        prev = state["events"].get(key)
        done = t["done"]
        overdue_now = is_overdue(t)
        updated_str = t["updated"]

        if done:
            candidates.append(t)
        else:
            if overdue_now:
                if (
                    prev
                    and (prev["done"] is False)
                    and (prev["last_updated"] == updated_str)
                ):
                    continue
                candidates.append(t)
            else:
                candidates.append(t)

    # Upsert to Google Calendar
    for t in candidates:
        key = task_key(t)
        body = build_event_body(
            t, projects_map.get(t["project_id"], f"Project {t['project_id']}")
        )
        if not body:
            continue

        known = state["events"].get(key)
        try:
            if known and known.get("event_id"):
                ev = (
                    service.events()
                    .patch(
                        calendarId=cal_id,
                        eventId=known["event_id"],
                        body=body,
                        sendUpdates="none",
                    )
                    .execute()
                )
                state["events"][key] = {
                    "event_id": ev["id"],
                    "done": t["done"],
                    "last_updated": t["updated"],
                }
            else:
                ev = (
                    service.events()
                    .insert(calendarId=cal_id, body=body, sendUpdates="none")
                    .execute()
                )
                state["events"][key] = {
                    "event_id": ev["id"],
                    "done": t["done"],
                    "last_updated": t["updated"],
                }
        except Exception as e:
            print(f"[WARN] Upsert failed for {key}: {e}")

    # Optional ICS export
    tasks_to_ics(tasks, projects_map)

    save_state(state)
    print(
        f"✅ Sync complete. Calendar: {GOOGLE_CALENDAR_NAME} | State: {STATE_FILE} | ICS: {ICS_OUTPUT}"
    )


if __name__ == "__main__":
    main()
