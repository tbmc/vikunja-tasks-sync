"""Google Calendar v3: payload models, typed client, OAuth and calendar helpers."""

import os
from collections.abc import Sequence
from typing import Protocol, cast

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from vikunja_sync.config import Settings
from vikunja_sync.state import State

SCOPES = ["https://www.googleapis.com/auth/calendar"]


# ------------------
# Payloads
# ------------------
class GoogleModel(BaseModel):
    """snake_case in Python, camelCase on the wire."""

    model_config = ConfigDict(
        alias_generator=to_camel, validate_by_name=True, validate_by_alias=True
    )


class EventReminder(GoogleModel):
    method: str
    minutes: int


class EventReminders(GoogleModel):
    use_default: bool
    overrides: list[EventReminder] | None = None


class EventDateTime(GoogleModel):
    # `date_time` for timed events, `date` (YYYY-MM-DD) for all-day events
    date_time: str | None = None
    date: str | None = None


class PrivateProperties(BaseModel):
    vikunja_task_id: str
    vikunja_project_id: str
    vikunja_done: str
    vikunja_updated: str


class EventExtendedProperties(GoogleModel):
    private: PrivateProperties


class EventSource(GoogleModel):
    title: str
    url: str


class EventBody(GoogleModel):
    summary: str
    description: str
    start: EventDateTime
    end: EventDateTime
    extended_properties: EventExtendedProperties
    source: EventSource
    reminders: EventReminders
    # RRULE lines; always sent so a patch clears it when a task stops recurring
    recurrence: list[str] = []
    color_id: str | None = None


class CreatedEvent(GoogleModel):
    id: str


class CalendarBody(GoogleModel):
    summary: str
    time_zone: str


class CalendarListEntry(GoogleModel):
    # Keep the fields we don't model: the entry is sent back whole on update
    model_config = ConfigDict(extra="allow")

    id: str
    summary: str | None = None
    default_reminders: list[EventReminder] | None = None


class CalendarListPage(GoogleModel):
    items: list[CalendarListEntry] = []
    next_page_token: str | None = None


# ------------------
# Client
# ------------------
class CalendarService(Protocol):
    """The Google Calendar operations this app uses."""

    def get_calendar(self, calendar_id: str) -> CalendarBody: ...
    def insert_calendar(self, body: CalendarBody) -> CalendarListEntry: ...
    def list_calendars(self, page_token: str | None) -> CalendarListPage: ...
    def get_calendar_list_entry(self, calendar_id: str) -> CalendarListEntry: ...
    def update_calendar_list_entry(
        self, entry: CalendarListEntry
    ) -> CalendarListEntry: ...
    def insert_event(self, calendar_id: str, body: EventBody) -> CreatedEvent: ...
    def patch_event(
        self, calendar_id: str, event_id: str, body: EventBody
    ) -> CreatedEvent: ...


# googleapiclient only speaks plain JSON dicts: they never leave this section.
# Minimal, Any-free view of its v3 resource (the official stubs use `Any` everywhere).
type _Json = dict[str, object]


class _Request(Protocol):
    def execute(self) -> _Json: ...


class _Calendars(Protocol):
    def get(self, *, calendarId: str) -> _Request: ...
    def insert(self, *, body: _Json) -> _Request: ...


class _CalendarList(Protocol):
    def list(self, *, pageToken: str | None = None) -> _Request: ...
    def get(self, *, calendarId: str) -> _Request: ...
    def update(self, *, calendarId: str, body: _Json) -> _Request: ...


class _Events(Protocol):
    def insert(self, *, calendarId: str, body: _Json, sendUpdates: str) -> _Request: ...
    def patch(
        self, *, calendarId: str, eventId: str, body: _Json, sendUpdates: str
    ) -> _Request: ...


class _Resource(Protocol):
    def calendars(self) -> _Calendars: ...
    def calendarList(self) -> _CalendarList: ...
    def events(self) -> _Events: ...


def _dump(model: BaseModel) -> _Json:
    return cast(_Json, model.model_dump(mode="json", by_alias=True, exclude_none=True))


