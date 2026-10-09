## Vikunja → Google Calendar Sync

Sync your **Vikunja tasks** (via API) with **Google Calendar** automatically.
Each task becomes a calendar event linked to its project, with reminders for upcoming deadlines.

### Features

* Logs in to Vikunja and fetches all (or selected) projects
* Creates or updates Google Calendar events (no duplicates)
* Marks done tasks as ✅ and colors them green
* Skips unchanged overdue tasks
* Adds popup reminders: 1 week, 3 days, 2 days, 1 day, and same day
* Exports an optional `.ics` backup file
* Tracks state locally (`.state/state.json`)

---

### Setup

1. **Create a Google Cloud project** and enable the *Google Calendar API*

   * Create **OAuth credentials → Desktop app**
   * Download `credentials.json` → place it in this folder
2. **Copy and edit the environment file**

   ```bash
   cp .env.example .env
   ```

   Fill in your Vikunja URL and either an API token (`VIKUNJA_API_TOKEN`, created in Vikunja under *Settings → API Tokens* with read access to projects and tasks) or your username and password.
3. **Install dependencies** (requires [uv](https://docs.astral.sh/uv/) and Python 3.14+)

   ```bash
   uv sync
   ```
4. **Run the sync**

   ```bash
   uv run python -m vikunja_sync
   ```

   The first run opens a browser window to authorize Google access and creates `token.json`.

---

### Docker

The image is built in two stages: `uv` installs the locked runtime dependencies into a virtualenv, which is then copied into a slim Python image (no `uv`, no dev dependencies).

```bash
docker build -t vikunja-tasks-sync .
```

The container works in `/data`: relative paths from `.env` (`credentials.json`, `token.json`, `.state/`, `.out/`) resolve there, so mount a folder on it.
The Google OAuth flow needs a browser, so generate `token.json` once on your machine (`uv run python -m vikunja_sync`) and copy it to that folder along with `credentials.json`.

```bash
mkdir -p data && cp credentials.json token.json data/
docker run --rm --env-file .env -v "$PWD/data:/data" vikunja-tasks-sync
```

The container runs as uid `1000`; make sure it can write to the mounted folder (the token is refreshed in place).

With Docker Compose (`compose.yaml` builds the image, reads `.env` and mounts `./data` on `/data`):

```bash
docker compose run --rm --build vikunja-sync
```

The sync runs once and exits; schedule it with cron, e.g. every 15 minutes:

```cron
*/15 * * * * cd /path/to/vikunja-tasks-sync && docker compose run --rm vikunja-sync
```

---

### Code layout

The `vikunja_sync` package (all data structures are [pydantic](https://docs.pydantic.dev/) models):

* `config.py`: `Settings` model, validated from the environment
* `vikunja.py`: Vikunja API client (API token or login, projects, tasks)
* `google_calendar.py`: Google Calendar payload models, typed client over `googleapiclient`, OAuth and calendar/event helpers
* `events.py`: task → event conversion (dates, summary, reminders)
* `state.py`: local task → event mapping (`STATE_FILE`)
* `ics_export.py`: `.ics` backup
* `sync.py`: sync rules and orchestration (`run()`)
* `__main__.py`: entry point (loads `.env`, then runs the sync)

---

### Type checking

The code is checked with [mypy](https://mypy.readthedocs.io/) in its strictest configuration (see `[tool.mypy]` in `pyproject.toml`), with the pydantic mypy plugin.
Minimal stubs for untyped dependencies (`ics`, `google_auth_oauthlib`) live in `typings/`.

```bash
uv run mypy
```

---

### Tests

```bash
uv run pytest
```

* `tests/test_unit.py`: pure helpers (config, parsing, dates, sync rules, event bodies, state, ICS export) and calendar helpers
* `tests/test_integration.py`: the Vikunja client against a fake Vikunja server over real HTTP (login, pagination, project filter, errors)
* `tests/test_e2e.py`: full sync runs (`run()`) against the fake Vikunja server and an in-memory Google Calendar (creation, updates, skipped overdue tasks, failures, reminders, output folders)

No network access or Google credentials are needed: the doubles live in `tests/fakes.py`.

---

### Automation (optional)

Add a cron job to run every 15 minutes:

```bash
*/15 * * * * cd /path/to/vikunja-tasks-sync && /path/to/uv run python -m vikunja_sync
```

---

### Notes

* All configuration is handled in `.env`
* The script safely refreshes your Google token automatically
* You can manually delete `.state/state.json` to rebuild mappings if needed