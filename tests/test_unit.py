"""Unit tests: models, pure helpers and Google Calendar helpers against the in-memory fake."""

from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from tests.conftest import iso, make_task
from tests.fakes import FakeCalendarService
from vikunja_sync.config import Settings, load_settings
from vikunja_sync.events import build_event_body, is_overdue, iso_to_utc_dt, task_key
from vikunja_sync.google_calendar import (
    CalendarBody,
    CalendarListEntry,
    EventDateTime,
    EventReminder,
    EventReminders,
    EventSource,
    PrivateProperties,
    ensure_calendar,
    set_calendar_default_reminders,
)
from vikunja_sync.ics_export import tasks_to_ics
from vikunja_sync.state import EventState, State, load_state, save_state
from vikunja_sync.sync import should_sync
from vikunja_sync.vikunja import Project, Projects, TaskList, VikunjaTask

HOME = Projects([Project(id=1, title="Home")])


# ------------------
# Vikunja models
# ------------------
class TestVikunjaTask:
    def test_full_task(self) -> None:
        raw = """{
            "id": 7, "project_id": 3, "title": "Buy milk", "description": "<p>2 litres</p>",
            "done": true, "due_date": "2026-05-01T10:00:00Z", "start_date": "2026-04-30T10:00:00Z",
            "updated": "2026-04-01T08:00:00Z", "extra_field": "ignored"
        }"""
        assert VikunjaTask.model_validate_json(raw) == VikunjaTask(
            id=7,
            project_id=3,
            title="Buy milk",
            description="<p>2 litres</p>",
            done=True,
            due_date="2026-05-01T10:00:00Z",
            start_date="2026-04-30T10:00:00Z",
            updated="2026-04-01T08:00:00Z",
        )

    def test_missing_null_or_mistyped_fields_get_defaults(self) -> None:
        task = VikunjaTask.model_validate_json('{"id": 1, "project_id": 2, "title": 5, "description": null, "done": null}')
        assert task.title == "Untitled"
        assert task.description == ""
        assert task.done is False
        assert task.due_date == ""

    def test_missing_id_raises(self) -> None:
        with pytest.raises(ValidationError):
            VikunjaTask.model_validate_json('{"project_id": 2}')

    @pytest.mark.parametrize("value", ["true", '"42"', "4.2", "null"])
    def test_non_int_id_raises(self, value: str) -> None:
        with pytest.raises(ValidationError):
            VikunjaTask.model_validate_json(f'{{"id": {value}, "project_id": 2}}')

    def test_wrapped_task_list(self) -> None:
        assert TaskList.model_validate_json('{"tasks": [{"id": 1, "project_id": 2}]}').tasks == [
            VikunjaTask(id=1, project_id=2)
        ]


def test_project_title_falls_back_to_id() -> None:
    assert HOME.title_of(1) == "Home"
    assert HOME.title_of(7) == "Project 7"


# ------------------
# Configuration
# ------------------
class TestLoadSettings:
    def test_parses_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VIKUNJA_API_BASE", "https://v.example.com/api/v1/")
        monkeypatch.setenv("VIKUNJA_VERIFY_SSL", "False")
        monkeypatch.setenv("PROJECT_IDS", "2, 5,x,9")
        monkeypatch.setenv("REMINDER_MINUTES", "10080,-5,abc,0")
        settings = load_settings()
        assert settings.vikunja_api_base == "https://v.example.com/api/v1"
        assert settings.vikunja_web_base == "https://v.example.com"
        assert settings.vikunja_verify_ssl is False
        assert settings.project_ids == (2, 5, 9)
        assert settings.reminder_minutes == (10080, -5, 0)

    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in ("VIKUNJA_API_BASE", "VIKUNJA_VERIFY_SSL", "PROJECT_IDS", "GOOGLE_CALENDAR_NAME",
                     "GOOGLE_CREDENTIALS_FILE", "GOOGLE_TOKEN_FILE", "STATE_FILE", "ICS_OUTPUT",
                     "TIMEZONE", "REMINDER_MINUTES"):
            monkeypatch.delenv(name, raising=False)
        settings = load_settings()
        assert settings == Settings(
            vikunja_username=settings.vikunja_username, vikunja_password=settings.vikunja_password
        )


# ------------------
# Dates & task rules
# ------------------
class TestIsoToUtcDt:
    @pytest.mark.parametrize("value", ["", "0001-01-01T00:00:00Z"])
    def test_empty_or_vikunja_null_date(self, value: str) -> None:
        assert iso_to_utc_dt(value) is None

    def test_converts_offset_to_utc(self) -> None:
        assert iso_to_utc_dt("2026-03-01T10:00:00+02:00") == datetime(2026, 3, 1, 8, 0, tzinfo=timezone.utc)

    def test_result_is_utc(self) -> None:
        dt = iso_to_utc_dt("2026-03-01T10:00:00Z")
        assert dt is not None and dt.tzinfo == timezone.utc


def test_task_key() -> None:
    assert task_key(make_task(12, project_id=4)) == "4:12"


