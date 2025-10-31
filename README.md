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

   Fill in your Vikunja URL, username, and password.
3. **Install dependencies**

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
4. **Run the sync**

   ```bash
   python vikunja_sync.py
   ```

   The first run opens a browser window to authorize Google access and creates `token.json`.

---

### Automation (optional)

Add a cron job to run every 15 minutes:

```bash
*/15 * * * * cd /path/to/vikunja-gcal-sync && /path/to/.venv/bin/python vikunja_sync.py
```

---

### Notes

* All configuration is handled in `.env`
* The script safely refreshes your Google token automatically
* You can manually delete `.state/state.json` to rebuild mappings if needed