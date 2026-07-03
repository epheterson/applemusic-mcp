"""Cross-platform browser engine — drives the music.apple.com web player via a
local, user-signed-in Chrome (Playwright).

It captures the user's ``media-user-token`` (via a one-time sign-in) so the
unified Apple Music API works without a $99 Apple Developer account — the one
credential that genuinely needs a login.

Threading model
---------------
FastMCP runs each ``@mcp.tool()`` in an anyio worker thread, and which thread is
used can vary per call. Playwright's **sync** API (a) refuses to run inside an
asyncio loop and (b) binds its objects to the thread that created them. To
sidestep both, this module owns a single **dedicated thread** that starts
Playwright, launches one persistent Chrome context, and serves commands off a
queue. Tool code calls the module-level helpers, which submit a callable to that
thread and block for the result.

Persistence
-----------
The context uses a dedicated on-disk profile (``~/.applemusic-mcp/chrome/``), so
the user signs into Apple Music **once** and the session survives restarts. It is
a separate Chrome instance from the user's daily browser — isolated cookies, no
hijacking of their main profile.

DRM
---
We prefer the *system* Chrome (``channel="chrome"``) because it ships Widevine,
which Apple Music web playback requires. Playwright's bundled Chromium has no
Widevine; it works for mutations (add/playlist) but not protected playback, so it
is only a fallback.
"""

from __future__ import annotations

import logging
import os
import queue
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

MUSIC_URL = "https://music.apple.com"
BROWSE_URL = f"{MUSIC_URL}/us/browse"
from . import paths
from .musickit_js import (  # shared MusicKit JS (Chrome + Safari engines)
    _ADD_JS,
    _CONTROL_JS,
    _MUSICKIT_READY,
    _NOW_PLAYING_JS,
    _PLAY_QUEUE_JS,
    _PLAY_SONG_JS,
    _QUEUE_AUTOPLAY_JS,
    _QUEUE_CLEAR_JS,
    _QUEUE_JUMP_BY_ID_JS,
    _QUEUE_JUMP_JS,
    _QUEUE_LIST_JS,
    _QUEUE_PLAY_LATER_JS,
    _QUEUE_PLAY_NEXT_JS,
    _QUEUE_REMOVE_JS,
    _QUEUE_SET_JS,
    _SETTINGS_JS,
)

PROFILE_DIR = paths.data_dir() / "chrome"

# Sentinel that tells the owner thread to shut down.
_STOP = object()


def _looks_like_missing_browser(exc: BaseException) -> bool:
    """True if a Playwright launch error means the browser build isn't installed."""
    s = str(exc).lower()
    return (
        "playwright install" in s
        or "executable doesn't exist" in s
        or "looks like playwright was just installed" in s
    )


def _install_playwright_chromium() -> bool:
    """One-time auto-install of Playwright's Chromium build. Returns True on success."""
    import subprocess
    import sys

    try:
        # NEVER print to stdout here: the MCP server speaks JSON-RPC over stdout, and
        # this can run on the engine thread during a tool call. Diagnostics go to stderr.
        print("Installing Playwright's Chromium (one-time, ~150MB)…", file=sys.stderr, flush=True)
        r = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True,
            text=True,
            timeout=600,
        )
        if r.returncode != 0:
            print(
                f"Chromium auto-install failed: {(r.stderr or '')[:500]}",
                file=sys.stderr,
                flush=True,
            )
        return r.returncode == 0
    except Exception as exc:
        print(f"Chromium auto-install error: {exc}", file=sys.stderr, flush=True)
        return False


