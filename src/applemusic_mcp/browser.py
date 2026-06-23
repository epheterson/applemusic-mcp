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

import queue
import threading
from pathlib import Path
from typing import Any, Callable, Optional

MUSIC_URL = "https://music.apple.com"
BROWSE_URL = f"{MUSIC_URL}/us/browse"
PROFILE_DIR = Path.home() / ".applemusic-mcp" / "chrome"

# Sentinel that tells the owner thread to shut down.
_STOP = object()


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
            self._start_error = BrowserUnavailable(
                "Playwright (a core dependency) failed to import — reinstall the package."
            )
            self._ready.set()
            return

        try:
            self._pw = sync_playwright().start()
            PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            self._ctx = self._launch_context()
            self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        except BaseException as exc:  # noqa: BLE001 - report any launch failure to caller
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
        try:
            ctx = launch(
                user_data_dir,
                channel="chrome",
                headless=False,
                args=args,
            )
            self._using_chrome = True
            return ctx
        except Exception:
            ctx = launch(
                user_data_dir,
                headless=False,
                args=args,
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

    def shutdown(self) -> None:
        if self._thread and self._thread.is_alive():
            self._cmd_q.put(_STOP)


_engine = _BrowserEngine()


# ---------------------------------------------------------------------------
# Page operations (run on the owner thread via _engine.submit)
# ---------------------------------------------------------------------------


def _check_signed_in(page) -> bool:
    """True if the persistent session is authenticated.

    Authoritative signal: a non-empty ``media-user-token`` cookie on
    music.apple.com — this is the credential the web player itself sends as the
    ``Music-User-Token`` header, so its presence == logged in. (Far more reliable
    than scraping a "Sign In" element out of the Ember SPA.) Falls back to
    MusicKit's ``isAuthorized`` if cookies can't be read.
    """
    page.goto(BROWSE_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    try:
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


_MUSICKIT_READY = (
    "() => { try { return !!(window.MusicKit && window.MusicKit.getInstance "
    "&& window.MusicKit.getInstance().developerToken "
    "&& window.MusicKit.getInstance().musicUserToken); } catch (e) { return false; } }"
)

# In-page add: make the SAME request the web player's "Add to Library" button
# fires — POST /v1/me/library?ids[songs]=<id> — using MusicKit's own tokens, from
# inside the authenticated music.apple.com page. No DOM clicking, no token
# exfiltration; the call is indistinguishable from the player's own traffic.
# (Validated out-of-band: this endpoint returns 202 on success — see
# docs/plans/browser-first-architecture.md Appendix A.)
_ADD_JS = """
async (songId) => {
  const mk = window.MusicKit.getInstance();
  const resp = await fetch(
    'https://amp-api.music.apple.com/v1/me/library?ids[songs]=' + encodeURIComponent(songId),
    { method: 'POST',
      headers: { 'Authorization': 'Bearer ' + mk.developerToken,
                 'Music-User-Token': mk.musicUserToken } }
  );
  return resp.status;
}
"""


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
            f"out. Run `python -m applemusic_mcp.browser signin`."
        )
    return False, f"Add returned HTTP {status} for {label}"


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

    save_user_token(token)
    return True


def _cli_signin(timeout_s: int = 600) -> int:
    """Interactive sign-in helper: open the window once, then poll the cookie
    (without navigating) until the user has signed in. The persistent profile
    makes this a one-time step. Run via ``python -m applemusic_mcp.browser signin``."""
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
            print("Not signed in. Run: python -m applemusic_mcp.browser signin", flush=True)
            return 1
    except BrowserUnavailable as exc:
        print(f"Browser engine unavailable:\n{exc}", flush=True)
        return 2
    ok, msg = add_catalog_track_to_library(song_id)
    print(("OK: " if ok else "FAIL: ") + msg, flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "signin":
        raise SystemExit(_cli_signin())
    if cmd == "add" and len(sys.argv) > 2:
        raise SystemExit(_cli_add(sys.argv[2]))
    print("usage: python -m applemusic_mcp.browser {signin | add <song_id>}")
