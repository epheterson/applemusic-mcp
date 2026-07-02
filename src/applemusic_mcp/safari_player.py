"""Drive Apple Music in a signed-in Safari (macOS) via AppleScript `do JavaScript`.

Safari is already signed into Apple Music for most macOS users, runs the same
MusicKit the Chrome web player does, and decodes Apple Music DRM natively (no
Widevine/Playwright). So on macOS we can play and manage the Up Next queue in the
user's own Safari — no Chrome, no ~500 MB Playwright — using the same
Apple-Events `do JavaScript` channel as the token harvest.

This module mirrors `browser.py`'s public surface so `server.py` can route to
either engine interchangeably.

Transport: `do JavaScript` does NOT await promises and returns immediately, so we
(1) kick an async wrapper that stores its JSON result on `window.__amR`, then
(2) poll `window.__amR` until it's set or we time out — all inside one AppleScript
`repeat`/`delay` loop. The MusicKit commands themselves come from `musickit_js`,
shared verbatim with the Chrome engine.

Prereq (user-enabled, one-time security setting — we never flip it): Safari →
Settings → Advanced → "Show features for web developers" → Develop → "Allow
JavaScript from Apple Events", and be signed into Apple Music at music.apple.com.
"""

from __future__ import annotations

import itertools
import json
import logging
import platform
import threading