class GoogleCalendarClient:
    """`CalendarService` backed by the official Google API client."""

    def __init__(self, resource: _Resource) -> None:
        self._api = resource

    def get_calendar(self, calendar_id: str) -> CalendarBody:
        return CalendarBody.model_validate(
            self._api.calendars().get(calendarId=calendar_id).execute()
        )

    def insert_calendar(self, body: CalendarBody) -> CalendarListEntry:
        return CalendarListEntry.model_validate(
            self._api.calendars().insert(body=_dump(body)).execute()
        )

    def list_calendars(self, page_token: str | None) -> CalendarListPage:
        return CalendarListPage.model_validate(
            self._api.calendarList().list(pageToken=page_token).execute()
        )

    def get_calendar_list_entry(self, calendar_id: str) -> CalendarListEntry:
        return CalendarListEntry.model_validate(
            self._api.calendarList().get(calendarId=calendar_id).execute()
        )

    def update_calendar_list_entry(self, entry: CalendarListEntry) -> CalendarListEntry:
        return CalendarListEntry.model_validate(
            self._api.calendarList()
            .update(calendarId=entry.id, body=_dump(entry))
            .execute()
        )

    def insert_event(self, calendar_id: str, body: EventBody) -> CreatedEvent:
        return CreatedEvent.model_validate(
            self._api.events()
            .insert(calendarId=calendar_id, body=_dump(body), sendUpdates="none")
            .execute()
        )

    def patch_event(
        self, calendar_id: str, event_id: str, body: EventBody
    ) -> CreatedEvent:
        return CreatedEvent.model_validate(
            self._api.events()
            .patch(
                calendarId=calendar_id,
                eventId=event_id,
                body=_dump(body),
                sendUpdates="none",
            )
            .execute()
        )


# ------------------
# Helpers
# ------------------
def google_service(settings: Settings) -> CalendarService:
    # google.oauth2.credentials ships `py.typed` but its methods are unannotated.
    creds: Credentials | None = None
    if os.path.exists(settings.google_token_file):
        creds = cast(
            Credentials,
            Credentials.from_authorized_user_file(settings.google_token_file, SCOPES),  # type: ignore[no-untyped-call, misc]
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
                settings.google_credentials_file, SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open(settings.google_token_file, "w") as token:
            token.write(cast(str, creds.to_json()))  # type: ignore[no-untyped-call]
    resource = cast(_Resource, build("calendar", "v3", credentials=creds))
    return GoogleCalendarClient(resource)


def ensure_calendar(service: CalendarService, state: State, settings: Settings) -> str:
    """Return the sync calendar id (cached, found by name, or newly created) and cache it in `state`."""
    cached_id = state.calendar_id
    if cached_id:
        try:
            service.get_calendar(cached_id)
            return cached_id
        # Any CalendarService backend error: fall back to lookup by name
        except Exception as e:  # noqa: BLE001
            print(
                f"[INFO] Cached calendar {cached_id} unavailable ({e}), looking up by name"
            )
    # Find by name
    page_token: str | None = None
    while True:
        page = service.list_calendars(page_token)
        for cal in page.items:
            if cal.summary == settings.google_calendar_name:
                state.calendar_id = cal.id
                return cal.id
        page_token = page.next_page_token
        if not page_token:
            break
    # Create new
    created = service.insert_calendar(
        CalendarBody(summary=settings.google_calendar_name, time_zone=settings.timezone)
    )
    state.calendar_id = created.id
    return created.id


def popup_reminders(minutes_list: Sequence[int]) -> list[EventReminder]:
    return [EventReminder(method="popup", minutes=m) for m in minutes_list]


def set_calendar_default_reminders(
    service: CalendarService, calendar_id: str, minutes_list: Sequence[int]
) -> None:
    entry = service.get_calendar_list_entry(calendar_id)
    entry.default_reminders = popup_reminders(minutes_list)
    service.update_calendar_list_entry(entry)


def upsert_event(
    service: CalendarService, calendar_id: str, event_id: str | None, body: EventBody
) -> str:
    """Patch `event_id` if given, else create a new event; returns the event id."""
    if event_id:
        return service.patch_event(calendar_id, event_id, body).id
    return service.insert_event(calendar_id, body).id
