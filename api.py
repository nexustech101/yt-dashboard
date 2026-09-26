from __future__ import annotations

import asyncio
import random
import time
from pathlib import Path
from typing import Callable

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from utils import APP_DATA_DIR, RunStatus, State, StateStore

# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------
# One-time setup - each client does this once, for their own channel,
# in their own Google Cloud project. Walked through step-by-step on
# the app's "Getting Started" page; summarized here for reference:
#   1. https://console.cloud.google.com/ -> new project
#   2. APIs & Services -> Library -> enable "YouTube Data API v3"
#   3. APIs & Services -> OAuth consent screen -> External
#   4. APIs & Services -> Credentials -> Create credentials -> OAuth
#      client ID -> Desktop app -> download the JSON
#   5. OAuth consent screen -> Audience -> "Publish app". This is the
#      part people skip: leaving the app in "Testing" caps you at 100
#      total users AND expires refresh tokens every 7 days, even for
#      your own account. Publishing (no Google review needed just to
#      click the button - review is only required to remove the
#      "unverified app" warning) fixes both. The warning itself is
#      harmless for a single-user tool on your own channel: click
#      Advanced -> "Go to <app name> (unsafe)" once and move on.
#
# The downloaded JSON is uploaded through the Getting Started page,
# not hand-placed - see app.py / pages/1_Getting_Started.py.
#
# pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib

CLIENT_SECRETS_FILE = APP_DATA_DIR / "client_secret.json"
TOKEN_FILE = APP_DATA_DIR / "token.json"

# youtube.upload covers videos.insert + thumbnails.set only. It does
# NOT cover videos.update or videos.delete - those need the broader
# "https://www.googleapis.com/auth/youtube" (or youtube.force-ssl)
# scope. Left narrow for now (least privilege); widen this - and have
# every client re-run the Getting Started authorization step, since a
# scope change forces a fresh consent - once the dashboard needs
# editing/deleting/playlists.
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def get_credentials() -> Credentials:
    """
    Same shape as start_chromium() in utils.py: reuse a saved,
    already-authenticated token if it's still good, otherwise run the
    one-time OAuth consent flow and save the result for next time.

    *Usage:*
    >>> creds = get_credentials()
    >>> youtube = build("youtube", "v3", credentials=creds)
    """
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    creds = None

    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(
            str(TOKEN_FILE),
            SCOPES,
        )

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CLIENT_SECRETS_FILE.exists():
                raise FileNotFoundError(
                    f"YouTube OAuth credentials not found: {CLIENT_SECRETS_FILE}\n"
                    "Open the app's Getting Started page and upload your "
                    "client_secret.json - see that page for how to create one."
                )

            flow = InstalledAppFlow.from_client_secrets_file(
                str(CLIENT_SECRETS_FILE),
                SCOPES,
            )
            creds = flow.run_local_server(port=0)

        TOKEN_FILE.write_text(creds.to_json())

    return creds


def get_service():
    """*Usage:*
    >>> youtube = get_service()
    """
    return build("youtube", "v3", credentials=get_credentials())


def is_configured() -> bool:
    """
    True once this client has uploaded their client_secret.json AND
    completed the authorization step. Cheap, no network call - just
    checks for the two files get_credentials() would otherwise need.
    Used to gate the dashboard behind the Getting Started page instead
    of surprising a first-time user with a browser consent popup
    mid-upload.

    *Usage:*
    >>> if not is_configured():
    ...     st.warning("Finish setup on the Getting Started page first.")
    """
    return CLIENT_SECRETS_FILE.exists() and TOKEN_FILE.exists()


# --------------------------------------------------------------------------
# Categories (optional - videoCategories.list costs 1 unit)
# --------------------------------------------------------------------------


def list_categories(youtube, region_code: str = "US") -> dict[str, str]:
    """*Usage:*
    >>> youtube = get_service()
    >>> categories = list_categories(youtube)
    >>> categories["People & Blogs"]
    '22'
    """
    response = youtube.videoCategories().list(part="snippet", regionCode=region_code).execute()
    return {item["snippet"]["title"]: item["id"] for item in response["items"]}


# --------------------------------------------------------------------------
# Upload
# --------------------------------------------------------------------------
# IMPORTANT: as of July 28, 2020, videos.insert from an unaudited API
# project is force-set to "private" regardless of the privacyStatus you
# pass. This is a YouTube-side restriction, not something this code can
# work around. To publish public/unlisted videos through the API,
# submit Google's compliance audit form (linked from the videos.insert
# docs) first - until then "private" is the only status that sticks,
# which is also why it's the default below.
#
# Quota cost: Google's own docs currently disagree with each other -
# the videos.insert reference page lists 1,600 units/call, while the
# quota-overview page describes videos.insert as having moved to its
# own 100-calls/day bucket at 1 unit/call. Don't plan capacity around
# either number - check the Quotas page in Cloud Console for your own
# project after enabling the API.

