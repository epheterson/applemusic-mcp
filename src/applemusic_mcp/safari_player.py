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

import json
import logging
import platform

from . import safari
from .applescript import run_applescript
from .musickit_js import _CONTROL_JS, _PLAY_SONG_JS, _QUEUE_LIST_JS, _QUEUE_SET_JS

logger = logging.getLogger(__name__)

_NOT_AUTH_MSG = (
    "Not signed into Apple Music in Safari — open music.apple.com in Safari and sign in."
)
_NOT_READY_MSG = (
    "Safari's Apple Music player isn't ready yet — open music.apple.com in Safari first."
)
_TIMEOUT_MSG = "Safari: timed out waiting for the Apple Music player to respond."


def _as_applescript_string(s: str) -> str:
    """Quote one line as an AppleScript string literal (escape \\ and ")."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _build_kick(js_fn: str, arg=None) -> str:
    """Wrap a MusicKit arrow-fn expression in an async IIFE that stores a JSON
    result on window.__amR. Gates on MusicKit being present + authorized so the
    caller gets a clear reason instead of an opaque throw."""
    arg_js = "" if arg is None else json.dumps(arg)
    return (
        "window.__amR='';\n"
        "(async () => {\n"
        "  try {\n"
        "    var mk = (window.MusicKit && MusicKit.getInstance && MusicKit.getInstance());\n"
        "    if (!mk) { window.__amR = JSON.stringify({ok:0,e:'musickit-not-ready'}); return; }\n"
        "    if (!mk.isAuthorized) { window.__amR = JSON.stringify({ok:0,e:'not-authorized'}); return; }\n"
        "    var __f = (" + js_fn.strip() + ");\n"
        "    var __v = await __f(" + arg_js + ");\n"
        "    window.__amR = JSON.stringify({ok:1, v:(__v===undefined?null:__v)});\n"
        "  } catch (e) {\n"
        "    window.__amR = JSON.stringify({ok:0, e:String((e&&e.message)||e)});\n"
        "  }\n"
        "})();"
    )


def _applescript(kick: str, attempts: int, delay: float) -> str:
    """Find (or open) a music.apple.com Safari tab, kick the JS, poll the result."""
    js_expr = " & linefeed & ".join(_as_applescript_string(ln) for ln in kick.split("\n"))
    return f"""tell application "Safari"
    set theTab to missing value
    repeat with w in windows
        repeat with t in tabs of w
            if (URL of t) starts with "https://music.apple.com" then
                set theTab to t
                exit repeat
            end if
        end repeat
        if theTab is not missing value then exit repeat
    end repeat
    if theTab is missing value then
        make new document with properties {{URL:"https://music.apple.com"}}
        delay 5
        set theTab to front document
    end if
    set jsText to {js_expr}
    do JavaScript jsText in theTab
    set theResult to "pending"
    repeat {attempts} times
        delay {delay}
        set r to (do JavaScript "window.__amR" in theTab)
        if r is not missing value and r is not "" then
            set theResult to r
            exit repeat
        end if
    end repeat
    return theResult
end tell"""


def _run_musickit(js_fn: str, arg=None, attempts: int = 24, delay: float = 0.4):
    """Run a MusicKit command in Safari; return (ok, value) or (False, message)."""
    script = _applescript(_build_kick(js_fn, arg), attempts, delay)
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
    ok, v = _run_musickit(_PLAY_SONG_JS, str(catalog_id))
    return (True, f"Playing: {v}") if ok else (False, v)


def queue_set(catalog_ids: list) -> tuple[bool, str]:
    """Replace Up Next with ``catalog_ids`` in order (one MusicKit setQueue)."""
    ids = [str(i) for i in catalog_ids]
    ok, n = _run_musickit(_QUEUE_SET_JS, ids)
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
    return (v == "ok"), v


def is_available() -> bool:
    """True if Safari can be driven (macOS + JS-from-Apple-Events on + signed in)."""
    if platform.system() != "Darwin":
        return False
    ok, _ = _run_musickit("() => true", attempts=6, delay=0.3)
    return ok
