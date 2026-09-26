# YouTube Automation Control Panel

Upload and (soon) manage videos on your own YouTube channel through a local
Streamlit dashboard, backed by the official YouTube Data API v3.

Each install is authorized against **your own** Google Cloud project and
**your own** channel - there is no shared account, no data leaves your
machine, and nothing here needs Charles/ArchTech's involvement after setup.

## Requirements

- Python 3.12+ (`python3 --version`)
- Windows, macOS, or Linux
- A Google account with the channel you want to manage
- Optional: a Chrome/Edge/Brave install, only if you enable the browser-automation extra (see below)

## Install

```bash
git clone https://github.com/nexustech101/yt-dashboard.git
cd <repo-folder>
```

Then either:

- **Double-click the launcher** - `launch.bat` (Windows) or `launch.command` (macOS). On Linux,
  run `./launch.sh` from a terminal. First run creates a virtual environment and installs
  dependencies; every run after that just starts the app.
- **Or manually:**
  ```bash
  python3 -m venv .venv
  source .venv/bin/activate      # Windows: .venv\Scripts\activate
  pip install .
  streamlit run app.py
  ```

Either way, the app opens in your browser at `http://localhost:8501`.

To also enable Studio browser automation (see [Browser automation](#browser-automation-optional)
below): `pip install .[browser]` instead of `pip install .`.

## First run

The dashboard is gated behind a **Getting Started** page (sidebar) until setup is done. It walks
you through:

1. Creating a Google Cloud project + OAuth "Desktop app" credential (Google only exposes this
   through their Console UI - it can't be scripted for you).
2. Uploading the downloaded credentials JSON.
3. Authorizing (opens a Google sign-in tab, approve, come back).

This is a one-time, ~10 minute setup per install. Nothing is guessed or auto-filled on your
behalf beyond that page's instructions.

## Project layout

```
app.py                       Main dashboard (Streamlit) - upload form + run progress
pages/1_Getting_Started.py   Setup wizard: OAuth credentials, authorization, status
api.py                       YouTube Data API v3 client - auth, resumable upload
yt.py                        Optional: YouTube Studio browser automation (patchright)
utils.py                     Shared: run-state tracking, browser session management, config
config.json                  Browser executable paths per OS, used only by yt.py
pyproject.toml               Dependencies
launch.bat / .sh / .command  Clickable launchers (create venv, install, run)
```

Per-client runtime data (OAuth credentials, cached token, run state, staged uploads) lives under
`~/.yt_automation/`, not in this folder - it's never git-tracked and survives a `git pull`.

## Tracing a feature

**Upload flow:** `app.py` (form) -> `start_upload()` -> `api.run_api_upload()` (async wrapper,
runs in a background thread) -> `api.upload_video()` (resumable upload + retry) -> progress is
written to `~/.yt_automation/state/yt_api_upload.json` via `utils.StateStore`, and `app.py` polls
it back with `utils.read_state()`.

**Auth flow:** `pages/1_Getting_Started.py` -> `api.get_credentials()` -> first run: Google's
`InstalledAppFlow.run_local_server()` (opens your browser, waits for consent) -> caches to
`~/.yt_automation/token.json`. Later runs: reuses/refreshes that token silently.

## Quota

Per Google Cloud project, current as of mid-2026 (Google's Quotas page in Cloud Console is the
source of truth - these numbers have changed before and will again):

- `videos.insert` (uploads): 100/day, own bucket
- `search.list`: 100/day, own bucket
- everything else: 10,000 units/day combined (reads ~1 unit, writes ~50 units)

Quota resets at midnight Pacific Time. Since this install is on its own Google Cloud project, it
never shares this with any other client's install.

## Known limitations (current)

- **Uploads land as `private`** regardless of the privacy setting chosen, until this Google Cloud
  project passes Google's compliance audit (their restriction on any unaudited API project,
  unrelated to this app - see the Privacy dropdown on the upload form, and the
  [Browser automation](#browser-automation-optional) section below for a workaround).
- **OAuth scope is `youtube.upload` only** - covers uploading and thumbnails. Editing or deleting
  existing videos needs a broader scope grant and a re-authorization; not requested yet since the
  dashboard doesn't have those features wired up.
- Dashboard currently covers upload + progress only. Metadata editing, playlists, captions, and
  comment moderation are candidates for a later pass.

## Browser automation (optional)

`yt.py` drives real YouTube Studio through a Chromium-based browser (not the API), which sidesteps
the forced-private restriction above since it isn't an API call. It's an optional extra:

```bash
pip install .[browser]
```

Also requires Chrome, Edge, or Brave installed, and Playwright's Chromium driver via patchright.
This path isn't wired into the dashboard UI yet - `yt.py` runs standalone
(`python yt.py <path-to-video>`). Browser executable paths are read from `config.json`; edit it if
your browser is installed somewhere config.json doesn't already check.