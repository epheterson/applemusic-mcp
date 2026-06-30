"""Coverage tests for the queue and playback tools (server.py lines ~6859–7955).

Drives _queue_resolve_catalog_id, _format_queue, queue(), playback() dispatcher,
and all nested AppleScript helpers: _catalog_song_name, _convert_song_url_to_album,
_try_ui_catalog_play, _catalog_miss_play, _playback_play, _playback_control,
_playback_now_playing, _playback_settings, _playlist_remove, _library_remove,
_playlist_delete, _playlist_rename, _playback_reveal, _playback_airplay.

UNREACHABLE lines:
  server.py:7955-7957  def main() / mcp.run() — module entrypoint, never called by tests.
  server.py:7960-7961  if __name__ == "__main__": main() — ditto.
"""

import time as time_module
from unittest.mock import MagicMock

import pytest
import responses

from applemusic_mcp import server
from applemusic_mcp.server import InputType, ResolvedInput, ResolvedPlaylist


@pytest.fixture(autouse=True)
def _reset_active_playback():
    """The active-engine hint is a module global; reset it so play/queue tests
    that set it don't leak into later tests that read it."""
    server._active_playback_engine = ""
    yield
    server._active_playback_engine = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _native(monkeypatch):
    """Route playback() to the native (AppleScript) path.

    Also stubs the cross-engine now-playing peek (``asc.now_playing_if_running``
    and ``browser.now_playing_if_running``) to a quiet None default so the
    ``control`` action's now-playing tail and the ``now_playing`` action don't
    fall through to real AppleScript. Tests that need a specific peek value
    override these after calling ``_native`` (monkeypatch is last-wins)."""
    from applemusic_mcp import browser

    monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
    monkeypatch.setattr(server, "_playback_engine", lambda *a, **k: "native")
    monkeypatch.setattr(server, "_get_active_playback", lambda: "native")
    monkeypatch.setattr(server.asc, "now_playing_if_running", lambda: None)
    monkeypatch.setattr(server.asc, "get_current_track", lambda: (True, {"state": "stopped"}))
    monkeypatch.setattr(browser, "now_playing_if_running", lambda: None)


def _mock_headers(monkeypatch):
    monkeypatch.setattr(server, "get_headers", lambda: {"Authorization": "Bearer TEST"})
    monkeypatch.setattr(server, "get_storefront", lambda: "us")


def _resolved_name(value="Strobe", artist="deadmau5"):
    return [ResolvedInput(input_type=InputType.NAME, value=value, artist=artist)]


# ===========================================================================
# Queue tool — remaining uncovered branches
# ===========================================================================


class TestQueueMisc:
    def test_resolve_catalog_id_empty_string_returns_none(self):
        """Line 6864: empty / whitespace-only track -> None."""
        assert server._queue_resolve_catalog_id("") is None
        assert server._queue_resolve_catalog_id("   ") is None

    def test_autoplay_enabled_none_error(self, monkeypatch):
        """Line 6940: autoplay without enabled param."""
        out = server.queue(action="autoplay")
        assert "enabled" in out.lower()

    def test_queue_clear_error_surfaces(self, monkeypatch):
        """Error branch in queue clear — ok=False."""
        from applemusic_mcp import browser

        monkeypatch.setattr(browser, "queue_clear", lambda: (False, "not signed in"))
        out = server.queue(action="clear", engine="chrome")
        assert out.startswith("Error:")

    def test_queue_play_next_error_surfaces(self, monkeypatch):
        """ok=False for play_next."""
        monkeypatch.setattr(server.amp_api, "search_catalog_songs", lambda q, n=1: [{"id": "1"}])
        from applemusic_mcp import browser

        monkeypatch.setattr(browser, "queue_play_next", lambda cid: (False, "browser offline"))
        out = server.queue(action="play_next", track="X", engine="chrome")
        assert out.startswith("Error:")

    def test_queue_play_last_error_surfaces(self, monkeypatch):
        monkeypatch.setattr(server.amp_api, "search_catalog_songs", lambda q, n=1: [{"id": "1"}])
        from applemusic_mcp import browser

        monkeypatch.setattr(browser, "queue_play_later", lambda cid: (False, "offline"))
        out = server.queue(action="play_last", track="X", engine="chrome")
        assert out.startswith("Error:")

    def test_queue_remove_error_surfaces(self, monkeypatch):
        from applemusic_mcp import browser

        monkeypatch.setattr(browser, "queue_remove", lambda i: (False, "remove failed"))
        out = server.queue(action="remove", index=0, engine="chrome")
        assert out.startswith("Error:")

    def test_queue_jump_error_surfaces(self, monkeypatch):
        from applemusic_mcp import browser

        monkeypatch.setattr(browser, "queue_jump", lambda i: (False, "jump failed"))
        out = server.queue(action="jump", index=0, engine="chrome")
        assert out.startswith("Error:")

    def test_queue_autoplay_error_surfaces(self, monkeypatch):
        from applemusic_mcp import browser

        monkeypatch.setattr(browser, "queue_autoplay", lambda e: (False, "autoplay failed"))
        out = server.queue(action="autoplay", enabled=True, engine="chrome")
        assert out.startswith("Error:")


# ===========================================================================
# Playback dispatcher — remaining uncovered branches
# ===========================================================================


class TestPlaybackDispatcher:
    def test_play_native_routes_to_playback_play(self, monkeypatch):
        """Line 7004: native mode routes to _playback_play."""
        _native(monkeypatch)
        monkeypatch.setattr(server, "_playback_play", lambda *a, **k: "native result")
        assert server.playback(action="play", track="X") == "native result"

    def test_engine_override_web_forces_browser(self, monkeypatch):
        """engine='web' uses the browser path even when mode resolves to native."""
        monkeypatch.setattr(server, "_use_browser_playback", lambda: False)
        monkeypatch.setattr(server, "_browser_play", lambda *a, **k: "browser played")
        monkeypatch.setattr(server, "_playback_play", lambda *a, **k: "native played")
        assert server.playback(action="play", track="X", engine="web") == "browser played"

    def test_engine_override_native_forces_native(self, monkeypatch):
        """engine='native' uses Music.app even when mode resolves to web."""
        # Real resolver — engine='native' must win over a web-leaning mode.
        monkeypatch.setattr(server, "get_user_preferences", lambda: {"mode": "chrome"})
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_browser_play", lambda *a, **k: "browser played")
        monkeypatch.setattr(server, "_playback_play", lambda *a, **k: "native played")
        assert server.playback(action="play", track="X", engine="native") == "native played"

    def test_engine_override_invalid_value_errors(self, monkeypatch):
        _native(monkeypatch)
        out = server.playback(action="play", track="X", engine="bogus")
        assert "Error" in out and "engine" in out.lower()

    def test_control_no_control_param_error(self, monkeypatch):
        """Lines 7006-7007: control without control param."""
        _native(monkeypatch)
        out = server.playback(action="control")
        assert "control param" in out.lower() or "Error" in out

    def test_control_browser_success(self, monkeypatch):
        """Lines 7008-7011: control via browser, success."""
        monkeypatch.setattr(server, "_playback_engine", lambda *a, **k: "chrome")
        monkeypatch.setattr(server, "_get_active_playback", lambda: "chrome")
        from applemusic_mcp import browser

        monkeypatch.setattr(browser, "playback_control", lambda c, s: (True, "Paused"))
        # The control tail re-renders now_playing; stub both engines' peeks so it
        # stays offline (web peek + native peek at the dispatcher).
        monkeypatch.setattr(browser, "now_playing", lambda: None)
        monkeypatch.setattr(server.asc, "now_playing_if_running", lambda: None)
        out = server.playback(action="control", control="pause")
        assert "Paused" in out

    def test_control_browser_error(self, monkeypatch):
        """Line 7011: control via browser, error branch."""
        monkeypatch.setattr(server, "_playback_engine", lambda *a, **k: "chrome")
        monkeypatch.setattr(server, "_get_active_playback", lambda: "chrome")
        from applemusic_mcp import browser

        monkeypatch.setattr(browser, "playback_control", lambda c, s: (False, "offline"))
        out = server.playback(action="control", control="next")
        assert out.startswith("Error:")

    def test_control_no_applescript_needs_browser(self, monkeypatch):
        """control with the native engine active but no AppleScript → needs web."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        monkeypatch.setattr(server, "_get_active_playback", lambda: "native")
        out = server.playback(action="control", control="play")
        assert "web" in out.lower() or "login" in out.lower()

    def test_control_native_routes(self, monkeypatch):
        """Line 7015: control in native mode -> _playback_control."""
        _native(monkeypatch)
        monkeypatch.setattr(server, "_playback_control", lambda a, s=0: "Playback: play")
        out = server.playback(action="control", control="play")
        assert "Playback: play" in out

    def test_now_playing_browser_track_found(self, monkeypatch):
        """now_playing primary = the active Chrome engine's track."""
        monkeypatch.setattr(server, "_get_active_playback", lambda: "chrome")
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)  # chrome-only host
        from applemusic_mcp import browser

        monkeypatch.setattr(
            browser,
            "now_playing",
            lambda: {"name": "Strobe", "artist": "deadmau5", "album": "4x4=12"},
        )
        out = server.playback(action="now_playing")
        assert "Strobe" in out and "deadmau5" in out

    def test_now_playing_browser_nothing(self, monkeypatch):
        """Active Chrome engine, nothing playing → 'Nothing playing (Chrome...)'."""
        monkeypatch.setattr(server, "_get_active_playback", lambda: "chrome")
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        from applemusic_mcp import browser

        monkeypatch.setattr(browser, "now_playing", lambda: None)
        out = server.playback(action="now_playing")
        assert "Nothing" in out

    def test_now_playing_surfaces_other_engines(self, monkeypatch):
        """Primary = active engine; OTHER engines also playing are surfaced below it
        with a hint to drive a specific one (replaces the old single-split warning)."""
        monkeypatch.setattr(server, "_get_active_playback", lambda: "chrome")
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        from applemusic_mcp import browser, safari_player

        # active engine primary (direct read)
        monkeypatch.setattr(
            browser,
            "now_playing",
            lambda: {"name": "Now's The Time", "artist": "Charlie Parker", "album": "X"},
        )
        # other engine peek (no-launch)
        monkeypatch.setattr(
            server.asc,
            "now_playing_if_running",
            lambda: {"name": "Jeanie", "artist": "Foster", "state": "playing"},
        )
        monkeypatch.setattr(safari_player, "now_playing_if_running", lambda: None)
        monkeypatch.setattr(safari_player, "music_tab_count", lambda: 0)
        out = server.playback(action="now_playing")
        assert "Now's The Time" in out  # primary (active chrome)
        assert "Jeanie" in out and "also on Music.app" in out  # other engine surfaced
        assert "engine=" in out  # how to drive a specific one

    def test_now_playing_lists_paused_other_engine(self, monkeypatch):
        """A loaded-but-paused OTHER engine is still surfaced (with its state)."""
        monkeypatch.setattr(server, "_get_active_playback", lambda: "chrome")
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        from applemusic_mcp import browser, safari_player

        monkeypatch.setattr(
            browser, "now_playing", lambda: {"name": "Now's The Time", "artist": "Bird"}
        )
        monkeypatch.setattr(
            server.asc,
            "now_playing_if_running",
            lambda: {"name": "Top Back", "artist": "TI", "state": "paused"},
        )
        monkeypatch.setattr(safari_player, "now_playing_if_running", lambda: None)
        monkeypatch.setattr(safari_player, "music_tab_count", lambda: 0)
        out = server.playback(action="now_playing")
        assert "Top Back" in out and "paused" in out.lower()

    def test_now_playing_multi_tab_fyi(self, monkeypatch):
        """Multiple Safari Apple Music tabs → an FYI note (a consistent one is driven)."""
        monkeypatch.setattr(server, "_get_active_playback", lambda: "safari")
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        from applemusic_mcp import browser, safari_player

        monkeypatch.setattr(
            safari_player, "now_playing", lambda: {"name": "Strobe", "artist": "deadmau5"}
        )
        monkeypatch.setattr(server.asc, "now_playing_if_running", lambda: None)
        monkeypatch.setattr(browser, "now_playing_if_running", lambda: None)
        monkeypatch.setattr(safari_player, "music_tab_count", lambda: 3)
        out = server.playback(action="now_playing")
        assert "Strobe" in out and "3 Apple Music tabs" in out

    def test_now_playing_no_applescript(self, monkeypatch):
        """Off-mac, active Chrome engine, nothing playing → 'Nothing playing (Chrome...)'."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        from applemusic_mcp import browser

        monkeypatch.setattr(browser, "now_playing", lambda: None)
        out = server.playback(action="now_playing")
        assert "Chrome" in out and "Nothing" in out

    def test_now_playing_native(self, monkeypatch):
        """now_playing primary = native Music.app, with its rich detail."""
        _native(monkeypatch)
        monkeypatch.setattr(
            server.asc,
            "get_current_track",
            lambda: (True, {"state": "playing", "name": "X", "artist": "Y"}),
        )
        from applemusic_mcp import browser, safari_player

        monkeypatch.setattr(browser, "now_playing_if_running", lambda: None)
        monkeypatch.setattr(safari_player, "now_playing_if_running", lambda: None)
        monkeypatch.setattr(safari_player, "music_tab_count", lambda: 0)
        out = server.playback(action="now_playing")
        assert "X" in out and "playing" in out.lower()

    def test_settings_browser_success(self, monkeypatch):
        """Lines 7028-7036: settings via browser."""
        monkeypatch.setattr(server, "_playback_engine", lambda *a, **k: "chrome")
        monkeypatch.setattr(server, "_get_active_playback", lambda: "chrome")
        from applemusic_mcp import browser

        monkeypatch.setattr(browser, "browser_settings", lambda v, s, r: (True, "Volume set to 80"))
        out = server.playback(action="settings", volume=80)
        assert "Volume" in out

    def test_settings_browser_shuffle_none(self, monkeypatch):
        """Line 7031: shuffle_mode empty -> shuffle_b=None passed to browser."""
        monkeypatch.setattr(server, "_playback_engine", lambda *a, **k: "chrome")
        monkeypatch.setattr(server, "_get_active_playback", lambda: "chrome")
        from applemusic_mcp import browser

        seen = {}
        monkeypatch.setattr(
            browser, "browser_settings", lambda v, s, r: seen.update(s=s) or (True, "OK")
        )
        server.playback(action="settings")  # no shuffle_mode
        assert seen.get("s") is None

    def test_settings_browser_shuffle_on(self, monkeypatch):
        """Line 7031-7033: shuffle_mode='on' -> True."""
        monkeypatch.setattr(server, "_playback_engine", lambda *a, **k: "chrome")
        monkeypatch.setattr(server, "_get_active_playback", lambda: "chrome")
        from applemusic_mcp import browser

        seen = {}
        monkeypatch.setattr(
            browser, "browser_settings", lambda v, s, r: seen.update(s=s) or (True, "OK")
        )
        server.playback(action="settings", shuffle_mode="on")
        assert seen.get("s") is True

    def test_settings_browser_error(self, monkeypatch):
        """Line 7036: settings browser returns error."""
        monkeypatch.setattr(server, "_playback_engine", lambda *a, **k: "chrome")
        monkeypatch.setattr(server, "_get_active_playback", lambda: "chrome")
        from applemusic_mcp import browser

        monkeypatch.setattr(browser, "browser_settings", lambda v, s, r: (False, "oops"))
        out = server.playback(action="settings", volume=50)
        assert out.startswith("Error:")

    def test_settings_no_applescript(self, monkeypatch):
        """settings with the native engine active but no AppleScript → needs web."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        monkeypatch.setattr(server, "_get_active_playback", lambda: "native")
        out = server.playback(action="settings", volume=50)
        assert "web" in out.lower()

    def test_settings_native(self, monkeypatch):
        """Line 7039: settings in native mode."""
        _native(monkeypatch)
        monkeypatch.setattr(server, "_playback_settings", lambda v, s, r: "Volume: 70")
        out = server.playback(action="settings", volume=70)
        assert "Volume: 70" in out

    def test_reveal_no_name_no_url_error(self, monkeypatch):
        """Line 7043: reveal without track name or url."""
        _native(monkeypatch)
        out = server.playback(action="reveal")
        assert "required" in out.lower()

    def test_reveal_browser_resolve_fails(self, monkeypatch):
        """Line 7051: reveal via browser, catalog resolve returns None."""
        monkeypatch.setattr(server, "_playback_engine", lambda *a, **k: "chrome")
        monkeypatch.setattr(server, "_get_active_playback", lambda: "chrome")
        monkeypatch.setattr(server, "_resolve_catalog_track_itunes", lambda n, a="": None)
        out = server.playback(action="reveal", track="zzznope")
        assert "not found" in out.lower()

    def test_reveal_browser_url_ok(self, monkeypatch):
        """Lines 7044-7054: reveal via browser with a resolved URL."""
        monkeypatch.setattr(server, "_playback_engine", lambda *a, **k: "chrome")
        monkeypatch.setattr(server, "_get_active_playback", lambda: "chrome")
        from applemusic_mcp import browser as br

        monkeypatch.setattr(
            server,
            "_resolve_catalog_track_itunes",
            lambda n, a="": {"url": "https://music.apple.com/us/song/x/1"},
        )
        monkeypatch.setattr(br, "reveal_url", lambda u: (True, f"Showing: {u}"))
        out = server.playback(action="reveal", track="Strobe")
        assert "Showing" in out

    def test_reveal_browser_url_error(self, monkeypatch):
        """Line 7054: reveal_url returns error."""
        monkeypatch.setattr(server, "_playback_engine", lambda *a, **k: "chrome")
        monkeypatch.setattr(server, "_get_active_playback", lambda: "chrome")
        from applemusic_mcp import browser as br

        monkeypatch.setattr(
            server,
            "_resolve_catalog_track_itunes",
            lambda n, a="": {"url": "https://music.apple.com/us/song/x/1"},
        )
        monkeypatch.setattr(br, "reveal_url", lambda u: (False, "no browser"))
        out = server.playback(action="reveal", track="Strobe")
        assert out.startswith("Error:")

    def test_reveal_native_routes(self, monkeypatch):
        """Lines 7055-7057: reveal in native macOS mode."""
        _native(monkeypatch)
        monkeypatch.setattr(server, "_playback_reveal", lambda n, a="": f"Revealed: {n}")
        out = server.playback(action="reveal", track="Strobe")
        assert "Revealed: Strobe" in out

    def test_reveal_native_with_url(self, monkeypatch):
        """Lines 7041-7057: reveal with url provided."""
        _native(monkeypatch)
        monkeypatch.setattr(server, "_playback_reveal", lambda n, a="": f"Revealed: {n}")
        out = server.playback(action="reveal", url="https://music.apple.com/...", track="")
        # url sets name="" but url is truthy -> _macos_only passes -> _playback_reveal called
        # Actually: name = track_name or track = "" — and url is truthy, so no error
        assert "Revealed" in out

    def test_airplay_native_routes(self, monkeypatch):
        """Lines 7058-7061: airplay in native mode."""
        _native(monkeypatch)
        monkeypatch.setattr(server, "_playback_airplay", lambda d="": "AirPlay devices (1): ...")
        out = server.playback(action="airplay")
        assert "AirPlay" in out

    def test_unknown_action_error(self, monkeypatch):
        """Lines 7062-7065: unknown action string."""
        _native(monkeypatch)
        out = server.playback(action="foobar_xyz")
        assert "Unknown" in out


