from __future__ import annotations

import os
import json
import time
import asyncio
import platform
import subprocess
import urllib.request
import tempfile
import threading
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import (
    AsyncGenerator, 
    Awaitable, 
    Callable, 
    Generator
)
from dataclasses import dataclass, asdict
from contextlib import asynccontextmanager, contextmanager

try:
    from patchright.sync_api import (  # Sync: used for the search-results page only
        sync_playwright,
        BrowserContext,
        Playwright,
        Browser,
        Page,
    )
    from patchright.async_api import (  # Async: used for parallel detail-page tabs
        BrowserContext as AsyncBrowserContext,
        Playwright as AsyncPlaywright,
        Browser as AsyncBrowser,
        Page as AsyncPage,
        async_playwright,
    )
    _HAS_PATCHRIGHT = True
except ImportError:
    # Browser automation (Studio publishing, scraping) is an optional
    # extra - `pip install .[browser]` - not everyone using the pure
    # API path (api.py) needs a real Chromium install + patchright.
    # `from __future__ import annotations` above means every type hint
    # in this file is a lazy string, so BrowserSession/AsyncBrowserSession
    # below can still reference these names without evaluating them.
    _HAS_PATCHRIGHT = False


def _require_patchright() -> None:
    """*Usage:*
    >>> _require_patchright()  # first line of any function that touches Playwright
    """
    if not _HAS_PATCHRIGHT:
        raise ImportError(
            "Browser automation isn't installed. Run `pip install .[browser]` "
            "(or `pip install patchright && patchright install chromium`) to enable it."
        )


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

ProjectConfig = dict[str, dict[str, dict[str, list[str]]]]
BrowserConfig = dict[str, dict[str, list[str]]]

# NOTE: platform.system() actually returns "Windows", "Darwin" (macOS -
# not "Mac"), or "Linux". config.json's browser path tables are keyed
# on these exact strings.
_SYSTEM = platform.system()
CONFIG_FILE = Path(__file__).parent / "config.json"

# Per-client app data: OAuth secrets, cached tokens, run state, staged
# uploads. Deliberately outside the repo folder (never git-tracked,
# never wiped by `git pull`) and, unlike Path(__file__).parent, still
# correct if this ever runs from an installed package rather than a
# checkout.
APP_DATA_DIR = Path.home() / ".yt_automation"

# Dedicated profile(s) so automation never touches your everyday
# browser profile, but logins still persist between runs.
PROFILE_ROOT = Path.home() / ".browser_automation"


def load_config(file_path: Path | str = CONFIG_FILE) -> ProjectConfig:
    """
    Missing or malformed config.json degrades to an empty browser
    table instead of raising, so importing this module - and anything
    that only needs State/StateStore from it, like api.py - never
    fails just because browser automation hasn't been set up.

    *Usage:*
    >>> config = load_config()  # Or
    >>> config = load_config("other_config.json")
    """
    try:
        with open(file_path, "r", encoding="utf-8") as file:
            return json.load(file)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"Warning: could not load {file_path} ({exc}); browser automation will be unavailable.")
        return {"browsers": {}}


def get_browsers(config: ProjectConfig) -> BrowserConfig:
    """*Usage:*
    >>> browsers = get_browsers(load_config())
    >>> browsers["brave"]["Windows"]
    ['C:\\Program Files\\BraveSoftware\\Brave-Browser\\Application\\brave.exe']
    """
    return config.get("browsers", {})


_config: ProjectConfig = load_config(file_path=CONFIG_FILE)
BROWSER_PATHS: BrowserConfig = get_browsers(config=_config)

# NOTE: "firefox" is intentionally excluded here - it's routed to
# start_firefox() in start_browser() below, since Firefox doesn't
# support the same connect_over_cdp() attach path as real Chromium.
CHROMIUM_BROWSERS = {"chrome", "edge", "brave"}

