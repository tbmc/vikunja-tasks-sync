"""Optional `.ics` backup of the tasks."""

from ics import Calendar, Event

from vikunja_sync.events import event_summary, iso_to_utc_dt
from vikunja_sync.vikunja import Projects, VikunjaTask


def tasks_to_ics(tasks: list[VikunjaTask], projects: Projects, path: str) -> None:
    cal = Calendar()
    for t in tasks:
        due_dt = iso_to_utc_dt(t.due_date)
        if not due_dt:
            continue
        e = Event()
        e.name = event_summary(t, projects.title_of(t.project_id))
        e.description = t.description
        e.begin = iso_to_utc_dt(t.start_date) or due_dt
        e.end = due_dt
        cal.events.add(e)
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(cal)