# ===========================================================================
# _catalog_song_name  (lines 7080-7096)
# ===========================================================================


class TestCatalogSongName:
    @responses.activate
    def test_success(self, monkeypatch):
        _mock_headers(monkeypatch)
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/songs/12345",
            json={"data": [{"attributes": {"name": "Strobe"}}]},
            status=200,
        )
        assert server._catalog_song_name("12345") == "Strobe"

    @responses.activate
    def test_non_200_returns_empty(self, monkeypatch):
        _mock_headers(monkeypatch)
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/songs/99999",
            status=404,
        )
        assert server._catalog_song_name("99999") == ""

    @responses.activate
    def test_empty_data_returns_empty(self, monkeypatch):
        _mock_headers(monkeypatch)
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/songs/11111",
            json={"data": []},
            status=200,
        )
        assert server._catalog_song_name("11111") == ""

    @responses.activate
    def test_no_name_attr_returns_empty(self, monkeypatch):
        _mock_headers(monkeypatch)
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/songs/22222",
            json={"data": [{"attributes": {}}]},
            status=200,
        )
        assert server._catalog_song_name("22222") == ""

    def test_exception_returns_empty(self, monkeypatch):
        def _boom():
            raise ValueError("no token")

        monkeypatch.setattr(server, "get_headers", _boom)
        assert server._catalog_song_name("12345") == ""


# ===========================================================================
# _convert_song_url_to_album  (lines 7098-7136)
# ===========================================================================


class TestConvertSongUrlToAlbum:
    def test_no_song_id_in_url_returns_none(self):
        assert server._convert_song_url_to_album("https://music.apple.com/us/album/x/123") is None

    @responses.activate
    def test_success(self, monkeypatch):
        _mock_headers(monkeypatch)
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/songs/99999",
            json={
                "data": [
                    {
                        "attributes": {"albumName": "4x4=12"},
                        "relationships": {"albums": {"data": [{"id": "888888"}]}},
                    }
                ]
            },
            status=200,
        )
        result = server._convert_song_url_to_album("https://music.apple.com/us/song/strobe/99999")
        assert result is not None
        assert "888888" in result
        assert "?i=99999" in result

    @responses.activate
    def test_non_200_returns_none(self, monkeypatch):
        _mock_headers(monkeypatch)
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/songs/77777",
            status=401,
        )
        assert server._convert_song_url_to_album("https://music.apple.com/us/song/x/77777") is None

    @responses.activate
    def test_empty_data_returns_none(self, monkeypatch):
        _mock_headers(monkeypatch)
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/songs/55555",
            json={"data": []},
            status=200,
        )
        assert server._convert_song_url_to_album("https://music.apple.com/us/song/x/55555") is None

    @responses.activate
    def test_no_albums_relationship_returns_none(self, monkeypatch):
        _mock_headers(monkeypatch)
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/songs/44444",
            json={
                "data": [
                    {
                        "attributes": {"albumName": "X"},
                        "relationships": {"albums": {"data": []}},
                    }
                ]
            },
            status=200,
        )
        assert server._convert_song_url_to_album("https://music.apple.com/us/song/x/44444") is None

    def test_exception_returns_none(self, monkeypatch):
        monkeypatch.setattr(server, "get_headers", lambda: (_ for _ in ()).throw(Exception("fail")))
        assert server._convert_song_url_to_album("https://music.apple.com/us/song/x/33333") is None


# ===========================================================================
# _try_ui_catalog_play  (lines 7138-7167)
# ===========================================================================


class TestTryUiCatalogPlay:
    def test_applescript_unavailable_returns_false_none(self, monkeypatch):
        """Lines 7157-7158: APPLESCRIPT_AVAILABLE=False."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        ok, msg = server._try_ui_catalog_play("Song", "Artist")
        assert ok is False
        assert msg is None

    def test_success(self, monkeypatch):
        """Lines 7159-7166: UI play succeeds."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server.asc, "ui_play_result_by_query", lambda q: (True, "Playing Song"))
        ok, msg = server._try_ui_catalog_play("Song", "Artist")
        assert ok is True
        assert "Playing Song" in msg

    def test_custom_prefix(self, monkeypatch):
        """Custom prefix and source_label."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server.asc, "ui_play_result_by_query", lambda q: (True, "Playing"))
        ok, msg = server._try_ui_catalog_play(
            "Song", "Artist", source_label="ui_search", prefix="[UI Search]"
        )
        assert ok is True
        assert "[UI Search]" in msg

    def test_failure_returns_error_msg(self, monkeypatch):
        """Line 7167: UI play fails."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server.asc, "ui_play_result_by_query", lambda q: (False, "UI error"))
        ok, msg = server._try_ui_catalog_play("Song", "Artist")
        assert ok is False
        assert msg == "UI error"


# ===========================================================================
# _catalog_miss_play  (lines 7169-7212)
# ===========================================================================


