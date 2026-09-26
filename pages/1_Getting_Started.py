from __future__ import annotations

import streamlit as st
from googleapiclient.errors import HttpError

from api import CLIENT_SECRETS_FILE, TOKEN_FILE, get_credentials, get_service, is_configured

# --------------------------------------------------------------------------
# Why this page exists
# --------------------------------------------------------------------------
# YouTube uploads happen on YOUR channel, through YOUR Google Cloud
# project - not a shared one this app ships with. That's deliberate:
# a shared project would mean every client's uploads eating from the
# same 100-videos/day quota bucket, and Google requiring an app-review
# process before more than ~100 people could ever authorize it. Each
# client owning their own project sidesteps both problems, at the cost
# of a one-time ~10 minute setup walked through below. Nothing here is
# scriptable further than this - Google only exposes OAuth client
# creation through the Cloud Console UI, not an API.

st.set_page_config(page_title="Getting Started", page_icon="🚀")
st.title("🚀 Getting Started")
st.write("Connect this app to your own YouTube channel. One-time setup, done once per install.")

configured = is_configured()
if configured:
    st.success("Setup complete - this install is authorized.")
else:
    st.info("Not connected yet. Follow the steps below.")

st.divider()

# --------------------------------------------------------------------------
# Step 1 - Google Cloud project + OAuth client
# --------------------------------------------------------------------------

st.subheader("Step 1 - Create your OAuth credentials")
st.markdown(
    """
1. Go to the [Google Cloud Console](https://console.cloud.google.com/) and create a new project
   (top-left project selector -> **New Project**). Any name is fine.
2. **APIs & Services -> Library** -> search **YouTube Data API v3** -> **Enable**.
3. **APIs & Services -> OAuth consent screen** -> User type **External** -> fill in the required
   fields (app name, your email) -> **Save and Continue** through the rest.
4. **APIs & Services -> Credentials -> Create Credentials -> OAuth client ID** -> Application type
   **Desktop app** -> **Create**.
5. Click the download icon next to the new client ID to save the JSON file.
6. Back on **OAuth consent screen -> Audience**, click **Publish app**.
   *Don't skip this* - left in "Testing", Google expires your login every 7 days. Publishing needs
   no review or waiting; it's one click. The first time you sign in afterward, Google shows an
   "unverified app" warning - that's expected for a single-user tool on your own channel. Click
   **Advanced -> Go to (your app name) (unsafe)** to continue.
"""
)

st.divider()

# --------------------------------------------------------------------------
# Step 2 - upload client_secret.json
# --------------------------------------------------------------------------

st.subheader("Step 2 - Upload the credentials file")

if CLIENT_SECRETS_FILE.exists():
    st.success(f"client_secret.json is saved ({CLIENT_SECRETS_FILE}).")
    replace = st.checkbox("Replace it with a different file")
else:
    replace = True

if replace:
    uploaded = st.file_uploader("The JSON file you downloaded in step 1", type=["json"])
    if uploaded is not None:
        CLIENT_SECRETS_FILE.parent.mkdir(parents=True, exist_ok=True)
        CLIENT_SECRETS_FILE.write_bytes(uploaded.getvalue())
        st.success("Saved. Continue to step 3.")
        st.rerun()

st.divider()

# --------------------------------------------------------------------------
# Step 3 - authorize
# --------------------------------------------------------------------------

st.subheader("Step 3 - Authorize with Google")
st.caption(
    "Opens a Google sign-in tab in your browser. Approve access, then come back here - "
    "this page picks it up automatically."
)

if not CLIENT_SECRETS_FILE.exists():
    st.button("Authorize with Google", disabled=True)
    st.caption("Finish step 2 first.")
else:
    if st.button("Authorize with Google", type="primary"):
        with st.spinner("Waiting for you to approve access in your browser..."):
            try:
                get_credentials()
            except Exception as exc:  # noqa: BLE001 - surfacing whatever Google/the flow raised, verbatim
                st.error(f"Authorization failed: {exc}")
            else:
                st.success("Authorized.")
                st.rerun()

if TOKEN_FILE.exists():
    try:
        channel = get_service().channels().list(part="snippet", mine=True).execute()
        items = channel.get("items", [])
        if items:
            st.caption(f"Connected channel: **{items[0]['snippet']['title']}**")
    except HttpError as exc:
        st.caption(f"Connected, but couldn't confirm the channel name ({exc.resp.status}).")

st.divider()

# --------------------------------------------------------------------------
# Optional - Studio browser automation
# --------------------------------------------------------------------------
# Separate from the API path above. Only needed if you want a video
# public/unlisted immediately, without waiting on Google's compliance
# audit (see the Privacy help text on the upload form). Requires a
# real Chromium-based browser installed and `pip install .[browser]`.

with st.expander("Optional: publish public/unlisted videos immediately (no audit wait)"):
    st.markdown(
        """
By default, uploads through the API above land as **private** until this Google Cloud project
passes Google's compliance audit (their process, not a limitation of this app - see the Privacy
dropdown on the main upload form). Two ways around that:

- **Wait it out**: submit the audit form from your Cloud Console project once you're ready to go
  public, and use the API path above for everything.
- **Browser automation**: this app can drive YouTube Studio directly, the same way you would by
  hand, to set visibility on a video already uploaded - not subject to the audit restriction since
  it isn't going through the API. Requires `pip install .[browser]` and a Chrome/Edge/Brave install.

This section will grow into a guided setup once that path is finalized.
"""
    )

st.divider()

# --------------------------------------------------------------------------
# Maintenance
# --------------------------------------------------------------------------

with st.expander("Reset / re-authorize"):
    st.caption("Use this if you've changed Google accounts or need to fix a broken setup.")
    confirm = st.checkbox("I understand this removes the saved credentials from this machine")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Clear saved login", disabled=not confirm):
            TOKEN_FILE.unlink(missing_ok=True)
            st.success("Cleared. Re-run step 3.")
            st.rerun()
    with col2:
        if st.button("Clear login + credentials file", disabled=not confirm):
            TOKEN_FILE.unlink(missing_ok=True)
            CLIENT_SECRETS_FILE.unlink(missing_ok=True)
            st.success("Cleared. Start again from step 2.")
            st.rerun()