class TestIsOverdue:
    def test_past_due_not_done(self) -> None:
        assert is_overdue(make_task(1, due_date=iso(-1))) is True

    def test_past_due_but_done(self) -> None:
        assert is_overdue(make_task(1, due_date=iso(-1), done=True)) is False

    def test_future_due(self) -> None:
        assert is_overdue(make_task(1, due_date=iso(1))) is False

    def test_no_due_date(self) -> None:
        assert is_overdue(make_task(1)) is False


class TestShouldSync:
    def test_no_due_date(self) -> None:
        assert should_sync(make_task(1), None) is False

    def test_upcoming_task(self) -> None:
        assert should_sync(make_task(1, due_date=iso(1)), None) is True

    def test_new_overdue_task(self) -> None:
        assert should_sync(make_task(1, due_date=iso(-1)), None) is True

    def test_unchanged_overdue_task_is_skipped(self) -> None:
        task = make_task(1, due_date=iso(-1))
        prev = EventState(event_id="evt-1", done=False, last_updated=task.updated)
        assert should_sync(task, prev) is False

    def test_changed_overdue_task(self) -> None:
        prev = EventState(event_id="evt-1", done=False, last_updated="2025-01-01T00:00:00Z")
        assert should_sync(make_task(1, due_date=iso(-1)), prev) is True

    def test_done_task(self) -> None:
        task = make_task(1, done=True, due_date=iso(-1))
        prev = EventState(event_id="evt-1", done=True, last_updated=task.updated)
        assert should_sync(task, prev) is True


class TestBuildEventBody:
    def test_no_due_date_returns_none(self, settings: Settings) -> None:
        assert build_event_body(make_task(1), "Home", settings) is None

    def test_due_only_is_instant_event(self, settings: Settings) -> None:
        body = build_event_body(make_task(1, due_date="2026-05-01T10:00:00Z"), "Home", settings)
        assert body is not None
        assert body.start == EventDateTime(date_time="2026-05-01T10:00:00+00:00")
        assert body.end == EventDateTime(date_time="2026-05-01T10:00:00+00:00")

    def test_start_before_due_spans_range(self, settings: Settings) -> None:
        task = make_task(1, start_date="2026-04-30T09:00:00Z", due_date="2026-05-01T10:00:00Z")
        body = build_event_body(task, "Home", settings)
        assert body is not None
        assert body.start == EventDateTime(date_time="2026-04-30T09:00:00+00:00")
        assert body.end == EventDateTime(date_time="2026-05-01T10:00:00+00:00")

    def test_start_after_due_falls_back_to_due(self, settings: Settings) -> None:
        task = make_task(1, start_date="2026-05-02T09:00:00Z", due_date="2026-05-01T10:00:00Z")
        body = build_event_body(task, "Home", settings)
        assert body is not None
        assert body.start == body.end == EventDateTime(date_time="2026-05-01T10:00:00+00:00")

    def test_summary_description_and_metadata(self, settings: Settings) -> None:
        task = make_task(
            9, project_id=2, title="Report", description="Q3", due_date="2026-05-01T10:00:00Z",
            updated="2026-04-01T08:00:00+02:00",
        )
        body = build_event_body(task, "Work", settings)
        assert body is not None
        assert body.summary == "[Work] Report"
        assert body.description == "Q3"
        assert body.extended_properties.private == PrivateProperties(
            vikunja_task_id="9",
            vikunja_project_id="2",
            vikunja_done="False",
            vikunja_updated="2026-04-01T06:00:00+00:00",
        )
        assert body.source == EventSource(title="Vikunja", url="https://vikunja.example.com/tasks/9")
        assert body.color_id is None

    def test_done_task_is_green_with_check_mark(self, settings: Settings) -> None:
        body = build_event_body(make_task(1, done=True, due_date="2026-05-01T10:00:00Z"), "Home", settings)
        assert body is not None
        assert body.summary == "✅ [Home] Task 1"
        assert body.color_id == "10"
        assert body.extended_properties.private.vikunja_done == "True"

    def test_default_reminders_when_none_configured(self, settings: Settings) -> None:
        body = build_event_body(make_task(1, due_date="2026-05-01T10:00:00Z"), "Home", settings)
        assert body is not None
        assert body.reminders == EventReminders(use_default=True)

    def test_custom_reminders(self, settings: Settings) -> None:
        settings.reminder_minutes = (1440, 0)
        body = build_event_body(make_task(1, due_date="2026-05-01T10:00:00Z"), "Home", settings)
        assert body is not None
        assert body.reminders == EventReminders(
            use_default=False,
            overrides=[EventReminder(method="popup", minutes=1440), EventReminder(method="popup", minutes=0)],
        )

    def test_wire_format_is_camel_case_without_nulls(self, settings: Settings) -> None:
        body = build_event_body(make_task(1, due_date="2026-05-01T10:00:00Z"), "Home", settings)
        assert body is not None
        wire = body.model_dump_json(by_alias=True, exclude_none=True)
        assert '"extendedProperties"' in wire
        assert '"start":{"dateTime":"2026-05-01T10:00:00+00:00"}' in wire
        assert '"reminders":{"useDefault":true}' in wire
        assert "colorId" not in wire


