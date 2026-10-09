"""End-to-end tests: run the sync against a fake Vikunja (real HTTP) and an in-memory Google Calendar."""

from pathlib import Path

import pytest

from tests.conftest import iso
from tests.fakes import FakeCalendarService, FakeVikunja
from vikunja_sync.config import Settings
from vikunja_sync.google_calendar import (
    CalendarBody,
    EventBody,
    EventReminder,
    EventReminders,
)
from vikunja_sync.state import EventState, State
from vikunja_sync.sync import run
from vikunja_sync.vikunja import VikunjaTask


def read_state(settings: Settings) -> State:
    return State.model_validate_json(Path(settings.state_file).read_bytes())


def calendar_events(google: FakeCalendarService) -> dict[str, EventBody]:
    (cal_id,) = google.event_store
    return google.event_store[cal_id]


@pytest.fixture()
def tasks(vikunja: FakeVikunja) -> FakeVikunja:
    vikunja.tasks = [
        VikunjaTask(
            id=1,
            project_id=1,
            title="Dentist",
            due_date=iso(3),
            updated="2026-01-01T10:00:00Z",
        ),
        VikunjaTask(
            id=2,
            project_id=2,
            title="Report",
            due_date=iso(-2),
            updated="2026-01-01T10:00:00Z",
        ),
        VikunjaTask(
            id=3,
            project_id=2,
            title="Ship v1",
            done=True,
            due_date=iso(-5),
            updated="2026-01-01T10:00:00Z",
        ),
        VikunjaTask(
            id=4, project_id=1, title="Someday", due_date="0001-01-01T00:00:00Z"
        ),
        VikunjaTask(
            id=5, project_id=7, title="Orphan", due_date=iso(10), start_date=iso(9)
        ),
    ]
    return vikunja