class TestCatalogMissPlay:
    def test_reveal_with_url_success(self, monkeypatch):
        """Lines 7181-7183: reveal=True + url -> open_catalog_song."""
        monkeypatch.setattr(server.asc, "open_catalog_song", lambda url: (True, "Opened"))
        out = server._catalog_miss_play(
            "Strobe", "deadmau5", "https://music.apple.com/...", reveal=True
        )
        assert "click play" in out or "Opened" in out

    def test_reveal_with_url_open_fails(self, monkeypatch):
        """reveal=True but open_catalog_song fails -> falls through to open_and_play."""
        monkeypatch.setattr(server.asc, "open_catalog_song", lambda url: (False, "fail"))
        monkeypatch.setattr(
            server.asc, "open_catalog_and_play", lambda url, track_name="": (True, "Playing!")
        )
        monkeypatch.setattr(server, "get_user_preferences", lambda: {"playback": "auto"})
        out = server._catalog_miss_play(
            "Strobe", "deadmau5", "https://music.apple.com/...", reveal=True
        )
        assert "[Catalog]" in out

    def test_no_url_returns_found_message(self):
        """Line 7185: no url -> found message without playing."""
        out = server._catalog_miss_play("Strobe", "deadmau5", "", reveal=False)
        assert "Strobe" in out and "deadmau5" in out

    def test_open_and_play_success(self, monkeypatch):
        """Lines 7187-7192: asc.open_catalog_and_play succeeds."""
        monkeypatch.setattr(
            server.asc, "open_catalog_and_play", lambda url, track_name="": (True, "Playing!")
        )
        out = server._catalog_miss_play(
            "Strobe", "deadmau5", "https://music.apple.com/...", reveal=False
        )
        assert "[Catalog]" in out and "Playing!" in out

    def test_native_fails_browser_fallback(self, monkeypatch):
        """Lines 7194-7201: native play fails, auto pref -> browser fallback."""
        monkeypatch.setattr(
            server.asc, "open_catalog_and_play", lambda url, track_name="": (False, "UI fail")
        )
        monkeypatch.setattr(server, "get_user_preferences", lambda: {"playback": "auto"})
        monkeypatch.setattr(server, "has_user_token", lambda: True)
        monkeypatch.setattr(server, "_browser_play", lambda *a, **k: "Playing via browser")
        out = server._catalog_miss_play(
            "Strobe", "deadmau5", "https://music.apple.com/...", reveal=False
        )
        assert "Browser" in out

    def test_browser_fallback_itself_errors(self, monkeypatch):
        """Browser fallback returns Error: -> falls through to Accessibility message."""
        monkeypatch.setattr(
            server.asc, "open_catalog_and_play", lambda url, track_name="": (False, "UI fail")
        )
        monkeypatch.setattr(server, "get_user_preferences", lambda: {"playback": "auto"})
        monkeypatch.setattr(server, "has_user_token", lambda: True)
        monkeypatch.setattr(server, "_browser_play", lambda *a, **k: "Error: not signed in")
        out = server._catalog_miss_play(
            "Strobe", "deadmau5", "https://music.apple.com/...", reveal=False
        )
        assert "Accessibility" in out or "couldn't" in out.lower()

    def test_pinned_native_no_browser(self, monkeypatch):
        """Line 7197: pinned native -> actionable error, no browser attempt."""
        monkeypatch.setattr(
            server.asc, "open_catalog_and_play", lambda url, track_name="": (False, "UI fail")
        )
        monkeypatch.setattr(server, "get_user_preferences", lambda: {"playback": "native"})
        monkeypatch.setattr(server, "has_user_token", lambda: True)
        out = server._catalog_miss_play(
            "Strobe", "deadmau5", "https://music.apple.com/...", reveal=False
        )
        assert "Accessibility" in out and ("web" in out.lower() or "engine=" in out.lower())

    def test_no_user_token_signin_message(self, monkeypatch):
        """Line 7210: no user token -> 'signin' in message."""
        monkeypatch.setattr(
            server.asc, "open_catalog_and_play", lambda url, track_name="": (False, "UI fail")
        )
        monkeypatch.setattr(server, "get_user_preferences", lambda: {"playback": "auto"})
        monkeypatch.setattr(server, "has_user_token", lambda: False)
        out = server._catalog_miss_play(
            "Strobe", "deadmau5", "https://music.apple.com/...", reveal=False
        )
        assert "login" in out.lower()


# ===========================================================================
# _playback_play  (lines 7214-7525, called via server.playback action="play")
# ===========================================================================


