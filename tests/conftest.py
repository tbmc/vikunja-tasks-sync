import os
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Keep the developer's real .env out of the tests: vikunja_sync reads its config at import time.
os.environ["VIKUNJA_API_BASE"] = ""
os.environ["PROJECT_IDS"] = ""
os.environ["REMINDER_MINUTES"] = ""

import vikunja_sync as vs  # noqa: E402

from tests.fakes import FakeCalendarService, FakeVikunja, serve_vikunja  # noqa: E402


def iso(days: float) -> str:
    """ISO timestamp `days` from now (negative = past), in Vikunja's format."""
    return (
        (datetime.now(timezone.utc) + timedelta(days=days))
        .replace(microsecond=0)
        .isoformat()
    )


def make_task(
    task_id: int,
    project_id: int = 1,
    *,
    title: str | None = None,
    description: str = "",
    done: bool = False,
    due_date: str = "0001-01-01T00:00:00Z",
    start_date: str = "0001-01-01T00:00:00Z",
    updated: str = "2026-01-01T10:00:00Z",
) -> vs.VikunjaTask:
    return {
        "id": task_id,
        "project_id": project_id,
        "title": title if title is not None else f"Task {task_id}",
        "description": description,
        "done": done,
        "due_date": due_date,
        "start_date": start_date,
        "updated": updated,
    }


@pytest.fixture()
def config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point every module-level setting at test values; returns the temp working folder."""
    monkeypatch.setattr(vs, "VIKUNJA_API_BASE", "https://vikunja.example.com/api/v1")
    monkeypatch.setattr(vs, "VIKUNJA_USERNAME", "alice")
    monkeypatch.setattr(vs, "VIKUNJA_PASSWORD", "s3cret")
    monkeypatch.setattr(vs, "VIKUNJA_VERIFY_SSL", True)
    monkeypatch.setattr(vs, "PROJECT_IDS", [])
    monkeypatch.setattr(vs, "GOOGLE_CALENDAR_NAME", "Vikunja Tasks")
    monkeypatch.setattr(vs, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(vs, "ICS_OUTPUT", str(tmp_path / "calendar.ics"))
    monkeypatch.setattr(vs, "TIMEZONE", "Europe/Paris")
    monkeypatch.setattr(vs, "REMINDER_MINUTES", [])
    return tmp_path


@pytest.fixture()
def vikunja(config: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeVikunja]:
    """A fake Vikunja reachable over real HTTP, wired into the module config."""
    fake = FakeVikunja(
        projects=[{"id": 1, "title": "Home"}, {"id": 2, "title": "Work"}]
    )
    with serve_vikunja(fake) as base_url:
        monkeypatch.setattr(vs, "VIKUNJA_API_BASE", base_url)
        yield fake


@pytest.fixture()
def google(monkeypatch: pytest.MonkeyPatch) -> FakeCalendarService:
    """An in-memory Google Calendar returned by `google_service()` (skips OAuth)."""
    fake = FakeCalendarService()

    def fake_google_service() -> vs.CalendarService:
        return fake

    monkeypatch.setattr(vs, "google_service", fake_google_service)
    return fake
