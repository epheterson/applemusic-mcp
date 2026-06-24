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