class TestPlaybackPlay:
    def _setup(self, monkeypatch):
        _native(monkeypatch)

    # --- URL path ---

    def test_url_with_conflicting_param_error(self, monkeypatch):
        """Line 7234-7235: url + track -> error."""
        self._setup(monkeypatch)
        out = server.playback(action="play", url="https://music.apple.com/...", track="Song")
        assert "Error" in out and "url" in out.lower()

    @responses.activate
    def test_url_song_path_converts_to_album(self, monkeypatch):
        """Lines 7237-7240: /song/ URL without ?i= -> _convert_song_url_to_album."""
        self._setup(monkeypatch)
        _mock_headers(monkeypatch)
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/songs/12345",
            json={
                "data": [
                    {
                        "attributes": {"albumName": "4x4=12"},
                        "relationships": {"albums": {"data": [{"id": "9999"}]}},
                    }
                ]
            },
            status=200,
        )
        seen_url = []
        monkeypatch.setattr(
            server.asc,
            "open_catalog_and_play",
            lambda url, shuffle=False, track_name="": seen_url.append(url) or (True, "Playing"),
        )
        monkeypatch.setattr(server, "get_user_preferences", lambda: {"playback": "auto"})
        server.playback(action="play", url="https://music.apple.com/us/song/strobe/12345")
        assert seen_url and "9999" in seen_url[0]

    @responses.activate
    def test_url_with_i_param_looks_up_song_name(self, monkeypatch):
        """Lines 7243-7246: URL with ?i= calls _catalog_song_name."""
        self._setup(monkeypatch)
        _mock_headers(monkeypatch)
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/songs/77777",
            json={"data": [{"attributes": {"name": "Strobe"}}]},
            status=200,
        )
        seen = []
        monkeypatch.setattr(
            server.asc,
            "open_catalog_and_play",
            lambda url, shuffle=False, track_name="": seen.append(track_name)
            or (True, f"Playing {track_name}"),
        )
        monkeypatch.setattr(server, "get_user_preferences", lambda: {"playback": "auto"})
        out = server.playback(action="play", url="https://music.apple.com/us/album/x/9999?i=77777")
        assert seen and seen[0] == "Strobe"

    def test_url_success(self, monkeypatch):
        """Lines 7247-7252: URL play succeeds."""
        self._setup(monkeypatch)
        _mock_headers(monkeypatch)
        monkeypatch.setattr(
            server.asc,
            "open_catalog_and_play",
            lambda url, shuffle=False, track_name="": (True, "Playing album X"),
        )
        monkeypatch.setattr(server, "get_user_preferences", lambda: {"playback": "auto"})
        out = server.playback(action="play", url="https://music.apple.com/us/album/x/9999")
        assert "Playing album X" in out

    def test_url_fail_browser_fallback(self, monkeypatch):
        """Lines 7254-7259: URL play fails, browser fallback succeeds."""
        self._setup(monkeypatch)
        _mock_headers(monkeypatch)
        monkeypatch.setattr(
            server.asc,
            "open_catalog_and_play",
            lambda url, shuffle=False, track_name="": (False, "UI failed"),
        )
        monkeypatch.setattr(server, "get_user_preferences", lambda: {"playback": "auto"})
        monkeypatch.setattr(server, "has_user_token", lambda: True)
        monkeypatch.setattr(server, "_browser_play", lambda *a, **k: "Playing via browser")
        out = server.playback(action="play", url="https://music.apple.com/us/album/x/9999")
        assert "[Browser]" in out

    def test_url_fail_browser_also_errors(self, monkeypatch):
        """Line 7259: browser fallback also fails -> falls through to Error."""
        self._setup(monkeypatch)
        _mock_headers(monkeypatch)
        monkeypatch.setattr(
            server.asc,
            "open_catalog_and_play",
            lambda url, shuffle=False, track_name="": (False, "UI failed"),
        )
        monkeypatch.setattr(server, "get_user_preferences", lambda: {"playback": "auto"})
        monkeypatch.setattr(server, "has_user_token", lambda: True)
        monkeypatch.setattr(server, "_browser_play", lambda *a, **k: "Error: not signed in")
        out = server.playback(action="play", url="https://music.apple.com/us/album/x/9999")
        assert "Error" in out

    def test_url_fail_pinned_native(self, monkeypatch):
        """Line 7261: URL play fails, pinned native -> error returned."""
        self._setup(monkeypatch)
        _mock_headers(monkeypatch)
        monkeypatch.setattr(
            server.asc,
            "open_catalog_and_play",
            lambda url, shuffle=False, track_name="": (False, "UI failed"),
        )
        monkeypatch.setattr(server, "get_user_preferences", lambda: {"playback": "native"})
        out = server.playback(action="play", url="https://music.apple.com/us/album/x/9999")
        assert "Error" in out and "UI failed" in out

    # --- target count checks ---

    def test_no_targets_error(self, monkeypatch):
        """Lines 7263-7264: no track/playlist/album."""
        self._setup(monkeypatch)
        out = server.playback(action="play")
        assert "Error" in out and ("Provide" in out or "track" in out.lower())

    def test_multiple_targets_error(self, monkeypatch):
        """Lines 7265-7266: two targets -> error."""
        self._setup(monkeypatch)
        out = server.playback(action="play", track="X", playlist="Y")
        assert "Error" in out and "ONE" in out

    # --- playlist path ---

    def test_playlist_play_success(self, monkeypatch):
        """Lines 7270-7273: play playlist."""
        self._setup(monkeypatch)
        monkeypatch.setattr(
            server.asc, "play_playlist", lambda n, s=False: (True, "Playing playlist")
        )
        out = server.playback(action="play", playlist="Rock Classics")
        assert "Playing playlist" in out

    def test_playlist_play_error(self, monkeypatch):
        """Line 7274: play playlist fails."""
        self._setup(monkeypatch)
        monkeypatch.setattr(server.asc, "play_playlist", lambda n, s=False: (False, "Not found"))
        out = server.playback(action="play", playlist="Nonexistent")
        assert "Error" in out and "Not found" in out

    # --- album path ---

    def test_album_in_library(self, monkeypatch):
        """Lines 7282-7300: album found in library."""
        self._setup(monkeypatch)
        monkeypatch.setattr(
            server.asc,
            "search_library",
            lambda q, t: (True, [{"name": "Track 1", "album": "Abbey Road", "artist": "Beatles"}]),
        )
        monkeypatch.setattr(server.asc, "play_track", lambda n, a: (True, "Playing track"))
        out = server.playback(action="play", album="Abbey Road", artist="Beatles")
        assert "[Library]" in out and "Abbey Road" in out

    def test_album_in_library_shuffle(self, monkeypatch):
        """Line 7293-7294: album in library with shuffle=True."""
        self._setup(monkeypatch)
        shuffle_set = []
        monkeypatch.setattr(
            server.asc,
            "search_library",
            lambda q, t: (True, [{"name": "T", "album": "Abbey Road", "artist": "Beatles"}]),
        )
        monkeypatch.setattr(
            server.asc, "set_shuffle", lambda e: shuffle_set.append(e) or (True, "OK")
        )
        monkeypatch.setattr(server.asc, "play_track", lambda n, a: (True, "Playing"))
        out = server.playback(action="play", album="Abbey Road", artist="Beatles", shuffle=True)
        assert shuffle_set == [True]
        assert "shuffled" in out

    def test_album_library_play_fails_then_catalog_not_found(self, monkeypatch):
        """Line 7301: album found in library but play_track fails -> break, catalog also empty."""
        self._setup(monkeypatch)
        monkeypatch.setattr(
            server.asc,
            "search_library",
            lambda q, t: (True, [{"name": "T", "album": "Abbey Road", "artist": "Beatles"}]),
        )
        monkeypatch.setattr(server.asc, "play_track", lambda n, a: (False, "Player error"))
        _mock_headers(monkeypatch)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"results": {"albums": {"data": []}}}
        monkeypatch.setattr(server.requests, "get", lambda *a, **k: mock_resp)
        out = server.playback(action="play", album="Abbey Road", artist="Beatles")
        assert "not found" in out.lower() or "Album not found" in out

    def test_album_in_catalog(self, monkeypatch):
        """Lines 7303-7365: album not in library, found in catalog via _catalog_miss_play."""
        self._setup(monkeypatch)
        monkeypatch.setattr(server.asc, "search_library", lambda q, t: (True, []))
        _mock_headers(monkeypatch)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "results": {
                "albums": {
                    "data": [
                        {
                            "id": "alb123",
                            "attributes": {
                                "name": "Abbey Road",
                                "artistName": "Beatles",
                                "url": "https://music.apple.com/us/album/abbey-road/123",
                            },
                        }
                    ]
                }
            }
        }
        monkeypatch.setattr(server.requests, "get", lambda *a, **k: mock_resp)
        monkeypatch.setattr(
            server, "_catalog_miss_play", lambda n, a, u, r: f"[Catalog] {n} by {a}"
        )
        out = server.playback(action="play", album="Abbey Road")
        assert "Abbey Road" in out

    def test_album_catalog_artist_filter_miss(self, monkeypatch):
        """Album found in catalog but artist doesn't match -> skipped -> not found."""
        self._setup(monkeypatch)
        monkeypatch.setattr(server.asc, "search_library", lambda q, t: (True, []))
        _mock_headers(monkeypatch)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "results": {
                "albums": {
                    "data": [
                        {
                            "id": "alb123",
                            "attributes": {
                                "name": "Abbey Road",
                                "artistName": "Beatles",
                                "url": "https://music.apple.com/...",
                            },
                        }
                    ]
                }
            }
        }
        monkeypatch.setattr(server.requests, "get", lambda *a, **k: mock_resp)
        out = server.playback(action="play", album="Abbey Road", artist="Rolling Stones")
        assert "not found" in out.lower() or "Album not found" in out

    def test_album_catalog_request_exception(self, monkeypatch):
        """Line 7366-7367: requests.exceptions.RequestException -> API Error."""
        self._setup(monkeypatch)
        monkeypatch.setattr(server.asc, "search_library", lambda q, t: (True, []))
        _mock_headers(monkeypatch)
        import requests as _req

        monkeypatch.setattr(
            server.requests,
            "get",
            lambda *a, **k: (_ for _ in ()).throw(_req.exceptions.RequestException("network fail")),
        )
        out = server.playback(action="play", album="Abbey Road")
        assert "API Error" in out or "Error" in out

    def test_album_catalog_value_error(self, monkeypatch):
        """Line 7368: FileNotFoundError/ValueError -> Error."""
        self._setup(monkeypatch)
        monkeypatch.setattr(server.asc, "search_library", lambda q, t: (True, []))
        monkeypatch.setattr(server, "get_headers", lambda: (_ for _ in ()).throw(ValueError("bad")))
        out = server.playback(action="play", album="Abbey Road")
        assert "Error" in out

    def test_album_not_found(self, monkeypatch):
        """Line 7370: album not found in library or catalog."""
        self._setup(monkeypatch)
        monkeypatch.setattr(server.asc, "search_library", lambda q, t: (True, []))
        _mock_headers(monkeypatch)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"results": {"albums": {"data": []}}}
        monkeypatch.setattr(server.requests, "get", lambda *a, **k: mock_resp)
        out = server.playback(action="play", album="Nonexistent Album 99999")
        assert "not found" in out.lower() or "Album not found" in out

    def test_album_add_to_library_success(self, monkeypatch):
        """Lines 7327-7359: album in catalog, add_to_library=True, play succeeds."""
        self._setup(monkeypatch)
        _mock_headers(monkeypatch)
        monkeypatch.setattr(server, "_add_album_to_library", lambda aid: (True, "Added"))
        monkeypatch.setattr(time_module, "sleep", lambda s: None)
        monkeypatch.setattr(server, "PLAY_TRACK_MAX_ATTEMPTS", 1)

        call_count = [0]

        def mock_search_library(q, t):
            call_count[0] += 1
            if call_count[0] <= 1:
                return True, []
            return True, [{"name": "Track1", "album": "Abbey Road", "artist": "Beatles"}]

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "results": {
                "albums": {
                    "data": [
                        {
                            "id": "alb123",
                            "attributes": {
                                "name": "Abbey Road",
                                "artistName": "Beatles",
                                "url": "https://...",
                            },
                        }
                    ]
                }
            }
        }
        monkeypatch.setattr(server.requests, "get", lambda *a, **k: mock_resp)
        monkeypatch.setattr(server.asc, "search_library", mock_search_library)
        monkeypatch.setattr(server.asc, "play_track", lambda n, a: (True, "Playing"))
        out = server.playback(action="play", album="Abbey Road", add_to_library=True)
        assert "Abbey Road" in out

    def test_album_add_to_library_sync_pending(self, monkeypatch):
        """Line 7361: add_to_library=True but re-search finds nothing -> sync pending."""
        self._setup(monkeypatch)
        _mock_headers(monkeypatch)
        monkeypatch.setattr(server, "_add_album_to_library", lambda aid: (True, "Added"))
        monkeypatch.setattr(time_module, "sleep", lambda s: None)
        monkeypatch.setattr(server, "PLAY_TRACK_MAX_ATTEMPTS", 1)
        # Always return empty from library search
        monkeypatch.setattr(server.asc, "search_library", lambda q, t: (True, []))
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "results": {
                "albums": {
                    "data": [
                        {
                            "id": "alb123",
                            "attributes": {
                                "name": "Abbey Road",
                                "artistName": "Beatles",
                                "url": "https://...",
                            },
                        }
                    ]
                }
            }
        }
        monkeypatch.setattr(server.requests, "get", lambda *a, **k: mock_resp)
        out = server.playback(action="play", album="Abbey Road", add_to_library=True)
        assert "sync pending" in out.lower() or "Abbey Road" in out

    def test_album_add_to_library_fails(self, monkeypatch):
        """Line 7362: _add_album_to_library fails."""
        self._setup(monkeypatch)
        _mock_headers(monkeypatch)
        monkeypatch.setattr(server, "_add_album_to_library", lambda aid: (False, "add failed"))
        monkeypatch.setattr(server.asc, "search_library", lambda q, t: (True, []))
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "results": {
                "albums": {
                    "data": [
                        {
                            "id": "alb123",
                            "attributes": {
                                "name": "Abbey Road",
                                "artistName": "Beatles",
                                "url": "https://...",
                            },
                        }
                    ]
                }
            }
        }
        monkeypatch.setattr(server.requests, "get", lambda *a, **k: mock_resp)
        out = server.playback(action="play", album="Abbey Road", add_to_library=True)
        assert "Failed to add" in out or "Error" in out

    # --- track path ---

    def test_track_resolve_returns_error(self, monkeypatch):
        """Line 7381-7382: _resolve_track returns item with error."""
        self._setup(monkeypatch)
        monkeypatch.setattr(
            server,
            "_resolve_track",
            lambda t, a="": [
                ResolvedInput(input_type=InputType.NAME, value="", error="Could not resolve")
            ],
        )
        out = server.playback(action="play", track="X")
        assert "Error" in out

    def test_track_resolve_empty_list(self, monkeypatch):
        """Lines 7376-7378: _resolve_track returns empty -> error."""
        self._setup(monkeypatch)
        monkeypatch.setattr(server, "_resolve_track", lambda t, a="": [])
        out = server.playback(action="play", track="X")
        assert "Error" in out and "resolve" in out.lower()

    def test_catalog_id_track_ui_play_success(self, monkeypatch):
        """Lines 7388-7428: catalog ID -> API lookup -> UI play succeeds."""
        self._setup(monkeypatch)
        _mock_headers(monkeypatch)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data": [
                {"attributes": {"name": "Strobe", "artistName": "deadmau5", "url": "https://..."}}
            ]
        }
        monkeypatch.setattr(server.requests, "get", lambda *a, **k: mock_resp)
        monkeypatch.setattr(
            server, "_try_ui_catalog_play", lambda n, a, **k: (True, "[UI Catalog] Playing")
        )
        # "1234567890" is 10 digits -> InputType.CATALOG_ID
        out = server.playback(action="play", track="1234567890")
        assert "[UI Catalog]" in out or "Playing" in out

    def test_catalog_id_track_catalog_miss(self, monkeypatch):
        """Line 7429: catalog ID -> UI play fails -> catalog_miss_play."""
        self._setup(monkeypatch)
        _mock_headers(monkeypatch)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data": [
                {"attributes": {"name": "Strobe", "artistName": "deadmau5", "url": "https://url"}}
            ]
        }
        monkeypatch.setattr(server.requests, "get", lambda *a, **k: mock_resp)
        monkeypatch.setattr(server, "_try_ui_catalog_play", lambda n, a, **k: (False, None))
        monkeypatch.setattr(server, "_catalog_miss_play", lambda n, a, u, r: f"[Miss] {n}")
        out = server.playback(action="play", track="1234567890")
        assert "[Miss]" in out

    def test_catalog_id_add_to_library(self, monkeypatch):
        """Lines 7406-7423: catalog ID + add_to_library."""
        self._setup(monkeypatch)
        _mock_headers(monkeypatch)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data": [
                {"attributes": {"name": "Strobe", "artistName": "deadmau5", "url": "https://..."}}
            ]
        }
        monkeypatch.setattr(server.requests, "get", lambda *a, **k: mock_resp)
        monkeypatch.setattr(server, "_add_songs_to_library", lambda ids: (True, "Added"))
        monkeypatch.setattr(time_module, "sleep", lambda s: None)
        monkeypatch.setattr(server, "PLAY_TRACK_MAX_ATTEMPTS", 1)
        monkeypatch.setattr(server.asc, "play_track", lambda n, a: (True, "Playing"))
        out = server.playback(action="play", track="1234567890", add_to_library=True)
        assert "Catalog" in out or "Playing" in out

    def test_catalog_id_add_to_library_fail(self, monkeypatch):
        """Line 7423: _add_songs_to_library fails."""
        self._setup(monkeypatch)
        _mock_headers(monkeypatch)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data": [
                {"attributes": {"name": "Strobe", "artistName": "deadmau5", "url": "https://..."}}
            ]
        }
        monkeypatch.setattr(server.requests, "get", lambda *a, **k: mock_resp)
        monkeypatch.setattr(server, "_add_songs_to_library", lambda ids: (False, "add failed"))
        out = server.playback(action="play", track="1234567890", add_to_library=True)
        assert "Failed to add" in out

    def test_catalog_id_add_sync_pending(self, monkeypatch):
        """Line 7422: add succeeds but play_track never succeeds -> sync pending."""
        self._setup(monkeypatch)
        _mock_headers(monkeypatch)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data": [
                {"attributes": {"name": "Strobe", "artistName": "deadmau5", "url": "https://..."}}
            ]
        }
        monkeypatch.setattr(server.requests, "get", lambda *a, **k: mock_resp)
        monkeypatch.setattr(server, "_add_songs_to_library", lambda ids: (True, "Added"))
        monkeypatch.setattr(time_module, "sleep", lambda s: None)
        monkeypatch.setattr(server, "PLAY_TRACK_MAX_ATTEMPTS", 1)
        monkeypatch.setattr(server.asc, "play_track", lambda n, a: (False, "not found yet"))
        out = server.playback(action="play", track="1234567890", add_to_library=True)
        assert "sync pending" in out.lower() or "Strobe" in out

    def test_catalog_id_api_404(self, monkeypatch):
        """Line 7432: catalog ID -> API returns 404 -> track not found."""
        self._setup(monkeypatch)
        _mock_headers(monkeypatch)
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        monkeypatch.setattr(server.requests, "get", lambda *a, **k: mock_resp)
        out = server.playback(action="play", track="1234567890")
        assert "not found" in out.lower()

    def test_catalog_id_exception(self, monkeypatch):
        """Line 7430-7432: catalog ID -> exception -> track not found."""
        self._setup(monkeypatch)
        monkeypatch.setattr(server, "get_headers", lambda: (_ for _ in ()).throw(Exception("fail")))
        out = server.playback(action="play", track="1234567890")
        # Honest: a real error is surfaced, NOT disguised as "track not found".
        assert "error" in out.lower() and "fail" in out.lower()

    def test_track_in_library(self, monkeypatch):
        """Lines 7439-7455: track found in library."""
        self._setup(monkeypatch)
        monkeypatch.setattr(
            server.asc,
            "search_library",
            lambda q, t: (True, [{"name": "Strobe", "artist": "deadmau5"}]),
        )
        monkeypatch.setattr(server.asc, "play_track", lambda n, a: (True, f"Playing {n}"))
        out = server.playback(action="play", track="Strobe")
        assert "[Library]" in out and "Playing" in out

    def test_track_in_library_with_reveal(self, monkeypatch):
        """Lines 7452-7454: library track + reveal=True."""
        self._setup(monkeypatch)
        monkeypatch.setattr(
            server.asc,
            "search_library",
            lambda q, t: (True, [{"name": "Strobe", "artist": "deadmau5"}]),
        )
        monkeypatch.setattr(server.asc, "play_track", lambda n, a: (True, "Playing Strobe"))
        revealed = []
        monkeypatch.setattr(
            server.asc, "reveal_track", lambda n, a=None: revealed.append(1) or (True, "Revealed")
        )
        out = server.playback(action="play", track="Strobe", reveal=True)
        assert "[Library]" in out
        assert len(revealed) == 1

    def test_track_in_library_play_fails(self, monkeypatch):
        """Line 7456: library match found but play_track fails -> break, continue to catalog."""
        self._setup(monkeypatch)
        monkeypatch.setattr(
            server.asc,
            "search_library",
            lambda q, t: (True, [{"name": "Strobe", "artist": "deadmau5"}]),
        )
        monkeypatch.setattr(server.asc, "play_track", lambda n, a: (False, "Player busy"))
        monkeypatch.setattr(server, "_search_catalog_songs", lambda t, limit=5: [])
        monkeypatch.setattr(server.asc, "ui_play_result_by_query", lambda q: (False, "Not found"))
        out = server.playback(action="play", track="Strobe")
        assert "not found" in out.lower()

    def test_track_in_catalog_ui_success(self, monkeypatch):
        """Lines 7504-7506: track in catalog, UI play succeeds."""
        self._setup(monkeypatch)
        monkeypatch.setattr(server.asc, "search_library", lambda q, t: (True, []))
        monkeypatch.setattr(
            server,
            "_search_catalog_songs",
            lambda t, limit=5: [
                {
                    "id": "111",
                    "attributes": {
                        "name": "Strobe",
                        "artistName": "deadmau5",
                        "url": "https://...",
                    },
                }
            ],
        )
        monkeypatch.setattr(
            server, "_try_ui_catalog_play", lambda n, a, **k: (True, "[UI Catalog] Playing Strobe")
        )
        out = server.playback(action="play", track="Strobe", artist="deadmau5")
        assert "[UI Catalog]" in out

    def test_track_in_catalog_ui_fails_with_msg(self, monkeypatch):
        """Lines 7507-7511: UI attempted and failed with a message -> surface error."""
        self._setup(monkeypatch)
        monkeypatch.setattr(server.asc, "search_library", lambda q, t: (True, []))
        monkeypatch.setattr(
            server,
            "_search_catalog_songs",
            lambda t, limit=5: [
                {
                    "id": "111",
                    "attributes": {
                        "name": "Strobe",
                        "artistName": "deadmau5",
                        "url": "https://...",
                    },
                }
            ],
        )
        monkeypatch.setattr(
            server, "_try_ui_catalog_play", lambda n, a, **k: (False, "Accessibility denied")
        )
        out = server.playback(action="play", track="Strobe", artist="deadmau5")
        assert "UI Catalog failed" in out or "Accessibility denied" in out

    def test_track_in_catalog_ui_none_catalog_miss(self, monkeypatch):
        """Lines 7513-7514: UI returns (False, None) -> fall through to _catalog_miss_play."""
        self._setup(monkeypatch)
        monkeypatch.setattr(server.asc, "search_library", lambda q, t: (True, []))
        monkeypatch.setattr(
            server,
            "_search_catalog_songs",
            lambda t, limit=5: [
                {
                    "id": "111",
                    "attributes": {
                        "name": "Strobe",
                        "artistName": "deadmau5",
                        "url": "https://url",
                    },
                }
            ],
        )
        monkeypatch.setattr(server, "_try_ui_catalog_play", lambda n, a, **k: (False, None))
        monkeypatch.setattr(server, "_catalog_miss_play", lambda n, a, u, r: f"[CatalogMiss] {n}")
        out = server.playback(action="play", track="Strobe", artist="deadmau5")
        assert "[CatalogMiss]" in out

    def test_track_in_catalog_add_to_library(self, monkeypatch):
        """Lines 7483-7500: track in catalog, add_to_library=True."""
        self._setup(monkeypatch)
        monkeypatch.setattr(server.asc, "search_library", lambda q, t: (True, []))
        monkeypatch.setattr(
            server,
            "_search_catalog_songs",
            lambda t, limit=5: [
                {
                    "id": "111",
                    "attributes": {
                        "name": "Strobe",
                        "artistName": "deadmau5",
                        "url": "https://...",
                    },
                }
            ],
        )
        monkeypatch.setattr(server, "_add_songs_to_library", lambda ids: (True, "Added"))
        monkeypatch.setattr(time_module, "sleep", lambda s: None)
        monkeypatch.setattr(server, "PLAY_TRACK_MAX_ATTEMPTS", 1)
        monkeypatch.setattr(server.asc, "play_track", lambda n, a: (True, "Playing"))
        out = server.playback(action="play", track="Strobe", artist="deadmau5", add_to_library=True)
        assert "Catalog" in out or "Playing" in out

    def test_track_in_catalog_add_sync_pending(self, monkeypatch):
        """Line 7499: add succeeds but play_track never succeeds."""
        self._setup(monkeypatch)
        monkeypatch.setattr(server.asc, "search_library", lambda q, t: (True, []))
        monkeypatch.setattr(
            server,
            "_search_catalog_songs",
            lambda t, limit=5: [
                {
                    "id": "111",
                    "attributes": {
                        "name": "Strobe",
                        "artistName": "deadmau5",
                        "url": "https://...",
                    },
                }
            ],
        )
        monkeypatch.setattr(server, "_add_songs_to_library", lambda ids: (True, "Added"))
        monkeypatch.setattr(time_module, "sleep", lambda s: None)
        monkeypatch.setattr(server, "PLAY_TRACK_MAX_ATTEMPTS", 1)
        monkeypatch.setattr(server.asc, "play_track", lambda n, a: (False, "not found"))
        out = server.playback(action="play", track="Strobe", artist="deadmau5", add_to_library=True)
        assert "sync pending" in out.lower() or "Strobe" in out

    def test_track_in_catalog_add_fails(self, monkeypatch):
        """Line 7500: _add_songs_to_library fails."""
        self._setup(monkeypatch)
        monkeypatch.setattr(server.asc, "search_library", lambda q, t: (True, []))
        monkeypatch.setattr(
            server,
            "_search_catalog_songs",
            lambda t, limit=5: [
                {
                    "id": "111",
                    "attributes": {
                        "name": "Strobe",
                        "artistName": "deadmau5",
                        "url": "https://...",
                    },
                }
            ],
        )
        monkeypatch.setattr(server, "_add_songs_to_library", lambda ids: (False, "add fail"))
        out = server.playback(action="play", track="Strobe", artist="deadmau5", add_to_library=True)
        assert "Failed to add" in out

    def test_track_in_catalog_add_with_reveal(self, monkeypatch):
        """Lines 7493-7494: add_to_library=True + reveal=True."""
        self._setup(monkeypatch)
        monkeypatch.setattr(server.asc, "search_library", lambda q, t: (True, []))
        monkeypatch.setattr(
            server,
            "_search_catalog_songs",
            lambda t, limit=5: [
                {
                    "id": "111",
                    "attributes": {
                        "name": "Strobe",
                        "artistName": "deadmau5",
                        "url": "https://...",
                    },
                }
            ],
        )
        monkeypatch.setattr(server, "_add_songs_to_library", lambda ids: (True, "Added"))
        monkeypatch.setattr(time_module, "sleep", lambda s: None)
        monkeypatch.setattr(server, "PLAY_TRACK_MAX_ATTEMPTS", 1)
        monkeypatch.setattr(server.asc, "play_track", lambda n, a: (True, "Playing"))
        revealed = []
        monkeypatch.setattr(
            server.asc, "reveal_track", lambda n, a: revealed.append(1) or (True, "Revealed")
        )
        server.playback(action="play", track="Strobe", add_to_library=True, reveal=True)
        assert len(revealed) == 1

    def test_track_not_found_ui_search_last_resort_success(self, monkeypatch):
        """Lines 7519-7523: catalog search empty -> UI search last resort succeeds."""
        self._setup(monkeypatch)
        monkeypatch.setattr(server.asc, "search_library", lambda q, t: (True, []))
        monkeypatch.setattr(server, "_search_catalog_songs", lambda t, limit=5: [])
        monkeypatch.setattr(
            server.asc, "ui_play_result_by_query", lambda q: (True, "Playing via UI search")
        )
        out = server.playback(action="play", track="Strobe", artist="deadmau5")
        assert "[UI Search]" in out or "Playing" in out

    def test_track_not_found_anywhere(self, monkeypatch):
        """Line 7525: not found in library, catalog, or UI."""
        self._setup(monkeypatch)
        monkeypatch.setattr(server.asc, "search_library", lambda q, t: (True, []))
        monkeypatch.setattr(server, "_search_catalog_songs", lambda t, limit=5: [])
        monkeypatch.setattr(server.asc, "ui_play_result_by_query", lambda q: (False, "Not found"))
        out = server.playback(action="play", track="zzznopereallynothing9999")
        assert "not found" in out.lower()

    def test_catalog_id_with_add_and_reveal(self, monkeypatch):
        """Lines 7415-7416: catalog ID + add + reveal=True."""
        self._setup(monkeypatch)
        _mock_headers(monkeypatch)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data": [
                {"attributes": {"name": "Strobe", "artistName": "deadmau5", "url": "https://..."}}
            ]
        }
        monkeypatch.setattr(server.requests, "get", lambda *a, **k: mock_resp)
        monkeypatch.setattr(server, "_add_songs_to_library", lambda ids: (True, "Added"))
        monkeypatch.setattr(time_module, "sleep", lambda s: None)
        monkeypatch.setattr(server, "PLAY_TRACK_MAX_ATTEMPTS", 1)
        monkeypatch.setattr(server.asc, "play_track", lambda n, a: (True, "Playing"))
        revealed = []
        monkeypatch.setattr(
            server.asc, "reveal_track", lambda n, a: revealed.append(1) or (True, "Revealed")
        )
        server.playback(action="play", track="1234567890", add_to_library=True, reveal=True)
        assert len(revealed) == 1


