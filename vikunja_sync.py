import os
import json
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

from dateutil import parser as dateparse
from dotenv import load_dotenv
import requests
from ics import Calendar, Event

# Google API
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ------------------
# Load .env
# ------------------
load_dotenv()

VIKUNJA_API_BASE   = os.getenv("VIKUNJA_API_BASE", "").rstrip("/")
VIKUNJA_USERNAME   = os.getenv("VIKUNJA_USERNAME", "")
VIKUNJA_PASSWORD   = os.getenv("VIKUNJA_PASSWORD", "")
VIKUNJA_VERIFY_SSL = os.getenv("VIKUNJA_VERIFY_SSL", "true").lower() == "true"

PROJECT_IDS_RAW = os.getenv("PROJECT_IDS", "").strip()

GOOGLE_CALENDAR_NAME   = os.getenv("GOOGLE_CALENDAR_NAME", "Vikunja Tasks")
GOOGLE_CREDENTIALS_FILE= os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
GOOGLE_TOKEN_FILE      = os.getenv("GOOGLE_TOKEN_FILE", "token.json")
STATE_FILE             = os.getenv("STATE_FILE", ".state/state.json")
ICS_OUTPUT             = os.getenv("ICS_OUTPUT", ".out/vikunja_calendar.ics")
TIMEZONE               = os.getenv("TIMEZONE", "UTC")

REMINDER_MINUTES_RAW   = os.getenv("REMINDER_MINUTES", "").strip()

SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Parse PROJECT_IDS env ("2,5,9" -> [2,5,9])
PROJECT_IDS: List[int] = []
if PROJECT_IDS_RAW:
    PROJECT_IDS = [int(x) for x in PROJECT_IDS_RAW.split(",") if x.strip().isdigit()]

# Parse reminders
REMINDER_MINUTES: List[int] = []
if REMINDER_MINUTES_RAW:
    REMINDER_MINUTES = [int(x) for x in REMINDER_MINUTES_RAW.split(",") if x.strip().lstrip("-").isdigit()]

# Ensure output folders exist
for path in [STATE_FILE, ICS_OUTPUT]:
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)

# ------------------
# State helpers
# ------------------
def load_state() -> Dict[str, Any]:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"events": {}, "calendar_id": None}

def save_state(state: Dict[str, Any]):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

