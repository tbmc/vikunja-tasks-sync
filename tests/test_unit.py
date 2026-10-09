"""Unit tests: models, pure helpers and Google Calendar helpers against the in-memory fake."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from tests.conftest import iso, make_task
from tests.fakes import FakeCalendarService
from vikunja_sync.config import Settings, load_settings
from vikunja_sync.events import (
    all_day_dates,
    all_day_recurrence,
    build_event_body,
    is_overdue,
    iso_to_utc_dt,
    recurrence_rule,
    task_key,
)
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
from vikunja_sync.vikunja import Project, Projects, Reminder, TaskList, VikunjaTask

HOME = Projects([Project(id=1, title="Home")])


# ------------------
# Vikunja models
# ------------------
def test_vikunja_task_full_task() -> None:
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


def test_vikunja_task_missing_null_or_mistyped_fields_get_defaults() -> None:
    task = VikunjaTask.model_validate_json(
        '{"id": 1, "project_id": 2, "title": 5, "description": null, "done": null}'
    )
    assert task.title == "Untitled"
    assert task.description == ""
    assert task.done is False
    assert task.due_date == ""


def test_vikunja_task_missing_id_raises() -> None:
    with pytest.raises(ValidationError):
        VikunjaTask.model_validate_json('{"project_id": 2}')


@pytest.mark.parametrize("value", ["true", '"42"', "4.2", "null"])
def test_vikunja_task_non_int_id_raises(value: str) -> None:
    with pytest.raises(ValidationError):
        VikunjaTask.model_validate_json(f'{{"id": {value}, "project_id": 2}}')


def test_vikunja_task_wrapped_task_list() -> None:
    assert TaskList.model_validate_json(
        '{"tasks": [{"id": 1, "project_id": 2}]}'
    ).tasks == [VikunjaTask(id=1, project_id=2)]


def test_project_title_falls_back_to_id() -> None:
    assert HOME.title_of(1) == "Home"
    assert HOME.title_of(7) == "Project 7"


def test_vikunja_task_recurrence_fields() -> None:
    raw = """{
        "id": 8, "project_id": 10, "created": "2026-10-09T20:29:47+02:00",
        "repeat_after": 2592000, "repeat_mode": 1,
        "reminders": [{"reminder": "2026-10-04T12:00:00+02:00", "relative_period": 0, "relative_to": ""}]
    }"""
    task = VikunjaTask.model_validate_json(raw)
    assert task.created == "2026-10-09T20:29:47+02:00"
    assert (task.repeat_after, task.repeat_mode) == (2592000, 1)
    assert task.reminders == [Reminder(reminder="2026-10-04T12:00:00+02:00")]


def test_vikunja_task_null_or_mistyped_recurrence_fields_get_defaults() -> None:
    task = VikunjaTask.model_validate_json(
        '{"id": 1, "project_id": 2, "repeat_after": null, "repeat_mode": "x", "reminders": null}'
    )
    assert (task.repeat_after, task.repeat_mode, task.reminders) == (0, 0, [])


# ------------------
# Configuration
# ------------------
def test_load_settings_parses_env(monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_load_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "VIKUNJA_API_BASE",
        "VIKUNJA_VERIFY_SSL",
        "PROJECT_IDS",
        "GOOGLE_CALENDAR_NAME",
        "GOOGLE_CREDENTIALS_FILE",
        "GOOGLE_TOKEN_FILE",
        "STATE_FILE",
        "ICS_OUTPUT",
        "TIMEZONE",
        "REMINDER_MINUTES",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = load_settings()
    assert settings == Settings(
        vikunja_api_token=settings.vikunja_api_token,
        vikunja_username=settings.vikunja_username,
        vikunja_password=settings.vikunja_password,
    )


# ------------------
# Dates & task rules
# ------------------
@pytest.mark.parametrize("value", ["", "0001-01-01T00:00:00Z"])
def test_iso_to_utc_dt_empty_or_vikunja_null_date(value: str) -> None:
    assert iso_to_utc_dt(value) is None


def test_iso_to_utc_dt_converts_offset_to_utc() -> None:
    assert iso_to_utc_dt("2026-03-01T10:00:00+02:00") == datetime(
        2026, 3, 1, 8, 0, tzinfo=UTC
    )


def test_iso_to_utc_dt_result_is_utc() -> None:
    dt = iso_to_utc_dt("2026-03-01T10:00:00Z")
    assert dt is not None and dt.tzinfo == UTC


def test_task_key() -> None:
    assert task_key(make_task(12, project_id=4)) == "4:12"


def test_is_overdue_past_due_not_done() -> None:
    assert is_overdue(make_task(1, due_date=iso(-1))) is True


def test_is_overdue_past_due_but_done() -> None:
    assert is_overdue(make_task(1, due_date=iso(-1), done=True)) is False


def test_is_overdue_future_due() -> None:
    assert is_overdue(make_task(1, due_date=iso(1))) is False


def test_is_overdue_no_due_date() -> None:
    assert is_overdue(make_task(1)) is False


def test_should_sync_no_due_date() -> None:
    assert should_sync(make_task(1), None) is False


def test_should_sync_upcoming_task() -> None:
    assert should_sync(make_task(1, due_date=iso(1)), None) is True


def test_should_sync_new_overdue_task() -> None:
    assert should_sync(make_task(1, due_date=iso(-1)), None) is True


def test_should_sync_unchanged_overdue_task_is_skipped() -> None:
    task = make_task(1, due_date=iso(-1))
    prev = EventState(event_id="evt-1", done=False, last_updated=task.updated)
    assert should_sync(task, prev) is False


def test_should_sync_changed_overdue_task() -> None:
    prev = EventState(event_id="evt-1", done=False, last_updated="2025-01-01T00:00:00Z")
    assert should_sync(make_task(1, due_date=iso(-1)), prev) is True


def test_should_sync_done_task() -> None:
    task = make_task(1, done=True, due_date=iso(-1))
    prev = EventState(event_id="evt-1", done=True, last_updated=task.updated)
    assert should_sync(task, prev) is True


def test_build_event_body_no_due_date_returns_none(settings: Settings) -> None:
    assert build_event_body(make_task(1), "Home", settings) is None


def test_build_event_body_due_only_is_instant_event(settings: Settings) -> None:
    body = build_event_body(
        make_task(1, due_date="2026-05-01T10:00:00Z"), "Home", settings
    )
    assert body is not None
    assert body.start == EventDateTime(date_time="2026-05-01T10:00:00+00:00")
    assert body.end == EventDateTime(date_time="2026-05-01T10:00:00+00:00")


def test_build_event_body_start_before_due_spans_range(settings: Settings) -> None:
    task = make_task(
        1, start_date="2026-04-30T09:00:00Z", due_date="2026-05-01T10:00:00Z"
    )
    body = build_event_body(task, "Home", settings)
    assert body is not None
    assert body.start == EventDateTime(date_time="2026-04-30T09:00:00+00:00")
    assert body.end == EventDateTime(date_time="2026-05-01T10:00:00+00:00")


def test_build_event_body_start_after_due_falls_back_to_due(settings: Settings) -> None:
    task = make_task(
        1, start_date="2026-05-02T09:00:00Z", due_date="2026-05-01T10:00:00Z"
    )
    body = build_event_body(task, "Home", settings)
    assert body is not None
    assert (
        body.start == body.end == EventDateTime(date_time="2026-05-01T10:00:00+00:00")
    )


def test_build_event_body_summary_description_and_metadata(settings: Settings) -> None:
    task = make_task(
        9,
        project_id=2,
        title="Report",
        description="Q3",
        due_date="2026-05-01T10:00:00Z",
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
    assert body.source == EventSource(
        title="Vikunja", url="https://vikunja.example.com/tasks/9"
    )
    assert body.color_id is None


def test_build_event_body_done_task_is_green_with_check_mark(
    settings: Settings,
) -> None:
    body = build_event_body(
        make_task(1, done=True, due_date="2026-05-01T10:00:00Z"), "Home", settings
    )
    assert body is not None
    assert body.summary == "✅ [Home] Task 1"
    assert body.color_id == "10"
    assert body.extended_properties.private.vikunja_done == "True"


def test_build_event_body_default_reminders_when_none_configured(
    settings: Settings,
) -> None:
    body = build_event_body(
        make_task(1, due_date="2026-05-01T10:00:00Z"), "Home", settings
    )
    assert body is not None
    assert body.reminders == EventReminders(use_default=True)


def test_build_event_body_custom_reminders(settings: Settings) -> None:
    settings.reminder_minutes = (1440, 0)
    body = build_event_body(
        make_task(1, due_date="2026-05-01T10:00:00Z"), "Home", settings
    )
    assert body is not None
    assert body.reminders == EventReminders(
        use_default=False,
        overrides=[
            EventReminder(method="popup", minutes=1440),
            EventReminder(method="popup", minutes=0),
        ],
    )


def test_build_event_body_wire_format_is_camel_case_without_nulls(
    settings: Settings,
) -> None:
    body = build_event_body(
        make_task(1, due_date="2026-05-01T10:00:00Z"), "Home", settings
    )
    assert body is not None
    wire = body.model_dump_json(by_alias=True, exclude_none=True)
    assert '"extendedProperties"' in wire
    assert '"start":{"dateTime":"2026-05-01T10:00:00+00:00"}' in wire
    assert '"reminders":{"useDefault":true}' in wire
    assert '"recurrence":[]' in wire
    assert "colorId" not in wire


@pytest.mark.parametrize(
    ("repeat_after", "repeat_mode", "expected"),
    [
        (0, 0, None),
        (604800, 0, "FREQ=WEEKLY;INTERVAL=1"),
        (1209600, 0, "FREQ=WEEKLY;INTERVAL=2"),
        (3 * 86400, 0, "FREQ=DAILY;INTERVAL=3"),
        (3600, 0, "FREQ=DAILY;INTERVAL=1"),
        (86400, 2, "FREQ=DAILY;INTERVAL=1"),
        (2592000, 1, "FREQ=MONTHLY"),
        (0, 1, "FREQ=MONTHLY"),
    ],
)
def test_recurrence_rule(
    repeat_after: int, repeat_mode: int, expected: str | None
) -> None:
    task = make_task(1, repeat_after=repeat_after, repeat_mode=repeat_mode)
    assert recurrence_rule(task) == expected


def test_all_day_dates_are_the_absolute_reminder_days() -> None:
    task = make_task(
        1,
        reminders=[
            Reminder(reminder="2026-10-08T12:00:00+02:00"),
            Reminder(reminder="2026-10-04T12:00:00+02:00"),
            Reminder(reminder="2026-10-04T18:00:00+02:00"),
            Reminder(reminder="2026-09-01T12:00:00+02:00", relative_to="due_date"),
        ],
    )
    assert all_day_dates(task, "Europe/Paris") == [
        datetime(2026, 10, 4).date(),
        datetime(2026, 10, 8).date(),
    ]


def test_all_day_dates_recurring_without_reminder_starts_on_creation_day() -> None:
    task = make_task(1, repeat_after=604800, created="2026-10-09T23:30:00Z")
    assert all_day_dates(task, "Europe/Paris") == [datetime(2026, 10, 10).date()]
    assert all_day_dates(task, "UTC") == [datetime(2026, 10, 9).date()]


def test_all_day_dates_empty_without_reminder_nor_recurrence() -> None:
    assert all_day_dates(make_task(1), "UTC") == []
    assert all_day_dates(make_task(1, repeat_after=604800, created=""), "UTC") == []


def test_all_day_recurrence() -> None:
    days = [datetime(2026, 10, d).date() for d in (4, 8, 12)]
    assert all_day_recurrence(make_task(1), days[:1]) == []
    assert all_day_recurrence(make_task(1, repeat_mode=1), days) == [
        "RRULE:FREQ=MONTHLY",
        "RDATE;VALUE=DATE:20261008,20261012",
    ]


def test_should_sync_task_with_only_a_reminder() -> None:
    task = make_task(1, reminders=[Reminder(reminder="2026-10-10T12:00:00+02:00")])
    assert should_sync(task, None) is True


def test_should_sync_task_with_only_a_relative_reminder_is_skipped() -> None:
    reminder = Reminder(reminder="2026-10-10T12:00:00+02:00", relative_to="due_date")
    assert should_sync(make_task(1, reminders=[reminder]), None) is False


def test_should_sync_recurring_task_without_due_date() -> None:
    assert should_sync(make_task(1, repeat_after=604800), None) is True


def test_build_event_body_recurring_without_due_is_all_day_recurring_event(
    settings: Settings,
) -> None:
    task = make_task(1, repeat_mode=1, created="2026-10-09T20:29:47+02:00")
    body = build_event_body(task, "Home", settings)
    assert body is not None
    assert body.start == EventDateTime(date="2026-10-09")
    assert body.end == EventDateTime(date="2026-10-10")
    assert body.recurrence == ["RRULE:FREQ=MONTHLY"]
    wire = body.model_dump_json(by_alias=True, exclude_none=True)
    assert '"start":{"date":"2026-10-09"}' in wire


def test_build_event_body_reminder_only_is_single_all_day_event(
    settings: Settings,
) -> None:
    task = make_task(1, reminders=[Reminder(reminder="2026-10-11T10:00:00+02:00")])
    body = build_event_body(task, "Home", settings)
    assert body is not None
    assert body.start == EventDateTime(date="2026-10-11")
    assert body.end == EventDateTime(date="2026-10-12")
    assert body.recurrence == []


def test_build_event_body_recurring_reminder_starts_series_on_reminder_day(
    settings: Settings,
) -> None:
    task = make_task(
        1, repeat_mode=1, reminders=[Reminder(reminder="2026-10-04T12:00:00+02:00")]
    )
    body = build_event_body(task, "Home", settings)
    assert body is not None
    assert body.start == EventDateTime(date="2026-10-04")
    assert body.recurrence == ["RRULE:FREQ=MONTHLY"]


def test_build_event_body_recurring_with_due_date_keeps_timed_event(
    settings: Settings,
) -> None:
    task = make_task(1, repeat_after=604800, due_date="2026-05-01T10:00:00Z")
    body = build_event_body(task, "Home", settings)
    assert body is not None
    assert body.start == EventDateTime(date_time="2026-05-01T10:00:00+00:00")
    assert body.recurrence == []


# ------------------
# State
# ------------------
def test_state_load_missing_file_returns_empty_state(
    settings: Settings, tmp_path: Path
) -> None:
    assert load_state(settings.state_file) == State()


def test_state_save_then_load_roundtrip(settings: Settings, tmp_path: Path) -> None:
    state = State(
        events={
            "1:2": EventState(
                event_id="evt-1", done=True, last_updated="2026-01-01T00:00:00Z"
            )
        },
        calendar_id="cal-1",
    )
    save_state(settings.state_file, state)
    assert load_state(settings.state_file) == state


def test_state_save_keeps_unicode_readable(settings: Settings, tmp_path: Path) -> None:
    save_state(settings.state_file, State(calendar_id="café"))
    assert "café" in (tmp_path / "state.json").read_text(encoding="utf-8")


# ------------------
# ICS export
# ------------------
def test_tasks_to_ics_exports_only_tasks_with_due_date(
    settings: Settings, tmp_path: Path
) -> None:
    tasks = [
        make_task(1, title="Dentist", due_date="2026-05-01T10:00:00Z"),
        make_task(2, title="Someday"),
        make_task(
            3,
            project_id=99,
            title="Ship",
            done=True,
            due_date="2026-05-02T10:00:00Z",
        ),
    ]
    tasks_to_ics(tasks, HOME, settings.ics_output)
    content = (tmp_path / "calendar.ics").read_text(encoding="utf-8")
    assert content.count("BEGIN:VEVENT") == 2
    assert "SUMMARY:[Home] Dentist" in content
    assert "SUMMARY:✅ [Project 99] Ship" in content
    assert "Someday" not in content


def test_tasks_to_ics_start_date_used_as_begin(
    settings: Settings, tmp_path: Path
) -> None:
    task = make_task(
        1, start_date="2026-04-30T09:00:00Z", due_date="2026-05-01T10:00:00Z"
    )
    tasks_to_ics([task], HOME, settings.ics_output)
    content = (tmp_path / "calendar.ics").read_text(encoding="utf-8")
    assert "DTSTART:20260430T090000Z" in content
    assert "DTEND:20260501T100000Z" in content


@pytest.mark.xfail(
    strict=True,
    raises=ValueError,
    reason="Bug: start_date after due_date crashes the ICS export",
)
def test_tasks_to_ics_start_after_due_does_not_crash(
    settings: Settings, tmp_path: Path
) -> None:
    task = make_task(
        1, start_date="2026-05-02T09:00:00Z", due_date="2026-05-01T10:00:00Z"
    )
    tasks_to_ics([task], HOME, settings.ics_output)


def test_tasks_to_ics_reminder_days_are_all_day_with_rdate(
    settings: Settings, tmp_path: Path
) -> None:
    task = make_task(
        1,
        reminders=[
            Reminder(reminder="2026-10-04T12:00:00+02:00"),
            Reminder(reminder="2026-10-08T12:00:00+02:00"),
        ],
    )
    tasks_to_ics([task], HOME, settings.ics_output, "Europe/Paris")
    content = (tmp_path / "calendar.ics").read_text(encoding="utf-8")
    assert "DTSTART;VALUE=DATE:20261004" in content
    assert "RDATE;VALUE=DATE:20261008" in content
    assert "RRULE" not in content


def test_tasks_to_ics_recurring_without_due_is_all_day_with_rrule(
    settings: Settings, tmp_path: Path
) -> None:
    task = make_task(1, repeat_after=604800, created="2026-10-09T20:29:47+02:00")
    tasks_to_ics([task], HOME, settings.ics_output, "Europe/Paris")
    content = (tmp_path / "calendar.ics").read_text(encoding="utf-8")
    assert content.count("BEGIN:VEVENT") == 1
    assert "DTSTART;VALUE=DATE:20261009" in content
    assert "RRULE:FREQ=WEEKLY;INTERVAL=1" in content


# ------------------
# Google Calendar helpers
# ------------------
def test_ensure_calendar_reuses_cached_calendar_id(settings: Settings) -> None:
    google = FakeCalendarService()
    google.add_calendar("cal-cached", "Something else")
    state = State(calendar_id="cal-cached")
    assert ensure_calendar(google, state, settings) == "cal-cached"
    assert len(google.calendar_bodies) == 1


def test_ensure_calendar_stale_cached_id_falls_back_to_name_lookup(
    settings: Settings,
) -> None:
    google = FakeCalendarService()
    google.add_calendar("cal-real", "Vikunja Tasks")
    state = State(calendar_id="cal-deleted")
    assert ensure_calendar(google, state, settings) == "cal-real"
    assert state.calendar_id == "cal-real"


def test_ensure_calendar_finds_calendar_by_name_across_pages(
    settings: Settings,
) -> None:
    google = FakeCalendarService(list_page_size=2)
    for i in range(5):
        google.add_calendar(f"other-{i}", f"Other {i}")
    google.add_calendar("cal-target", "Vikunja Tasks")
    state = State()
    assert ensure_calendar(google, state, settings) == "cal-target"


def test_ensure_calendar_creates_calendar_when_missing(settings: Settings) -> None:
    google = FakeCalendarService(list_page_size=2)
    google.add_calendar("other", "Personal")
    state = State()
    cal_id = ensure_calendar(google, state, settings)
    assert state.calendar_id == cal_id
    assert google.calendar_bodies[cal_id] == CalendarBody(
        summary="Vikunja Tasks", time_zone="Europe/Paris"
    )


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
    entry = CalendarListEntry.model_validate_json(
        '{"id": "cal-1", "colorId": "7", "accessRole": "owner"}'
    )
    entry.default_reminders = []
    wire = entry.model_dump_json(by_alias=True, exclude_none=True)
    assert '"colorId":"7"' in wire and '"accessRole":"owner"' in wire