# ------------------
# State
# ------------------
class TestState:
    def test_load_missing_file_returns_empty_state(self, settings: Settings, tmp_path: Path) -> None:
        assert load_state(settings.state_file) == State()

    def test_save_then_load_roundtrip(self, settings: Settings, tmp_path: Path) -> None:
        state = State(
            events={"1:2": EventState(event_id="evt-1", done=True, last_updated="2026-01-01T00:00:00Z")},
            calendar_id="cal-1",
        )
        save_state(settings.state_file, state)
        assert load_state(settings.state_file) == state

    def test_save_keeps_unicode_readable(self, settings: Settings, tmp_path: Path) -> None:
        save_state(settings.state_file, State(calendar_id="café"))
        assert "café" in (tmp_path / "state.json").read_text(encoding="utf-8")


# ------------------
# ICS export
# ------------------
class TestTasksToIcs:
    def test_exports_only_tasks_with_due_date(self, settings: Settings, tmp_path: Path) -> None:
        tasks = [
            make_task(1, title="Dentist", due_date="2026-05-01T10:00:00Z"),
            make_task(2, title="Someday"),
            make_task(3, project_id=99, title="Ship", done=True, due_date="2026-05-02T10:00:00Z"),
        ]
        tasks_to_ics(tasks, HOME, settings.ics_output)
        content = (tmp_path / "calendar.ics").read_text(encoding="utf-8")
        assert content.count("BEGIN:VEVENT") == 2
        assert "SUMMARY:[Home] Dentist" in content
        assert "SUMMARY:✅ [Project 99] Ship" in content
        assert "Someday" not in content

    def test_start_date_used_as_begin(self, settings: Settings, tmp_path: Path) -> None:
        task = make_task(1, start_date="2026-04-30T09:00:00Z", due_date="2026-05-01T10:00:00Z")
        tasks_to_ics([task], HOME, settings.ics_output)
        content = (tmp_path / "calendar.ics").read_text(encoding="utf-8")
        assert "DTSTART:20260430T090000Z" in content
        assert "DTEND:20260501T100000Z" in content

    @pytest.mark.xfail(strict=True, raises=ValueError, reason="Bug: start_date after due_date crashes the ICS export")
    def test_start_after_due_does_not_crash(self, settings: Settings, tmp_path: Path) -> None:
        task = make_task(1, start_date="2026-05-02T09:00:00Z", due_date="2026-05-01T10:00:00Z")
        tasks_to_ics([task], HOME, settings.ics_output)


# ------------------
# Google Calendar helpers
# ------------------
class TestEnsureCalendar:
    def test_reuses_cached_calendar_id(self, settings: Settings) -> None:
        google = FakeCalendarService()
        google.add_calendar("cal-cached", "Something else")
        state = State(calendar_id="cal-cached")
        assert ensure_calendar(google, state, settings) == "cal-cached"
        assert len(google.calendar_bodies) == 1

    def test_stale_cached_id_falls_back_to_name_lookup(self, settings: Settings) -> None:
        google = FakeCalendarService()
        google.add_calendar("cal-real", "Vikunja Tasks")
        state = State(calendar_id="cal-deleted")
        assert ensure_calendar(google, state, settings) == "cal-real"
        assert state.calendar_id == "cal-real"

    def test_finds_calendar_by_name_across_pages(self, settings: Settings) -> None:
        google = FakeCalendarService(list_page_size=2)
        for i in range(5):
            google.add_calendar(f"other-{i}", f"Other {i}")
        google.add_calendar("cal-target", "Vikunja Tasks")
        state = State()
        assert ensure_calendar(google, state, settings) == "cal-target"

    def test_creates_calendar_when_missing(self, settings: Settings) -> None:
        google = FakeCalendarService(list_page_size=2)
        google.add_calendar("other", "Personal")
        state = State()
        cal_id = ensure_calendar(google, state, settings)
        assert state.calendar_id == cal_id
        assert google.calendar_bodies[cal_id] == CalendarBody(summary="Vikunja Tasks", time_zone="Europe/Paris")


def test_set_calendar_default_reminders() -> None:
    google = FakeCalendarService()
    google.add_calendar("cal-1", "Vikunja Tasks")
    set_calendar_default_reminders(google, "cal-1", [60, 0])
    assert google.list_entries["cal-1"].default_reminders == [
        EventReminder(method="popup", minutes=60),
        EventReminder(method="popup", minutes=0),
    ]
    assert google.list_entries["cal-1"].summary == "Vikunja Tasks"


def test_calendar_list_entry_keeps_unknown_fields() -> None:
    # The entry is sent back whole on update: fields we don't model must survive
    entry = CalendarListEntry.model_validate_json('{"id": "cal-1", "colorId": "7", "accessRole": "owner"}')
    entry.default_reminders = []
    wire = entry.model_dump_json(by_alias=True, exclude_none=True)
    assert '"colorId":"7"' in wire and '"accessRole":"owner"' in wire

