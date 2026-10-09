"""Test doubles: a real HTTP server faking the Vikunja API, and an in-memory Google Calendar."""

import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import override
from urllib.parse import parse_qs, urlsplit

from pydantic import BaseModel, ConfigDict, RootModel

from vikunja_sync.google_calendar import (
    CalendarBody,
    CalendarListEntry,
    CalendarListPage,
    CreatedEvent,
    EventBody,
)
from vikunja_sync.vikunja import (
    LoginRequest,
    LoginResponse,
    Project,
    Projects,
    TaskList,
    VikunjaTask,
)

# ------------------
# Fake Vikunja API
# ------------------
API_PREFIX = "/api/v1"


class RecordedRequest(BaseModel):
    method: str
    path: str
    authorization: str | None


class ApiMessage(BaseModel):
    message: str


class Tasks(RootModel[list[VikunjaTask]]):
    pass


class FakeVikunja(BaseModel):
    """In-memory Vikunja data served over HTTP by `serve_vikunja`."""

    model_config = ConfigDict(validate_assignment=True)

    username: str = "alice"
    password: str = "s3cret"
    token: str = "jwt-token-123"
    # Name of the token field in the login response (anything else = token missing)
    token_field: str = "token"
    projects: list[Project] = []
    tasks: list[VikunjaTask] = []
    page_size: int = 2
    # Wrap /projects/{id}/tasks responses in {"tasks": [...]} like some Vikunja versions do
    wrap_project_tasks: bool = False
    requests: list[RecordedRequest] = []
    # API base URL, set while served by `serve_vikunja`
    base_url: str = ""

    def tasks_page(self, page: int) -> list[VikunjaTask]:
        start = (page - 1) * self.page_size
        return self.tasks[start : start + self.page_size]

    def project_tasks(self, project_id: int) -> list[VikunjaTask]:
        return [t for t in self.tasks if t.project_id == project_id]


def _make_handler(fake: FakeVikunja) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        @override
        def log_message(
            self, format: str, *args: object
        ) -> None:  # silence test output
            pass

        def _send(self, status: int, payload: BaseModel, *, include: set[str] | None = None) -> None:
            body = payload.model_dump_json(include=include).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _record(self) -> None:
            fake.requests.append(
                RecordedRequest(
                    method=self.command,
                    path=self.path,
                    authorization=self.headers.get("Authorization"),
                )
            )

        def do_POST(self) -> None:
            self._record()
            if self.path != f"{API_PREFIX}/login":
                self._send(404, ApiMessage(message="not found"))
                return
            length = int(self.headers.get("Content-Length") or 0)
            creds = LoginRequest.model_validate_json(self.rfile.read(length))
            if creds != LoginRequest(username=fake.username, password=fake.password):
                self._send(412, ApiMessage(message="Wrong username or password."))
                return
            token = fake.token
            response = LoginResponse(token=token, access_token=token, jwt=token)
            self._send(200, response, include={fake.token_field})

        def do_GET(self) -> None:
            self._record()
            if self.headers.get("Authorization") != f"Bearer {fake.token}":
                message = "missing, malformed, expired or otherwise invalid token"
                self._send(401, ApiMessage(message=message))
                return
            url = urlsplit(self.path)
            if url.path == f"{API_PREFIX}/projects":
                self._send(200, Projects(fake.projects))
                return
            if url.path == f"{API_PREFIX}/tasks/all":
                page = int(parse_qs(url.query).get("page", ["1"])[0])
                self._send(200, Tasks(fake.tasks_page(page)))
                return
            match = re.fullmatch(rf"{API_PREFIX}/projects/(\d+)/tasks", url.path)
            if match:
                items = fake.project_tasks(int(match.group(1)))
                self._send(200, TaskList(tasks=items) if fake.wrap_project_tasks else Tasks(items))
                return
            self._send(404, ApiMessage(message="not found"))

    return Handler


@contextmanager
def serve_vikunja(fake: FakeVikunja) -> Iterator[str]:
    """Run the fake Vikunja on a random local port; yields (and sets `fake.base_url` to) the API base URL."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(fake))
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    fake.base_url = f"http://127.0.0.1:{server.server_address[1]}{API_PREFIX}"
    try:
        yield fake.base_url
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


# ------------------
# Fake Google Calendar
# ------------------
class NotFound(Exception):
    pass


class FakeCalendarService:
    """In-memory stand-in for the Google Calendar API (satisfies `CalendarService`)."""

    def __init__(self, list_page_size: int = 100) -> None:
        self.list_page_size = list_page_size
        self.calendar_bodies: dict[str, CalendarBody] = {}
        self.list_entries: dict[str, CalendarListEntry] = {}
        self.event_store: dict[str, dict[str, EventBody]] = {}
        self.event_counter = 0
        self.inserts: list[str] = []
        self.patches: list[str] = []
        self.fail_summaries: set[str] = set()

    def add_calendar(self, cal_id: str, summary: str, time_zone: str = "UTC") -> None:
        self.calendar_bodies[cal_id] = CalendarBody(summary=summary, time_zone=time_zone)
        self.list_entries[cal_id] = CalendarListEntry(id=cal_id, summary=summary)

    # Calendars
    def get_calendar(self, calendar_id: str) -> CalendarBody:
        if calendar_id not in self.calendar_bodies:
            raise NotFound(calendar_id)
        return self.calendar_bodies[calendar_id]

    def insert_calendar(self, body: CalendarBody) -> CalendarListEntry:
        cal_id = f"cal-{len(self.calendar_bodies) + 1}"
        self.add_calendar(cal_id, body.summary, body.time_zone)
        return CalendarListEntry(id=cal_id, summary=body.summary)

    # Calendar list
    def list_calendars(self, page_token: str | None) -> CalendarListPage:
        entries = list(self.list_entries.values())
        start = int(page_token) if page_token else 0
        end = start + self.list_page_size
        return CalendarListPage(
            items=entries[start:end],
            next_page_token=str(end) if end < len(entries) else None,
        )

    def get_calendar_list_entry(self, calendar_id: str) -> CalendarListEntry:
        if calendar_id not in self.list_entries:
            raise NotFound(calendar_id)
        return self.list_entries[calendar_id].model_copy(deep=True)

    def update_calendar_list_entry(self, entry: CalendarListEntry) -> CalendarListEntry:
        if entry.id not in self.list_entries:
            raise NotFound(entry.id)
        self.list_entries[entry.id] = entry
        return entry

    # Events
    def _check(self, calendar_id: str, body: EventBody) -> None:
        if calendar_id not in self.calendar_bodies:
            raise NotFound(calendar_id)
        if body.summary in self.fail_summaries:
            raise RuntimeError(f"simulated API failure for {body.summary!r}")

    def insert_event(self, calendar_id: str, body: EventBody) -> CreatedEvent:
        self._check(calendar_id, body)
        self.event_counter += 1
        event_id = f"evt-{self.event_counter}"
        self.event_store.setdefault(calendar_id, {})[event_id] = body
        self.inserts.append(event_id)
        return CreatedEvent(id=event_id)

    def patch_event(self, calendar_id: str, event_id: str, body: EventBody) -> CreatedEvent:
        self._check(calendar_id, body)
        events = self.event_store.setdefault(calendar_id, {})
        if event_id not in events:
            raise NotFound(event_id)
        events[event_id] = body
        self.patches.append(event_id)
        return CreatedEvent(id=event_id)
