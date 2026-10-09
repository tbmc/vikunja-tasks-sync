"""End-to-end tests: run `main()` against a fake Vikunja (real HTTP) and an in-memory Google Calendar."""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

import vikunja_sync as vs
from tests.conftest import iso
from tests.fakes import FakeCalendarService, FakeVikunja

REPO_ROOT = Path(__file__).resolve().parent.parent


def read_state(config: Path) -> vs.State:
    return cast(vs.State, json.loads((config / "state.json").read_text(encoding="utf-8")))


def calendar_events(google: FakeCalendarService) -> dict[str, vs.EventBody]:
    (cal_id,) = google.event_store
    return google.event_store[cal_id]


@pytest.fixture()
def tasks(vikunja: FakeVikunja) -> FakeVikunja:
    vikunja.tasks = [
        {"id": 1, "project_id": 1, "title": "Dentist", "due_date": iso(3), "updated": "2026-01-01T10:00:00Z"},
        {"id": 2, "project_id": 2, "title": "Report", "due_date": iso(-2), "updated": "2026-01-01T10:00:00Z"},
        {"id": 3, "project_id": 2, "title": "Ship v1", "done": True, "due_date": iso(-5), "updated": "2026-01-01T10:00:00Z"},
        {"id": 4, "project_id": 1, "title": "Someday", "due_date": "0001-01-01T00:00:00Z"},
        {"id": 5, "project_id": 7, "title": "Orphan", "due_date": iso(10), "start_date": iso(9)},
    ]
    return vikunja