CHUNK_SIZE = 10 * 1024 * 1024  # 10 MiB - must be a multiple of 256 KiB per the resumable upload protocol
MAX_RETRIES = 10
RETRIABLE_STATUS_CODES = {500, 502, 503, 504}

Progress = Callable[..., None]  # sync callback - see run_api_upload() for the async bridge


def upload_video(
    youtube,
    video_path: str | Path,
    title: str,
    description: str = "",
    tags: list[str] | None = None,
    category_id: str = "22",  # People & Blogs - see list_categories() for others
    privacy_status: str = "private",
    progress: Progress | None = None,
) -> str:
    """
    Resumable upload with chunked progress reporting and retry on
    transient errors. Returns the new video's id.

    *Usage:*
    >>> youtube = get_service()
    >>> video_id = upload_video(youtube, "my_video.mp4", title="Test upload")
    >>> print(f"https://youtu.be/{video_id}")
    """
    video_path = Path(video_path)
    if not video_path.exists():
        raise FileNotFoundError(video_path)

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags or [],
            "categoryId": category_id,
        },
        "status": {"privacyStatus": privacy_status},
    }
    media = MediaFileUpload(str(video_path), chunksize=CHUNK_SIZE, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    retries = 0
    while response is None:
        try:
            status, response = request.next_chunk()
            if status and progress:
                pct = int(status.progress() * 100)
                progress(current=pct, message=f"Uploading... {pct}%")
        except HttpError as exc:
            if exc.resp.status not in RETRIABLE_STATUS_CODES:
                raise
            retries += 1
            if retries > MAX_RETRIES:
                raise RuntimeError(f"Upload failed after {MAX_RETRIES} retries") from exc
            sleep_seconds = min(2**retries, 60) + random.random()
            if progress:
                progress(message=f"Retriable error ({exc.resp.status}), retrying in {sleep_seconds:.0f}s...")
            time.sleep(sleep_seconds)

    if progress:
        progress(message=f"Upload complete: {response['id']}")
    return response["id"]


# --------------------------------------------------------------------------
# Async wrapper (for app.py / consistency with yt.py's run_upload)
# --------------------------------------------------------------------------
# googleapiclient has no async variant - next_chunk() is a blocking
# call - so the whole upload runs in a worker thread via
# asyncio.to_thread(). This deliberately doesn't reuse utils.py's
# async_update_state(): its yielded progress() is an async closure,
# awkward to call from inside a plain thread. StateStore.write() is
# already thread-safe (see utils.py) on its own, so this talks to it
# directly instead - a small, intentional duplication of
# async_update_state's RUNNING/SUCCESS/ERROR bookkeeping, in exchange
# for not needing an asyncio bridge for a handful of lines.


def _sync_run_upload(video_path, title, description, tags, category_id, privacy_status, store: StateStore) -> None:
    state = State(task="yt_api_upload", status=RunStatus.RUNNING, total=100)
    store.write(state)

    def progress(**fields) -> None:
        for key, value in fields.items():
            setattr(state, key, value)
        store.write(state)

    try:
        youtube = get_service()
        upload_video(
            youtube,
            video_path,
            title=title,
            description=description,
            tags=tags,
            category_id=category_id,
            privacy_status=privacy_status,
            progress=progress,
        )
    except Exception as exc:
        state.status = RunStatus.ERROR
        state.error = str(exc)
        store.write(state)
        raise
    else:
        state.status = RunStatus.SUCCESS
        store.write(state)


async def run_api_upload(
    video_path: str | Path,
    title: str,
    description: str = "",
    tags: list[str] | None = None,
    category_id: str = "22",
    privacy_status: str = "private",
) -> None:
    """*Usage:*
    >>> asyncio.run(run_api_upload("my_video.mp4", title="Test upload"))
    """
    store = StateStore("yt_api_upload")
    await asyncio.to_thread(
        _sync_run_upload, video_path, title, description, tags, category_id, privacy_status, store
    )


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print("Usage: python api.py <path-to-video> <title>")
        raise SystemExit(1)
    asyncio.run(run_api_upload(sys.argv[1], title=sys.argv[2]))
