"""Optional `.ics` backup of the tasks."""

from datetime import datetime, time

from ics import Calendar, Event
from ics.grammar.parse import ContentLine

from vikunja_sync.events import (
    all_day_dates,
    all_day_recurrence,
    event_summary,
    iso_to_utc_dt,
)
from vikunja_sync.vikunja import Projects, VikunjaTask


def tasks_to_ics(
    tasks: list[VikunjaTask],
    projects: Projects,
    path: str,
    timezone: str = "UTC",
) -> None:
    cal = Calendar()
    for t in tasks:
        e = Event()
        e.name = event_summary(t, projects.title_of(t.project_id))
        e.description = t.description
        if due_dt := iso_to_utc_dt(t.due_date):
            e.begin = iso_to_utc_dt(t.start_date) or due_dt
            e.end = due_dt
        elif dates := all_day_dates(t, timezone):
            # No due date: all-day event on the reminder days, repeating with the task
            e.begin = datetime.combine(dates[0], time())
            e.make_all_day()
            e.extra.extend(map(ContentLine.parse, all_day_recurrence(t, dates)))
        else:
            continue
        cal.events.add(e)
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(cal)
