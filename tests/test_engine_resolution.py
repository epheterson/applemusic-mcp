"""Engine resolution: mode × per-call override × platform → engine.

Covers _playback_engine / _queue_engine / _engine (data) across the five modes,
the legacy `web` alias, per-call `engine=` overrides, and macOS vs non-macOS
(APPLESCRIPT_AVAILABLE as the macOS proxy).
"""

from __future__ import annotations

import pytest

import applemusic_mcp.server as server


@pytest.fixture
def mode(monkeypatch):
    """Set the `mode` pref + platform; return a setter."""

    def _set(mode_value, mac=True):
        monkeypatch.setattr(server, "get_user_preferences", lambda: {"mode": mode_value})
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", mac)

    return _set


# -- plain playback ---------------------------------------------------------


@pytest.mark.parametrize(
    "mode_value,mac,expected",
    [
        ("auto", True, "native"),
        ("auto", False, "chrome"),
        ("native", True, "native"),
        ("native", False, "none"),
        ("safari", True, "safari"),
        ("safari", False, "none"),
        ("chrome", True, "chrome"),
        ("chrome", False, "chrome"),
        ("api", True, "none"),
        ("web", True, "safari"),  # "web" forces the web engine → Safari on macOS
        ("web", False, "chrome"),
        ("browser", True, "chrome"),  # legacy alias for the Chrome web player
    ],
)
def test_playback_engine(mode, mode_value, mac, expected):
    mode(mode_value, mac)
    assert server._playback_engine() == expected


def test_playback_engine_override_wins(mode):
    mode("chrome", mac=True)  # mode says chrome...
    assert server._playback_engine(engine_override="safari") == "safari"  # ...override wins
    assert server._playback_engine(engine_override="native") == "native"


def test_playback_engine_override_safari_off_mac(mode):
    mode("auto", mac=False)
    assert server._playback_engine(engine_override="safari") == "none"  # macOS-only


# -- queue (Up Next) --------------------------------------------------------


@pytest.mark.parametrize(
    "mode_value,mac,expected",
    [
        ("auto", True, "safari"),  # macOS auto queue → Safari (no Chrome needed)
        ("auto", False, "chrome"),
        ("native", True, "none"),  # native Music.app has no exposed Up Next
        ("safari", True, "safari"),
        ("chrome", True, "chrome"),
        ("api", True, "none"),
        ("web", True, "safari"),  # legacy web → auto queue → Safari on mac
    ],
)
def test_queue_engine(mode, mode_value, mac, expected):
    mode(mode_value, mac)
    assert server._queue_engine() == expected


def test_queue_engine_override(mode):
    mode("safari", mac=True)
    assert server._queue_engine(engine_override="chrome") == "chrome"


# -- data engine ------------------------------------------------------------


@pytest.mark.parametrize(
    "mode_value,mac,expected",
    [
        ("auto", True, "native"),
        ("auto", False, "api"),
        ("native", True, "native"),
        ("safari", True, "api"),  # safari is a player; data still via API
        ("chrome", True, "api"),
        ("api", True, "api"),
    ],
)
def test_data_engine(mode, monkeypatch, mode_value, mac, expected):
    monkeypatch.delenv("APPLEMUSIC_FORCE_API_MODE", raising=False)
    mode(mode_value, mac)
    assert server._engine() == expected


# -- module selection + error messaging -------------------------------------


def test_web_player_module():
    from applemusic_mcp import browser, safari_player

    assert server._web_player("safari") is safari_player
    assert server._web_player("chrome") is browser


def test_no_player_msg_native_queue(mode):
    mode("native", mac=True)
    msg = server._no_player_msg(for_queue=True)
    assert "safari" in msg.lower() and "chrome" in msg.lower()


def test_no_player_msg_api(mode):
    mode("api", mac=True)
    assert "API mode" in server._no_player_msg()


# -- permission levels ------------------------------------------------------


@pytest.mark.parametrize(
    "level,stored,expected",
    [
        ("music", "safari", "native"),
        ("music", "chrome", "native"),
        ("music", "api", "native"),
        ("api", "safari", "native"),
        ("web", "safari", "safari"),
    ],
)
def test_stored_mode_above_level_is_clamped(mode, monkeypatch, level, stored, expected):
    mode(stored, mac=True)
    monkeypatch.setenv("APPLEMUSIC_LEVEL", level)
    assert server._playback_engine() == expected


def test_engine_override_cannot_exceed_level(mode, monkeypatch):
    mode("auto", mac=True)
    monkeypatch.setenv("APPLEMUSIC_LEVEL", "api")
    assert server._playback_engine("safari") == "none"
    assert server._playback_engine("chrome") == "none"
    assert server._queue_engine() == "none"


def test_auto_off_mac_below_web_has_no_player(mode, monkeypatch):
    mode("auto", mac=False)
    monkeypatch.setenv("APPLEMUSIC_LEVEL", "api")
    assert server._playback_engine() == "none"


def test_data_engine_native_at_music_even_if_mode_api(mode, monkeypatch):
    mode("api", mac=True)
    monkeypatch.setenv("APPLEMUSIC_LEVEL", "music")
    assert server._engine() == "native"


def test_mode_pinned_native_respects_clamp(mode, monkeypatch):
    mode("native", mac=True)
    monkeypatch.setenv("APPLEMUSIC_LEVEL", "music")
    assert server._mode_pinned_native() is True


def test_no_player_msg_names_level_not_chrome(mode, monkeypatch):
    mode("auto", mac=True)
    monkeypatch.setenv("APPLEMUSIC_LEVEL", "api")
    msg = server._no_player_msg(for_queue=True)
    assert "--level web" in msg and "Chrome" not in msg


def test_web_player_refused_below_web(monkeypatch):
    from applemusic_mcp.capabilities import CapabilityDenied

    monkeypatch.setenv("APPLEMUSIC_LEVEL", "api")
    with pytest.raises(CapabilityDenied):
        server._web_player("safari")


def test_conversational_signin_refused_below_web(monkeypatch):
    monkeypatch.setenv("APPLEMUSIC_LEVEL", "api")
    out = server.config(action="signin")
    assert "--level web" in out and "applemusic-mcp login" in out