# ===========================================================================
# _playback_control  (lines 7527-7558)
# ===========================================================================


class TestPlaybackControl:
    def _setup(self, monkeypatch):
        _native(monkeypatch)

    def test_seek_success(self, monkeypatch):
        self._setup(monkeypatch)
        monkeypatch.setattr(server.asc, "seek", lambda s: (True, "Seeked"))
        out = server.playback(action="control", control="seek", seconds=90.0)
        assert "1:30" in out

    def test_seek_failure(self, monkeypatch):
        self._setup(monkeypatch)
        monkeypatch.setattr(server.asc, "seek", lambda s: (False, "Seek failed"))
        out = server.playback(action="control", control="seek", seconds=90.0)
        assert "Error" in out

    def test_play(self, monkeypatch):
        self._setup(monkeypatch)
        monkeypatch.setattr(server.asc, "play", lambda: (True, "Playing"))
        assert "Playback: play" in server.playback(action="control", control="play")

    def test_pause(self, monkeypatch):
        self._setup(monkeypatch)
        monkeypatch.setattr(server.asc, "pause", lambda: (True, "Paused"))
        assert "Playback: pause" in server.playback(action="control", control="pause")

    def test_playpause(self, monkeypatch):
        self._setup(monkeypatch)
        monkeypatch.setattr(server.asc, "playpause", lambda: (True, "Toggled"))
        assert "Playback: playpause" in server.playback(action="control", control="playpause")

    def test_stop(self, monkeypatch):
        self._setup(monkeypatch)
        monkeypatch.setattr(server.asc, "stop", lambda: (True, "Stopped"))
        assert "Playback: stop" in server.playback(action="control", control="stop")

    def test_next(self, monkeypatch):
        self._setup(monkeypatch)
        monkeypatch.setattr(server.asc, "next_track", lambda: (True, "Next"))
        assert "Playback: next" in server.playback(action="control", control="next")

    def test_previous(self, monkeypatch):
        self._setup(monkeypatch)
        monkeypatch.setattr(server.asc, "previous_track", lambda: (True, "Previous"))
        assert "Playback: previous" in server.playback(action="control", control="previous")

    def test_control_failure(self, monkeypatch):
        self._setup(monkeypatch)
        monkeypatch.setattr(server.asc, "play", lambda: (False, "Player error"))
        out = server.playback(action="control", control="play")
        assert "Error" in out

    def test_invalid_action(self, monkeypatch):
        self._setup(monkeypatch)
        out = server.playback(action="control", control="teleport")
        assert "Invalid action" in out


# ===========================================================================
# _playback_now_playing  (lines 7560-7590)
# ===========================================================================


class TestPlaybackNowPlaying:
    def _setup(self, monkeypatch):
        _native(monkeypatch)

    def test_stopped(self, monkeypatch):
        self._setup(monkeypatch)
        monkeypatch.setattr(server.asc, "get_current_track", lambda: (True, {"state": "stopped"}))
        out = server.playback(action="now_playing")
        assert "stopped" in out.lower()

    def test_playing_with_full_track_info(self, monkeypatch):
        self._setup(monkeypatch)
        monkeypatch.setattr(
            server.asc,
            "get_current_track",
            lambda: (
                True,
                {
                    "state": "playing",
                    "name": "Strobe",
                    "artist": "deadmau5",
                    "album": "4x4=12",
                    "position": 90.0,
                    "duration": 605.0,
                },
            ),
        )
        out = server.playback(action="now_playing")
        assert "Strobe" in out
        assert "deadmau5" in out
        assert "1:30" in out

    def test_playing_minimal_info(self, monkeypatch):
        self._setup(monkeypatch)
        monkeypatch.setattr(server.asc, "get_current_track", lambda: (True, {"state": "playing"}))
        out = server.playback(action="now_playing")
        assert "playing" in out.lower()

    def test_error_from_applescript(self, monkeypatch):
        self._setup(monkeypatch)
        monkeypatch.setattr(server.asc, "get_current_track", lambda: (False, "AS error"))
        out = server.playback(action="now_playing")
        assert "Error" in out

    def test_invalid_position_duration_no_crash(self, monkeypatch):
        """Lines 7581-7588: bad position/duration values -> ValueError caught."""
        self._setup(monkeypatch)
        monkeypatch.setattr(
            server.asc,
            "get_current_track",
            lambda: (
                True,
                {
                    "state": "playing",
                    "name": "X",
                    "position": "bad",
                    "duration": "bad",
                },
            ),
        )
        out = server.playback(action="now_playing")
        # Should not crash; position line skipped
        assert "X" in out or "playing" in out.lower()


# ===========================================================================
# _playback_settings  (lines 7592-7644)
# ===========================================================================


class TestPlaybackSettings:
    def _setup(self, monkeypatch):
        _native(monkeypatch)

    def test_set_volume(self, monkeypatch):
        self._setup(monkeypatch)
        monkeypatch.setattr(server.asc, "set_volume", lambda v: (True, "OK"))
        out = server.playback(action="settings", volume=80)
        assert "Volume: 80" in out

    def test_set_volume_clamped_to_100(self, monkeypatch):
        self._setup(monkeypatch)
        seen = []
        monkeypatch.setattr(server.asc, "set_volume", lambda v: seen.append(v) or (True, "OK"))
        server.playback(action="settings", volume=150)
        assert seen[0] == 100

    def test_volume_error(self, monkeypatch):
        self._setup(monkeypatch)
        monkeypatch.setattr(server.asc, "set_volume", lambda v: (False, "Vol error"))
        out = server.playback(action="settings", volume=80)
        assert "Error setting volume" in out

    def test_set_shuffle_on(self, monkeypatch):
        self._setup(monkeypatch)
        monkeypatch.setattr(server.asc, "set_shuffle", lambda e: (True, "OK"))
        out = server.playback(action="settings", shuffle_mode="on")
        assert "Shuffle: on" in out

    def test_set_shuffle_off(self, monkeypatch):
        self._setup(monkeypatch)
        monkeypatch.setattr(server.asc, "set_shuffle", lambda e: (True, "OK"))
        out = server.playback(action="settings", shuffle_mode="off")
        assert "Shuffle: off" in out

    def test_shuffle_error(self, monkeypatch):
        self._setup(monkeypatch)
        monkeypatch.setattr(server.asc, "set_shuffle", lambda e: (False, "Shuffle error"))
        out = server.playback(action="settings", shuffle_mode="on")
        assert "Error setting shuffle" in out

    def test_set_repeat(self, monkeypatch):
        self._setup(monkeypatch)
        monkeypatch.setattr(server.asc, "set_repeat", lambda r: (True, "OK"))
        out = server.playback(action="settings", repeat="all")
        assert "Repeat: all" in out

    def test_repeat_error(self, monkeypatch):
        self._setup(monkeypatch)
        monkeypatch.setattr(server.asc, "set_repeat", lambda r: (False, "Repeat error"))
        out = server.playback(action="settings", repeat="all")
        assert "Error setting repeat" in out

    def test_get_current_settings(self, monkeypatch):
        self._setup(monkeypatch)
        monkeypatch.setattr(
            server.asc,
            "get_library_stats",
            lambda: (
                True,
                {"player_state": "playing", "volume": 75, "shuffle": True, "repeat": "off"},
            ),
        )
        out = server.playback(action="settings")
        assert "Volume: 75" in out
        assert "Shuffle: on" in out

    def test_get_settings_error(self, monkeypatch):
        self._setup(monkeypatch)
        monkeypatch.setattr(server.asc, "get_library_stats", lambda: (False, "Stats error"))
        out = server.playback(action="settings")
        assert "Error" in out


