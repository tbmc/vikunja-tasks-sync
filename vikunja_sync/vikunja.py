"""Vikunja REST API client."""

from typing import Annotated

import requests
from pydantic import BaseModel, BeforeValidator, RootModel, StrictInt, TypeAdapter
from pydantic_core import PydanticUseDefault

from vikunja_sync.config import Settings

REQUEST_TIMEOUT = 30


def _str_or_default(value: object) -> object:
    if not isinstance(value, str):
        raise PydanticUseDefault()
    return value


# A string field falling back to its default when missing, null or of another type
type LenientStr = Annotated[str, BeforeValidator(_str_or_default)]


class VikunjaTask(BaseModel):
    id: StrictInt
    project_id: StrictInt
    title: LenientStr = "Untitled"
    description: LenientStr = ""
    done: Annotated[bool, BeforeValidator(bool)] = False
    due_date: LenientStr = ""
    start_date: LenientStr = ""
    updated: LenientStr = ""


class TaskList(BaseModel):
    """Some Vikunja versions wrap task lists in {"tasks": [...]}."""

    tasks: list[VikunjaTask] = []


class Project(BaseModel):
    id: StrictInt
    title: LenientStr = ""


class Projects(RootModel[list[Project]]):
    def title_of(self, project_id: int) -> str:
        return next(
            (p.title for p in self.root if p.id == project_id), f"Project {project_id}"
        )


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    # The token field name depends on the Vikunja version
    token: LenientStr = ""
    access_token: LenientStr = ""
    jwt: LenientStr = ""


TASKS = TypeAdapter(list[VikunjaTask] | TaskList)


def login(settings: Settings) -> str:
    """
    POST /login with {"username": "...", "password": "..."}
    Returns a JWT token string.
    """
    if (
        not settings.vikunja_api_base
        or not settings.vikunja_username
        or not settings.vikunja_password
    ):
        raise SystemExit(
            "VIKUNJA_API_BASE, VIKUNJA_USERNAME and VIKUNJA_PASSWORD must be set in .env"
        )

    payload = LoginRequest(
        username=settings.vikunja_username, password=settings.vikunja_password
    )
    try:
        r = requests.post(
            f"{settings.vikunja_api_base}/login",
            data=payload.model_dump_json(),
            headers={"Content-Type": "application/json"},
            timeout=REQUEST_TIMEOUT,
            verify=settings.vikunja_verify_ssl,
        )
        r.raise_for_status()
        data = LoginResponse.model_validate_json(r.content)
        token = data.token or data.access_token or data.jwt
        if not token:
            raise RuntimeError(f"Login response missing token field: {r.text}")
        return token
    except Exception as e:
        raise SystemExit(f"Failed to login to Vikunja: {e}")


def _get(settings: Settings, token: str, path: str) -> bytes:
    r = requests.get(
        f"{settings.vikunja_api_base}{path}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=REQUEST_TIMEOUT,
        verify=settings.vikunja_verify_ssl,
    )
    r.raise_for_status()
    return r.content


def fetch_projects(settings: Settings, token: str) -> Projects:
    return Projects.model_validate_json(_get(settings, token, "/projects"))


def _parse_tasks(content: bytes) -> list[VikunjaTask]:
    parsed = TASKS.validate_json(content)
    return parsed.tasks if isinstance(parsed, TaskList) else parsed


def fetch_tasks(settings: Settings, token: str) -> list[VikunjaTask]:
    tasks: list[VikunjaTask] = []
    if settings.project_ids:
        for pid in settings.project_ids:
            tasks.extend(_parse_tasks(_get(settings, token, f"/projects/{pid}/tasks")))
    else:
        # Pull all tasks, page by page until an empty page
        page = 1
        while items := _parse_tasks(_get(settings, token, f"/tasks/all?page={page}")):
            tasks.extend(items)
            page += 1
    return tasks
