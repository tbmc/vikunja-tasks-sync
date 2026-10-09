"""Integration tests: the Vikunja client talking to a fake Vikunja server over real HTTP."""

import pytest
import requests

from tests.fakes import FakeVikunja
from vikunja_sync.config import Settings
from vikunja_sync.vikunja import (
    Project,
    Projects,
    VikunjaTask,
    fetch_projects,
    fetch_tasks,
    login,
)


class TestLogin:
    def test_returns_token(self, vikunja: FakeVikunja, live_settings: Settings) -> None:
        assert login(live_settings) == "jwt-token-123"
        assert [(r.method, r.path) for r in vikunja.requests] == [
            ("POST", "/api/v1/login")
        ]

    @pytest.mark.parametrize("field", ["access_token", "jwt"])
    def test_accepts_alternative_token_fields(
        self, vikunja: FakeVikunja, live_settings: Settings, field: str
    ) -> None:
        vikunja.token_field = field
        assert login(live_settings) == "jwt-token-123"

    def test_missing_token_field_exits(
        self, vikunja: FakeVikunja, live_settings: Settings
    ) -> None:
        vikunja.token_field = "something_else"
        with pytest.raises(SystemExit, match="Login response missing token field"):
            login(live_settings)

    def test_wrong_password_exits(self, live_settings: Settings) -> None:
        live_settings.vikunja_password = "wrong"
        with pytest.raises(SystemExit, match="Failed to login to Vikunja: 412"):
            login(live_settings)

    def test_unreachable_server_exits(self, settings: Settings) -> None:
        settings.vikunja_api_base = "http://127.0.0.1:1/api/v1"
        with pytest.raises(SystemExit, match="Failed to login to Vikunja"):
            login(settings)

    def test_missing_config_exits_without_request(
        self, vikunja: FakeVikunja, live_settings: Settings
    ) -> None:
        live_settings.vikunja_username = ""
        with pytest.raises(SystemExit, match="must be set"):
            login(live_settings)
        assert vikunja.requests == []


def test_fetch_projects(vikunja: FakeVikunja, live_settings: Settings) -> None:
    projects = fetch_projects(live_settings, "jwt-token-123")
    assert projects == Projects(
        [Project(id=1, title="Home"), Project(id=2, title="Work")]
    )
    assert vikunja.requests[-1].authorization == "Bearer jwt-token-123"


def test_fetch_projects_rejected_token(live_settings: Settings) -> None:
    with pytest.raises(requests.HTTPError, match="401"):
        fetch_projects(live_settings, "bad-token")


class TestFetchTasks:
    def test_all_tasks_are_paginated_until_empty_page(
        self, vikunja: FakeVikunja, live_settings: Settings
    ) -> None:
        vikunja.tasks = [
            VikunjaTask(id=i, project_id=1 + i % 2, title=f"T{i}") for i in range(1, 6)
        ]
        tasks = fetch_tasks(live_settings, "jwt-token-123")
        assert [t.id for t in tasks] == [1, 2, 3, 4, 5]
        assert [r.path for r in vikunja.requests] == [
            f"/api/v1/tasks/all?page={p}" for p in (1, 2, 3, 4)
        ]

    def test_no_tasks(self, live_settings: Settings) -> None:
        assert fetch_tasks(live_settings, "jwt-token-123") == []

    @pytest.mark.parametrize("wrapped", [False, True])
    def test_selected_projects_only(
        self, vikunja: FakeVikunja, live_settings: Settings, wrapped: bool
    ) -> None:
        vikunja.wrap_project_tasks = wrapped
        vikunja.tasks = [
            VikunjaTask(id=1, project_id=1),
            VikunjaTask(id=2, project_id=2),
            VikunjaTask(id=3, project_id=3),
            VikunjaTask(id=4, project_id=3),
        ]
        live_settings.project_ids = (3, 1)
        tasks = fetch_tasks(live_settings, "jwt-token-123")
        assert [t.id for t in tasks] == [3, 4, 1]
        assert [r.path for r in vikunja.requests] == [
            "/api/v1/projects/3/tasks",
            "/api/v1/projects/1/tasks",
        ]

    def test_unknown_project_raises(self, live_settings: Settings) -> None:
        live_settings.project_ids = (1,)
        live_settings.vikunja_api_base = live_settings.vikunja_api_base + "/nope"
        with pytest.raises(requests.HTTPError, match="404"):
            fetch_tasks(live_settings, "jwt-token-123")