# ===========================================================================
# _playlist_remove  (lines 7646-7766)
# ===========================================================================


class TestPlaylistRemove:
    def test_playlist_resolve_error(self, monkeypatch):
        """Lines 7655-7656: _resolve_playlist returns error."""
        monkeypatch.setattr(
            server,
            "_resolve_playlist",
            lambda n: ResolvedPlaylist(error="Error: playlist not found", raw_input="Bad"),
        )
        out = server._playlist_remove(playlist="Bad", track="Strobe")
        assert "playlist not found" in out.lower() or "Error" in out

    def test_no_applescript_name_error(self, monkeypatch):
        """Lines 7659-7660: resolved playlist has no applescript_name."""
        monkeypatch.setattr(
            server,
            "_resolve_playlist",
            lambda n: ResolvedPlaylist(api_id="p.abc", applescript_name=None, raw_input="Rock"),
        )
        out = server._playlist_remove(playlist="Rock", track="Song")
        assert "Error" in out

    def test_no_track_error(self, monkeypatch):
        """Lines 7662-7663: track param empty."""
        monkeypatch.setattr(
            server,
            "_resolve_playlist",
            lambda n: ResolvedPlaylist(applescript_name="Rock", raw_input="Rock"),
        )
        out = server._playlist_remove(playlist="Rock", track="")
        assert "track" in out.lower() and "Error" in out

    def test_remove_by_name_success_verified(self, monkeypatch):
        """Lines 7748-7752: remove NAME type, verify succeeds."""
        monkeypatch.setattr(
            server,
            "_resolve_playlist",
            lambda n: ResolvedPlaylist(applescript_name="Rock", raw_input="Rock"),
        )
        monkeypatch.setattr(
            server,
            "_resolve_track",
            lambda t, a="": [
                ResolvedInput(input_type=InputType.NAME, value="Strobe", artist="deadmau5")
            ],
        )
        monkeypatch.setattr(
            server.asc,
            "remove_track_from_playlist",
            lambda pl, track_name=None, artist=None, track_id=None: (True, "Removed Strobe"),
        )
        monkeypatch.setattr(server, "_verify_track_not_in_playlist", lambda pl, n, a: True)
        out = server._playlist_remove(playlist="Rock", track="Strobe", verify=True)
        assert "removed" in out.lower()

    def test_remove_verify_still_present(self, monkeypatch):
        """Lines 7686-7693: remove succeeds but verify says track still there."""
        monkeypatch.setattr(
            server,
            "_resolve_playlist",
            lambda n: ResolvedPlaylist(applescript_name="Rock", raw_input="Rock"),
        )
        monkeypatch.setattr(
            server,
            "_resolve_track",
            lambda t, a="": [ResolvedInput(input_type=InputType.NAME, value="Strobe", artist="")],
        )
        monkeypatch.setattr(
            server.asc,
            "remove_track_from_playlist",
            lambda pl, track_name=None, artist=None, track_id=None: (True, "Removed"),
        )
        monkeypatch.setattr(server, "_verify_track_not_in_playlist", lambda pl, n, a: False)
        out = server._playlist_remove(playlist="Rock", track="Strobe", verify=True)
        assert "still" in out.lower() or "revert" in out.lower()

    def test_remove_no_verify(self, monkeypatch):
        """Line 7681-7682: verify=False path."""
        monkeypatch.setattr(
            server,
            "_resolve_playlist",
            lambda n: ResolvedPlaylist(applescript_name="Rock", raw_input="Rock"),
        )
        monkeypatch.setattr(
            server,
            "_resolve_track",
            lambda t, a="": [ResolvedInput(input_type=InputType.NAME, value="Strobe", artist="")],
        )
        monkeypatch.setattr(
            server.asc,
            "remove_track_from_playlist",
            lambda pl, track_name=None, artist=None, track_id=None: (True, "Removed Strobe"),
        )
        out = server._playlist_remove(playlist="Rock", track="Strobe", verify=False)
        assert "removed" in out.lower()

    def test_remove_asc_fails(self, monkeypatch):
        """Lines 7678-7679: asc.remove_track_from_playlist fails."""
        monkeypatch.setattr(
            server,
            "_resolve_playlist",
            lambda n: ResolvedPlaylist(applescript_name="Rock", raw_input="Rock"),
        )
        monkeypatch.setattr(
            server,
            "_resolve_track",
            lambda t, a="": [ResolvedInput(input_type=InputType.NAME, value="Strobe", artist="")],
        )
        monkeypatch.setattr(
            server.asc,
            "remove_track_from_playlist",
            lambda pl, track_name=None, artist=None, track_id=None: (False, "Track not found"),
        )
        out = server._playlist_remove(playlist="Rock", track="Strobe", verify=False)
        assert "failed" in out.lower() or "Track not found" in out

    def test_remove_resolved_track_error(self, monkeypatch):
        """Line 7699: ResolvedInput has error."""
        monkeypatch.setattr(
            server,
            "_resolve_playlist",
            lambda n: ResolvedPlaylist(applescript_name="Rock", raw_input="Rock"),
        )
        monkeypatch.setattr(
            server,
            "_resolve_track",
            lambda t, a="": [ResolvedInput(input_type=InputType.NAME, value="", error="Bad input")],
        )
        out = server._playlist_remove(playlist="Rock", track="badtrack")
        assert "failed" in out.lower() or "Bad input" in out

    def test_remove_by_persistent_id(self, monkeypatch):
        """Lines 7702-7708: PERSISTENT_ID type - verify skipped (name=None)."""
        monkeypatch.setattr(
            server,
            "_resolve_playlist",
            lambda n: ResolvedPlaylist(applescript_name="Rock", raw_input="Rock"),
        )
        monkeypatch.setattr(
            server,
            "_resolve_track",
            lambda t, a="": [
                ResolvedInput(input_type=InputType.PERSISTENT_ID, value="ABC123DEF456")
            ],
        )
        monkeypatch.setattr(
            server.asc,
            "remove_track_from_playlist",
            lambda pl, track_name=None, artist=None, track_id=None: (True, "Removed"),
        )
        out = server._playlist_remove(playlist="Rock", track="ABC123DEF456", verify=True)
        assert "removed" in out.lower()

    def test_remove_by_catalog_id_in_cache(self, monkeypatch):
        """Lines 7710-7724: CATALOG_ID in track cache."""
        monkeypatch.setattr(
            server,
            "_resolve_playlist",
            lambda n: ResolvedPlaylist(applescript_name="Rock", raw_input="Rock"),
        )
        monkeypatch.setattr(
            server,
            "_resolve_track",
            lambda t, a="": [ResolvedInput(input_type=InputType.CATALOG_ID, value="1234567890")],
        )
        mock_cache = MagicMock()
        mock_cache.get_track_info.return_value = {"name": "Strobe", "artist": "deadmau5"}
        monkeypatch.setattr(server, "get_track_cache", lambda: mock_cache)
        monkeypatch.setattr(
            server.asc,
            "remove_track_from_playlist",
            lambda pl, track_name=None, artist=None, track_id=None: (True, "Removed Strobe"),
        )
        monkeypatch.setattr(server, "_verify_track_not_in_playlist", lambda pl, n, a: True)
        out = server._playlist_remove(playlist="Rock", track="1234567890", verify=True)
        assert "removed" in out.lower()

    def test_remove_by_catalog_id_not_in_cache(self, monkeypatch):
        """Line 7727: CATALOG_ID not in cache."""
        monkeypatch.setattr(
            server,
            "_resolve_playlist",
            lambda n: ResolvedPlaylist(applescript_name="Rock", raw_input="Rock"),
        )
        monkeypatch.setattr(
            server,
            "_resolve_track",
            lambda t, a="": [ResolvedInput(input_type=InputType.CATALOG_ID, value="1234567890")],
        )
        mock_cache = MagicMock()
        mock_cache.get_track_info.return_value = None
        monkeypatch.setattr(server, "get_track_cache", lambda: mock_cache)
        out = server._playlist_remove(playlist="Rock", track="1234567890")
        assert "Not in cache" in out or "failed" in out.lower()

    def test_remove_by_library_id_in_cache(self, monkeypatch):
        """Lines 7729-7745: LIBRARY_ID type in cache."""
        monkeypatch.setattr(
            server,
            "_resolve_playlist",
            lambda n: ResolvedPlaylist(applescript_name="Rock", raw_input="Rock"),
        )
        monkeypatch.setattr(
            server,
            "_resolve_track",
            lambda t, a="": [ResolvedInput(input_type=InputType.LIBRARY_ID, value="i.ABC123")],
        )
        mock_cache = MagicMock()
        mock_cache.get_track_info.return_value = {"name": "Strobe", "artist": "deadmau5"}
        monkeypatch.setattr(server, "get_track_cache", lambda: mock_cache)
        monkeypatch.setattr(
            server.asc,
            "remove_track_from_playlist",
            lambda pl, track_name=None, artist=None, track_id=None: (True, "Removed Strobe"),
        )
        monkeypatch.setattr(server, "_verify_track_not_in_playlist", lambda pl, n, a: True)
        out = server._playlist_remove(playlist="Rock", track="i.ABC123", verify=True)
        assert "removed" in out.lower()

    def test_remove_by_library_id_not_in_cache(self, monkeypatch):
        """Line 7746: LIBRARY_ID not in cache."""
        monkeypatch.setattr(
            server,
            "_resolve_playlist",
            lambda n: ResolvedPlaylist(applescript_name="Rock", raw_input="Rock"),
        )
        monkeypatch.setattr(
            server,
            "_resolve_track",
            lambda t, a="": [ResolvedInput(input_type=InputType.LIBRARY_ID, value="i.ABC123")],
        )
        mock_cache = MagicMock()
        mock_cache.get_track_info.return_value = None
        monkeypatch.setattr(server, "get_track_cache", lambda: mock_cache)
        out = server._playlist_remove(playlist="Rock", track="i.ABC123")
        assert "Not in cache" in out or "failed" in out.lower()


# ===========================================================================
# _library_remove  (lines 7768-7884)
# ===========================================================================


