"""Transform Vikunja tasks into calendar events."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

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
SECONDS_PER_DAY = 24 * 60 * 60
REPEAT_MODE_MONTHLY = 1


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


def recurrence_rule(task: VikunjaTask) -> str | None:
    """iCalendar RRULE value for a recurring task (sub-daily repeats become daily)."""
    if task.repeat_mode == REPEAT_MODE_MONTHLY:
        return "FREQ=MONTHLY"
    if task.repeat_after <= 0:
        return None
    days = max(1, round(task.repeat_after / SECONDS_PER_DAY))
    if days % 7 == 0:
        return f"FREQ=WEEKLY;INTERVAL={days // 7}"
    return f"FREQ=DAILY;INTERVAL={days}"


def local_date(dt: datetime, timezone: str) -> date:
    return dt.astimezone(ZoneInfo(timezone)).date()


def absolute_reminders(task: VikunjaTask) -> list[datetime]:
    """Reminders at a fixed date (relative ones need a due date)."""
    return [
        dt
        for r in task.reminders
        if not r.relative_to and (dt := iso_to_utc_dt(r.reminder))
    ]


def all_day_dates(task: VikunjaTask, timezone: str) -> list[date]:
    """
    Days shown for a task without due date: the days of its reminders.
    A recurring task without reminder starts on its creation day.
    """
    anchors = absolute_reminders(task)
    if not anchors and recurrence_rule(task):
        anchors = [dt] if (dt := iso_to_utc_dt(task.created)) else []
    return sorted({local_date(dt, timezone) for dt in anchors})


def all_day_recurrence(task: VikunjaTask, dates: list[date]) -> list[str]:
    """RRULE of a recurring task (from the first day), RDATE for the other reminder days."""
    lines = [f"RRULE:{rule}"] if (rule := recurrence_rule(task)) else []
    if extra := dates[1:]:
        lines.append("RDATE;VALUE=DATE:" + ",".join(f"{d:%Y%m%d}" for d in extra))
    return lines


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

    recurrence: list[str] = []
    if due_dt:
        # If both start and due exist and due > start, use both; else use due as an instant point
        start_iso = (start_dt if start_dt and due_dt > start_dt else due_dt).isoformat()
        start = EventDateTime(date_time=start_iso)
        end = EventDateTime(date_time=due_dt.isoformat())
    elif dates := all_day_dates(task, settings.timezone):
        # No due date: all-day event on the reminder days, repeating with the task
        start = EventDateTime(date=dates[0].isoformat())
        end = EventDateTime(date=(dates[0] + timedelta(days=1)).isoformat())
        recurrence = all_day_recurrence(task, dates)
    else:
        return None

    return EventBody(
        summary=event_summary(task, project),
        description=task.description,
        start=start,
        end=end,
        recurrence=recurrence,
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
