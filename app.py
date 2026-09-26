from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import streamlit as st

from utils import RunStatus, read_state
from api import run_api_upload

TASK = "yt_api_upload"

# Local to app.py on purpose: staging an in-memory upload to a real
# path Playwright/the API client can read, not a general utils.py
# concern.
UPLOAD_DIR = Path(__file__).parent / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)


def start_upload(video_path: Path, title: str, description: str, privacy_status: str) -> None:
    """
    Runs the upload in a background thread so this script doesn't
    block for its duration. Progress is read back from the state file
    api.py writes via utils.StateStore - see read_state() below.

    *Usage:*
    >>> start_upload(Path("uploads/my_video.mp4"), "Test upload", "", "private")
    """
    threading.Thread(
        target=lambda: asyncio.run(
            run_api_upload(video_path, title=title, description=description, privacy_status=privacy_status)
        ),
        daemon=True,
    ).start()


st.title("YouTube Automation Control Panel")

state = read_state(TASK)
running = state is not None and state.status == RunStatus.RUNNING

st.subheader("Upload a video")
uploaded_file = st.file_uploader(
    "Video file",
    type=["mp4", "mov", "mkv", "webm"],
    disabled=running,
)
title = st.text_input("Title", disabled=running)
description = st.text_area("Description", disabled=running)
privacy_status = st.selectbox(
    "Privacy",
    ["private", "unlisted", "public"],
    disabled=running,
    help=(
        "Uploads from an unaudited API project are forced to private by "
        "YouTube regardless of what's selected here, until you submit "
        "Google's compliance audit."
    ),
)

if st.button("Start upload", disabled=running or uploaded_file is None or not title):
    video_path = UPLOAD_DIR / uploaded_file.name
    video_path.write_bytes(uploaded_file.getvalue())
    start_upload(video_path, title, description, privacy_status)
    st.rerun()

st.divider()
st.subheader("Progress")

if state is None:
    st.info("No run yet.")
else:
    st.write(f"Status: **{state.status.value}**")
    st.progress(state.current / max(state.total, 1))
    st.caption(state.message)
    if state.error:
        st.error(state.error)

# Manual refresh works on any Streamlit version. If you're on >=1.33,
# wrapping the block above in @st.fragment(run_every="2s") gets you
# auto-refresh instead of the button.
if st.button("Refresh"):
    st.rerun()