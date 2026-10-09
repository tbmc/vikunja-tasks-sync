from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.fakes import FakeCalendarService, FakeVikunja, serve_vikunja
from vikunja_sync import sync
from vikunja_sync.config import Settings
from vikunja_sync.google_calendar import CalendarService
from vikunja_sync.vikunja import Project, VikunjaTask


def iso(days: float) -> str:
    """ISO timestamp `days` from now (negative = past), in Vikunja's format."""
    return (datetime.now(UTC) + timedelta(days=days)).replace(microsecond=0).isoformat()


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
) -> VikunjaTask:
    return VikunjaTask(
        id=task_id,
        project_id=project_id,
        title=title if title is not None else f"Task {task_id}",
        description=description,
        done=done,
        due_date=due_date,
        start_date=start_date,
        updated=updated,
    )


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    """Test settings writing into the temp folder (`tmp_path`)."""
    return Settings(
        vikunja_api_base="https://vikunja.example.com/api/v1",
        vikunja_username="alice",
        vikunja_password="s3cret",
        state_file=str(tmp_path / "state.json"),
        ics_output=str(tmp_path / "calendar.ics"),
        timezone="Europe/Paris",
    )


@pytest.fixture()
def vikunja() -> Iterator[FakeVikunja]:
    """A fake Vikunja reachable over real HTTP (see `live_settings`)."""
    fake = FakeVikunja(
        projects=[Project(id=1, title="Home"), Project(id=2, title="Work")]
    )
    with serve_vikunja(fake):
        yield fake


@pytest.fixture()
def live_settings(settings: Settings, vikunja: FakeVikunja) -> Settings:
    """`settings` pointing at the fake Vikunja server."""
    settings.vikunja_api_base = vikunja.base_url
    return settings


@pytest.fixture()
def google(monkeypatch: pytest.MonkeyPatch) -> FakeCalendarService:
    """An in-memory Google Calendar returned by `google_service()` (skips OAuth)."""
    fake = FakeCalendarService()

    def fake_google_service(settings: Settings) -> CalendarService:
        return fake

    monkeypatch.setattr(sync, "google_service", fake_google_service)
    return fake
