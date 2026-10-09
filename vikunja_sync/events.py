"""Transform Vikunja tasks into calendar events."""

from datetime import UTC, datetime

from dateutil import parser as dateparse

from vikunja_sync.config import Settings
from vikunja_sync.google_calendar import (
    EventBody,
    EventDateTime,
    EventExtendedProperties,
    EventReminders,
    EventSource,
    PrivateProperties,
    popup_reminders,
)
from vikunja_sync.vikunja import VikunjaTask

# Vikunja's "no date" value
NULL_DATE = "0001-01-01T00:00:00Z"


def iso_to_utc_dt(iso_str: str) -> datetime | None:
    if not iso_str or iso_str == NULL_DATE:
        return None
    dt = dateparse.isoparse(iso_str)
    return dt.astimezone(UTC)


def task_key(task: VikunjaTask) -> str:
    return f"{task.project_id}:{task.id}"


def is_overdue(task: VikunjaTask) -> bool:
    due = iso_to_utc_dt(task.due_date)
    if not due:
        return False
    return (not task.done) and (due < datetime.now(UTC))


def event_summary(task: VikunjaTask, project: str) -> str:
    status_prefix = "✅ " if task.done else ""
    return f"{status_prefix}[{project}] {task.title}"


def build_event_body(
    task: VikunjaTask, project: str, settings: Settings
) -> EventBody | None:
    start_dt = iso_to_utc_dt(task.start_date)
    due_dt = iso_to_utc_dt(task.due_date)
    updated_dt = iso_to_utc_dt(task.updated)
    done = task.done

    if not due_dt:
        return None

    # If both start and due exist and due > start, use both; else use due as an instant point
    start_iso = (start_dt if start_dt and due_dt > start_dt else due_dt).isoformat()
    end_iso = due_dt.isoformat()

    return EventBody(
        summary=event_summary(task, project),
        description=task.description,
        start=EventDateTime(date_time=start_iso),
        end=EventDateTime(date_time=end_iso),
        extended_properties=EventExtendedProperties(
            private=PrivateProperties(
                vikunja_task_id=str(task.id),
                vikunja_project_id=str(task.project_id),
                vikunja_done=str(done),
                vikunja_updated=updated_dt.isoformat() if updated_dt else "",
            )
        ),
        source=EventSource(
            title="Vikunja", url=f"{settings.vikunja_web_base}/tasks/{task.id}"
        ),
        # Per-event reminders
        reminders=(
            EventReminders(
                use_default=False,
                overrides=popup_reminders(settings.reminder_minutes),
            )
            if settings.reminder_minutes
            else EventReminders(use_default=True)
        ),
        color_id="10" if done else None,  # green
    )
