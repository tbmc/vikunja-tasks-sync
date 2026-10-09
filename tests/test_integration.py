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
    get_token,
    login,
)


def test_login_returns_token(vikunja: FakeVikunja, live_settings: Settings) -> None:
    assert login(live_settings) == "jwt-token-123"
    assert [(r.method, r.path) for r in vikunja.requests] == [("POST", "/api/v1/login")]


@pytest.mark.parametrize("field", ["access_token", "jwt"])
def test_login_accepts_alternative_token_fields(
    vikunja: FakeVikunja, live_settings: Settings, field: str
) -> None:
    vikunja.token_field = field
    assert login(live_settings) == "jwt-token-123"


def test_login_missing_token_field_exits(
    vikunja: FakeVikunja, live_settings: Settings
) -> None:
    vikunja.token_field = "something_else"
    with pytest.raises(SystemExit, match="Login response missing token field"):
        login(live_settings)


def test_login_wrong_password_exits(live_settings: Settings) -> None:
    live_settings.vikunja_password = "wrong"
    with pytest.raises(SystemExit, match="Failed to login to Vikunja: 412"):
        login(live_settings)


def test_login_unreachable_server_exits(settings: Settings) -> None:
    settings.vikunja_api_base = "http://127.0.0.1:1/api/v1"
    with pytest.raises(SystemExit, match="Failed to login to Vikunja"):
        login(settings)


def test_login_missing_config_exits_without_request(
    vikunja: FakeVikunja, live_settings: Settings
) -> None:
    live_settings.vikunja_username = ""
    with pytest.raises(SystemExit, match="must be set"):
        login(live_settings)
    assert vikunja.requests == []


def test_get_token_uses_api_token_without_request(
    vikunja: FakeVikunja, live_settings: Settings
) -> None:
    live_settings.vikunja_api_token = "tk_abc"
    live_settings.vikunja_password = ""
    assert get_token(live_settings) == "tk_abc"
    assert vikunja.requests == []


def test_get_token_falls_back_to_login(
    vikunja: FakeVikunja, live_settings: Settings
) -> None:
    assert get_token(live_settings) == "jwt-token-123"
    assert [(r.method, r.path) for r in vikunja.requests] == [("POST", "/api/v1/login")]


def test_get_token_missing_api_base_exits(settings: Settings) -> None:
    settings.vikunja_api_base = ""
    settings.vikunja_api_token = "tk_abc"
    with pytest.raises(SystemExit, match="must be set"):
        get_token(settings)


def test_fetch_projects(vikunja: FakeVikunja, live_settings: Settings) -> None:
    projects = fetch_projects(live_settings, "jwt-token-123")
    assert projects == Projects(
        [Project(id=1, title="Home"), Project(id=2, title="Work")]
    )
    assert vikunja.requests[-1].authorization == "Bearer jwt-token-123"


def test_fetch_projects_rejected_token(live_settings: Settings) -> None:
    with pytest.raises(requests.HTTPError, match="401"):
        fetch_projects(live_settings, "bad-token")


def test_fetch_tasks_all_tasks_are_paginated_until_empty_page(
    vikunja: FakeVikunja, live_settings: Settings
) -> None:
    vikunja.tasks = [
        VikunjaTask(id=i, project_id=1 + i % 2, title=f"T{i}") for i in range(1, 6)
    ]
    tasks = fetch_tasks(live_settings, "jwt-token-123")
    assert [t.id for t in tasks] == [1, 2, 3, 4, 5]
    assert [r.path for r in vikunja.requests] == [
        f"/api/v1/tasks/all?page={p}" for p in (1, 2, 3, 4)
    ]


def test_fetch_tasks_no_tasks(live_settings: Settings) -> None:
    assert fetch_tasks(live_settings, "jwt-token-123") == []


@pytest.mark.parametrize("wrapped", [False, True])
def test_fetch_tasks_selected_projects_only(
    vikunja: FakeVikunja, live_settings: Settings, wrapped: bool
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


def test_fetch_tasks_unknown_project_raises(live_settings: Settings) -> None:
    live_settings.project_ids = (1,)
    live_settings.vikunja_api_base = live_settings.vikunja_api_base + "/nope"
    with pytest.raises(requests.HTTPError, match="404"):
        fetch_tasks(live_settings, "jwt-token-123")
