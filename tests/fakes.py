"""Test doubles: a real HTTP server faking the Vikunja API, and an in-memory Google Calendar."""

import json
import re
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast, override
from urllib.parse import parse_qs, urlsplit

from vikunja_sync import (
    CalendarBody,
    CalendarListEntry,
    CalendarListPage,
    CreatedEvent,
    EventBody,
    JsonObject,
)

# ------------------
# Fake Vikunja API
# ------------------
API_PREFIX = "/api/v1"


@dataclass
class RecordedRequest:
    method: str
    path: str
    authorization: str | None


@dataclass
class FakeVikunja:
    """In-memory Vikunja data served over HTTP by `serve_vikunja`."""

    username: str = "alice"
    password: str = "s3cret"
    token: str = "jwt-token-123"
    token_field: str = "token"
    projects: list[JsonObject] = field(default_factory=list)
    tasks: list[JsonObject] = field(default_factory=list)
    page_size: int = 2
    # Wrap /projects/{id}/tasks responses in {"tasks": [...]} like some Vikunja versions do
    wrap_project_tasks: bool = False
    requests: list[RecordedRequest] = field(default_factory=list)

    def tasks_page(self, page: int) -> list[JsonObject]:
        start = (page - 1) * self.page_size
        return self.tasks[start : start + self.page_size]

    def project_tasks(self, project_id: int) -> list[JsonObject]:
        return [t for t in self.tasks if t.get("project_id") == project_id]


def _make_handler(fake: FakeVikunja) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        @override
        def log_message(
            self, format: str, *args: object
        ) -> None:  # silence test output
            pass

        def _send(self, status: int, payload: object) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _record(self) -> None:
            fake.requests.append(
                RecordedRequest(
                    self.command, self.path, self.headers.get("Authorization")
                )
            )

        def do_POST(self) -> None:
            self._record()
            if self.path != f"{API_PREFIX}/login":
                self._send(404, {"message": "not found"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            creds = cast(object, json.loads(self.rfile.read(length)))
            if creds != {"username": fake.username, "password": fake.password}:
                self._send(412, {"message": "Wrong username or password."})
                return
            self._send(200, {fake.token_field: fake.token})

        def do_GET(self) -> None:
            self._record()
            if self.headers.get("Authorization") != f"Bearer {fake.token}":
                self._send(
                    401,
                    {
                        "message": "missing, malformed, expired or otherwise invalid token"
                    },
                )
                return
            url = urlsplit(self.path)
            if url.path == f"{API_PREFIX}/projects":
                self._send(200, fake.projects)
                return
            if url.path == f"{API_PREFIX}/tasks/all":
                page = int(parse_qs(url.query).get("page", ["1"])[0])
                self._send(200, fake.tasks_page(page))
                return
            match = re.fullmatch(rf"{API_PREFIX}/projects/(\d+)/tasks", url.path)
            if match:
                items = fake.project_tasks(int(match.group(1)))
                self._send(200, {"tasks": items} if fake.wrap_project_tasks else items)
                return
            self._send(404, {"message": "not found"})

    return Handler


@contextmanager
def serve_vikunja(fake: FakeVikunja) -> Iterator[str]:
    """Run the fake Vikunja on a random local port; yields the API base URL."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(fake))
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}{API_PREFIX}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


# ------------------
# Fake Google Calendar
# ------------------
class NotFound(Exception):
    pass


class Call[T]:
    def __init__(self, fn: Callable[[], T]) -> None:
        self._fn = fn

    def execute(self) -> T:
        return self._fn()


class FakeCalendars:
    def __init__(self, owner: "FakeCalendarService") -> None:
        self._owner = owner

    def get(self, *, calendarId: str) -> Call[CalendarBody]:
        def run() -> CalendarBody:
            if calendarId not in self._owner.calendar_bodies:
                raise NotFound(calendarId)
            return self._owner.calendar_bodies[calendarId]

        return Call(run)

    def insert(self, *, body: CalendarBody) -> Call[CalendarListEntry]:
        def run() -> CalendarListEntry:
            cal_id = f"cal-{len(self._owner.calendar_bodies) + 1}"
            self._owner.add_calendar(cal_id, body["summary"], body["timeZone"])
            return {"id": cal_id, "summary": body["summary"]}

        return Call(run)


class FakeCalendarList:
    def __init__(self, owner: "FakeCalendarService") -> None:
        self._owner = owner

    def list(self, *, pageToken: str | None = None) -> Call[CalendarListPage]:
        def run() -> CalendarListPage:
            entries = list(self._owner.list_entries.values())
            start = int(pageToken) if pageToken else 0
            end = start + self._owner.list_page_size
            page: CalendarListPage = {"items": entries[start:end]}
            if end < len(entries):
                page["nextPageToken"] = str(end)
            return page

        return Call(run)

    def get(self, *, calendarId: str) -> Call[CalendarListEntry]:
        def run() -> CalendarListEntry:
            if calendarId not in self._owner.list_entries:
                raise NotFound(calendarId)
            return self._owner.list_entries[calendarId].copy()

        return Call(run)

    def update(
        self, *, calendarId: str, body: CalendarListEntry
    ) -> Call[CalendarListEntry]:
        def run() -> CalendarListEntry:
            if calendarId not in self._owner.list_entries:
                raise NotFound(calendarId)
            self._owner.list_entries[calendarId] = body
            return body

        return Call(run)


class FakeEvents:
    def __init__(self, owner: "FakeCalendarService") -> None:
        self._owner = owner

    def _check(self, calendarId: str, body: EventBody, sendUpdates: str) -> None:
        if calendarId not in self._owner.calendar_bodies:
            raise NotFound(calendarId)
        if sendUpdates != "none":
            raise AssertionError(f"unexpected sendUpdates={sendUpdates!r}")
        if body["summary"] in self._owner.fail_summaries:
            raise RuntimeError(f"simulated API failure for {body['summary']!r}")

    def insert(
        self, *, calendarId: str, body: EventBody, sendUpdates: str
    ) -> Call[CreatedEvent]:
        def run() -> CreatedEvent:
            self._check(calendarId, body, sendUpdates)
            self._owner.event_counter += 1
            event_id = f"evt-{self._owner.event_counter}"
            self._owner.event_store.setdefault(calendarId, {})[event_id] = body
            self._owner.inserts.append(event_id)
            return {"id": event_id}

        return Call(run)

    def patch(
        self, *, calendarId: str, eventId: str, body: EventBody, sendUpdates: str
    ) -> Call[CreatedEvent]:
        def run() -> CreatedEvent:
            self._check(calendarId, body, sendUpdates)
            events = self._owner.event_store.setdefault(calendarId, {})
            if eventId not in events:
                raise NotFound(eventId)
            events[eventId] = body
            self._owner.patches.append(eventId)
            return {"id": eventId}

        return Call(run)


class FakeCalendarService:
    """In-memory stand-in for the Google Calendar v3 client (satisfies `CalendarService`)."""

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
        self.calendar_bodies[cal_id] = {"summary": summary, "timeZone": time_zone}
        self.list_entries[cal_id] = {"id": cal_id, "summary": summary}

    def calendars(self) -> FakeCalendars:
        return FakeCalendars(self)

    def calendarList(self) -> FakeCalendarList:
        return FakeCalendarList(self)

    def events(self) -> FakeEvents:
        return FakeEvents(self)
