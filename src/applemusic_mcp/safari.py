"""Harvest the Apple Music ``media-user-token`` from a signed-in Safari (macOS).

On macOS the heavy Chrome/Playwright stack (~1 GB) exists almost only to capture
this one cookie and to run the web player. But the user is usually already signed
into Apple Music in Safari, and ``media-user-token`` is **not** httpOnly — so we
can read it with one AppleScript ``do JavaScript "document.cookie"`` and skip
Chrome entirely. Paired with the tokenless developer-token harvest
(``auth.harvest_developer_token``) and native Music.app playback, a Mac user needs
no Chrome, no Playwright, and no fresh sign-in.

This is an **opt-in** fast path (``applemusic-mcp login --safari``); Chrome remains
the default onboarding for users who want web playback or aren't on macOS.

Prereq (one-time, user-enabled — it's a security setting, we never flip it for
them): Safari → Settings → Advanced → "Show features for web developers" →
Develop → **Allow JavaScript from Apple Events**.
"""

from __future__ import annotations

import re

from .applescript import run_applescript

MUSIC_URL = "https://music.apple.com"

# Reuse an open music.apple.com tab if there is one (no navigation/side effect);
# otherwise open one, give it a moment to set cookies, then read document.cookie.
_HARVEST_SCRIPT = """
tell application "Safari"
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
        set newDoc to make new document with properties {URL:"https://music.apple.com"}
        delay 4
        set theTab to front document
    end if
    return (do JavaScript "document.cookie" in theTab)
end tell
"""

# Concise reasons only — the CLI (cmd_login) owns the full guidance menu.
_SETTING_OFF_MSG = (
    'Safari blocked reading the session — "Allow JavaScript from Apple Events" is off.'
)

_NOT_SIGNED_IN_MSG = "No Apple Music session found in Safari — sign in at music.apple.com first."


def _parse_media_user_token(cookie_str: str) -> str:
    """Pull media-user-token out of a document.cookie string, or '' if absent."""
    m = re.search(r"(?:^|;\s*)media-user-token=([^;]+)", cookie_str or "")
    return m.group(1).strip() if m else ""


def _looks_like_js_blocked(err: str) -> bool:
    """True if the AppleScript error means 'Allow JavaScript from Apple Events' is off."""
    e = (err or "").lower()
    return "javascript" in e and ("apple events" in e or "not allowed" in e or "turned off" in e)


def media_user_token() -> tuple[bool, str]:
    """Harvest the media-user-token from a signed-in Safari.

    Returns ``(True, token)`` on success, or ``(False, actionable_message)`` —
    distinguishing the two recoverable cases (the JS-from-Apple-Events setting is
    off; Safari isn't signed into Apple Music) so the caller can guide the user or
    fall back to the Chrome flow."""
    ok, out = run_applescript(_HARVEST_SCRIPT)
    if not ok:
        return False, (
            _SETTING_OFF_MSG
            if _looks_like_js_blocked(out)
            else (f"Couldn't read the session from Safari: {out}")
        )
    token = _parse_media_user_token(out)
    if not token:
        return False, _NOT_SIGNED_IN_MSG
    return True, token
