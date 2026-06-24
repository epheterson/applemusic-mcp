"""Tests for the playback-engine routing (branch B: browser-first playback).

The browser playback primitives themselves drive a live MusicKit instance, so
they're exercised manually; here we cover the pure routing decision and the
_browser_play track/url dispatch with the browser module mocked.
"""

import pytest

from applemusic_mcp import server


@pytest.mark.parametrize(
    "pref,applescript,expected",
    [
        ("auto", True, False),  # macOS + AppleScript -> native
        ("auto", False, True),  # no AppleScript (non-mac) -> browser
        ("native", False, False),  # pinned native, even without AppleScript
        ("browser", True, True),  # pinned browser, even on macOS
    ],
)
def test_use_browser_playback(monkeypatch, pref, applescript, expected):
    monkeypatch.delenv("APPLEMUSIC_FORCE_BROWSER_PLAYBACK", raising=False)
    monkeypatch.setattr(server, "get_user_preferences", lambda: {"playback": pref})
    monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", applescript)
    assert server._use_browser_playback() is expected


def test_force_browser_playback_env(monkeypatch):
    monkeypatch.setenv("APPLEMUSIC_FORCE_BROWSER_PLAYBACK", "1")
    monkeypatch.setattr(server, "get_user_preferences", lambda: {"playback": "native"})
    monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
    assert server._use_browser_playback() is True


def test_browser_play_with_url(monkeypatch):
    import applemusic_mcp.browser as browser

    calls = {}
    monkeypatch.setattr(browser, "play_url", lambda u: calls.update(url=u) or (True, "Playing: X"))
    out = server._browser_play(track="", artist="", url="https://music.apple.com/us/song/x/1")
    assert calls["url"].endswith("/1")
    assert "Playing" in out


def test_browser_play_resolves_track(monkeypatch):
    import applemusic_mcp.browser as browser

    monkeypatch.setattr(
        server,
        "_resolve_catalog_track_itunes",
        lambda t, a="": {"name": t, "artist": a, "url": "https://music.apple.com/us/album/x/9?i=5"},
    )
    seen = {}
    monkeypatch.setattr(browser, "play_url", lambda u: seen.update(url=u) or (True, "Playing: T"))
    out = server._browser_play(track="Strobe", artist="deadmau5")
    assert "i=5" in seen["url"]
    assert "Playing" in out


def test_browser_play_track_not_found(monkeypatch):
    monkeypatch.setattr(server, "_resolve_catalog_track_itunes", lambda t, a="": None)
    out = server._browser_play(track="zzznope", artist="")
    assert "not found" in out.lower()


@pytest.mark.parametrize(
    "mode,applescript,expected",
    [
        ("auto", True, "native"),  # macOS + AppleScript -> native
        ("auto", False, "api"),  # no AppleScript -> api
        ("native", True, "native"),  # pinned native on macOS
        ("native", False, "api"),  # pinned native off-Mac -> falls back to api (no AppleScript)
        ("api", True, "api"),  # pinned api even on macOS
    ],
)
def test_engine_mode(monkeypatch, mode, applescript, expected):
    monkeypatch.delenv("APPLEMUSIC_FORCE_API_MODE", raising=False)
    monkeypatch.setattr(server, "get_user_preferences", lambda: {"mode": mode})
    monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", applescript)
    assert server._engine() == expected


def test_force_api_mode_env(monkeypatch):
    monkeypatch.setenv("APPLEMUSIC_FORCE_API_MODE", "1")
    monkeypatch.setattr(server, "get_user_preferences", lambda: {"mode": "native"})
    monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
    assert server._engine() == "api"


def test_api_mode_implies_browser_playback(monkeypatch):
    monkeypatch.delenv("APPLEMUSIC_FORCE_BROWSER_PLAYBACK", raising=False)
    monkeypatch.setattr(server, "get_user_preferences", lambda: {"mode": "api", "playback": "auto"})
    monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
    assert server._use_browser_playback() is True


def test_playback_tool_registered_unconditionally():
    """Regression: `playback` used to live inside `if APPLESCRIPT_AVAILABLE`, so
    it was missing entirely on Windows/Linux. It must be a module-level tool."""
    assert callable(getattr(server, "playback", None))


def test_playback_play_routes_to_browser_on_non_mac(monkeypatch):
    """Non-mac, browser engine: play dispatches to the browser, never the
    (absent) AppleScript helper."""
    monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
    monkeypatch.setattr(server, "_use_browser_playback", lambda: True)
    monkeypatch.setattr(server, "_browser_play", lambda t, a="", u="": "Playing: X")
    assert server.playback(action="play", track="X") == "Playing: X"


def test_playback_native_pin_on_non_mac_gives_clear_message(monkeypatch):
    """Non-mac with native playback pinned (no browser) returns an actionable
    message instead of a NameError on the missing AppleScript helper."""
    monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
    monkeypatch.setattr(server, "_use_browser_playback", lambda: False)
    out = server.playback(action="play", track="X")
    assert "browser" in out.lower() and "signin" in out.lower()


def test_playback_reveal_in_browser(monkeypatch):
    """reveal now works cross-platform: in browser mode it opens the track's
    page in Chrome instead of being macOS-only."""
    import applemusic_mcp.browser as browser

    monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
    monkeypatch.setattr(server, "_use_browser_playback", lambda: True)
    monkeypatch.setattr(
        server,
        "_resolve_catalog_track_itunes",
        lambda n, a="": {"url": "https://music.apple.com/us/song/x/1"},
    )
    seen = {}
    monkeypatch.setattr(
        browser, "reveal_url", lambda u: seen.update(url=u) or (True, f"Showing in browser: {u}")
    )
    out = server.playback(action="reveal", track="X")
    assert "/song/x/1" in seen["url"]
    assert "browser" in out.lower()