CDP_HOST = "127.0.0.1"
CDP_PORT = 9222
CDP_URL = f"http://{CDP_HOST}:{CDP_PORT}"


# --------------------------------------------------------------------------
# Async context managers (for updating state to front-end)
# --------------------------------------------------------------------------

STATE_DIR = APP_DATA_DIR / "state"


class RunStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"


@dataclass
class State:
    """*Usage:*
    >>> state = State(task="yt_scrape", total=20)
    >>> state.status
    <RunStatus.IDLE: 'idle'>
    """

    task: str
    status: RunStatus = RunStatus.IDLE
    current: int = 0
    total: int = 0
    message: str = ""
    error: str | None = None
    started_at: str | None = None
    updated_at: str | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "State":
        data = dict(data)
        data["status"] = RunStatus(data["status"])
        return cls(**data)


class StateStore:
    """
    Reads/writes a State as JSON, atomically (temp file + os.replace),
    so a Streamlit process polling this file never reads a
    half-written one - no matter whether the automation runs in the
    same process, a background thread, or a separate script.

    *Usage:*
    >>> store = StateStore("yt_scrape")
    >>> store.write(State(task="yt_scrape", status=RunStatus.RUNNING))
    >>> store.read().status
    <RunStatus.RUNNING: 'running'>
    """

    _lock = threading.Lock()

    def __init__(self, task: str, state_dir: Path = STATE_DIR):
        state_dir.mkdir(parents=True, exist_ok=True)
        self.path = state_dir / f"{task}.json"

    def read(self) -> State | None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return State.from_dict(json.load(f))
        except FileNotFoundError:
            return None

    def write(self, state: State) -> None:
        state.updated_at = datetime.now(timezone.utc).isoformat()
        payload = json.dumps(state.to_dict(), indent=2)
        with self._lock:
            fd, tmp_path = tempfile.mkstemp(dir=self.path.parent, prefix=".tmp_")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(payload)
                os.replace(tmp_path, self.path)
            except Exception:
                Path(tmp_path).unlink(missing_ok=True)
                raise


def read_state(task: str, state_dir: Path = STATE_DIR) -> State | None:
    """*Usage:*
    >>> # inside the Streamlit app
    >>> state = read_state("yt_scrape")
    >>> if state:
    ...     st.progress(state.current / max(state.total, 1))
    ...     st.write(state.message)
    """
    return StateStore(task, state_dir).read()


def list_states(state_dir: Path = STATE_DIR) -> list[State]:
    """*Usage:*
    >>> for state in list_states():
    ...     st.write(f"{state.task}: {state.status.value}")
    """
    if not state_dir.exists():
        return []
    return [
        state
        for path in sorted(state_dir.glob("*.json"))
        if (state := StateStore(path.stem, state_dir).read()) is not None
    ]


Progress = Callable[..., Awaitable[None]]


