"""Sync orchestration: Vikunja -> Google Calendar (+ ICS backup)."""

from pathlib import Path

from vikunja_sync import vikunja
from vikunja_sync.config import Settings
from vikunja_sync.events import (
    build_event_body,
    is_overdue,
    iso_to_utc_dt,
    task_key,
)
from vikunja_sync.google_calendar import (
    CalendarService,
    ensure_calendar,
    google_service,
    set_calendar_default_reminders,
    upsert_event,
)
from vikunja_sync.ics_export import tasks_to_ics
from vikunja_sync.state import EventState, State, load_state, save_state
from vikunja_sync.vikunja import Projects, VikunjaTask


def should_sync(task: VikunjaTask, prev: EventState | None) -> bool:
    """
    - tasks without a due date are skipped
    - done tasks are always upserted (created/updated as done)
    - overdue tasks already synced are skipped unless they changed
    """
    if not iso_to_utc_dt(task.due_date):
        return False
    unchanged = (
        prev is not None
        and not prev.done
        and prev.last_updated == task.updated
    )
    return not (is_overdue(task) and unchanged)


def sync_events(
    service: CalendarService,
    calendar_id: str,
    tasks: list[VikunjaTask],
    projects: Projects,
    state: State,
    settings: Settings,
) -> None:
    for t in tasks:
        key = task_key(t)
        known = state.events.get(key)
        if not should_sync(t, known):
            continue
        body = build_event_body(t, projects.title_of(t.project_id), settings)
        if not body:
            continue
        try:
            event_id = upsert_event(
                service, calendar_id, known.event_id if known else None, body
            )
        except Exception as e:
            print(f"[WARN] Upsert failed for {key}: {e}")
            continue
        state.events[key] = EventState(
            event_id=event_id, done=t.done, last_updated=t.updated
        )


def ensure_output_dirs(settings: Settings) -> None:
    for path in (settings.state_file, settings.ics_output):
        Path(path).parent.mkdir(parents=True, exist_ok=True)


def run(settings: Settings) -> None:
    ensure_output_dirs(settings)

    # Login to Vikunja (fresh JWT)
    token = vikunja.login(settings)

    state = load_state(settings.state_file)
    service = google_service(settings)
    cal_id = ensure_calendar(service, state, settings)

    # Optional: set calendar default reminders (safety net)
    if settings.reminder_minutes:
        try:
            set_calendar_default_reminders(service, cal_id, settings.reminder_minutes)
        except Exception as e:
            print(f"[WARN] Could not set default reminders on calendar: {e}")

    projects = vikunja.fetch_projects(settings, token)
    tasks = vikunja.fetch_tasks(settings, token)

    sync_events(service, cal_id, tasks, projects, state, settings)
    tasks_to_ics(tasks, projects, settings.ics_output)

    save_state(settings.state_file, state)
    print(
        f"✅ Sync complete. Calendar: {settings.google_calendar_name}"
        f" | State: {settings.state_file} | ICS: {settings.ics_output}"
    )
