"""Settings read from the environment (see `.env.example`)."""

import os
from typing import Annotated

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict


def parse_int_list(raw: object, *, allow_negative: bool = False) -> object:
    """Parse "2, 5,x,9" -> (2, 5, 9); invalid items are ignored. Non-strings are left to pydantic."""
    if not isinstance(raw, str):
        return raw
    items = (x.strip() for x in raw.split(","))
    return tuple(
        int(x) for x in items if (x.lstrip("-") if allow_negative else x).isdigit()
    )


def parse_signed_int_list(raw: object) -> object:
    return parse_int_list(raw, allow_negative=True)


def strip_trailing_slash(url: str) -> str:
    return url.rstrip("/")


class Settings(BaseModel):
    # Fields are read from the upper-cased env variables (VIKUNJA_API_BASE, ...)
    model_config = ConfigDict(
        validate_assignment=True,
        alias_generator=str.upper,
        validate_by_name=True,
        validate_by_alias=True,
    )

    vikunja_api_base: Annotated[str, AfterValidator(strip_trailing_slash)] = ""
    vikunja_username: str = ""
    vikunja_password: str = ""
    vikunja_verify_ssl: bool = True
    # Empty = all accessible projects
    project_ids: Annotated[tuple[int, ...], BeforeValidator(parse_int_list)] = ()
    google_calendar_name: str = "Vikunja Tasks"
    google_credentials_file: str = "credentials.json"
    google_token_file: str = "token.json"
    state_file: str = ".state/state.json"
    ics_output: str = ".out/vikunja_calendar.ics"
    timezone: str = "UTC"
    # Empty = use the calendar's default reminders
    reminder_minutes: Annotated[
        tuple[int, ...], BeforeValidator(parse_signed_int_list)
    ] = ()

    @property
    def vikunja_web_base(self) -> str:
        return self.vikunja_api_base.replace("/api/v1", "")


def load_settings() -> Settings:
    return Settings.model_validate(os.environ)
