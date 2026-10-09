"""Unit tests: pure helpers and Google Calendar helpers against the in-memory fake."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

import vikunja_sync as vs
from tests.conftest import iso, make_task
from tests.fakes import FakeCalendarService


# ------------------
# JSON helpers
# ------------------
class TestJsonHelpers:
    def test_as_object_accepts_dict(self) -> None:
        assert vs.as_object({"a": 1}) == {"a": 1}

    @pytest.mark.parametrize("value", [[], "x", 1, None])
    def test_as_object_rejects_non_dict(self, value: object) -> None:
        with pytest.raises(TypeError, match="Expected a JSON object"):
            vs.as_object(value)

    def test_as_list_accepts_list(self) -> None:
        assert vs.as_list([1, "a"]) == [1, "a"]

    @pytest.mark.parametrize("value", [{}, "x", 1, None])
    def test_as_list_rejects_non_list(self, value: object) -> None:
        with pytest.raises(TypeError, match="Expected a JSON array"):
            vs.as_list(value)

    def test_get_str_returns_value_or_default(self) -> None:
        obj: vs.JsonObject = {"s": "hello", "n": 3}
        assert vs.get_str(obj, "s") == "hello"
        assert vs.get_str(obj, "n") == ""
        assert vs.get_str(obj, "missing", "fallback") == "fallback"

    def test_get_int_returns_int(self) -> None:
        assert vs.get_int({"id": 42}, "id") == 42

    @pytest.mark.parametrize("value", [True, "42", 4.2, None])
    def test_get_int_rejects_non_int(self, value: object) -> None:
        with pytest.raises(TypeError, match="Expected integer field 'id'"):
            vs.get_int({"id": value}, "id")


class TestParseTask:
    def test_full_task(self) -> None:
        raw = {
            "id": 7,
            "project_id": 3,
            "title": "Buy milk",
            "description": "<p>2 litres</p>",
            "done": True,
            "due_date": "2026-05-01T10:00:00Z",
            "start_date": "2026-04-30T10:00:00Z",
            "updated": "2026-04-01T08:00:00Z",
            "extra_field": "ignored",
        }
        assert vs.parse_task(raw) == {
            "id": 7,
            "project_id": 3,
            "title": "Buy milk",
            "description": "<p>2 litres</p>",
            "done": True,
            "due_date": "2026-05-01T10:00:00Z",
            "start_date": "2026-04-30T10:00:00Z",
            "updated": "2026-04-01T08:00:00Z",
        }

    def test_missing_optional_fields_get_defaults(self) -> None:
        task = vs.parse_task({"id": 1, "project_id": 2, "description": None})
        assert task["title"] == "Untitled"
        assert task["description"] == ""
        assert task["done"] is False
        assert task["due_date"] == ""

    def test_missing_id_raises(self) -> None:
        with pytest.raises(TypeError):
            vs.parse_task({"project_id": 2})


# ------------------
# Dates & task rules
# ------------------
class TestIsoToUtcDt:
    @pytest.mark.parametrize("value", ["", "0001-01-01T00:00:00Z"])
    def test_empty_or_vikunja_null_date(self, value: str) -> None:
        assert vs.iso_to_utc_dt(value) is None

    def test_converts_offset_to_utc(self) -> None:
        assert vs.iso_to_utc_dt("2026-03-01T10:00:00+02:00") == datetime(2026, 3, 1, 8, 0, tzinfo=timezone.utc)

    def test_result_is_utc(self) -> None:
        dt = vs.iso_to_utc_dt("2026-03-01T10:00:00Z")
        assert dt is not None and dt.tzinfo == timezone.utc


def test_task_key() -> None:
    assert vs.task_key(make_task(12, project_id=4)) == "4:12"


class TestIsOverdue:
    def test_past_due_not_done(self) -> None:
        assert vs.is_overdue(make_task(1, due_date=iso(-1))) is True

    def test_past_due_but_done(self) -> None:
        assert vs.is_overdue(make_task(1, due_date=iso(-1), done=True)) is False

    def test_future_due(self) -> None:
        assert vs.is_overdue(make_task(1, due_date=iso(1))) is False

    def test_no_due_date(self) -> None:
        assert vs.is_overdue(make_task(1)) is False


class TestBuildEventBody:
    @pytest.fixture(autouse=True)
    def _config(self, config: Path) -> None:
        pass

    def test_no_due_date_returns_none(self) -> None:
        assert vs.build_event_body(make_task(1), "Home") is None

    def test_due_only_is_instant_event(self) -> None:
        body = vs.build_event_body(make_task(1, due_date="2026-05-01T10:00:00Z"), "Home")
        assert body is not None
        assert body["start"] == {"dateTime": "2026-05-01T10:00:00+00:00"}
        assert body["end"] == {"dateTime": "2026-05-01T10:00:00+00:00"}

    def test_start_before_due_spans_range(self) -> None:
        task = make_task(1, start_date="2026-04-30T09:00:00Z", due_date="2026-05-01T10:00:00Z")
        body = vs.build_event_body(task, "Home")
        assert body is not None
        assert body["start"] == {"dateTime": "2026-04-30T09:00:00+00:00"}
        assert body["end"] == {"dateTime": "2026-05-01T10:00:00+00:00"}

    def test_start_after_due_falls_back_to_due(self) -> None:
        task = make_task(1, start_date="2026-05-02T09:00:00Z", due_date="2026-05-01T10:00:00Z")
        body = vs.build_event_body(task, "Home")
        assert body is not None
        assert body["start"] == body["end"] == {"dateTime": "2026-05-01T10:00:00+00:00"}

    def test_summary_description_and_metadata(self) -> None:
        task = make_task(
            9, project_id=2, title="Report", description="Q3", due_date="2026-05-01T10:00:00Z",
            updated="2026-04-01T08:00:00+02:00",
        )
        body = vs.build_event_body(task, "Work")
        assert body is not None
        assert body["summary"] == "[Work] Report"
        assert body["description"] == "Q3"
        assert body["extendedProperties"]["private"] == {
            "vikunja_task_id": "9",
            "vikunja_project_id": "2",
            "vikunja_done": "False",
            "vikunja_updated": "2026-04-01T06:00:00+00:00",
        }
        assert body["source"] == {"title": "Vikunja", "url": "https://vikunja.example.com/tasks/9"}
        assert "colorId" not in body

    def test_done_task_is_green_with_check_mark(self) -> None:
        body = vs.build_event_body(make_task(1, done=True, due_date="2026-05-01T10:00:00Z"), "Home")
        assert body is not None
        assert body["summary"] == "✅ [Home] Task 1"
        assert body["colorId"] == "10"
        assert body["extendedProperties"]["private"]["vikunja_done"] == "True"

    def test_default_reminders_when_none_configured(self) -> None:
        body = vs.build_event_body(make_task(1, due_date="2026-05-01T10:00:00Z"), "Home")
        assert body is not None
        assert body["reminders"] == {"useDefault": True}

    def test_custom_reminders(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(vs, "REMINDER_MINUTES", [1440, 0])
        body = vs.build_event_body(make_task(1, due_date="2026-05-01T10:00:00Z"), "Home")
        assert body is not None
        assert body["reminders"] == {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": 1440}, {"method": "popup", "minutes": 0}],
        }


# ------------------
# State
# ------------------
class TestState:
    def test_load_missing_file_returns_empty_state(self, config: Path) -> None:
        assert vs.load_state() == {"events": {}, "calendar_id": None}

    def test_save_then_load_roundtrip(self, config: Path) -> None:
        state: vs.State = {
            "events": {"1:2": {"event_id": "evt-1", "done": True, "last_updated": "2026-01-01T00:00:00Z"}},
            "calendar_id": "cal-1",
        }
        vs.save_state(state)
        assert vs.load_state() == state

    def test_save_keeps_unicode_readable(self, config: Path) -> None:
        vs.save_state({"events": {}, "calendar_id": "café"})
        assert "café" in (config / "state.json").read_text(encoding="utf-8")


# ------------------
# ICS export
# ------------------
class TestTasksToIcs:
    def test_exports_only_tasks_with_due_date(self, config: Path) -> None:
        tasks = [
            make_task(1, title="Dentist", due_date="2026-05-01T10:00:00Z"),
            make_task(2, title="Someday"),
            make_task(3, project_id=99, title="Ship", done=True, due_date="2026-05-02T10:00:00Z"),
        ]
        vs.tasks_to_ics(tasks, {1: "Home"})
        content = (config / "calendar.ics").read_text(encoding="utf-8")
        assert content.count("BEGIN:VEVENT") == 2
        assert "SUMMARY:[Home] Dentist" in content
        assert "SUMMARY:✅ [Project 99] Ship" in content
        assert "Someday" not in content

    def test_start_date_used_as_begin(self, config: Path) -> None:
        task = make_task(1, start_date="2026-04-30T09:00:00Z", due_date="2026-05-01T10:00:00Z")
        vs.tasks_to_ics([task], {1: "Home"})
        content = (config / "calendar.ics").read_text(encoding="utf-8")
        assert "DTSTART:20260430T090000Z" in content
        assert "DTEND:20260501T100000Z" in content

    @pytest.mark.xfail(strict=True, raises=ValueError, reason="Bug: start_date after due_date crashes the ICS export")
    def test_start_after_due_does_not_crash(self, config: Path) -> None:
        task = make_task(1, start_date="2026-05-02T09:00:00Z", due_date="2026-05-01T10:00:00Z")
        vs.tasks_to_ics([task], {1: "Home"})


# ------------------
# Google Calendar helpers
# ------------------
class TestEnsureCalendar:
    @pytest.fixture(autouse=True)
    def _config(self, config: Path) -> None:
        pass

    def test_reuses_cached_calendar_id(self) -> None:
        google = FakeCalendarService()
        google.add_calendar("cal-cached", "Something else")
        state: vs.State = {"events": {}, "calendar_id": "cal-cached"}
        assert vs.ensure_calendar(google, state) == "cal-cached"
        assert len(google.calendar_bodies) == 1

    def test_stale_cached_id_falls_back_to_name_lookup(self) -> None:
        google = FakeCalendarService()
        google.add_calendar("cal-real", "Vikunja Tasks")
        state: vs.State = {"events": {}, "calendar_id": "cal-deleted"}
        assert vs.ensure_calendar(google, state) == "cal-real"
        assert state["calendar_id"] == "cal-real"

    def test_finds_calendar_by_name_across_pages(self) -> None:
        google = FakeCalendarService(list_page_size=2)
        for i in range(5):
            google.add_calendar(f"other-{i}", f"Other {i}")
        google.add_calendar("cal-target", "Vikunja Tasks")
        state: vs.State = {"events": {}, "calendar_id": None}
        assert vs.ensure_calendar(google, state) == "cal-target"

    def test_creates_calendar_when_missing(self) -> None:
        google = FakeCalendarService(list_page_size=2)
        google.add_calendar("other", "Personal")
        state: vs.State = {"events": {}, "calendar_id": None}
        cal_id = vs.ensure_calendar(google, state)
        assert state["calendar_id"] == cal_id
        assert google.calendar_bodies[cal_id] == {"summary": "Vikunja Tasks", "timeZone": "Europe/Paris"}


def test_set_calendar_default_reminders() -> None:
    google = FakeCalendarService()
    google.add_calendar("cal-1", "Vikunja Tasks")
    vs.set_calendar_default_reminders(google, "cal-1", [60, 0])
    assert google.list_entries["cal-1"]["defaultReminders"] == [
        {"method": "popup", "minutes": 60},
        {"method": "popup", "minutes": 0},
    ]
    assert google.list_entries["cal-1"]["summary"] == "Vikunja Tasks"

