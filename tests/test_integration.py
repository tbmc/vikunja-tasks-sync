"""Integration tests: the Vikunja client talking to a fake Vikunja server over real HTTP."""

import pytest
import requests

import vikunja_sync as vs
from tests.fakes import FakeVikunja


class TestLogin:
    def test_returns_token(self, vikunja: FakeVikunja) -> None:
        assert vs.vikunja_login() == "jwt-token-123"
        assert [(r.method, r.path) for r in vikunja.requests] == [("POST", "/api/v1/login")]

    @pytest.mark.parametrize("field", ["access_token", "jwt"])
    def test_accepts_alternative_token_fields(self, vikunja: FakeVikunja, field: str) -> None:
        vikunja.token_field = field
        assert vs.vikunja_login() == "jwt-token-123"

    def test_missing_token_field_exits(self, vikunja: FakeVikunja) -> None:
        vikunja.token_field = "something_else"
        with pytest.raises(SystemExit, match="Login response missing token field"):
            vs.vikunja_login()

    def test_wrong_password_exits(self, vikunja: FakeVikunja, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(vs, "VIKUNJA_PASSWORD", "wrong")
        with pytest.raises(SystemExit, match="Failed to login to Vikunja: 412"):
            vs.vikunja_login()

    def test_unreachable_server_exits(self, config: object, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(vs, "VIKUNJA_API_BASE", "http://127.0.0.1:1/api/v1")
        with pytest.raises(SystemExit, match="Failed to login to Vikunja"):
            vs.vikunja_login()

    def test_missing_config_exits_without_request(
        self, vikunja: FakeVikunja, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(vs, "VIKUNJA_USERNAME", "")
        with pytest.raises(SystemExit, match="must be set"):
            vs.vikunja_login()
        assert vikunja.requests == []


def test_fetch_projects_map(vikunja: FakeVikunja) -> None:
    assert vs.fetch_projects_map("jwt-token-123") == {1: "Home", 2: "Work"}
    assert vikunja.requests[-1].authorization == "Bearer jwt-token-123"


def test_fetch_projects_map_rejected_token(vikunja: FakeVikunja) -> None:
    with pytest.raises(requests.HTTPError, match="401"):
        vs.fetch_projects_map("bad-token")


class TestFetchTasks:
    def test_all_tasks_are_paginated_until_empty_page(self, vikunja: FakeVikunja) -> None:
        vikunja.tasks = [{"id": i, "project_id": 1 + i % 2, "title": f"T{i}"} for i in range(1, 6)]
        tasks = vs.fetch_tasks("jwt-token-123")
        assert [t["id"] for t in tasks] == [1, 2, 3, 4, 5]
        assert [r.path for r in vikunja.requests] == [f"/api/v1/tasks/all?page={p}" for p in (1, 2, 3, 4)]

    def test_no_tasks(self, vikunja: FakeVikunja) -> None:
        assert vs.fetch_tasks("jwt-token-123") == []

    @pytest.mark.parametrize("wrapped", [False, True])
    def test_selected_projects_only(
        self, vikunja: FakeVikunja, monkeypatch: pytest.MonkeyPatch, wrapped: bool
    ) -> None:
        vikunja.wrap_project_tasks = wrapped
        vikunja.tasks = [
            {"id": 1, "project_id": 1},
            {"id": 2, "project_id": 2},
            {"id": 3, "project_id": 3},
            {"id": 4, "project_id": 3},
        ]
        monkeypatch.setattr(vs, "PROJECT_IDS", [3, 1])
        tasks = vs.fetch_tasks("jwt-token-123")
        assert [t["id"] for t in tasks] == [3, 4, 1]
        assert [r.path for r in vikunja.requests] == ["/api/v1/projects/3/tasks", "/api/v1/projects/1/tasks"]

    def test_unknown_project_raises(self, vikunja: FakeVikunja, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(vs, "PROJECT_IDS", [1])
        monkeypatch.setattr(vs, "VIKUNJA_API_BASE", vs.VIKUNJA_API_BASE + "/nope")
        with pytest.raises(requests.HTTPError, match="404"):
            vs.fetch_tasks("jwt-token-123")