# ------------------
# Google Calendar helpers
# ------------------
def google_service():
    creds: Optional[Credentials] = None
    if os.path.exists(GOOGLE_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(GOOGLE_TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            from google.auth.transport.requests import Request
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(GOOGLE_CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(GOOGLE_TOKEN_FILE, "w") as token:
            token.write(creds.to_json())
    return build("calendar", "v3", credentials=creds)

def ensure_calendar(service, state) -> str:
    # Use cached calendar_id if still valid
    if state.get("calendar_id"):
        try:
            service.calendars().get(calendarId=state["calendar_id"]).execute()
            return state["calendar_id"]
        except Exception:
            pass
    # Find by name
    page_token = None
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
    cal = {"summary": GOOGLE_CALENDAR_NAME, "timeZone": TIMEZONE}
    created = service.calendars().insert(body=cal).execute()
    state["calendar_id"] = created["id"]
    return created["id"]

def set_calendar_default_reminders(service, calendar_id: str, minutes_list: List[int]):
    overrides = [{"method": "popup", "minutes": m} for m in minutes_list]
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
        raise SystemExit("VIKUNJA_API_BASE, VIKUNJA_USERNAME and VIKUNJA_PASSWORD must be set in .env")

    login_url = f"{VIKUNJA_API_BASE}/login"
    try:
        r = requests.post(
            login_url,
            json={"username": VIKUNJA_USERNAME, "password": VIKUNJA_PASSWORD},
            timeout=30,
            verify=VIKUNJA_VERIFY_SSL,
        )
        r.raise_for_status()
        data = r.json()
        token = data.get("token") or data.get("access_token") or data.get("jwt")
        if not token:
            raise RuntimeError(f"Login response missing token field: {data}")
        return token
    except Exception as e:
        raise SystemExit(f"Failed to login to Vikunja: {e}")

def vikunja_headers(token: str):
    return {"Authorization": f"Bearer {token}"}

def fetch_projects_map(token: str) -> Dict[int, str]:
    url = f"{VIKUNJA_API_BASE}/projects"
    resp = requests.get(url, headers=vikunja_headers(token), timeout=30, verify=VIKUNJA_VERIFY_SSL)
    resp.raise_for_status()
    return {p["id"]: p["title"] for p in resp.json()}

def fetch_tasks(token: str) -> List[Dict[str, Any]]:
    tasks: List[Dict[str, Any]] = []
    if PROJECT_IDS:
        for pid in PROJECT_IDS:
            url = f"{VIKUNJA_API_BASE}/projects/{pid}/tasks"
            r = requests.get(url, headers=vikunja_headers(token), timeout=30, verify=VIKUNJA_VERIFY_SSL)
            r.raise_for_status()
            items = r.json()
            if isinstance(items, dict) and "tasks" in items:
                items = items["tasks"]
            tasks.extend(items)
    else:
        # Pull all tasks (paginate if supported)
        page = 1
        while True:
            url = f"{VIKUNJA_API_BASE}/tasks/all?page={page}"
            r = requests.get(url, headers=vikunja_headers(token), timeout=30, verify=VIKUNJA_VERIFY_SSL)
            r.raise_for_status()
            data = r.json()
            items = data if isinstance(data, list) else data.get("tasks", [])
            if not items:
                break
            tasks.extend(items)
            page += 1
    return tasks

# ------------------
# Transform & Sync
# ------------------
def iso_to_utc_dt(iso_str: str) -> Optional[datetime]:
    if not iso_str or iso_str == "0001-01-01T00:00:00Z":
        return None
    dt = dateparse.isoparse(iso_str)
    return dt.astimezone(timezone.utc)

def task_key(task: Dict[str, Any]) -> str:
    return f"{task['project_id']}:{task['id']}"

def is_overdue(task: Dict[str, Any]) -> bool:
    due = iso_to_utc_dt(task.get("due_date"))
    if not due:
        return False
    return (not task.get("done")) and (due < datetime.now(timezone.utc))

def build_event_body(task: Dict[str, Any], project_name: str) -> Optional[Dict[str, Any]]:
    title = task.get("title", "Untitled")
    desc = task.get("description") or ""
    start_dt = iso_to_utc_dt(task.get("start_date"))
    due_dt = iso_to_utc_dt(task.get("due_date"))
    updated_dt = iso_to_utc_dt(task.get("updated"))
    done = bool(task.get("done"))

    if not due_dt:
        return None

    # If both start and due exist and due > start, use both; else use due as an instant point
    if start_dt and due_dt and due_dt > start_dt:
        start_iso = start_dt.isoformat()
        end_iso = due_dt.isoformat()
    else:
        start_iso = due_dt.isoformat()
        end_iso = due_dt.isoformat()

    status_prefix = "✅ " if done else ""
    summary = f"{status_prefix}[{project_name}] {title}"

    body: Dict[str, Any] = {
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
            "url": f"{VIKUNJA_API_BASE.replace('/api/v1', '')}/tasks/{task['id']}"
        }
    }

    # Per-event reminders
    if REMINDER_MINUTES:
        body["reminders"] = {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": m} for m in REMINDER_MINUTES]
        }
    else:
        body["reminders"] = {"useDefault": True}

    if done:
        body["colorId"] = "10"  # green
    return body

def tasks_to_ics(tasks: List[Dict[str, Any]], projects_map: Dict[int, str]):
    cal = Calendar()
    for t in tasks:
        due_dt = iso_to_utc_dt(t.get("due_date"))
        if not due_dt:
            continue
        project_name = projects_map.get(t["project_id"], f"Project {t['project_id']}")
        e = Event()
        e.name = f"{'✅ ' if t.get('done') else ''}[{project_name}] {t.get('title','Untitled')}"
        e.description = (t.get("description") or "")
        start_dt = iso_to_utc_dt(t.get("start_date")) or due_dt
        e.begin = start_dt
        e.end = due_dt
        cal.events.add(e)
    with open(ICS_OUTPUT, "w", encoding="utf-8") as f:
        f.writelines(cal)

# ------------------
# Main
# ------------------
def main():
    # Sanity
    if not VIKUNJA_API_BASE or not VIKUNJA_USERNAME or not VIKUNJA_PASSWORD:
        raise SystemExit("VIKUNJA_API_BASE, VIKUNJA_USERNAME and VIKUNJA_PASSWORD must be set in .env")

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
    candidates: List[Dict[str, Any]] = []

    for t in tasks:
        if not t.get("due_date") or t["due_date"] == "0001-01-01T00:00:00Z":
            continue

        key = task_key(t)
        prev = state["events"].get(key)
        done = bool(t.get("done"))
        overdue_now = is_overdue(t)
        updated_str = t.get("updated")

        if done:
            candidates.append(t)
        else:
            if overdue_now:
                if prev and (prev.get("done") is False) and (prev.get("last_updated") == updated_str):
                    continue
                candidates.append(t)
            else:
                candidates.append(t)

    # Upsert to Google Calendar
    for t in candidates:
        key = task_key(t)
        body = build_event_body(t, projects_map.get(t["project_id"], f"Project {t['project_id']}"))
        if not body:
            continue

        known = state["events"].get(key)
        try:
            if known and known.get("event_id"):
                ev = service.events().patch(
                    calendarId=cal_id,
                    eventId=known["event_id"],
                    body=body,
                    sendUpdates="none"
                ).execute()
                state["events"][key] = {
                    "event_id": ev["id"],
                    "done": bool(t.get("done")),
                    "last_updated": t.get("updated"),
                }
            else:
                ev = service.events().insert(
                    calendarId=cal_id,
                    body=body,
                    sendUpdates="none"
                ).execute()
                state["events"][key] = {
                    "event_id": ev["id"],
                    "done": bool(t.get("done")),
                    "last_updated": t.get("updated"),
                }
        except Exception as e:
            print(f"[WARN] Upsert failed for {key}: {e}")

    # Optional ICS export
    tasks_to_ics(tasks, projects_map)

    save_state(state)
    print(f"✅ Sync complete. Calendar: {GOOGLE_CALENDAR_NAME} | State: {STATE_FILE} | ICS: {ICS_OUTPUT}")

if __name__ == "__main__":
    main()