class TestLibraryRemove:
    def test_no_track_error(self):
        """Lines 7774-7775: no track param."""
        out = server._library_remove(track="")
        assert "Error" in out and "track" in out.lower()

    def test_remove_by_name_no_verify(self, monkeypatch):
        """Lines 7806-7807: verify=False -> skip _verify_gone."""
        monkeypatch.setattr(
            server,
            "_resolve_track",
            lambda t, a="": [
                ResolvedInput(input_type=InputType.NAME, value="Strobe", artist="deadmau5")
            ],
        )
        monkeypatch.setattr(
            server.asc,
            "remove_from_library",
            lambda track_name=None, artist=None, track_id=None: (True, "Removed Strobe"),
        )
        out = server._library_remove(track="Strobe", verify=False)
        assert "removed" in out.lower()

    def test_remove_verify_track_gone(self, monkeypatch):
        """Lines 7808-7809: verify=True, _verify_gone returns True (track gone)."""
        monkeypatch.setattr(
            server,
            "_resolve_track",
            lambda t, a="": [
                ResolvedInput(input_type=InputType.NAME, value="Strobe", artist="deadmau5")
            ],
        )
        monkeypatch.setattr(
            server.asc,
            "remove_from_library",
            lambda track_name=None, artist=None, track_id=None: (True, "Removed Strobe"),
        )
        # search_library returns empty -> _verify_gone returns True immediately
        monkeypatch.setattr(server.asc, "search_library", lambda q, t: (True, []))
        out = server._library_remove(track="Strobe", verify=True)
        assert "removed" in out.lower()

    def test_remove_verify_track_still_there(self, monkeypatch):
        """Lines 7811-7817: verify=True, _verify_gone returns False (still in library)."""
        monkeypatch.setattr(
            server,
            "_resolve_track",
            lambda t, a="": [
                ResolvedInput(input_type=InputType.NAME, value="Strobe", artist="deadmau5")
            ],
        )
        monkeypatch.setattr(
            server.asc,
            "remove_from_library",
            lambda track_name=None, artist=None, track_id=None: (True, "Removed Strobe"),
        )
        # Always return the track -> _verify_gone returns False after all attempts
        monkeypatch.setattr(
            server.asc,
            "search_library",
            lambda q, t: (True, [{"name": "Strobe", "artist": "deadmau5"}]),
        )
        monkeypatch.setattr(time_module, "sleep", lambda s: None)
        out = server._library_remove(track="Strobe", artist="deadmau5", verify=True)
        assert "still" in out.lower() or "failed" in out.lower()

    def test_verify_gone_search_fails(self, monkeypatch):
        """Line 7786: search_library returns ok=False -> _verify_gone returns True."""
        monkeypatch.setattr(
            server,
            "_resolve_track",
            lambda t, a="": [
                ResolvedInput(input_type=InputType.NAME, value="Strobe", artist="deadmau5")
            ],
        )
        monkeypatch.setattr(
            server.asc,
            "remove_from_library",
            lambda track_name=None, artist=None, track_id=None: (True, "Removed"),
        )
        monkeypatch.setattr(server.asc, "search_library", lambda q, t: (False, "AS error"))
        out = server._library_remove(track="Strobe", verify=True)
        assert "removed" in out.lower()

    def test_verify_gone_artist_mismatch(self, monkeypatch):
        """Lines 7789-7795: track_artist set, but lib_results artist doesn't match -> True."""
        monkeypatch.setattr(
            server,
            "_resolve_track",
            lambda t, a="": [
                ResolvedInput(input_type=InputType.NAME, value="Strobe", artist="deadmau5")
            ],
        )
        monkeypatch.setattr(
            server.asc,
            "remove_from_library",
            lambda track_name=None, artist=None, track_id=None: (True, "Removed"),
        )
        # Returns track with different artist -> no match -> _verify_gone returns True
        monkeypatch.setattr(
            server.asc,
            "search_library",
            lambda q, t: (True, [{"name": "Strobe", "artist": "Completely Different"}]),
        )
        out = server._library_remove(track="Strobe", artist="deadmau5", verify=True)
        assert "removed" in out.lower()

    def test_remove_asc_fails(self, monkeypatch):
        """Lines 7803-7805: asc.remove_from_library fails."""
        monkeypatch.setattr(
            server,
            "_resolve_track",
            lambda t, a="": [ResolvedInput(input_type=InputType.NAME, value="Strobe", artist="")],
        )
        monkeypatch.setattr(
            server.asc,
            "remove_from_library",
            lambda track_name=None, artist=None, track_id=None: (False, "Not found"),
        )
        out = server._library_remove(track="Strobe", verify=False)
        assert "failed" in out.lower() or "Not found" in out

    def test_remove_resolved_error(self, monkeypatch):
        """Line 7823-7825: ResolvedInput has error."""
        monkeypatch.setattr(
            server,
            "_resolve_track",
            lambda t, a="": [ResolvedInput(input_type=InputType.NAME, value="", error="Bad input")],
        )
        out = server._library_remove(track="badtrack")
        assert "failed" in out.lower() or "Bad input" in out

    def test_remove_by_persistent_id(self, monkeypatch):
        """Lines 7827-7831: PERSISTENT_ID, verify skipped (name=None)."""
        monkeypatch.setattr(
            server,
            "_resolve_track",
            lambda t, a="": [
                ResolvedInput(input_type=InputType.PERSISTENT_ID, value="ABC123DEF456")
            ],
        )
        monkeypatch.setattr(
            server.asc,
            "remove_from_library",
            lambda track_name=None, artist=None, track_id=None: (True, "Removed"),
        )
        out = server._library_remove(track="ABC123DEF456", verify=True)
        assert "removed" in out.lower()

    def test_remove_by_catalog_id_in_cache(self, monkeypatch):
        """Lines 7833-7846: CATALOG_ID in track cache."""
        monkeypatch.setattr(
            server,
            "_resolve_track",
            lambda t, a="": [ResolvedInput(input_type=InputType.CATALOG_ID, value="1234567890")],
        )
        mock_cache = MagicMock()
        mock_cache.get_track_info.return_value = {"name": "Strobe", "artist": "deadmau5"}
        monkeypatch.setattr(server, "get_track_cache", lambda: mock_cache)
        monkeypatch.setattr(
            server.asc,
            "remove_from_library",
            lambda track_name=None, artist=None, track_id=None: (True, "Removed Strobe"),
        )
        monkeypatch.setattr(server.asc, "search_library", lambda q, t: (True, []))
        out = server._library_remove(track="1234567890", verify=True)
        assert "removed" in out.lower()

    def test_remove_by_catalog_id_not_in_cache(self, monkeypatch):
        """Line 7848: CATALOG_ID not in cache."""
        monkeypatch.setattr(
            server,
            "_resolve_track",
            lambda t, a="": [ResolvedInput(input_type=InputType.CATALOG_ID, value="1234567890")],
        )
        mock_cache = MagicMock()
        mock_cache.get_track_info.return_value = None
        monkeypatch.setattr(server, "get_track_cache", lambda: mock_cache)
        out = server._library_remove(track="1234567890")
        assert "Not in cache" in out or "failed" in out.lower()

    def test_remove_by_library_id_in_cache(self, monkeypatch):
        """Lines 7850-7863: LIBRARY_ID in cache."""
        monkeypatch.setattr(
            server,
            "_resolve_track",
            lambda t, a="": [ResolvedInput(input_type=InputType.LIBRARY_ID, value="i.ABC123")],
        )
        mock_cache = MagicMock()
        mock_cache.get_track_info.return_value = {"name": "Strobe", "artist": "deadmau5"}
        monkeypatch.setattr(server, "get_track_cache", lambda: mock_cache)
        monkeypatch.setattr(
            server.asc,
            "remove_from_library",
            lambda track_name=None, artist=None, track_id=None: (True, "Removed Strobe"),
        )
        monkeypatch.setattr(server.asc, "search_library", lambda q, t: (True, []))
        out = server._library_remove(track="i.ABC123", verify=True)
        assert "removed" in out.lower()

    def test_remove_by_library_id_not_in_cache(self, monkeypatch):
        """Line 7865: LIBRARY_ID not in cache."""
        monkeypatch.setattr(
            server,
            "_resolve_track",
            lambda t, a="": [ResolvedInput(input_type=InputType.LIBRARY_ID, value="i.ABC123")],
        )
        mock_cache = MagicMock()
        mock_cache.get_track_info.return_value = None
        monkeypatch.setattr(server, "get_track_cache", lambda: mock_cache)
        out = server._library_remove(track="i.ABC123")
        assert "Not in cache" in out or "failed" in out.lower()


# ===========================================================================
# _playlist_delete  (lines 7888-7911)
# ===========================================================================


class TestPlaylistDelete:
    def test_success(self, monkeypatch):
        """Lines 7893-7910: successful deletion."""
        monkeypatch.setattr(
            server.asc,
            "get_playlist_tracks",
            lambda n: (True, [{"name": "T1", "artist": "A1"}, {"name": "T2", "artist": "A2"}]),
        )
        monkeypatch.setattr(
            server.asc, "delete_playlist", lambda n: (True, "Deleted Rock Classics")
        )
        out = server._playlist_delete("Rock Classics")
        assert "Deleted Rock Classics" in out

    def test_get_tracks_fails_but_delete_succeeds(self, monkeypatch):
        """Lines 7893-7910: get_playlist_tracks fails -> track_count=0, but delete ok."""
        monkeypatch.setattr(server.asc, "get_playlist_tracks", lambda n: (False, []))
        monkeypatch.setattr(server.asc, "delete_playlist", lambda n: (True, "Deleted"))
        out = server._playlist_delete("Rock Classics")
        assert "Deleted" in out

    def test_delete_failure(self, monkeypatch):
        """Line 7911: delete_playlist fails."""
        monkeypatch.setattr(server.asc, "get_playlist_tracks", lambda n: (False, []))
        monkeypatch.setattr(server.asc, "delete_playlist", lambda n: (False, "Playlist not found"))
        out = server._playlist_delete("Nonexistent")
        assert "Error" in out and "not found" in out.lower()

    def test_many_tracks_truncated(self, monkeypatch):
        """Lines 7895-7896: >20 tracks, track_names is sliced to 20."""
        tracks = [{"name": f"T{i}", "artist": "A"} for i in range(30)]
        monkeypatch.setattr(server.asc, "get_playlist_tracks", lambda n: (True, tracks))
        monkeypatch.setattr(server.asc, "delete_playlist", lambda n: (True, "Deleted"))
        out = server._playlist_delete("Big Playlist")
        assert "Deleted" in out


# ===========================================================================
# _playlist_rename  (lines 7913-7929)
# ===========================================================================


class TestPlaylistRename:
    def test_no_playlist_name_error(self):
        """Line 7916: empty playlist name."""
        out = server._playlist_rename("", "New Name")
        assert "Error" in out and "name" in out.lower()

    def test_no_new_name_error(self):
        """Line 7918: empty new_name."""
        out = server._playlist_rename("Old Name", "")
        assert "Error" in out and "new_name" in out.lower()

    def test_rename_success(self, monkeypatch):
        """Lines 7920-7928: successful rename."""
        monkeypatch.setattr(
            server.asc, "rename_playlist", lambda n, new: (True, f"Renamed to {new}")
        )
        out = server._playlist_rename("Old Name", "New Name")
        assert "Renamed to New Name" in out

    def test_rename_failure(self, monkeypatch):
        """Line 7929: rename_playlist fails."""
        monkeypatch.setattr(server.asc, "rename_playlist", lambda n, new: (False, "Not found"))
        out = server._playlist_rename("Old Name", "New Name")
        assert "Error" in out and "Not found" in out


# ===========================================================================
# _playback_reveal  (lines 7931-7936)
# ===========================================================================


class TestPlaybackReveal:
    def _setup(self, monkeypatch):
        _native(monkeypatch)

    def test_reveal_success(self, monkeypatch):
        """Lines 7933-7935: reveal track in Music.app."""
        self._setup(monkeypatch)
        monkeypatch.setattr(server.asc, "reveal_track", lambda n, a=None: (True, f"Revealed {n}"))
        out = server.playback(action="reveal", track="Strobe")
        assert "Revealed Strobe" in out

    def test_reveal_with_artist(self, monkeypatch):
        """Lines 7933-7935: reveal with artist param."""
        self._setup(monkeypatch)
        seen = {}
        monkeypatch.setattr(
            server.asc,
            "reveal_track",
            lambda n, a=None: seen.update(n=n, a=a) or (True, "Revealed"),
        )
        server.playback(action="reveal", track="Strobe", artist="deadmau5")
        assert seen.get("a") == "deadmau5"

    def test_reveal_failure(self, monkeypatch):
        """Line 7936: reveal fails."""
        self._setup(monkeypatch)
        monkeypatch.setattr(
            server.asc, "reveal_track", lambda n, a=None: (False, "Track not found")
        )
        out = server.playback(action="reveal", track="Strobe")
        assert "Error" in out and "Track not found" in out

    def test_reveal_track_name_param(self, monkeypatch):
        """Lines 7041-7042: track_name param overrides track."""
        self._setup(monkeypatch)
        seen = {}
        monkeypatch.setattr(
            server.asc, "reveal_track", lambda n, a=None: seen.update(n=n) or (True, "Revealed")
        )
        server.playback(action="reveal", track_name="Explicit Name", track="Ignored")
        assert seen.get("n") == "Explicit Name"


# ===========================================================================
# _playback_airplay  (lines 7938-7952)
# ===========================================================================


class TestPlaybackAirplay:
    def _setup(self, monkeypatch):
        _native(monkeypatch)

    def test_list_devices(self, monkeypatch):
        """Lines 7947-7952: list AirPlay devices."""
        self._setup(monkeypatch)
        monkeypatch.setattr(
            server.asc, "get_airplay_devices", lambda: (True, ["Living Room", "Kitchen"])
        )
        out = server.playback(action="airplay")
        assert "Living Room" in out and "Kitchen" in out

    def test_no_devices(self, monkeypatch):
        """Lines 7950-7951: no AirPlay devices."""
        self._setup(monkeypatch)
        monkeypatch.setattr(server.asc, "get_airplay_devices", lambda: (True, []))
        out = server.playback(action="airplay")
        assert "No AirPlay" in out

    def test_get_devices_error(self, monkeypatch):
        """Lines 7948-7949: get_airplay_devices fails."""
        self._setup(monkeypatch)
        monkeypatch.setattr(server.asc, "get_airplay_devices", lambda: (False, "AirPlay error"))
        out = server.playback(action="airplay")
        assert "Error" in out and "AirPlay error" in out

    def test_switch_device_success(self, monkeypatch):
        """Lines 7940-7944: switch to named AirPlay device."""
        self._setup(monkeypatch)
        monkeypatch.setattr(server.asc, "set_airplay_device", lambda d: (True, f"Switched to {d}"))
        out = server.playback(action="airplay", device_name="Living Room")
        assert "Switched to Living Room" in out

    def test_switch_device_failure(self, monkeypatch):
        """Line 7945: set_airplay_device fails."""
        self._setup(monkeypatch)
        monkeypatch.setattr(server.asc, "set_airplay_device", lambda d: (False, "Device not found"))
        out = server.playback(action="airplay", device_name="Nonexistent")
        assert "Error" in out and "Device not found" in out