def _profile_in_use() -> bool:
    """True if a Chrome is already running on our persistent profile (e.g. the MCP
    server holds it). Used so a standalone CLI login fails fast instead of fighting
    the server for the profile lock and tearing down its browser context."""
    import subprocess

    try:
        r = subprocess.run(
            ["pgrep", "-f", f"--user-data-dir={PROFILE_DIR}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return r.returncode == 0 and bool(r.stdout.strip())
    except Exception:
        return False


class BrowserUnavailable(RuntimeError):
    """Raised when the browser engine can't start (Playwright missing, no Chrome,
    launch failure). Callers should fall back or surface a clear 'how to enable'."""


class _BrowserEngine:
    """Owns the Playwright sync instance on a single dedicated thread."""

    def __init__(self) -> None:
        self._cmd_q: "queue.Queue[Any]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._start_error: Optional[BaseException] = None
        self._lock = threading.Lock()
        self._pw = None
        self._ctx = None
        self._page = None
        self._using_chrome = False

    # -- lifecycle -----------------------------------------------------------

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                if self._start_error:
                    raise self._start_error
                return
            self._ready.clear()
            self._start_error = None
            self._thread = threading.Thread(
                target=self._run, name="applemusic-browser", daemon=True
            )
            self._thread.start()
        self._ready.wait()
        if self._start_error:
            raise self._start_error

    def _run(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            import sys as _sys

            if _sys.platform == "darwin":
                msg = (
                    "The Chrome web player needs Playwright, which macOS doesn't install "
                    "by default. Install it with:  pip install 'applemusic-mcp[browser]'  "
                    "— or skip Chrome entirely: sign in with `applemusic-mcp login --safari` "
                    "and use Safari/Music.app for playback."
                )
            else:
                msg = (
                    "The Chrome web player needs Playwright, which should ship with this "
                    "package off macOS. Reinstall it, then run:  pip install playwright  "
                    "&&  playwright install chromium"
                )
            self._start_error = BrowserUnavailable(msg)
            self._ready.set()
            return

        try:
            self._pw = sync_playwright().start()
            PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            # 0700 — this profile holds the signed-in Apple ID session + the
            # media-user-token cookie (the most sensitive store in the project).
            try:
                os.chmod(PROFILE_DIR, 0o700)
            except OSError:
                pass
            self._ctx = self._launch_context()
            self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        except BaseException as exc:  # noqa: BLE001 - report any launch failure to caller
            # Stop the Playwright driver we started, else each failed-launch retry
            # leaks another node/driver process.
            try:
                if self._pw is not None:
                    self._pw.stop()
                    self._pw = None
            except Exception:
                pass
            self._start_error = BrowserUnavailable(f"Could not launch browser: {exc}")
            self._ready.set()
            return

        self._ready.set()
        self._serve()

    def _launch_context(self):
        # Prefer real Chrome (Widevine -> DRM playback). Fall back to bundled
        # Chromium (mutations only) if Chrome isn't installed.
        assert self._pw is not None
        launch = self._pw.chromium.launch_persistent_context
        user_data_dir = str(PROFILE_DIR)
        args = ["--no-first-run", "--no-default-browser-check"]
        # Drop the Playwright automation defaults that would cripple a real
        # user-facing sign-in window. This is a persistent, human-signed-in profile,
        # not a throwaway CI browser, so it should behave like normal Chrome:
        #   --disable-component-update : re-enable it → registers the Widevine CDM,
        #       so MusicKit plays full protected audio instead of 90s previews.
        #   --use-mock-keychain / --password-store=basic : use the REAL macOS
        #       Keychain → Touch ID, passkeys, and saved/iCloud passwords work, so
        #       users sign in with their own credentials instead of being locked out.
        #   --disable-extensions : allow extensions (e.g. iCloud Passwords).
        # (The first Keychain access shows a one-time "Chrome wants to use your
        # keychain" prompt — expected.)
        ignore_defaults = [
            "--disable-component-update",
            "--use-mock-keychain",
            "--password-store=basic",
            "--disable-extensions",
        ]
        # Keep Chrome's sandbox ON by default. Playwright defaults chromium_sandbox=
        # False, which injects --no-sandbox → a scary "Stability and security will
        # suffer" banner and a real security downgrade. A normal desktop session runs
        # sandboxed like real Chrome, so we strip --no-sandbox.
        #
        # EXCEPTION — containers/CI: Chrome can't use its sandbox as root inside a
        # container, so APPLEMUSIC_BROWSER_NO_SANDBOX=1 turns the sandbox back off (and
        # keeps --no-sandbox). Only for headless/containerized testing, never the
        # desktop default.
        sandbox = os.environ.get("APPLEMUSIC_BROWSER_NO_SANDBOX") != "1"
        if sandbox:
            ignore_defaults.append("--no-sandbox")
        else:
            args.append("--no-sandbox")
        try:
            ctx = launch(
                user_data_dir,
                channel="chrome",
                headless=False,
                args=args,
                ignore_default_args=ignore_defaults,
                chromium_sandbox=sandbox,
            )
            self._using_chrome = True
            return ctx
        except Exception as exc:
            # No system Chrome → fall back to Playwright's bundled Chromium, which has
            # NO Widevine, so Apple Music plays 90s previews only. Don't let that
            # downgrade be silent — it's the usual cause of "playback cuts off."
            logger.warning(
                "System Chrome unavailable (%s) — falling back to bundled Chromium, "
                "which can't decode Apple Music DRM (preview-only ~90s). Install Google "
                "Chrome for full playback.",
                exc,
            )
            try:
                ctx = launch(
                    user_data_dir,
                    headless=False,
                    args=args,
                    ignore_default_args=ignore_defaults,
                    chromium_sandbox=sandbox,
                )
                self._using_chrome = False
                return ctx
            except Exception as cx_exc:
                # Bundled Chromium isn't installed (common right after a Playwright
                # version bump). Auto-install it once and retry instead of dumping a
                # raw Playwright stack trace at the user.
                if not _looks_like_missing_browser(cx_exc):
                    raise
                # The browser binary download is ~150 MB and can take minutes. Under the
                # MCP server (stdio, no TTY) that would look like a dead hang with no
                # output, so fail fast with actionable guidance instead — the visible
                # download happens in the terminal during `applemusic-mcp login`.
                if not sys.stdin.isatty():
                    raise BrowserUnavailable(
                        "First-time browser setup needed. Run `applemusic-mcp login` once "
                        "in a terminal — it downloads the browser engine (~150 MB) with "
                        "progress — then retry. (Install Google Chrome too for full-length "
                        "playback.)"
                    ) from cx_exc
                if not _install_playwright_chromium():
                    raise BrowserUnavailable(
                        "Playwright's Chromium isn't installed (and neither system "
                        "Chrome nor auto-install worked). Run:  "
                        "python -m playwright install chromium"
                    ) from cx_exc
                ctx = launch(
                    user_data_dir,
                    headless=False,
                    args=args,
                    ignore_default_args=ignore_defaults,
                    chromium_sandbox=sandbox,
                )
                self._using_chrome = False
                return ctx

    def _serve(self) -> None:
        while True:
            item = self._cmd_q.get()
            if item is _STOP:
                break
            fn, result_q = item
            try:
                result_q.put((True, fn(self._page)))
            except BaseException as exc:  # noqa: BLE001 - marshal exception to caller
                result_q.put((False, exc))
        try:
            if self._ctx:
                self._ctx.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass

    # -- public --------------------------------------------------------------

    def submit(self, fn: Callable[[Any], Any], timeout: float = 120.0) -> Any:
        """Run ``fn(page)`` on the owner thread and return its result (blocking)."""
        self._ensure_thread()
        result_q: "queue.Queue[Any]" = queue.Queue()
        self._cmd_q.put((fn, result_q))
        ok, val = result_q.get(timeout=timeout)
        if ok:
            return val
        raise val

    def shutdown(self, timeout: float = 10.0) -> None:
        """Stop the owner thread and WAIT for it to finish closing Chrome, so a
        caller (e.g. clear_session) can safely delete the profile afterward and a
        subsequent signin starts a clean thread instead of submitting to a dying
        one."""
        with self._lock:
            t = self._thread
            if t and t.is_alive():
                self._cmd_q.put(_STOP)
        if t and t.is_alive():
            t.join(timeout=timeout)


_engine = _BrowserEngine()

# Close the Chrome window + Playwright when the host process exits, so a
# long-running MCP server doesn't leave a stray window holding the profile lock.
import atexit as _atexit

_atexit.register(_engine.shutdown)


def drm_capable() -> bool:
    """True if the launched browser is system Chrome (Widevine present). Playback
    audio is silent on the bundled Chromium fallback. Meaningful only after launch."""
    return bool(_engine._using_chrome)


# ---------------------------------------------------------------------------
# Page operations (run on the owner thread via _engine.submit)
# ---------------------------------------------------------------------------


def _ensure_session_cookie(page) -> None:
    """Make our saved media-user-token the AUTHORITATIVE web-player cookie.

    Playwright's persistent profile does NOT reliably persist session cookies, and an
    ungraceful exit can leave a stale or undecryptable one behind (known upstream bugs:
    playwright #36139 / #35466) — which is what makes the web player look signed-out on
    a later launch. So we don't trust the profile: if we have a saved token (harvested
    from Safari via `login --safari`, or captured from a prior Chrome login), we set it
    as the cookie on every readiness check, REPLACING any stale value. This keeps the
    web player signed in regardless of the profile's state (and means we don't rely on
    Chrome being able to decrypt its own cookie store across launches).

    No-op if nothing is saved (leave whatever the profile has) or the cookie is already
    current. This is also what lets a macOS Safari sign-in drive the Chrome web player
    without a separate Chrome login."""
    try:
        from .auth import get_user_token

        tok = get_user_token()
    except Exception:
        tok = None  # no saved token (FileNotFoundError) or read error
    if not tok:
        return  # nothing saved → leave whatever the profile already has
    try:
        cookies = page.context.cookies("https://music.apple.com")
        current = next(
            (c.get("value") for c in cookies if c.get("name") == "media-user-token"), None
        )
        if current == tok:
            return  # already current — nothing to do
        # add_cookies upserts by (name, domain, path), so this REPLACES a stale value.
        page.context.add_cookies(
            [
                {
                    "name": "media-user-token",
                    "value": tok,
                    "domain": ".apple.com",
                    "path": "/",
                    "secure": True,
                }
            ]
        )
        logger.info("Set the saved media-user-token as the web-player cookie (authoritative).")
    except Exception as exc:
        logger.warning("Couldn't set the saved token cookie: %s", exc)


def _check_signed_in(page) -> bool:
    """True if the persistent session is authenticated.

    Authoritative signal: a non-empty ``media-user-token`` cookie on
    music.apple.com — this is the credential the web player itself sends as the
    ``Music-User-Token`` header, so its presence == logged in. (Far more reliable
    than scraping a "Sign In" element out of the Ember SPA.) Falls back to
    MusicKit's ``isAuthorized`` if cookies can't be read.
    """
    # Bridge a saved (e.g. Safari-harvested) token into the profile first, so a
    # macOS Safari sign-in also authorizes the web player.
    _ensure_session_cookie(page)
    # Try the cookie WITHOUT navigating first — the persistent profile's cookie jar
    # has media-user-token from the prior sign-in, and navigating would clobber any
    # active playback/queue. Only goto as a fallback when the cookie isn't present.
    try:
        cookies = page.context.cookies("https://music.apple.com")
        if any(c.get("name") == "media-user-token" and c.get("value") for c in cookies):
            return True
    except Exception:
        pass
    try:
        page.goto(BROWSE_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        cookies = page.context.cookies("https://music.apple.com")
        if any(c.get("name") == "media-user-token" and c.get("value") for c in cookies):
            return True
    except Exception:
        pass
    try:
        return bool(
            page.evaluate(
                "() => { try { const mk = window.MusicKit "
                "&& window.MusicKit.getInstance && window.MusicKit.getInstance(); "
                "return !!(mk && mk.isAuthorized); } catch (e) { return false; } }"
            )
        )
    except Exception:
        return False


def open_and_check_signin(page=None) -> bool:
    """Launch the browser (if needed), navigate to Apple Music, return signed-in
    state. Bringing the window up is the side effect the user needs in order to
    sign in."""
    return _engine.submit(_check_signed_in)


def signed_in() -> bool:
    """Return whether the persistent browser session is currently signed in."""
    return _engine.submit(_check_signed_in)


# In-page add: make the SAME request the web player's "Add to Library" button
# fires — POST /v1/me/library?ids[songs]=<id> — using MusicKit's own tokens, from
# inside the authenticated music.apple.com page — the same request the web player's
# own "Add" button makes. (Validated out-of-band: this endpoint returns 202 on success.)


def _add_via_api(page, song_id: str) -> int:
    page.goto(BROWSE_URL, wait_until="domcontentloaded")
    # The web app configures MusicKit asynchronously; wait for both tokens.
    page.wait_for_function(_MUSICKIT_READY, timeout=20000)
    return int(page.evaluate(_ADD_JS, str(song_id)))


def add_catalog_track_to_library(song_id: str, name: str = "") -> tuple[bool, str]:
    """Add a catalog track to the user's library via the in-browser API call.

    ``song_id`` is the Apple Music catalog song id (== the iTunes Search
    ``trackId``; the server resolves it tokenlessly). Returns (success, message).
    HTTP 202 (queued) or 200 is success; the library-verify is the source of
    truth for the caller.
    """
    if not str(song_id).strip():
        return False, "Empty song id"
    label = name or f"song {song_id}"
    try:
        status = _engine.submit(lambda page: _add_via_api(page, song_id))
    except BrowserUnavailable as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001 - surface page/JS errors to caller
        return False, f"In-browser add failed for {label}: {exc}"
    if status in (200, 202):
        return True, f"Added {label} to library (HTTP {status})"
    if status in (401, 403):
        return False, (
            f"Not authorized (HTTP {status}) — the browser session may be signed "
            f"out. Run `applemusic-mcp login`."
        )
    return False, f"Add returned HTTP {status} for {label}"


# ---------------------------------------------------------------------------
# Cross-platform playback (branch B: "browser-first" flow)
# ---------------------------------------------------------------------------
#
# Drives the music.apple.com web player's own MusicKit instance, so a catalog
# track actually *plays* (DRM/audio) in the signed-in Chrome window on any OS.
# This is the cross-platform counterpart to macOS AppleScript playback. Requires
# system Chrome (Widevine); Playwright's bundled Chromium can't decode protected
# audio.

# volume 0-100 (-1 = leave); shuffle/repeat null = leave.


def _ensure_player_ready(page) -> None:
    """Make sure the page has a configured MusicKit instance — navigating ONLY if
    needed. Re-navigating reloads the page and destroys the player queue/playback
    state, so once we're on a MusicKit-ready music.apple.com page we reuse it
    (this is what lets pause/skip/seek/volume act on the *currently playing*
    track instead of a freshly reloaded, empty player)."""
    _ensure_session_cookie(page)  # bridge a Safari/saved token so the web player authorizes
    if "music.apple.com" in (page.url or ""):
        try:
            if page.evaluate(_MUSICKIT_READY):
                return
        except Exception:
            pass
    page.goto(BROWSE_URL, wait_until="domcontentloaded")
    # Surface a sign-in problem with a clear, actionable message instead of a
    # cryptic MusicKit timeout (covers a signed-out or expired session).
    if not _read_user_token(page):
        raise BrowserUnavailable("Not signed in to Apple Music — run `applemusic-mcp login`.")
    try:
        page.wait_for_function(_MUSICKIT_READY, timeout=20000)
    except Exception as exc:
        raise BrowserUnavailable(
            "Apple Music web player didn't initialize (session may have expired) — "
            "re-run `applemusic-mcp login`."
        ) from exc


_NO_DRM_NOTE = (
    " (note: preview only, ~90s — this browser lacks DRM/Widevine; "
    "install Google Chrome for full-length playback)"
)


def _play_message(name: str) -> str:
    return f"Playing: {name}" + ("" if drm_capable() else _NO_DRM_NOTE)


def play_catalog_track(catalog_id: str) -> tuple[bool, str]:
    """Play a catalog song in the browser web player (cross-platform, DRM)."""
    if not str(catalog_id).strip():
        return False, "Empty catalog id"

    def run(page):
        _ensure_player_ready(page)
        return page.evaluate(_PLAY_SONG_JS, str(catalog_id))

    try:
        return True, _play_message(_engine.submit(run))
    except BrowserUnavailable as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, f"Browser play failed: {exc}"


def _parse_music_url(url: str) -> Optional[dict]:
    """Map an Apple Music URL to a MusicKit setQueue descriptor.

    song-in-album (``?i=<id>``) -> {song}; /album/ -> {album}; /playlist/ ->
    {playlist}; /song/ -> {song}. Returns None if unrecognized (caller can fall
    back to passing the raw {url}).
    """
    import re
    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    if "i" in qs and qs["i"]:
        return {"song": qs["i"][0]}
    m = re.search(r"/(album|playlist|song)/[^/]+/([^/?#]+)", parsed.path)
    if not m:
        return None
    kind, ident = m.group(1), m.group(2)
    return {"album" if kind == "album" else "playlist" if kind == "playlist" else "song": ident}


def play_descriptor(descriptor: dict, shuffle: bool = False) -> tuple[bool, str]:
    """Play a MusicKit queue descriptor directly — ``{"song": id}``, ``{"album":
    id}``, ``{"playlist": id}``, or ``{"songs": [ids]}``. Used for play-by-name
    (album/playlist) after resolving to an id, and by ``play_url``."""
    payload = dict(descriptor)
    payload["__shuffle"] = bool(shuffle)

    def run(page):
        _ensure_player_ready(page)
        return page.evaluate(_PLAY_QUEUE_JS, payload)

    try:
        return True, _play_message(_engine.submit(run))
    except BrowserUnavailable as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, f"Browser play failed: {exc}"


def play_url(music_url: str, shuffle: bool = False) -> tuple[bool, str]:
    """Play an Apple Music URL (song / album / playlist) in the browser web player
    — cross-platform parity for the native macOS play-from-URL."""
    if not music_url.strip():
        return False, "Empty URL"
    from urllib.parse import urlparse

    host = (urlparse(music_url).hostname or "").lower()
    # Exact host match (parity with reveal_url) — a substring check would pass
    # music.apple.com.evil.tld and navigate the user's window to an attacker page.
    if host != "music.apple.com" and not host.endswith(".music.apple.com"):
        return False, f"Not an Apple Music URL: {music_url}"
    descriptor = _parse_music_url(music_url)
    if descriptor is None:
        return False, f"Unrecognized Apple Music URL shape: {music_url}"
    return play_descriptor(descriptor, shuffle)


def reveal_url(music_url: str) -> tuple[bool, str]:
    """Open an Apple Music URL in the web-player window so the user can see the
    track — the cross-platform counterpart to macOS 'reveal in Music.app'. Does
    not start playback (it just navigates the page)."""
    from urllib.parse import urlparse

    host = (urlparse(music_url or "").hostname or "").lower()
    if host != "music.apple.com" and not host.endswith(".music.apple.com"):
        return False, f"Not an Apple Music URL: {music_url}"

    def run(page):
        page.goto(music_url, wait_until="domcontentloaded")
        return music_url

    try:
        _engine.submit(run)
        return True, f"Showing in browser: {music_url}"
    except BrowserUnavailable as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, f"Browser reveal failed: {exc}"


def playback_control(action: str, seconds: float = 0) -> tuple[bool, str]:
    """play | pause | stop | next | previous | seek on the browser web player."""

    def run(page):
        _ensure_player_ready(page)
        return page.evaluate(_CONTROL_JS, {"action": action, "seconds": seconds})

    try:
        res = _engine.submit(run)
        return (res == "ok"), res
    except Exception as exc:  # noqa: BLE001
        return False, f"Browser control failed: {exc}"


def browser_settings(
    volume: int = -1, shuffle: Optional[bool] = None, repeat: Optional[str] = None
) -> tuple[bool, str]:
    """Set volume (0-100, -1 = leave), shuffle (on/off), repeat
    (none/off/one/all) on the browser web player. Returns the resulting state."""

    def run(page):
        _ensure_player_ready(page)
        return page.evaluate(_SETTINGS_JS, {"volume": volume, "shuffle": shuffle, "repeat": repeat})

    try:
        state = _engine.submit(run)
        return True, (
            f"volume={state['volume']} shuffle={'on' if state['shuffle'] else 'off'} "
            f"repeat={['none', 'one', 'all'][state['repeat']]}"
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"Browser settings failed: {exc}"


def now_playing() -> Optional[dict]:
    """Current web-player track, or None."""
    try:
        return _engine.submit(lambda page: page.evaluate(_NOW_PLAYING_JS))
    except Exception:
        return None


def is_running() -> bool:
    """True if the web player's Chrome thread is already alive — i.e. a peek that
    does NOT launch the browser. Used to report a web/native split only when the
    web engine genuinely has live state, never to spin Chrome up just to check."""
    t = _engine._thread
    return bool(t and t.is_alive())


def now_playing_if_running() -> Optional[dict]:
    """now_playing(), but only if the browser is already running (never launches
    it). Returns None when the web player isn't up."""
    if not is_running():
        return None
    return now_playing()


# --- Up Next / play queue --------------------------------------------------
# The personal playback queue lives in the MusicKit instance (no REST endpoint),
# so these drive the same in-page MusicKit the web player's "Up Next" menu does.
# Method names verified live against MusicKit v3: mk.playNext / mk.playLater /
# mk.changeToMediaAtIndex / mk.clearQueue / mk.autoplayEnabled, mk.queue.remove(i).


def queue_list() -> tuple[bool, Any]:
    """Read the Up Next queue: {position, autoplay, items:[{index,name,artist}]}."""

    def run(page):
        _ensure_player_ready(page)
        return page.evaluate(_QUEUE_LIST_JS)

    try:
        return True, _engine.submit(run)
    except BrowserUnavailable as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, f"Queue read failed: {exc}"


def _queue_insert(catalog_id: str, later: bool) -> tuple[bool, str]:
    js = _QUEUE_PLAY_LATER_JS if later else _QUEUE_PLAY_NEXT_JS
    where = "end of Up Next" if later else "Up Next"

    def run(page):
        _ensure_player_ready(page)
        return page.evaluate(js, str(catalog_id))

    try:
        n = _engine.submit(run)
        return True, f"Queued to {where} ({n} in queue)"
    except BrowserUnavailable as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, f"Queue insert failed: {exc}"


def queue_play_next(catalog_id: str) -> tuple[bool, str]:
    """Insert a catalog song right after the current track (Play Next)."""
    return _queue_insert(catalog_id, later=False)


def queue_play_later(catalog_id: str) -> tuple[bool, str]:
    """Append a catalog song to the end of Up Next (Play Last)."""
    return _queue_insert(catalog_id, later=True)


def queue_set(catalog_ids: list) -> tuple[bool, str]:
    """Replace the entire Up Next with ``catalog_ids`` in order, atomically (one
    MusicKit setQueue call) — instead of clear + N sequential play_later calls."""
    ids = [str(i) for i in catalog_ids]

    def run(page):
        _ensure_player_ready(page)
        return page.evaluate(_QUEUE_SET_JS, ids)

    try:
        n = _engine.submit(run)
        msg = f"Queue set ({n} track(s))"
        # MusicKit silently drops ids it can't play (unplayable / region-locked),
        # so the queue can end up shorter than requested — say so, don't imply all
        # landed.
        if isinstance(n, int) and n < len(ids):
            msg += f" — {len(ids) - n} not queued by the player (unplayable/region-locked)"
        return True, msg
    except BrowserUnavailable as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, f"Queue set failed: {exc}"


def queue_remove(index: int) -> tuple[bool, str]:
    """Remove the Up Next item at ``index`` (0-based)."""

    def run(page):
        _ensure_player_ready(page)
        return page.evaluate(_QUEUE_REMOVE_JS, int(index))

    try:
        n = _engine.submit(run)
        if n == -2:
            return False, (
                "Can't remove the currently-playing item — jump to another track first "
                "(queue jump), then remove it."
            )
        if n < 0:
            return False, f"No queue item at index {index}"
        return True, f"Removed item {index} ({n} left in queue)"
    except BrowserUnavailable as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, f"Queue remove failed: {exc}"


def queue_clear() -> tuple[bool, str]:
    """Clear the entire Up Next queue."""

    def run(page):
        _ensure_player_ready(page)
        return page.evaluate(_QUEUE_CLEAR_JS)

    try:
        _engine.submit(run)
        return True, "Cleared the queue"
    except BrowserUnavailable as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, f"Queue clear failed: {exc}"


def queue_jump(index: int) -> tuple[bool, str]:
    """Jump playback to the Up Next item at ``index`` (0-based)."""

    def run(page):
        _ensure_player_ready(page)
        return page.evaluate(_QUEUE_JUMP_JS, int(index))

    try:
        pos = _engine.submit(run)
        if pos < 0:
            return False, f"No queue item at index {index}"
        return True, f"Jumped to item {pos}"
    except BrowserUnavailable as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, f"Queue jump failed: {exc}"


def queue_jump_id(catalog_id: str) -> tuple[bool, str]:
    """Jump to the Up Next item with the given catalog id — drift-proof: targets the
    track regardless of how the queue auto-advanced since it was listed."""

    def run(page):
        _ensure_player_ready(page)
        return page.evaluate(_QUEUE_JUMP_BY_ID_JS, catalog_id)

    try:
        pos = _engine.submit(run)
        if pos < 0:
            return False, f"No queue item with catalog id {catalog_id}"
        return True, f"Jumped to item {pos}"
    except BrowserUnavailable as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, f"Queue jump failed: {exc}"


def queue_autoplay(enabled: bool) -> tuple[bool, str]:
    """Toggle Autoplay (the ∞ button) — keep playing similar music when the
    queue runs out."""

    def run(page):
        _ensure_player_ready(page)
        return page.evaluate(_QUEUE_AUTOPLAY_JS, bool(enabled))

    try:
        state = _engine.submit(run)
        return True, f"Autoplay {'on' if state else 'off'}"
    except BrowserUnavailable as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, f"Queue autoplay failed: {exc}"


def is_available() -> bool:
    """Best-effort check that the engine can start (Playwright importable)."""
    try:
        import playwright  # noqa: F401

        return True
    except ImportError:
        return False


def _goto_music(page) -> None:
    """Bring the window up on Apple Music (one navigation — used at launch)."""
    page.goto(BROWSE_URL, wait_until="domcontentloaded")


def _read_user_token(page) -> Optional[str]:
    """Return the ``media-user-token`` cookie value from the signed-in context,
    or None if absent. Non-navigating."""
    try:
        for c in page.context.cookies("https://music.apple.com"):
            if c.get("name") == "media-user-token" and c.get("value"):
                return c["value"]
    except Exception:
        return None
    return None


def _cookie_signed_in(page) -> bool:
    """Non-navigating sign-in check: read the ``media-user-token`` cookie from the
    context. Crucially does NOT call goto — polling must not navigate the page out
    from under the user's in-progress Apple ID / 2FA flow."""
    return _read_user_token(page) is not None


def capture_user_token() -> Optional[str]:
    """Read the media-user-token from the signed-in browser profile (no navigation).
    Returns the token string, or None if not signed in."""
    return _engine.submit(_read_user_token)


def capture_and_save_user_token() -> bool:
    """Capture the media-user-token from the browser and persist it via the
    existing ``auth.save_user_token`` so the unified API path can use it. Returns
    True if a token was captured and saved."""
    token = capture_user_token()
    if not token:
        return False
    from .auth import save_user_token

    try:
        save_user_token(token)
    except Exception as exc:
        # Captured but couldn't persist (locked keychain, unwritable config dir).
        # Don't let it vanish into a poll's `except: pass` and time out silently.
        logger.warning("Captured the Apple Music token but failed to save it: %s", exc)
        return False
    return True


def signin_interactive(timeout_s: int = 90) -> tuple[bool, str]:
    """Open a Chrome window on music.apple.com and capture the media-user-token
    once the user has signed in. Returns ``(ok, message)``.

    If the persistent profile is already signed in, captures immediately. Polls
    for up to ``timeout_s`` so an MCP tool call stays bounded; the caller can
    invoke it again to keep waiting (the window stays open)."""
    import time as _time

    try:
        if capture_and_save_user_token():
            return True, "Already signed in — your Apple Music session is captured."
        _engine.submit(_goto_music)  # bring up the window once
    except BrowserUnavailable as exc:
        return False, str(exc)

    deadline = _time.monotonic() + timeout_s
    while _time.monotonic() < deadline:
        _time.sleep(3)
        try:
            if capture_and_save_user_token():
                return True, "Signed in — session captured and saved."
        except Exception:
            pass
    return False, "still-waiting"


def clear_session() -> None:
    """Shut the browser engine down and delete the persistent Chrome profile (the
    signed-in Apple ID session). Lets logout/reset hand the user a clean slate so
    the next sign-in can use a different account."""
    import shutil

    try:
        _engine.shutdown()
    except Exception:
        pass
    try:
        if PROFILE_DIR.exists():
            shutil.rmtree(PROFILE_DIR, ignore_errors=True)
    except Exception:
        pass


def _cli_signin(timeout_s: int = 600) -> int:
    """Interactive sign-in helper: open the window once, then poll the cookie
    (without navigating) until the user has signed in. The persistent profile
    makes this a one-time step. Run via `applemusic-mcp login`."""
    import time

    print(f"Launching Chrome (profile: {PROFILE_DIR}) ...", flush=True)
    try:
        if _engine.submit(_cookie_signed_in):
            saved = capture_and_save_user_token()
            print(
                "Already signed in to Apple Music." + (" Media-user-token saved." if saved else ""),
                flush=True,
            )
            return 0
        _engine.submit(_goto_music)
    except BrowserUnavailable as exc:
        print(f"Browser engine unavailable:\n{exc}", flush=True)
        return 2

    print(
        "A Chrome window is open on music.apple.com.\n"
        "  -> Sign in to Apple Music there (Apple ID + 2FA). I won't touch the page.\n"
        f"Waiting (up to {timeout_s // 60} min) ...",
        flush=True,
    )
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        time.sleep(3)
        try:
            if _engine.submit(_cookie_signed_in):
                saved = capture_and_save_user_token()
                print(
                    "Signed in. Session saved to the persistent profile."
                    + (" Media-user-token captured." if saved else ""),
                    flush=True,
                )
                return 0
        except Exception:
            pass
    print("Timed out waiting for sign-in.", flush=True)
    return 1


def _cli_add(song_id: str) -> int:
    """Test the in-browser add against the signed-in profile:
    ``python -m applemusic_mcp.browser add <catalog_song_id>``."""
    try:
        if not _engine.submit(_cookie_signed_in):
            print("Not signed in. Run: applemusic-mcp login", flush=True)
            return 1
    except BrowserUnavailable as exc:
        print(f"Browser engine unavailable:\n{exc}", flush=True)
        return 2
    ok, msg = add_catalog_track_to_library(song_id)
    print(("OK: " if ok else "FAIL: ") + msg, flush=True)
    return 0 if ok else 1


if (
    __name__ == "__main__"
):  # pragma: no cover - CLI entry: runs only as a script and launches a real Chrome
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "signin":
        raise SystemExit(_cli_signin())
    if cmd == "add" and len(sys.argv) > 2:
        raise SystemExit(_cli_add(sys.argv[2]))
    print("usage: python -m applemusic_mcp.browser {signin | add <song_id>}")
