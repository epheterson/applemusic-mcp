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