def test_first_sync_creates_calendar_events_state_and_ics(
    tasks: FakeVikunja,
    google: FakeCalendarService,
    live_settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run(live_settings)

    # Calendar created with the configured name and timezone
    assert list(google.calendar_bodies.values()) == [
        CalendarBody(summary="Vikunja Tasks", time_zone="Europe/Paris")
    ]

    # One event per task with a due date
    summaries = sorted(e.summary for e in calendar_events(google).values())
    assert summaries == [
        "[Home] Dentist",
        "[Project 7] Orphan",
        "[Work] Report",
        "✅ [Work] Ship v1",
    ]
    assert google.patches == []

    # State maps every synced task to its event
    state = read_state(live_settings)
    assert state.calendar_id == "cal-1"
    assert sorted(state.events) == ["1:1", "2:2", "2:3", "7:5"]
    assert state.events["2:3"].done is True
    assert state.events["1:1"].last_updated == "2026-01-01T10:00:00Z"

    # ICS backup has the same events
    ics = Path(live_settings.ics_output).read_text(encoding="utf-8")
    assert ics.count("BEGIN:VEVENT") == 4

    # Every Vikunja call after login was authenticated
    assert all(
        r.authorization == "Bearer jwt-token-123"
        for r in tasks.requests
        if r.method == "GET"
    )
    assert "Sync complete" in capsys.readouterr().out


def test_second_sync_updates_in_place_and_skips_unchanged_overdue(
    tasks: FakeVikunja, google: FakeCalendarService, live_settings: Settings
) -> None:
    run(live_settings)
    first_state = read_state(live_settings)
    google.inserts.clear()

    run(live_settings)

    # No duplicates; the unchanged overdue task ("Report", 2:2) is not touched
    assert google.inserts == []
    assert len(calendar_events(google)) == 4
    skipped = first_state.events["2:2"].event_id
    assert skipped not in google.patches
    assert sorted(google.patches) == sorted(
        first_state.events[k].event_id for k in ("1:1", "2:3", "7:5")
    )
    assert read_state(live_settings) == first_state


def test_task_changes_are_propagated(
    tasks: FakeVikunja, google: FakeCalendarService, live_settings: Settings
) -> None:
    run(live_settings)
    event_id = read_state(live_settings).events["2:2"].event_id

    # The overdue task gets completed in Vikunja
    report = tasks.tasks[1]
    report.done = True
    report.updated = "2026-02-01T10:00:00Z"
    run(live_settings)

    event = calendar_events(google)[event_id]
    assert event.summary == "✅ [Work] Report"
    assert event.color_id == "10"
    assert read_state(live_settings).events["2:2"] == EventState(
        event_id=event_id, done=True, last_updated="2026-02-01T10:00:00Z"
    )


def test_edited_overdue_task_is_updated(
    tasks: FakeVikunja, google: FakeCalendarService, live_settings: Settings
) -> None:
    run(live_settings)
    event_id = read_state(live_settings).events["2:2"].event_id

    tasks.tasks[1].title = "Report (late)"
    tasks.tasks[1].updated = "2026-02-01T10:00:00Z"
    run(live_settings)

    assert calendar_events(google)[event_id].summary == "[Work] Report (late)"


def test_reuses_existing_calendar_by_name(
    tasks: FakeVikunja, google: FakeCalendarService, live_settings: Settings
) -> None:
    google.add_calendar("primary", "Personal")
    google.add_calendar("existing", "Vikunja Tasks")
    run(live_settings)
    assert read_state(live_settings).calendar_id == "existing"
    assert set(google.event_store) == {"existing"}


def test_reminders_are_set_on_calendar_and_events(
    tasks: FakeVikunja, google: FakeCalendarService, live_settings: Settings
) -> None:
    live_settings.reminder_minutes = (1440, 0)
    run(live_settings)

    expected = [
        EventReminder(method="popup", minutes=1440),
        EventReminder(method="popup", minutes=0),
    ]
    assert google.list_entries["cal-1"].default_reminders == expected
    for event in calendar_events(google).values():
        assert event.reminders == EventReminders(use_default=False, overrides=expected)


def test_only_selected_projects_are_synced(
    tasks: FakeVikunja, google: FakeCalendarService, live_settings: Settings
) -> None:
    live_settings.project_ids = (1,)
    run(live_settings)
    assert sorted(e.summary for e in calendar_events(google).values()) == [
        "[Home] Dentist"
    ]


def test_failed_upsert_is_reported_and_retried_next_run(
    tasks: FakeVikunja,
    google: FakeCalendarService,
    live_settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    google.fail_summaries = {"[Home] Dentist"}
    run(live_settings)

    assert "[WARN] Upsert failed for 1:1" in capsys.readouterr().out
    state = read_state(live_settings)
    assert "1:1" not in state.events
    assert len(state.events) == 3

    google.fail_summaries = set()
    run(live_settings)
    assert "1:1" in read_state(live_settings).events
    assert len(calendar_events(google)) == 4


def test_sync_with_api_token_skips_login(
    tasks: FakeVikunja, google: FakeCalendarService, live_settings: Settings
) -> None:
    tasks.token = live_settings.vikunja_api_token = "tk_abc"
    live_settings.vikunja_password = "wrong"
    run(live_settings)
    assert len(calendar_events(google)) == 4
    assert all(r.method == "GET" for r in tasks.requests)
    assert all(r.authorization == "Bearer tk_abc" for r in tasks.requests)


def test_bad_credentials_abort_before_touching_google(
    tasks: FakeVikunja, google: FakeCalendarService, live_settings: Settings
) -> None:
    live_settings.vikunja_password = "wrong"
    with pytest.raises(SystemExit, match="Failed to login"):
        run(live_settings)
    assert google.calendar_bodies == {}
    assert not Path(live_settings.state_file).exists()


def test_missing_config_aborts(live_settings: Settings) -> None:
    live_settings.vikunja_api_base = ""
    with pytest.raises(SystemExit, match="must be set"):
        run(live_settings)


def test_output_folders_are_created(
    tasks: FakeVikunja,
    google: FakeCalendarService,
    live_settings: Settings,
    tmp_path: Path,
) -> None:
    live_settings.state_file = str(tmp_path / "nested" / "state" / "state.json")
    live_settings.ics_output = str(tmp_path / "out" / "cal.ics")
    run(live_settings)
    assert (tmp_path / "nested" / "state" / "state.json").is_file()
    assert (tmp_path / "out" / "cal.ics").is_file()
