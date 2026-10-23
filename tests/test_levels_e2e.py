"""Permission levels through the tool entry points: what a user at each level
actually gets back, with the network guard installed as `serve` installs it."""

import runpy
import sys

import pytest
import requests

import applemusic_mcp.server as server
from applemusic_mcp import capabilities, safari_player


@pytest.fixture
def level(monkeypatch, mock_config_dir):
    """Set a level the way `serve` does: env plus the network guard."""

    def _set(name, ui=False, mac=True):
        monkeypatch.setenv("APPLEMUSIC_LEVEL", name)
        if ui:
            monkeypatch.setenv("APPLEMUSIC_ALLOW_UI", "1")
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", mac)
        monkeypatch.setattr(requests.Session, "request", requests.Session.request)
        capabilities.install_network_guard()

    return _set


def _no_applescript(monkeypatch):
    monkeypatch.setattr(server.asc, "run_applescript", lambda *a, **k: pytest.fail("ran AppleScript"))


# -- now_playing / control never touch Safari or Chrome below web ------------


def test_now_playing_at_music_skips_web_peeks(monkeypatch, level):
    level("music")
    monkeypatch.setattr(server, "_playback_now_playing", lambda: "Playing: Strobe")
    monkeypatch.setattr(
        safari_player, "now_playing_if_running", lambda: pytest.fail("peeked Safari")
    )
    monkeypatch.setattr(safari_player, "music_tab_count", lambda: pytest.fail("counted Safari tabs"))
    from applemusic_mcp import browser

    monkeypatch.setattr(browser, "now_playing_if_running", lambda: pytest.fail("peeked Chrome"))
    monkeypatch.setattr(server.asc, "now_playing_if_running", lambda: None)
    out = server.playback(action="now_playing")
    assert "Strobe" in out


# -- catalog search at music + --allow-ui uses Music.app's UI search ---------


def test_catalog_search_at_music_with_ui_uses_ui_search(monkeypatch, level):
    level("music", ui=True)
    # A token saved earlier must not route the search to the (refused) API.
    monkeypatch.setattr(server, "get_headers", lambda: {"Authorization": "Bearer x"})
    monkeypatch.setattr(
        server.asc,
        "ui_search_catalog",
        lambda q: (True, [{"index": 1, "name": "Strobe", "artist": "deadmau5", "type": "Song"}], ""),
    )
    monkeypatch.setattr(server.asc, "ui_clear_search", lambda: None)
    out = server.catalog(action="search", query="Strobe")
    assert "UI Search" in out and "Strobe" in out


# -- playing a non-library song by name ------------------------------------


def _catalog_hit(monkeypatch):
    monkeypatch.setattr(server.asc, "search_library", lambda *a, **k: (True, []))
    monkeypatch.setattr(
        server,
        "_search_catalog_songs",
        lambda *a, **k: [
            {
                "id": "1",
                "attributes": {
                    "name": "Strobe",
                    "artistName": "deadmau5",
                    "url": "https://music.apple.com/us/album/x/1?i=1",
                },
            }
        ],
    )


def test_play_by_name_at_web_falls_back_to_browser(monkeypatch, level):
    level("web")
    _no_applescript(monkeypatch)
    _catalog_hit(monkeypatch)
    monkeypatch.setattr(server, "has_user_token", lambda: True)
    monkeypatch.setattr(server, "_web_player", lambda eng: object())
    monkeypatch.setattr(server, "_browser_play", lambda *a, **k: "Playing Strobe")
    out = server.playback(action="play", track="Strobe", artist="deadmau5")
    assert "Browser" in out and "Playing Strobe" in out


def test_play_by_name_at_api_names_what_to_change(monkeypatch, level):
    level("api")
    _no_applescript(monkeypatch)
    _catalog_hit(monkeypatch)
    monkeypatch.setattr(server, "has_user_token", lambda: True)
    out = server.playback(action="play", track="Strobe", artist="deadmau5")
    assert "--allow-ui" in out and "Falling back" not in out and "Chrome" not in out


def test_play_by_name_at_music_not_in_library_says_level(monkeypatch, level):
    level("music")
    _no_applescript(monkeypatch)
    monkeypatch.setattr(server.asc, "search_library", lambda *a, **k: (True, []))
    out = server.playback(action="play", track="Strobe", artist="deadmau5")
    assert "--level api" in out
    assert "not found in library or catalog" not in out.lower()


def test_catalog_song_search_does_not_swallow_the_level(monkeypatch, level):
    # Reachable from library/playlist add and rate paths: a refusal must not
    # read as "not in the catalog".
    level("music")
    monkeypatch.setattr(server, "get_headers", lambda: {"Authorization": "Bearer x"})
    with pytest.raises(capabilities.CapabilityDenied):
        server._search_catalog_songs("Strobe")
    with pytest.raises(capabilities.CapabilityDenied):
        server._search_catalog_albums("Strobe")


def test_library_add_at_music_says_level_not_login(monkeypatch, level):
    level("music")
    out = server.library(action="add", track="Strobe", artist="deadmau5")
    assert "--level api" in out and "login" not in out


# -- no-player message points at the real fix -------------------------------


def test_no_player_msg_api_mode_at_api_suggests_native(monkeypatch, level):
    level("api")
    monkeypatch.setattr(server, "get_user_preferences", lambda: {"mode": "api"})
    msg = server._no_player_msg()
    assert "--level web" not in msg and "native" in msg
    assert "safari" not in msg.lower() and "chrome" not in msg.lower()


def test_no_player_msg_queue_at_api_names_level(monkeypatch, level):
    level("api")
    monkeypatch.setattr(server, "get_user_preferences", lambda: {"mode": "auto"})
    assert "--level web" in server._no_player_msg(for_queue=True)


# -- python -m applemusic_mcp honors serve --level ---------------------------


def test_module_entry_honors_level(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["applemusic_mcp", "serve", "--level", "apis"])
    monkeypatch.delitem(sys.modules, "applemusic_mcp.__main__", raising=False)
    with pytest.raises(SystemExit) as e:
        runpy.run_module("applemusic_mcp", run_name="__main__")
    assert e.value.code == 2


def test_module_entry_without_args_serves(monkeypatch):
    # README's source install runs `python -m applemusic_mcp` with no arguments.
    ran = {}
    monkeypatch.setattr(sys, "argv", ["applemusic_mcp"])
    monkeypatch.setitem(
        sys.modules, "applemusic_mcp.server", type(sys)("fake_server")
    )
    sys.modules["applemusic_mcp.server"].main = lambda: ran.setdefault("served", True)
    monkeypatch.delitem(sys.modules, "applemusic_mcp.__main__", raising=False)
    runpy.run_module("applemusic_mcp", run_name="__main__")
    assert ran == {"served": True}


def test_active_engine_is_clamped_to_level(monkeypatch, level):
    level("api")
    monkeypatch.setattr(server, "_active_playback_engine", "safari")
    assert server._get_active_playback() == "native"


def test_play_album_at_music_not_in_library_says_level(monkeypatch, level):
    level("music")
    _no_applescript(monkeypatch)
    monkeypatch.setattr(server.asc, "search_library", lambda *a, **k: (True, []))
    monkeypatch.setattr(server, "get_headers", lambda: {"Authorization": "Bearer x"})
    out = server.playback(action="play", album="Random Access Memories", artist="Daft Punk")
    assert "--level api" in out and "Random Access Memories" in out