from . import safari
from .applescript import run_applescript
from .browser import _parse_music_url  # reuse the URL → MusicKit descriptor parser
from .musickit_js import (
    _CONTROL_JS,
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

logger = logging.getLogger(__name__)

# The kick/poll uses a `window.__amR` sentinel on the page, so two overlapping
# MusicKit ops would stomp each other. Serialize them (no-op for the usual
# sequential stdio request flow; correctness if a client ever calls concurrently).
_LOCK = threading.Lock()

_NOT_AUTH_MSG = (
    "Not signed into Apple Music in Safari — open music.apple.com in Safari and sign in."
)
_NOT_READY_MSG = (
    "Safari's Apple Music player isn't ready yet — open music.apple.com in Safari first."
)
_TIMEOUT_MSG = "Safari: timed out waiting for the Apple Music player to respond."
# WebKit won't start audio until the tab has had one real click ("sticky
# activation"), so a fresh/just-reloaded Safari tab loads the queue but won't play.
_GESTURE_MSG = (
    "Queued and ready — but Safari won't start audio until the tab gets one real "
    "click (a browser autoplay rule, not a bug). Click ▶ once in the Apple Music tab, "
    "then say play again; after that, play / pause / next all work hands-free."
)


def _gesture_blocked(v) -> bool:
    """True if a play result signals audio didn't start (the sticky-activation cliff)."""
    return isinstance(v, str) and "[no audio" in v


def _as_applescript_string(s: str) -> str:
    """Quote one line as an AppleScript string literal (escape \\ and ")."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


# Monotonic per-call id → a unique result sentinel per call, so a slow op that
# already timed out can't poison the NEXT op's poll when its promise lands late.
_COUNTER = itertools.count(1)

# True once MusicKit is constructed on the page (used to wait out a just-opened or
# still-loading tab instead of a blind fixed delay).
_READY_JS = (
    "(function(){try{return !!(window.MusicKit && MusicKit.getInstance "
    "&& MusicKit.getInstance())}catch(e){return false}})()"
)


def _build_kick(js_fn: str, arg, sentinel: str) -> str:
    """Wrap a MusicKit arrow-fn expression in an async IIFE that stores a JSON
    result on window[sentinel]. Gates on MusicKit being present + authorized so the
    caller gets a clear reason instead of an opaque throw."""
    arg_js = "" if arg is None else json.dumps(arg)
    w = f'window["{sentinel}"]'
    return (
        f"{w}='';\n"
        "(async () => {\n"
        "  try {\n"
        "    var mk = (window.MusicKit && MusicKit.getInstance && MusicKit.getInstance());\n"
        f"    if (!mk) {{ {w} = JSON.stringify({{ok:0,e:'musickit-not-ready'}}); return; }}\n"
        f"    if (!mk.isAuthorized) {{ {w} = JSON.stringify({{ok:0,e:'not-authorized'}}); return; }}\n"
        "    var __f = (" + js_fn.strip() + ");\n"
        "    var __v = await __f(" + arg_js + ");\n"
        f"    {w} = JSON.stringify({{ok:1, v:(__v===undefined?null:__v)}});\n"
        "  } catch (e) {\n"
        f"    {w} = JSON.stringify({{ok:0, e:String((e&&e.message)||e)}});\n"
        "  }\n"
        "})();"
    )


# Locate a signed-in music.apple.com tab without disturbing it (no navigation) —
# shared by the kick/poll runner and reveal. Sets `theTab` (or `missing value`).
_FIND_MUSIC_TAB = """    set theTab to missing value
    repeat with w in windows
        repeat with t in tabs of w
            if (URL of t) starts with "https://music.apple.com" then
                set theTab to t
                exit repeat
            end if
        end repeat
        if theTab is not missing value then exit repeat
    end repeat"""


def _applescript(
    kick: str, sentinel: str, ready_attempts: int, poll_attempts: int, delay: float
) -> str:
    """Find (or open) a music.apple.com Safari tab, WAIT for MusicKit to load (no
    blind fixed delay), kick the JS, then poll the per-call sentinel for the result."""
    js_expr = " & linefeed & ".join(_as_applescript_string(ln) for ln in kick.split("\n"))
    return f"""tell application "Safari"
{_FIND_MUSIC_TAB}
    if theTab is missing value then
        make new document with properties {{URL:"https://music.apple.com"}}
        set theTab to front document
    end if
    repeat {ready_attempts} times
        try
            if (do JavaScript "{_READY_JS}" in theTab) is "true" then exit repeat
        end try
        delay {delay}
    end repeat
    set jsText to {js_expr}
    do JavaScript jsText in theTab
    set theResult to "pending"
    repeat {poll_attempts} times
        delay {delay}
        set r to (do JavaScript "window[\\"{sentinel}\\"] || \\"\\"" in theTab)
        if r is not missing value and r is not "" then
            set theResult to r
            exit repeat
        end if
    end repeat
    return theResult
end tell"""


def _run_musickit(
    js_fn: str, arg=None, poll_attempts: int = 30, delay: float = 0.4, ready_attempts: int = 12
):
    """Run a MusicKit command in Safari; return (ok, value) or (False, message).
    ``poll_attempts``×``delay`` bounds the op (bump it for slow ops like queue set)."""
    sentinel = f"__amR_{next(_COUNTER)}"
    script = _applescript(
        _build_kick(js_fn, arg, sentinel), sentinel, ready_attempts, poll_attempts, delay
    )
    with _LOCK:
        ok, out = run_applescript(script)
    if not ok:
        if safari._looks_like_js_blocked(out):
            return False, safari._SETTING_OFF_MSG
        return False, f"Safari player error: {out}"
    out = (out or "").strip()
    if not out or out == "pending":
        return False, _TIMEOUT_MSG
    try:
        data = json.loads(out)
    except Exception:
        return False, f"Safari: unexpected response ({out[:120]})"
    if data.get("ok"):
        return True, data.get("v")
    err = str(data.get("e", ""))
    low = err.lower()
    if err == "not-authorized" or "authoriz" in low:
        return False, _NOT_AUTH_MSG
    if err == "musickit-not-ready" or "not-ready" in low or "not ready" in low:
        return False, _NOT_READY_MSG
    return False, f"Safari player: {err}"


# -- public surface (drop-in parity with browser.py) ------------------------


def play_catalog_track(catalog_id: str) -> tuple[bool, str]:
    """Play a catalog song in Safari's MusicKit (macOS, DRM-native)."""
    if not str(catalog_id).strip():
        return False, "Empty catalog id"
    ok, v = _run_musickit(_PLAY_SONG_JS, str(catalog_id), poll_attempts=60)
    if not ok:
        return False, v
    return (True, _GESTURE_MSG) if _gesture_blocked(v) else (True, f"Playing: {v}")


def queue_set(catalog_ids: list) -> tuple[bool, str]:
    """Replace Up Next with ``catalog_ids`` in order (one MusicKit setQueue)."""
    ids = [str(i) for i in catalog_ids]
    ok, n = _run_musickit(_QUEUE_SET_JS, ids, poll_attempts=60)
    if not ok:
        return False, n
    msg = f"Queue set ({n} track(s))"
    if isinstance(n, int) and n < len(ids):
        msg += f" — {len(ids) - n} not queued by the player (unplayable/region-locked)"
    return True, msg


def queue_list() -> tuple[bool, object]:
    """Read Up Next: {position, autoplay, items:[{index,id,name,artist}]}."""
    return _run_musickit(_QUEUE_LIST_JS)


def playback_control(action: str, seconds: float = 0) -> tuple[bool, str]:
    """play | pause | stop | next | previous | seek in Safari's MusicKit."""
    ok, v = _run_musickit(_CONTROL_JS, {"action": action, "seconds": seconds})
    if not ok:
        return False, v
    if v == "no-audio":  # play() ran but the tab needs one click first
        return True, _GESTURE_MSG
    return (v == "ok"), v


def play_descriptor(descriptor: dict, shuffle: bool = False) -> tuple[bool, str]:
    """Play a MusicKit queue descriptor — {song|album|playlist|songs}."""
    payload = dict(descriptor)
    payload["__shuffle"] = bool(shuffle)
    ok, v = _run_musickit(_PLAY_QUEUE_JS, payload, poll_attempts=60)
    if not ok:
        return False, v
    return (True, _GESTURE_MSG) if _gesture_blocked(v) else (True, f"Playing: {v}")


def play_url(music_url: str, shuffle: bool = False) -> tuple[bool, str]:
    """Play an Apple Music URL (song / album / playlist) in Safari's MusicKit."""
    from urllib.parse import urlparse

    if not (music_url or "").strip():
        return False, "Empty URL"
    host = (urlparse(music_url).hostname or "").lower()
    if host != "music.apple.com" and not host.endswith(".music.apple.com"):
        return False, f"Not an Apple Music URL: {music_url}"
    descriptor = _parse_music_url(music_url)
    if descriptor is None:
        return False, f"Unrecognized Apple Music URL shape: {music_url}"
    return play_descriptor(descriptor, shuffle)


def reveal_url(music_url: str) -> tuple[bool, str]:
    """Open an Apple Music URL in Safari so the user can see it (no playback)."""
    from urllib.parse import urlparse

    host = (urlparse(music_url or "").hostname or "").lower()
    if host != "music.apple.com" and not host.endswith(".music.apple.com"):
        return False, f"Not an Apple Music URL: {music_url}"
    u = _as_applescript_string(music_url)
    # Reuse the consistent music.apple.com tab (don't spawn duplicates) — UNLESS that
    # tab is actively playing, in which case navigating would stop playback, so open a
    # new tab instead.
    playing_js = (
        "(function(){try{var mk=MusicKit.getInstance();"
        "return !!(mk&&mk.isPlaying)}catch(e){return false}})()"
    )
    script = f"""tell application "Safari"
{_FIND_MUSIC_TAB}
    if theTab is missing value then
        make new document with properties {{URL:{u}}}
    else
        set isPlaying to false
        try
            if (do JavaScript "{playing_js}" in theTab) is "true" then set isPlaying to true
        end try
        if isPlaying then
            tell window 1 to set current tab to (make new tab with properties {{URL:{u}}})
        else
            set URL of theTab to {u}
        end if
    end if
    return "ok"
end tell"""
    ok, out = run_applescript(script)
    return (
        (True, f"Showing in Safari: {music_url}") if ok else (False, f"Safari reveal failed: {out}")
    )


def browser_settings(volume: int = -1, shuffle=None, repeat=None) -> tuple[bool, str]:
    """Set volume (0-100, -1 = leave), shuffle, repeat (none/off/one/all); report state."""
    ok, state = _run_musickit(
        _SETTINGS_JS, {"volume": volume, "shuffle": shuffle, "repeat": repeat}
    )
    if not ok:
        return False, state
    return True, (
        f"volume={state['volume']} shuffle={'on' if state['shuffle'] else 'off'} "
        f"repeat={['none', 'one', 'all'][state['repeat']]}"
    )


def now_playing():
    """Current Safari-player track dict, or None."""
    ok, v = _run_musickit(_NOW_PLAYING_JS)
    return v if ok else None


_TAB_COUNT_SCRIPT = """tell application "Safari"
    set n to 0
    repeat with w in windows
        repeat with t in tabs of w
            if (URL of t) starts with "https://music.apple.com" then set n to n + 1
        end repeat
    end repeat
    return n
end tell"""


def music_tab_count() -> int:
    """How many music.apple.com tabs Safari has open (0 if none / not macOS / blocked).
    Never opens one — used for peeking and the multi-tab FYI."""
    if platform.system() != "Darwin":
        return 0
    try:
        ok, out = run_applescript(_TAB_COUNT_SCRIPT)
    except Exception:
        return 0  # a peek probe must never break the caller (e.g. now_playing)
    if not ok:
        return 0
    try:
        return int((out or "0").strip())
    except ValueError:
        return 0


def now_playing_if_running():
    """now_playing(), but only if Safari already has a music.apple.com tab — never
    opens one just to check (parity with browser/native peeks). None otherwise."""
    if music_tab_count() < 1:
        return None
    try:
        return now_playing()
    except Exception:
        return None


def _queue_insert(catalog_id: str, later: bool) -> tuple[bool, str]:
    js = _QUEUE_PLAY_LATER_JS if later else _QUEUE_PLAY_NEXT_JS
    where = "end of Up Next" if later else "Up Next"
    ok, n = _run_musickit(js, str(catalog_id))
    return (True, f"Queued to {where} ({n} in queue)") if ok else (False, n)


def queue_play_next(catalog_id: str) -> tuple[bool, str]:
    """Insert a catalog song right after the current track (Play Next)."""
    return _queue_insert(catalog_id, later=False)


def queue_play_later(catalog_id: str) -> tuple[bool, str]:
    """Append a catalog song to the end of Up Next (Play Last)."""
    return _queue_insert(catalog_id, later=True)


def queue_remove(index: int) -> tuple[bool, str]:
    """Remove the Up Next item at ``index`` (0-based)."""
    ok, n = _run_musickit(_QUEUE_REMOVE_JS, int(index))
    if not ok:
        return False, n
    if n == -2:
        return False, (
            "Can't remove the currently-playing item — jump to another track first "
            "(queue jump), then remove it."
        )
    if n < 0:
        return False, f"No queue item at index {index}"
    return True, f"Removed item {index} ({n} left in queue)"


def queue_jump(index: int) -> tuple[bool, str]:
    """Jump playback to the Up Next item at ``index`` (0-based)."""
    ok, pos = _run_musickit(_QUEUE_JUMP_JS, int(index))
    if not ok:
        return False, pos
    if pos < 0:
        return False, f"No queue item at index {index}"
    return True, f"Jumped to item {pos}"


def queue_jump_id(catalog_id: str) -> tuple[bool, str]:
    """Jump to the Up Next item with the given catalog id (drift-proof)."""
    ok, pos = _run_musickit(_QUEUE_JUMP_BY_ID_JS, str(catalog_id))
    if not ok:
        return False, pos
    if pos < 0:
        return False, f"No queue item with catalog id {catalog_id}"
    return True, f"Jumped to item {pos}"


def queue_clear() -> tuple[bool, str]:
    """Clear the entire Up Next queue."""
    ok, v = _run_musickit(_QUEUE_CLEAR_JS)
    return (True, "Cleared the queue") if ok else (False, v)


def queue_autoplay(enabled: bool) -> tuple[bool, str]:
    """Toggle Autoplay (keep playing similar music when the queue runs out)."""
    ok, state = _run_musickit(_QUEUE_AUTOPLAY_JS, bool(enabled))
    return (True, f"Autoplay {'on' if state else 'off'}") if ok else (False, state)


def is_available() -> bool:
    """True if Safari can be driven (macOS + a usable Apple Music tab). Never opens
    a window as a side effect — if there's no music.apple.com tab to probe, assume
    usable (an actual op will open one and surface any real setting/sign-in error)."""
    if platform.system() != "Darwin":
        return False
    if music_tab_count() < 1:
        return True  # nothing to probe without opening a tab; don't open just to check
    ok, _ = _run_musickit("() => true", poll_attempts=6, ready_attempts=6, delay=0.3)
    return ok
