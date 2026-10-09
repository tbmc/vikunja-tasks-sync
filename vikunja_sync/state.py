"""Local state: which Google event each Vikunja task is synced to."""

from pathlib import Path

from pydantic import BaseModel


class EventState(BaseModel):
    event_id: str
    done: bool
    last_updated: str | None


class State(BaseModel):
    # Keyed by `task_key()` ("project_id:task_id")
    events: dict[str, EventState] = {}
    calendar_id: str | None = None


def load_state(path: str) -> State:
    file = Path(path)
    if file.exists():
        return State.model_validate_json(file.read_bytes())
    return State()


def save_state(path: str, state: State) -> None:
    Path(path).write_text(state.model_dump_json(indent=2), encoding="utf-8")