def test_first_sync_creates_calendar_events_state_and_ics(
    tasks: FakeVikunja, google: FakeCalendarService, config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vs.main()

    # Calendar created with the configured name and timezone
    assert list(google.calendar_bodies.values()) == [{"summary": "Vikunja Tasks", "timeZone": "Europe/Paris"}]

    # One event per task with a due date
    summaries = sorted(e["summary"] for e in calendar_events(google).values())
    assert summaries == ["[Home] Dentist", "[Project 7] Orphan", "[Work] Report", "✅ [Work] Ship v1"]
    assert google.patches == []

    # State maps every synced task to its event
    state = read_state(config)
    assert state["calendar_id"] == "cal-1"
    assert sorted(state["events"]) == ["1:1", "2:2", "2:3", "7:5"]
    assert state["events"]["2:3"]["done"] is True
    assert state["events"]["1:1"]["last_updated"] == "2026-01-01T10:00:00Z"

    # ICS backup has the same events
    ics = (config / "calendar.ics").read_text(encoding="utf-8")
    assert ics.count("BEGIN:VEVENT") == 4

    # Every Vikunja call after login was authenticated
    assert all(r.authorization == "Bearer jwt-token-123" for r in tasks.requests if r.method == "GET")
    assert "Sync complete" in capsys.readouterr().out


def test_second_sync_updates_in_place_and_skips_unchanged_overdue(
    tasks: FakeVikunja, google: FakeCalendarService, config: Path
) -> None:
    vs.main()
    first_state = read_state(config)
    google.inserts.clear()

    vs.main()

    # No duplicates; the unchanged overdue task ("Report", 2:2) is not touched
    assert google.inserts == []
    assert len(calendar_events(google)) == 4
    skipped = first_state["events"]["2:2"]["event_id"]
    assert skipped not in google.patches
    assert sorted(google.patches) == sorted(
        first_state["events"][k]["event_id"] for k in ("1:1", "2:3", "7:5")
    )
    assert read_state(config) == first_state


def test_task_changes_are_propagated(tasks: FakeVikunja, google: FakeCalendarService, config: Path) -> None:
    vs.main()
    event_id = read_state(config)["events"]["2:2"]["event_id"]

    # The overdue task gets completed in Vikunja
    report = tasks.tasks[1]
    report["done"] = True
    report["updated"] = "2026-02-01T10:00:00Z"
    vs.main()

    event = calendar_events(google)[event_id]
    assert event["summary"] == "✅ [Work] Report"
    assert event.get("colorId") == "10"
    assert read_state(config)["events"]["2:2"] == {
        "event_id": event_id,
        "done": True,
        "last_updated": "2026-02-01T10:00:00Z",
    }


def test_edited_overdue_task_is_updated(tasks: FakeVikunja, google: FakeCalendarService, config: Path) -> None:
    vs.main()
    event_id = read_state(config)["events"]["2:2"]["event_id"]

    tasks.tasks[1]["title"] = "Report (late)"
    tasks.tasks[1]["updated"] = "2026-02-01T10:00:00Z"
    vs.main()

    assert calendar_events(google)[event_id]["summary"] == "[Work] Report (late)"


def test_reuses_existing_calendar_by_name(tasks: FakeVikunja, google: FakeCalendarService, config: Path) -> None:
    google.add_calendar("primary", "Personal")
    google.add_calendar("existing", "Vikunja Tasks")
    vs.main()
    assert read_state(config)["calendar_id"] == "existing"
    assert set(google.event_store) == {"existing"}


def test_reminders_are_set_on_calendar_and_events(
    tasks: FakeVikunja, google: FakeCalendarService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vs, "REMINDER_MINUTES", [1440, 0])
    vs.main()

    expected = [{"method": "popup", "minutes": 1440}, {"method": "popup", "minutes": 0}]
    assert google.list_entries["cal-1"].get("defaultReminders") == expected
    for event in calendar_events(google).values():
        assert event["reminders"] == {"useDefault": False, "overrides": expected}


def test_only_selected_projects_are_synced(
    tasks: FakeVikunja, google: FakeCalendarService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vs, "PROJECT_IDS", [1])
    vs.main()
    assert sorted(e["summary"] for e in calendar_events(google).values()) == ["[Home] Dentist"]


def test_failed_upsert_is_reported_and_retried_next_run(
    tasks: FakeVikunja, google: FakeCalendarService, config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    google.fail_summaries = {"[Home] Dentist"}
    vs.main()

    assert "[WARN] Upsert failed for 1:1" in capsys.readouterr().out
    state = read_state(config)
    assert "1:1" not in state["events"]
    assert len(state["events"]) == 3

    google.fail_summaries = set()
    vs.main()
    assert "1:1" in read_state(config)["events"]
    assert len(calendar_events(google)) == 4


def test_bad_credentials_abort_before_touching_google(
    tasks: FakeVikunja, google: FakeCalendarService, config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vs, "VIKUNJA_PASSWORD", "wrong")
    with pytest.raises(SystemExit, match="Failed to login"):
        vs.main()
    assert google.calendar_bodies == {}
    assert not (config / "state.json").exists()


def test_missing_config_aborts(config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vs, "VIKUNJA_API_BASE", "")
    with pytest.raises(SystemExit, match="must be set"):
        vs.main()


# ------------------
# Configuration parsing (read once at import time, so checked in a fresh interpreter)
# ------------------
def test_env_configuration_is_parsed_at_import(tmp_path: Path) -> None:
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT),
        "VIKUNJA_API_BASE": "https://v.example.com/api/v1/",
        "VIKUNJA_VERIFY_SSL": "False",
        "PROJECT_IDS": "2, 5,x,9",
        "REMINDER_MINUTES": "10080,-5,abc,0",
        "STATE_FILE": "nested/state/state.json",
        "ICS_OUTPUT": "out/cal.ics",
    }
    code = (
        "import json, vikunja_sync as v; print(json.dumps("
        "[v.VIKUNJA_API_BASE, v.VIKUNJA_VERIFY_SSL, v.PROJECT_IDS, v.REMINDER_MINUTES]))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=tmp_path, env=env, capture_output=True, text=True, check=True
    )
    assert cast(object, json.loads(result.stdout)) == ["https://v.example.com/api/v1", False, [2, 5, 9], [10080, -5, 0]]
    # Output folders are created up front
    assert (tmp_path / "nested" / "state").is_dir()
    assert (tmp_path / "out").is_dir()