# ===========================================================================
# Remaining missing lines — filter continues, retry sleeps, macos_only gating
# ===========================================================================


class TestRemainingCoverage:
    """Mop-up tests for lines still missing after the first pass."""

    def _native(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_use_browser_playback", lambda: False)

    # --- Lines 7056, 7060: _macos_only returns error for reveal / airplay ---

    def test_reveal_macos_only_gate(self, monkeypatch):
        """Line 7056: reveal with no AppleScript and no browser -> _macos_only error."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        monkeypatch.setattr(server, "_use_browser_playback", lambda: False)
        out = server.playback(action="reveal", track="Strobe")
        assert "macOS" in out or "Error" in out

    def test_airplay_macos_only_gate(self, monkeypatch):
        """Line 7060: airplay with no AppleScript -> _macos_only error."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        monkeypatch.setattr(server, "_use_browser_playback", lambda: False)
        out = server.playback(action="airplay")
        assert "macOS" in out or "Error" in out

    # --- Lines 7288, 7290: album in library, filter continues ---

    def test_album_library_album_name_mismatch_skips(self, monkeypatch):
        """Line 7288: library result album name doesn't match -> continue."""
        self._native(monkeypatch)
        # Returns track with wrong album name, then empty
        monkeypatch.setattr(
            server.asc,
            "search_library",
            lambda q, t: (True, [{"name": "T", "album": "Wrong Album", "artist": "Beatles"}]),
        )
        monkeypatch.setattr(server, "get_headers", lambda: {})
        monkeypatch.setattr(server, "get_storefront", lambda: "us")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"results": {"albums": {"data": []}}}
        monkeypatch.setattr(server.requests, "get", lambda *a, **k: mock_resp)
        out = server.playback(action="play", album="Abbey Road", artist="Beatles")
        assert "not found" in out.lower() or "Album not found" in out

    def test_album_library_artist_mismatch_skips(self, monkeypatch):
        """Line 7290: library result album matches but artist doesn't -> continue."""
        self._native(monkeypatch)
        monkeypatch.setattr(
            server.asc,
            "search_library",
            lambda q, t: (True, [{"name": "T", "album": "Abbey Road", "artist": "Rolling Stones"}]),
        )
        monkeypatch.setattr(server, "get_headers", lambda: {})
        monkeypatch.setattr(server, "get_storefront", lambda: "us")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"results": {"albums": {"data": []}}}
        monkeypatch.setattr(server.requests, "get", lambda *a, **k: mock_resp)
        out = server.playback(action="play", album="Abbey Road", artist="Beatles")
        assert "not found" in out.lower() or "Album not found" in out

    # --- Line 7321: catalog album name doesn't match -> continue ---

    def test_album_catalog_name_mismatch_skips(self, monkeypatch):
        """Line 7321: catalog album returned but name doesn't match -> skipped."""
        self._native(monkeypatch)
        monkeypatch.setattr(server.asc, "search_library", lambda q, t: (True, []))
        monkeypatch.setattr(server, "get_headers", lambda: {})
        monkeypatch.setattr(server, "get_storefront", lambda: "us")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "results": {
                "albums": {
                    "data": [
                        {
                            "id": "alb1",
                            "attributes": {
                                "name": "Completely Different Album",
                                "artistName": "Beatles",
                                "url": "https://...",
                            },
                        }
                    ]
                }
            }
        }
        monkeypatch.setattr(server.requests, "get", lambda *a, **k: mock_resp)
        out = server.playback(action="play", album="Abbey Road")
        assert "not found" in out.lower() or "Album not found" in out

    # --- Lines 7334, 7345, 7360: add_to_library album retry loop ---

    def test_album_add_retry_sleep_and_play_fail_break(self, monkeypatch):
        """Lines 7334, 7360: retry loop runs 2 attempts; play_track fails -> break."""
        self._native(monkeypatch)
        monkeypatch.setattr(server, "_add_album_to_library", lambda aid: (True, "Added"))
        monkeypatch.setattr(time_module, "sleep", lambda s: None)
        monkeypatch.setattr(server, "PLAY_TRACK_MAX_ATTEMPTS", 2)
        monkeypatch.setattr(server, "get_headers", lambda: {})
        monkeypatch.setattr(server, "get_storefront", lambda: "us")

        call_count = [0]

        def search_lib(q, t):
            call_count[0] += 1
            # First call (initial search) returns nothing, subsequent calls return the album
            if call_count[0] <= 1:
                return True, []
            return True, [{"name": "Track1", "album": "Abbey Road", "artist": "Beatles"}]

        monkeypatch.setattr(server.asc, "search_library", search_lib)
        # play_track always fails -> triggers break at line 7360
        monkeypatch.setattr(server.asc, "play_track", lambda n, a: (False, "busy"))
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "results": {
                "albums": {
                    "data": [
                        {
                            "id": "alb1",
                            "attributes": {
                                "name": "Abbey Road",
                                "artistName": "Beatles",
                                "url": "https://...",
                            },
                        }
                    ]
                }
            }
        }
        monkeypatch.setattr(server.requests, "get", lambda *a, **k: mock_resp)
        out = server.playback(action="play", album="Abbey Road", add_to_library=True)
        # Falls through to "sync pending" after loop
        assert "sync pending" in out.lower() or "Abbey Road" in out

    def test_album_add_with_shuffle_in_retry(self, monkeypatch):
        """Line 7345: shuffle=True in the add_to_library retry loop."""
        self._native(monkeypatch)
        monkeypatch.setattr(server, "_add_album_to_library", lambda aid: (True, "Added"))
        monkeypatch.setattr(time_module, "sleep", lambda s: None)
        monkeypatch.setattr(server, "PLAY_TRACK_MAX_ATTEMPTS", 1)
        monkeypatch.setattr(server, "get_headers", lambda: {})
        monkeypatch.setattr(server, "get_storefront", lambda: "us")

        call_count = [0]

        def search_lib(q, t):
            call_count[0] += 1
            if call_count[0] <= 1:
                return True, []
            return True, [{"name": "Track1", "album": "Abbey Road", "artist": "Beatles"}]

        monkeypatch.setattr(server.asc, "search_library", search_lib)
        shuffle_set = []
        monkeypatch.setattr(
            server.asc, "set_shuffle", lambda e: shuffle_set.append(e) or (True, "OK")
        )
        monkeypatch.setattr(server.asc, "play_track", lambda n, a: (True, "Playing"))
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "results": {
                "albums": {
                    "data": [
                        {
                            "id": "alb1",
                            "attributes": {
                                "name": "Abbey Road",
                                "artistName": "Beatles",
                                "url": "https://...",
                            },
                        }
                    ]
                }
            }
        }
        monkeypatch.setattr(server.requests, "get", lambda *a, **k: mock_resp)
        out = server.playback(action="play", album="Abbey Road", add_to_library=True, shuffle=True)
        assert shuffle_set == [True]
        assert "shuffled" in out or "Abbey Road" in out

    # --- Line 7412: catalog ID add retry sleep ---

    def test_catalog_id_add_retry_sleep(self, monkeypatch):
        """Line 7412: retry loop has attempt>0 -> time.sleep called."""
        self._native(monkeypatch)
        monkeypatch.setattr(server, "get_headers", lambda: {})
        monkeypatch.setattr(server, "get_storefront", lambda: "us")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data": [
                {"attributes": {"name": "Strobe", "artistName": "deadmau5", "url": "https://..."}}
            ]
        }
        monkeypatch.setattr(server.requests, "get", lambda *a, **k: mock_resp)
        monkeypatch.setattr(server, "_add_songs_to_library", lambda ids: (True, "Added"))
        monkeypatch.setattr(time_module, "sleep", lambda s: None)
        monkeypatch.setattr(server, "PLAY_TRACK_MAX_ATTEMPTS", 2)

        attempt_count = [0]

        def play_track(n, a):
            attempt_count[0] += 1
            if attempt_count[0] < 2:
                return False, "not ready"
            return True, "Playing"

        monkeypatch.setattr(server.asc, "play_track", play_track)
        out = server.playback(action="play", track="1234567890", add_to_library=True)
        assert attempt_count[0] == 2
        assert "Playing" in out or "Strobe" in out

    # --- Lines 7446, 7448: library track filter continues ---

    def test_track_library_name_mismatch_skips(self, monkeypatch):
        """Line 7446: library track name doesn't match -> continue."""
        self._native(monkeypatch)
        # First result has wrong name, second has right name
        monkeypatch.setattr(
            server.asc,
            "search_library",
            lambda q, t: (
                True,
                [
                    {"name": "Different Song", "artist": "deadmau5"},
                    {"name": "Strobe", "artist": "deadmau5"},
                ],
            ),
        )
        monkeypatch.setattr(server.asc, "play_track", lambda n, a: (True, f"Playing {n}"))
        out = server.playback(action="play", track="Strobe")
        assert "[Library]" in out and "Playing Strobe" in out

    def test_track_library_artist_mismatch_skips(self, monkeypatch):
        """Line 7448: library track name matches but artist doesn't -> continue."""
        self._native(monkeypatch)
        # First result: name matches but wrong artist; no second match
        monkeypatch.setattr(
            server.asc,
            "search_library",
            lambda q, t: (True, [{"name": "Strobe", "artist": "Wrong Artist"}]),
        )
        # Falls through to catalog search -> nothing found
        monkeypatch.setattr(server, "_search_catalog_songs", lambda t, limit=5: [])
        monkeypatch.setattr(server.asc, "ui_play_result_by_query", lambda q: (False, "Not found"))
        out = server.playback(action="play", track="Strobe", artist="deadmau5")
        assert "not found" in out.lower()

    # --- Lines 7470, 7477: catalog song filter continues ---

    def test_track_catalog_name_mismatch_skips(self, monkeypatch):
        """Line 7470: catalog song name doesn't match -> continue."""
        self._native(monkeypatch)
        monkeypatch.setattr(server.asc, "search_library", lambda q, t: (True, []))
        # Return a song with wrong name, then one with right name
        monkeypatch.setattr(
            server,
            "_search_catalog_songs",
            lambda t, limit=5: [
                {
                    "id": "1",
                    "attributes": {
                        "name": "Completely Different",
                        "artistName": "deadmau5",
                        "url": "https://...",
                    },
                },
                {
                    "id": "2",
                    "attributes": {
                        "name": "Strobe",
                        "artistName": "deadmau5",
                        "url": "https://url",
                    },
                },
            ],
        )
        monkeypatch.setattr(
            server, "_try_ui_catalog_play", lambda n, a, **k: (True, "[UI Catalog] Playing")
        )
        out = server.playback(action="play", track="Strobe", artist="deadmau5")
        assert "[UI Catalog]" in out

    def test_track_catalog_artist_mismatch_skips(self, monkeypatch):
        """Line 7477: catalog song name matches but artist doesn't -> continue."""
        self._native(monkeypatch)
        monkeypatch.setattr(server.asc, "search_library", lambda q, t: (True, []))
        # Song name matches but artist is wrong -> filtered out
        monkeypatch.setattr(
            server,
            "_search_catalog_songs",
            lambda t, limit=5: [
                {
                    "id": "1",
                    "attributes": {
                        "name": "Strobe",
                        "artistName": "Wrong Artist",
                        "url": "https://...",
                    },
                },
            ],
        )
        monkeypatch.setattr(server.asc, "ui_play_result_by_query", lambda q: (False, "Not found"))
        out = server.playback(action="play", track="Strobe", artist="deadmau5")
        assert "not found" in out.lower()

    # --- Line 7490: track add retry sleep ---

    def test_track_catalog_add_retry_sleep(self, monkeypatch):
        """Line 7490: track add retry loop, attempt>0 -> sleep called."""
        self._native(monkeypatch)
        monkeypatch.setattr(server.asc, "search_library", lambda q, t: (True, []))
        monkeypatch.setattr(
            server,
            "_search_catalog_songs",
            lambda t, limit=5: [
                {
                    "id": "111",
                    "attributes": {
                        "name": "Strobe",
                        "artistName": "deadmau5",
                        "url": "https://...",
                    },
                }
            ],
        )
        monkeypatch.setattr(server, "_add_songs_to_library", lambda ids: (True, "Added"))
        monkeypatch.setattr(time_module, "sleep", lambda s: None)
        monkeypatch.setattr(server, "PLAY_TRACK_MAX_ATTEMPTS", 2)

        attempt_count = [0]

        def play_track(n, a):
            attempt_count[0] += 1
            if attempt_count[0] < 2:
                return False, "not ready"
            return True, "Playing"

        monkeypatch.setattr(server.asc, "play_track", play_track)
        out = server.playback(action="play", track="Strobe", artist="deadmau5", add_to_library=True)
        assert attempt_count[0] == 2
        assert "Playing" in out or "Strobe" in out