@asynccontextmanager
async def async_update_state(task: str, total: int = 0) -> AsyncGenerator[Progress]:
    """
    Wrap an automation run with this to keep a JSON progress file (read
    by the front-end via read_state()/list_states()) in sync with
    what's happening. Marks the run "running" on entry, "success" on a
    clean exit, "error" (with the exception message) if it raises -
    and always re-raises, it never swallows the exception.

    *Usage:*
    >>> async with async_update_state("yt_scrape", total=len(urls)) as progress:
    ...     for i, url in enumerate(urls, start=1):
    ...         await scrape_url(context, url, semaphore, extractor)
    ...         await progress(current=i, message=f"Scraped {url}")
    """
    store = StateStore(task)
    state = State(
        task=task,
        status=RunStatus.RUNNING,
        total=total,
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    store.write(state)

    async def progress(**fields) -> None:
        # No `await` in this body, so once called it runs to completion
        # without yielding control back to the event loop - concurrent
        # progress() calls from parallel scrape tasks (e.g. from inside
        # scrape_urls_parallel) can't interleave and corrupt `state`.
        for key, value in fields.items():
            setattr(state, key, value)
        store.write(state)

    try:
        yield progress
    except Exception as exc:
        state.status = RunStatus.ERROR
        state.error = str(exc)
        store.write(state)
        raise
    else:
        state.status = RunStatus.SUCCESS
        store.write(state)


# --------------------------------------------------------------------------
# Session handling
# --------------------------------------------------------------------------


@dataclass
class BrowserSession:
    """A live handle on a browser that Playwright is attached to.
    
    *Usage:*
    >>> session = start_browser("brave")
    >>> session.page.goto("https://example.com")
    >>> session.close()
    """

    playwright: Playwright
    browser: Browser | None
    context: BrowserContext
    page: Page
    process: subprocess.Popen | None = None  # None if we attached to a browser we didn't launch

    def close(self, terminate_process: bool = False) -> None:
        """*Usage:*
        >>> session.close()                        # detach, leave the window open
        >>> session.close(terminate_process=True)  # also kill the browser process
        """
        try:
            self.playwright.stop()
        finally:
            if terminate_process and self.process is not None:
                self.process.terminate()


@dataclass
class AsyncBrowserSession:
    """A live handle on a browser that Playwright is attached to (async).
    
    *Usage:*
    >>> async with async_browser_session() as session:
    ...     await session.page.goto("https://example.com")
    """

    playwright: AsyncPlaywright
    browser: AsyncBrowser | None
    context: AsyncBrowserContext
    page: AsyncPage
    process: subprocess.Popen | None = None  # None if we attached to a browser we didn't launch

    async def close(self, terminate_process: bool = False) -> None:
        """*Usage:*
        >>> await session.close()
        >>> await session.close(terminate_process=True)
        """
        try:
            await self.playwright.stop()
        finally:
            if terminate_process and self.process is not None:
                self.process.terminate()  # sync call - Popen.terminate() is not awaitable


# --------------------------------------------------------------------------
# Browser management utilities
# --------------------------------------------------------------------------


def find_browser(browser: str) -> str:
    """
    Candidate paths may contain env vars or `~` (e.g. an
    `%LOCALAPPDATA%\\...` per-user install path) - expanded here so
    config.json can list them literally.

    *Usage:*
    >>> find_browser("brave")
    'C:\\Program Files\\BraveSoftware\\Brave-Browser\\Application\\brave.exe'
    """
    if browser not in BROWSER_PATHS:
        raise FileNotFoundError(
            f"No entry for '{browser}' in config.json - see the Getting Started "
            "page to add one or pick a different browser."
        )
    candidates = BROWSER_PATHS[browser].get(_SYSTEM, [])
    for exe in candidates:
        resolved = Path(os.path.expandvars(exe)).expanduser()
        if resolved.exists():
            return str(resolved)
    raise FileNotFoundError(
        f"Could not find an installed '{browser}' on {_SYSTEM}. "
        f"Checked: {candidates or '(no known paths for this OS)'}"
    )


def cdp_is_up() -> bool:
    """*Usage:*
    >>> if not cdp_is_up():
    ...     wait_for_cdp()
    """
    try:
        with urllib.request.urlopen(f"{CDP_URL}/json/version", timeout=0.5):
            return True
    except Exception:
        return False


def wait_for_cdp(timeout: float = 15.0) -> None:
    """*Usage:*
    >>> wait_for_cdp(timeout=30.0)  # blocks until the CDP port answers, or raises
    """
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None

    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{CDP_URL}/json/version", timeout=0.5):
                return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(0.2)

    raise TimeoutError(f"Browser CDP endpoint never came up at {CDP_URL}") from last_error


def start_chromium(browser: str, headless: bool = False) -> BrowserSession:
    """
    Launch (or attach to) a Chromium-based browser over CDP.

    *Usage:*
    >>> session = start_chromium("brave")
    >>> session.page.goto("https://example.com")
    >>> session.close()
    """
    _require_patchright()
    playwright = sync_playwright().start()
    process = None

    if cdp_is_up():
        # Something is already listening on the debug port (e.g. from a
        # previous run) - attach instead of launching a second instance.
        pw_browser = playwright.chromium.connect_over_cdp(CDP_URL)
    else:
        executable = find_browser(browser)
        profile_dir = PROFILE_ROOT / f"{browser}_profile"
        profile_dir.mkdir(parents=True, exist_ok=True)

        args = [
            executable,
            f"--remote-debugging-address={CDP_HOST}",
            f"--remote-debugging-port={CDP_PORT}",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        args.append("--headless=new" if headless else "--start-maximized")

        process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wait_for_cdp()
        pw_browser = playwright.chromium.connect_over_cdp(CDP_URL)

    context = pw_browser.contexts[0] if pw_browser.contexts else pw_browser.new_context()
    page = context.pages[0] if context.pages else context.new_page()

    return BrowserSession(
        playwright=playwright,
        browser=pw_browser,
        context=context,
        page=page,
        process=process,
    )


def start_firefox(headless: bool = False) -> BrowserSession:
    """
    Drive Firefox through Playwright's native automation, pointed at
    your installed firefox.exe via a persistent profile.

    Caveat: Playwright's Firefox automation is only officially
    supported against Playwright's own bundled build. Pointing it at a
    stock system install via `executable_path` often works but isn't
    guaranteed - if it misbehaves, prefer Chrome/Edge/Brave for DOM
    work (extraction, form-filling).

    *Usage:*
    >>> session = start_firefox()
    >>> session.page.goto("https://example.com")
    >>> session.close()
    """
    _require_patchright()
    playwright = sync_playwright().start()
    executable = find_browser("firefox")
    profile_dir = PROFILE_ROOT / "firefox_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    context = playwright.firefox.launch_persistent_context(
        user_data_dir=str(profile_dir),
        executable_path=executable,
        headless=headless,
    )
    page = context.pages[0] if context.pages else context.new_page()

    return BrowserSession(
        playwright=playwright,
        browser=None,  # not exposed for persistent contexts
        context=context,
        page=page,
        process=None,
    )


def start_browser(browser: str = "brave", headless: bool = False) -> BrowserSession:
    """*Usage:*
    >>> session = start_browser("brave", headless=True)
    >>> session.page.goto("https://example.com")
    >>> session.close()
    """
    if browser in CHROMIUM_BROWSERS:
        return start_chromium(browser, headless=headless)
    if browser == "firefox":
        return start_firefox(headless=headless)
    raise ValueError(f"Unknown browser: {browser!r}")


@contextmanager
def sync_browser_session(browser: str = "brave", headless: bool = False) -> Generator[BrowserSession]:
    """*Usage:*
    >>> with sync_browser_session() as session:
    ...     session.page.goto("https://example.com")
    """
    session = start_browser(browser, headless=headless)
    try:
        yield session
    finally:
        session.close()


# --------------------------------------------------------------------------
# Async session handling (for parallel-tab detail scraping)
# --------------------------------------------------------------------------


@asynccontextmanager
async def async_browser_session(cdp_url: str = CDP_URL) -> AsyncGenerator[AsyncBrowserSession]:
    """
    Attach asynchronously to the same CDP-debugged browser that
    start_browser() already launched/attached to synchronously.
    Requires the browser to already be up - this does not launch
    anything, it piggybacks on the existing CDP port so the same
    logged-in profile/cookies are reused for every tab.

    *Usage:*
    >>> if not cdp_is_up():
    ...     start_browser().close()  # launch it once via the sync path
    >>> async with async_browser_session() as session:
    ...     await session.page.goto("https://example.com")
    """
    _require_patchright()
    if not cdp_is_up():
        await asyncio.to_thread(wait_for_cdp)  # can block up to `timeout`s - keep off the event loop

    playwright = await async_playwright().start()
    browser = await playwright.chromium.connect_over_cdp(cdp_url)
    context = browser.contexts[0] if browser.contexts else await browser.new_context()
    page = context.pages[0] if context.pages else await context.new_page()

    try:
        yield AsyncBrowserSession(playwright=playwright, browser=browser, context=context, page=page)
    finally:
        await playwright.stop()


# --------------------------------------------------------------------------
# Parallel-tab scraping
# --------------------------------------------------------------------------
# TODO: write sync equivalents once yt.py's extraction functions exist.


async def scrape_url(
    context: AsyncBrowserContext,
    url: str,
    semaphore: asyncio.Semaphore,
    extractor: Callable[[AsyncPage, str], Awaitable[dict | None]],
) -> dict | None:
    """
    Opens `url` in its own tab, hands the page to `extractor`, closes
    the tab. Extraction failures are caught and logged rather than
    aborting the whole batch - the caller gets None back for that url.

    *Usage:*
    >>> async def extract_title(page, url):
    ...     return {"url": url, "title": await page.title()}
    >>> result = await scrape_url(context, "https://example.com", asyncio.Semaphore(5), extract_title)
    """
    async with semaphore:
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded")
            return await extractor(page, url)
        except Exception as exc:  # noqa: BLE001
            print(f"Skipping {url!r}: {exc}")
            return None
        finally:
            await page.close()


async def scrape_urls_parallel(
    context: AsyncBrowserContext,
    urls: list[str],
    extractor: Callable[[AsyncPage, str], Awaitable[dict | None]],
    max_concurrent_tabs: int = 15,
) -> list[dict]:
    """
    Visits every url concurrently, each in its own tab, bounded by
    `max_concurrent_tabs`. Returns only the listings that scraped
    successfully.

    *Usage:*
    >>> async def extract_title(page, url):
    ...     return {"url": url, "title": await page.title()}
    >>> async with async_browser_session() as session:
    ...     results = await scrape_urls_parallel(session.context, urls, extract_title)
    """
    semaphore = asyncio.Semaphore(max_concurrent_tabs)
    results = await asyncio.gather(*(scrape_url(context, u, semaphore, extractor) for u in urls))
    scraped = [r for r in results if r is not None]
    print(f"Scraped {len(scraped)} of {len(urls)} pages.")
    return scraped


# --------------------------------------------------------------------------
# Navigation
# --------------------------------------------------------------------------


def navigate(page: Page, url: str, wait_seconds: float = 2.0, wait_until: str = "domcontentloaded") -> None:
    """*Usage:*
    >>> navigate(session.page, "https://example.com")
    """
    print(f"Navigating to {url}...")
    page.goto(url, wait_until=wait_until)
    if wait_seconds > 0:
        time.sleep(wait_seconds)
    print(f"Current URL: {page.url}")


# --------------------------------------------------------------------------
# Saving
# --------------------------------------------------------------------------


def save_json(data: list[dict], path: str | Path) -> None:
    """*Usage:*
    >>> save_json(results, "data/results.json")
    """
    path = Path(path)
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    print(f"Wrote {len(data)} records to {path}")


# --------------------------------------------------------------------------
# Demo
# --------------------------------------------------------------------------


async def demo() -> None:
    if not cdp_is_up():
        # Launches the browser via the sync path. Closing this
        # connection right after does NOT kill the browser - it's a
        # separate OS process, so the CDP port stays up for the async
        # attach below.
        await asyncio.to_thread(lambda: start_browser().close())

    async with async_browser_session() as session:
        page = session.page
        await page.goto("https://example.com")
        print(f"Title: {await page.title()}")
        print(f"URL:   {page.url}")
        print("\nApplication working. Exiting...")


if __name__ == "__main__":
    asyncio.run(demo())