"""Tests targeting library tool coverage slice in server.py.

Covers:
- library() dispatcher (~4104-4176)
- _library_search (~4176)
- _resolve_catalog_track_itunes (~4330)
- _verify_in_library (~4394)
- _library_add (~4419)
- _library_recently_played (~4546)
- _build_library_track_data (~4973)
- _library_browse (~5009)
- _library_favorites (~5173)
- _library_recently_added (~5305)
- _library_rate (~5630)
"""

import json
import time
import types
from unittest.mock import MagicMock, patch

import pytest
import requests
import responses

from applemusic_mcp import server

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_tokens(config_dir, dev_token="TEST_DEV_TOKEN", user_token="TEST_USER_TOKEN"):
    """Write token files used by get_headers() / _can_use_library_api()."""
    (config_dir / "developer_token.json").write_text(
        json.dumps({"token": dev_token, "expires": time.time() + 86400 * 60})
    )
    (config_dir / "music_user_token.json").write_text(json.dumps({"music_user_token": user_token}))


def _default_prefs(**overrides):
    prefs = {
        "mode": "native",
        "playback": "native",
        "fetch_explicit": False,
        "clean_only": False,
        "auto_add": True,
        "storefront": "us",
        "output_format": "text",
    }
    prefs.update(overrides)
    return prefs


def _make_song_data(
    name="Song A",
    artist="Artist A",
    album="Album A",
    duration="3:00",
    genre="Pop",
    year="2021",
    track_id="i.abc123",
    explicit="No",
):
    """Make a dict resembling an AppleScript library song result."""
    return {
        "name": name,
        "artist": artist,
        "album": album,
        "duration": duration,
        "genre": genre,
        "year": year,
        "id": track_id,
        "explicit": explicit,
    }


def _make_api_song(
    name="Api Song",
    artist="Api Artist",
    catalog_id="1234567890",
    album="Api Album",
    duration_ms=180000,
    explicit=False,
):
    return {
        "id": catalog_id,
        "type": "songs",
        "attributes": {
            "name": name,
            "artistName": artist,
            "albumName": album,
            "durationInMillis": duration_ms,
            "contentRating": "explicit" if explicit else "",
            "releaseDate": "2022-01-01",
            "genreNames": ["Pop"],
        },
    }


# ============================================================================
# library() DISPATCHER
# ============================================================================


class TestLibraryDispatcher:
    def test_search_requires_query(self, monkeypatch):
        monkeypatch.setattr(server, "get_user_preferences", lambda: _default_prefs())
        result = server.library(action="search", query="")
        assert "Error" in result
        assert "query" in result.lower()

    def test_rate_requires_rate_action(self, monkeypatch):
        monkeypatch.setattr(server, "get_user_preferences", lambda: _default_prefs())
        result = server.library(action="rate", track="Some Track", rate_action="")
        assert "Error" in result
        assert "rate_action" in result

    def test_favorites_requires_macos(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        monkeypatch.setattr(server, "get_user_preferences", lambda: _default_prefs())
        result = server.library(action="favorites")
        assert "Error" in result
        assert "macOS" in result

    def test_loved_alias_requires_macos(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        monkeypatch.setattr(server, "get_user_preferences", lambda: _default_prefs())
        result = server.library(action="loved")
        assert "Error" in result
        assert "macOS" in result

    def test_snapshot_requires_macos(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        monkeypatch.setattr(server, "get_user_preferences", lambda: _default_prefs())
        result = server.library(action="snapshot", query="new")
        assert "Error" in result
        assert "macOS" in result

    def test_unknown_action(self, monkeypatch):
        monkeypatch.setattr(server, "get_user_preferences", lambda: _default_prefs())
        result = server.library(action="bogus")
        assert "Unknown action" in result
        assert "bogus" in result

    def test_action_normalization_dash_to_underscore(self, monkeypatch):
        """recently-played should normalize to recently_played."""
        called = []

        def fake_recently_played(limit, format, export, full):
            called.append(True)
            return "OK recently played"

        monkeypatch.setattr(server, "_library_recently_played", fake_recently_played)
        monkeypatch.setattr(server, "get_user_preferences", lambda: _default_prefs())
        result = server.library(action="recently-played")
        assert called
        assert "OK" in result

    def test_remove_routes_to_api_when_engine_api(self, monkeypatch):
        called = []

        def fake_remove_api(track, artist):
            called.append((track, artist))
            return "Removed via API"

        monkeypatch.setattr(server, "_library_remove_api", fake_remove_api)
        monkeypatch.setattr(server, "_write_rail", lambda *a, **k: "web")
        monkeypatch.setattr(server, "get_user_preferences", lambda: _default_prefs())
        result = server.library(action="remove", track="MySong", artist="Artist")
        assert called
        assert "Removed" in result

    def test_remove_off_macos_routes_to_web(self, monkeypatch):
        """Off macOS, library remove routes to the web rail (not a macOS error)."""
        monkeypatch.setattr(server, "_write_rail", lambda *a, **k: "web")
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        monkeypatch.setattr(server, "_library_remove_api", lambda t, a: "Removed via API")
        monkeypatch.setattr(server, "get_user_preferences", lambda: _default_prefs())
        result = server.library(action="remove", track="MySong")
        assert "Removed" in result and "macOS" not in result

    def test_snapshot_new_routes_correctly(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "get_user_preferences", lambda: _default_prefs())
        monkeypatch.setattr(server, "_library_snapshot_new", lambda: "snapshot new result")
        result = server.library(action="snapshot", query="new")
        assert "snapshot new result" == result

    def test_snapshot_history_routes_correctly(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "get_user_preferences", lambda: _default_prefs())
        monkeypatch.setattr(server, "_library_history", lambda: "history result")
        result = server.library(action="snapshot", query="history")
        assert "history result" == result

    def test_snapshot_list_routes_correctly(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "get_user_preferences", lambda: _default_prefs())
        monkeypatch.setattr(server, "_library_snapshot_list", lambda: "list result")
        result = server.library(action="snapshot", query="list")
        assert "list result" == result

    def test_snapshot_delete_routes_correctly(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "get_user_preferences", lambda: _default_prefs())
        monkeypatch.setattr(server, "_library_snapshot_delete", lambda f: f"deleted {f}")
        result = server.library(action="snapshot", query="delete myfile.json")
        assert "deleted" in result
        assert "myfile.json" in result

    def test_snapshot_default_routes_correctly(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "get_user_preferences", lambda: _default_prefs())
        monkeypatch.setattr(server, "_library_snapshot_default", lambda: "default result")
        result = server.library(action="snapshot", query="")
        assert "default result" == result

    def test_recently_added_routing(self, monkeypatch):
        """library(action='recently_added') routes to _library_recently_added (line 4137)."""
        called = []

        def fake_recently_added(limit, format, export, full):
            called.append(True)
            return "recently added result"

        monkeypatch.setattr(server, "_library_recently_added", fake_recently_added)
        monkeypatch.setattr(server, "get_user_preferences", lambda: _default_prefs())
        result = server.library(action="recently_added")
        assert called
        assert "recently added result" == result

    def test_remove_native_path(self, monkeypatch):
        """library(action='remove') on macOS routes to _library_remove (native rail)."""
        monkeypatch.setattr(server, "_write_rail", lambda *a, **k: "native")
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_library_remove", lambda track, artist: "Removed natively")
        monkeypatch.setattr(server, "get_user_preferences", lambda: _default_prefs())
        result = server.library(action="remove", track="Song A")
        assert "Removed natively" in result


# ============================================================================
# _library_search
# ============================================================================


class TestLibrarySearch:
    def test_native_success_returns_formatted(self, monkeypatch):
        songs = [_make_song_data()]
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_engine", lambda: "native")
        monkeypatch.setattr(
            server.asc, "search_library_page", lambda q, t, offset, limit: (True, songs, 1, "")
        )
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: _default_prefs(fetch_explicit=False, clean_only=False),
        )
        result = server._library_search("Song A", types="songs")
        assert "Song A" in result

    def test_native_empty_no_token(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_engine", lambda: "native")
        monkeypatch.setattr(
            server.asc, "search_library_page", lambda q, t, offset, limit: (True, [], 0, "")
        )
        monkeypatch.setattr(server, "_has_developer_token", lambda: False)
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: _default_prefs(fetch_explicit=False, clean_only=False),
        )
        result = server._library_search("Missing Song", types="songs")
        assert "No" in result

    def test_native_empty_genre_returns_not_found(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_engine", lambda: "native")
        monkeypatch.setattr(
            server.asc, "search_library_page", lambda q, t, offset, limit: (True, [], 0, "")
        )
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: _default_prefs(fetch_explicit=False, clean_only=False),
        )
        result = server._library_search("Jazz", types="genre")
        assert "No tracks" in result
        assert "Jazz" in result

    def test_genre_without_native_returns_message(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        monkeypatch.setattr(server, "_engine", lambda: "api")
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: _default_prefs(fetch_explicit=False, clean_only=False),
        )
        result = server._library_search("Rock", types="genre")
        assert "genre" in result.lower()
        assert "macOS" in result or "native" in result or "isn't available" in result

    def test_genre_with_asc_error_shows_hint(self, monkeypatch):
        """Genre search via AS fails; asc_error appears in the message."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_engine", lambda: "native")
        monkeypatch.setattr(
            server.asc,
            "search_library_page",
            lambda q, t, offset, limit: (False, [], 0, "Music.app timeout"),
        )
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: _default_prefs(fetch_explicit=False, clean_only=False),
        )
        result = server._library_search("Blues", types="genre")
        assert "Music.app timeout" in result

    @responses.activate
    def test_api_fallback_returns_songs(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        monkeypatch.setattr(server, "_engine", lambda: "api")
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: _default_prefs(fetch_explicit=False, clean_only=False),
        )
        api_song = _make_api_song("Rock Song", "Rock Band", "1234567890")
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/search",
            json={"results": {"library-songs": {"data": [api_song]}}},
            status=200,
        )
        result = server._library_search("Rock Song")
        assert "Rock Song" in result

    @responses.activate
    def test_api_empty_returns_no_songs(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        monkeypatch.setattr(server, "_engine", lambda: "api")
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: _default_prefs(fetch_explicit=False, clean_only=False),
        )
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/search",
            json={"results": {"library-songs": {"data": []}}},
            status=200,
        )
        result = server._library_search("Nope")
        assert "No songs found" in result

    @responses.activate
    def test_api_error_returns_error_message(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        monkeypatch.setattr(server, "_engine", lambda: "api")
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: _default_prefs(fetch_explicit=False, clean_only=False),
        )
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/search",
            json={"errors": []},
            status=401,
        )
        result = server._library_search("Query")
        assert "API Error" in result or "401" in result

    def test_limit_zero_normalized_to_25(self, monkeypatch):
        """limit<=0 must not hit AS range errors; forced to 25."""
        songs = [_make_song_data()]
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_engine", lambda: "native")
        captured = []

        def fake_search(q, t, offset, limit):
            captured.append(limit)
            return (True, songs, 1, "")

        monkeypatch.setattr(server.asc, "search_library_page", fake_search)
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: _default_prefs(fetch_explicit=False, clean_only=False),
        )
        server._library_search("X", limit=0)
        assert captured[0] == 25

    def test_fetch_explicit_enriches_tracks(self, monkeypatch):
        songs = [_make_song_data(track_id="i.123")]
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_engine", lambda: "native")
        monkeypatch.setattr(
            server.asc, "search_library_page", lambda q, t, offset, limit: (True, songs, 1, "")
        )
        fake_cache = MagicMock()
        fake_cache.get_explicit.return_value = "Yes"
        monkeypatch.setattr(server, "get_track_cache", lambda: fake_cache)
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: _default_prefs(fetch_explicit=True, clean_only=False),
        )
        result = server._library_search("X")
        fake_cache.get_explicit.assert_called()

    def test_clean_only_filters_explicit(self, monkeypatch):
        songs = [
            _make_song_data(name="Clean", track_id="i.111"),
            _make_song_data(name="Dirty", track_id="i.222"),
        ]
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_engine", lambda: "native")
        monkeypatch.setattr(
            server.asc, "search_library_page", lambda q, t, offset, limit: (True, songs, 2, "")
        )
        fake_cache = MagicMock()

        def get_explicit(tid):
            return "Yes" if tid == "i.222" else "No"

        fake_cache.get_explicit.side_effect = get_explicit
        monkeypatch.setattr(server, "get_track_cache", lambda: fake_cache)
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: _default_prefs(fetch_explicit=False, clean_only=True),
        )
        result = server._library_search("X")
        assert "Dirty" not in result
        assert "Clean" in result

    @responses.activate
    def test_api_asc_error_surfaced_in_no_songs_message(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_engine", lambda: "native")
        monkeypatch.setattr(
            server.asc, "search_library_page", lambda q, t, offset, limit: (False, [], 0, "AS boom")
        )
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: _default_prefs(fetch_explicit=False, clean_only=False),
        )
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/search",
            json={"results": {"library-songs": {"data": []}}},
            status=200,
        )
        result = server._library_search("X")
        assert "AS boom" in result

    @responses.activate
    def test_api_clean_only_filters_explicit(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        monkeypatch.setattr(server, "_engine", lambda: "api")
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: _default_prefs(fetch_explicit=False, clean_only=True),
        )
        api_clean = _make_api_song("Clean Song", "Artist", "1111111111", explicit=False)
        api_dirty = _make_api_song("Dirty Song", "Artist", "2222222222", explicit=True)
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/search",
            json={"results": {"library-songs": {"data": [api_clean, api_dirty]}}},
            status=200,
        )
        result = server._library_search("Song")
        assert "Clean Song" in result
        assert "Dirty Song" not in result

    @responses.activate
    def test_api_valueerror_returned_as_string(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        monkeypatch.setattr(server, "_engine", lambda: "api")
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: _default_prefs(fetch_explicit=False, clean_only=False),
        )
        monkeypatch.setattr(
            server, "get_headers", lambda: (_ for _ in ()).throw(ValueError("bad token"))
        )
        result = server._library_search("X")
        assert "bad token" in result

    def test_explicit_cache_miss_sets_unknown(self, monkeypatch):
        """Cache returns None for id → 'Unknown'; no-id track → 'Unknown' (lines 4219-4221)."""
        songs = [
            _make_song_data(name="A", track_id="i.has_id"),
            _make_song_data(name="B", track_id=""),  # no id → line 4221
        ]
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_engine", lambda: "native")
        monkeypatch.setattr(
            server.asc,
            "search_library_page",
            lambda q, t, offset, limit: (True, songs, 2, ""),
        )
        fake_cache = MagicMock()
        fake_cache.get_explicit.return_value = None  # falsy → sets "Unknown" (line 4219)
        monkeypatch.setattr(server, "get_track_cache", lambda: fake_cache)
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: _default_prefs(fetch_explicit=True, clean_only=False),
        )
        result = server._library_search("X")
        assert "A" in result  # both tracks still returned
        assert "B" in result

    @responses.activate
    def test_api_error_with_asc_error_shown(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """AS error + API connection error → both surfaced in message (line 4321)."""
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_engine", lambda: "native")
        monkeypatch.setattr(
            server.asc,
            "search_library_page",
            lambda q, t, offset, limit: (False, [], 0, "AS exploded"),
        )
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: _default_prefs(fetch_explicit=False, clean_only=False),
        )
        import requests as _req

        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/search",
            body=_req.exceptions.ConnectionError("network down"),
        )
        result = server._library_search("X")
        assert "API Error" in result
        assert "AS exploded" in result


# ============================================================================
# _resolve_catalog_track_itunes
# ============================================================================


class TestResolveCatalogTrackItunes:
    @responses.activate
    def test_success_with_artist(self, monkeypatch):
        monkeypatch.setattr(server, "get_storefront", lambda: "us")
        responses.add(
            responses.GET,
            "https://itunes.apple.com/search",
            json={
                "results": [
                    {
                        "trackName": "Song A",
                        "artistName": "Artist A",
                        "trackViewUrl": "https://music.apple.com/us/album/song/123",
                    }
                ]
            },
            status=200,
        )
        result = server._resolve_catalog_track_itunes("Song A", "Artist A")
        assert result is not None
        assert result["name"] == "Song A"
        assert result["url"].startswith("https://music.apple.com")

    @responses.activate
    def test_non_200_returns_none(self, monkeypatch):
        monkeypatch.setattr(server, "get_storefront", lambda: "us")
        responses.add(
            responses.GET,
            "https://itunes.apple.com/search",
            status=500,
        )
        result = server._resolve_catalog_track_itunes("Song B")
        assert result is None

    @responses.activate
    def test_no_matching_title_returns_none(self, monkeypatch):
        monkeypatch.setattr(server, "get_storefront", lambda: "us")
        responses.add(
            responses.GET,
            "https://itunes.apple.com/search",
            json={
                "results": [
                    {
                        "trackName": "Completely Different",
                        "artistName": "Someone Else",
                        "trackViewUrl": "https://music.apple.com/us/album/x/1",
                    }
                ]
            },
            status=200,
        )
        result = server._resolve_catalog_track_itunes("Song A")
        assert result is None

    @responses.activate
    def test_best_match_missing_url_returns_none(self, monkeypatch):
        monkeypatch.setattr(server, "get_storefront", lambda: "us")
        responses.add(
            responses.GET,
            "https://itunes.apple.com/search",
            json={
                "results": [
                    {
                        "trackName": "Song A",
                        "artistName": "Artist A",
                        # no trackViewUrl
                    }
                ]
            },
            status=200,
        )
        result = server._resolve_catalog_track_itunes("Song A", "Artist A")
        assert result is None

    def test_network_exception_returns_none(self, monkeypatch):
        monkeypatch.setattr(server, "get_storefront", lambda: "us")
        import requests as req

        def boom(*a, **kw):
            raise req.exceptions.ConnectionError("timeout")

        with patch.object(req, "get", boom):
            result = server._resolve_catalog_track_itunes("Song C")
        assert result is None

    @responses.activate
    def test_artist_mismatch_skips_result(self, monkeypatch):
        """Track name matches but artist doesn't — should return None."""
        monkeypatch.setattr(server, "get_storefront", lambda: "us")
        responses.add(
            responses.GET,
            "https://itunes.apple.com/search",
            json={
                "results": [
                    {
                        "trackName": "Song A",
                        "artistName": "Wrong Artist",
                        "trackViewUrl": "https://music.apple.com/us/album/x/1",
                    }
                ]
            },
            status=200,
        )
        result = server._resolve_catalog_track_itunes("Song A", "Artist A")
        assert result is None

    @responses.activate
    def test_exact_title_no_artist(self, monkeypatch):
        """No artist given — only exact title match is trusted."""
        monkeypatch.setattr(server, "get_storefront", lambda: "us")
        responses.add(
            responses.GET,
            "https://itunes.apple.com/search",
            json={
                "results": [
                    {
                        "trackName": "ExactSong",
                        "artistName": "Someone",
                        "trackViewUrl": "https://music.apple.com/us/album/x/1",
                    }
                ]
            },
            status=200,
        )
        result = server._resolve_catalog_track_itunes("ExactSong")
        assert result is not None
        assert result["name"] == "ExactSong"


# ============================================================================
# _verify_in_library
# ============================================================================


class TestVerifyInLibrary:
    def test_found_on_first_attempt(self, monkeypatch):
        monkeypatch.setattr(
            server.asc, "find_library_track", lambda name, artist: (True, "Song A|||Artist A")
        )
        result = server._verify_in_library("Song A", "Artist A")
        assert result == ("Song A", "Artist A")

    def test_not_found_returns_none(self, monkeypatch):
        monkeypatch.setattr(server.asc, "find_library_track", lambda name, artist: (False, ""))
        monkeypatch.setattr(server.time, "sleep", lambda s: None)
        result = server._verify_in_library("Missing", "Nobody")
        assert result is None

    def test_found_on_second_attempt(self, monkeypatch):
        calls = [0]

        def fake_find(name, artist):
            calls[0] += 1
            if calls[0] >= 2:
                return (True, f"{name}|||{artist}")
            return (False, "")

        monkeypatch.setattr(server.asc, "find_library_track", fake_find)
        monkeypatch.setattr(server.time, "sleep", lambda s: None)
        result = server._verify_in_library("Song B", "Artist B")
        assert result == ("Song B", "Artist B")


# ============================================================================
# _library_add
# ============================================================================


class TestLibraryAdd:
    def test_no_track_or_album_returns_error(self, monkeypatch):
        result = server._library_add()
        assert "Error" in result
        assert "track" in result.lower() or "album" in result.lower()

    def test_no_api_returns_error(self, monkeypatch):
        monkeypatch.setattr(server, "_can_use_library_api", lambda: False)
        result = server._library_add(track="Song A")
        assert "Error" in result
        assert "signin" in result or "login --dev" in result

    @responses.activate
    def test_catalog_id_track_success(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(server, "_can_use_library_api", lambda: True)
        # catalog ID format: all digits, 9+ chars
        responses.add(
            responses.POST,
            "https://api.music.apple.com/v1/me/library",
            status=200,
        )
        result = server._library_add(track="1234567890")
        assert "Added" in result
        assert "1234567890" in result

    @responses.activate
    def test_track_by_name_success(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(server, "_can_use_library_api", lambda: True)
        api_song = _make_api_song("Song A", "Artist A", "1234567890")
        monkeypatch.setattr(
            server, "_find_matching_catalog_song", lambda name, artist: (api_song, None, None)
        )
        monkeypatch.setattr(
            server, "_add_to_library_api", lambda ids, typ: (True, "Added 1 song(s)")
        )
        result = server._library_add(track="Song A", artist="Artist A")
        assert "Added" in result
        assert "Song A" in result

    @responses.activate
    def test_track_by_name_not_found(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(server, "_can_use_library_api", lambda: True)
        monkeypatch.setattr(
            server,
            "_find_matching_catalog_song",
            lambda name, artist: (None, "Not found in catalog", None),
        )
        result = server._library_add(track="Phantom Song")
        assert "Errors" in result or "Error" in result
        assert "Not found" in result

    def test_track_already_library_id(self, monkeypatch):
        monkeypatch.setattr(server, "_can_use_library_api", lambda: True)
        # Library ID starts with "i."
        result = server._library_add(track="i.ABC123")
        assert "Already a library ID" in result

    @responses.activate
    def test_album_catalog_id_success(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(server, "_can_use_library_api", lambda: True)
        responses.add(
            responses.POST,
            "https://api.music.apple.com/v1/me/library",
            status=200,
        )
        result = server._library_add(album="1234567890")
        assert "Added" in result
        assert "Album ID" in result

    @responses.activate
    def test_album_by_name_success(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(server, "_can_use_library_api", lambda: True)
        api_album = {
            "id": "1234567890",
            "type": "albums",
            "attributes": {"name": "Great Album", "artistName": "Artist A"},
        }
        monkeypatch.setattr(
            server, "_find_matching_catalog_album", lambda name, artist: (api_album, None, None)
        )
        monkeypatch.setattr(
            server, "_add_to_library_api", lambda ids, typ: (True, "Added 1 album(s)")
        )
        result = server._library_add(album="Great Album", artist="Artist A")
        assert "Added" in result
        assert "Great Album" in result

    def test_album_already_library_id(self, monkeypatch):
        monkeypatch.setattr(server, "_can_use_library_api", lambda: True)
        # Album library ID starts with "l."
        result = server._library_add(album="l.ABC123")
        assert "Already a library ID" in result

    @responses.activate
    def test_mixed_success_and_error(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(server, "_can_use_library_api", lambda: True)
        api_song = _make_api_song("Good Song", "Artist A", "1234567890")
        call_count = [0]

        def fake_find(name, artist):
            call_count[0] += 1
            if call_count[0] == 1:
                return (api_song, None, None)
            return (None, "Not found in catalog", None)

        monkeypatch.setattr(server, "_find_matching_catalog_song", fake_find)
        monkeypatch.setattr(server, "_add_to_library_api", lambda ids, typ: (True, "Added"))
        result = server._library_add(track='["Good Song", "Bad Song"]')
        assert "Added" in result
        assert "Error" in result or "failed" in result

    @responses.activate
    def test_fuzzy_match_shown_in_result(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(server, "_can_use_library_api", lambda: True)
        api_song = _make_api_song("Actual Name", "Artist A", "1234567890")
        from applemusic_mcp.server import FuzzyMatchResult

        fuzzy = FuzzyMatchResult(
            matched_name="Actual Name",
            query="Fuzzy Query",
            normalized_query="fuzzy query",
            normalized_match="actual name",
            transformations=[],
            match_type="fuzzy",
        )
        monkeypatch.setattr(
            server, "_find_matching_catalog_song", lambda name, artist: (api_song, None, fuzzy)
        )
        monkeypatch.setattr(server, "_add_to_library_api", lambda ids, typ: (True, "Added"))
        result = server._library_add(track="Fuzzy Query", artist="Artist A")
        assert "fuzzy" in result.lower()
        assert "Fuzzy Query" in result

    @responses.activate
    def test_add_to_library_api_failure_produces_error(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(server, "_can_use_library_api", lambda: True)
        api_song = _make_api_song("Song X", "Artist X", "1234567890")
        monkeypatch.setattr(
            server, "_find_matching_catalog_song", lambda name, artist: (api_song, None, None)
        )
        monkeypatch.setattr(server, "_add_to_library_api", lambda ids, typ: (False, "Unauthorized"))
        result = server._library_add(track="Song X")
        assert "Error" in result or "Errors" in result

    def test_all_errors_returns_errors(self, monkeypatch):
        monkeypatch.setattr(server, "_can_use_library_api", lambda: True)
        monkeypatch.setattr(
            server, "_find_matching_catalog_song", lambda name, artist: (None, "Not found", None)
        )
        result = server._library_add(track="Ghost")
        assert "Error" in result or "Errors" in result

    def test_track_parse_error(self, monkeypatch):
        """Malformed JSON track input → r.error set → parse error appended (lines 4486-4487)."""
        monkeypatch.setattr(server, "_can_use_library_api", lambda: True)
        # "[invalid" starts with '[' but fails JSON parse → r.error = "Invalid JSON: ..."
        result = server._library_add(track="[invalid json")
        assert "parse error" in result.lower() or "Error" in result

    @responses.activate
    def test_catalog_id_track_api_failure(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Catalog ID add fails → error message appended (line 4494)."""
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(server, "_can_use_library_api", lambda: True)
        monkeypatch.setattr(
            server, "_add_to_library_api", lambda ids, typ: (False, "Not authorized")
        )
        result = server._library_add(track="1234567890")
        assert "Error" in result or "Errors" in result
        assert "1234567890" in result

    def test_album_parse_error(self, monkeypatch):
        """Malformed JSON album input → r.error set → parse error appended (lines 4506-4507)."""
        monkeypatch.setattr(server, "_can_use_library_api", lambda: True)
        result = server._library_add(album="[bad json{")
        assert "parse error" in result.lower() or "Error" in result

    @responses.activate
    def test_album_catalog_id_api_failure(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Album catalog ID add fails → error appended (line 4517)."""
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(server, "_can_use_library_api", lambda: True)
        monkeypatch.setattr(server, "_add_to_library_api", lambda ids, typ: (False, "Rate limited"))
        result = server._library_add(album="1234567890")
        assert "Error" in result or "Errors" in result
        assert "1234567890" in result

    def test_album_not_found_by_name(self, monkeypatch):
        """Album name search finds nothing → error appended (lines 4466-4467)."""
        monkeypatch.setattr(server, "_can_use_library_api", lambda: True)
        monkeypatch.setattr(
            server,
            "_find_matching_catalog_album",
            lambda name, artist: (None, "Not found in catalog", None),
        )
        result = server._library_add(album="Ghost Album")
        assert "Error" in result or "Errors" in result
        assert "Ghost Album" in result

    @responses.activate
    def test_album_fuzzy_match_shown(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Album add with fuzzy match shows fuzzy annotation (line 4476)."""
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(server, "_can_use_library_api", lambda: True)
        api_album = {
            "id": "1234567890",
            "type": "albums",
            "attributes": {"name": "Actual Album", "artistName": "Artist A"},
        }
        from applemusic_mcp.server import FuzzyMatchResult

        fuzzy = FuzzyMatchResult(
            matched_name="Actual Album",
            query="Fuzzy Album",
            normalized_query="fuzzy album",
            normalized_match="actual album",
            transformations=[],
            match_type="fuzzy",
        )
        monkeypatch.setattr(
            server,
            "_find_matching_catalog_album",
            lambda name, artist: (api_album, None, fuzzy),
        )
        monkeypatch.setattr(server, "_add_to_library_api", lambda ids, typ: (True, "Added"))
        result = server._library_add(album="Fuzzy Album")
        assert "fuzzy" in result.lower()
        assert "Fuzzy Album" in result

    @responses.activate
    def test_album_name_api_add_failure(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Album found but API add fails → error appended (line 4479)."""
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(server, "_can_use_library_api", lambda: True)
        api_album = {
            "id": "1234567890",
            "type": "albums",
            "attributes": {"name": "Real Album", "artistName": "Real Artist"},
        }
        monkeypatch.setattr(
            server,
            "_find_matching_catalog_album",
            lambda name, artist: (api_album, None, None),
        )
        monkeypatch.setattr(server, "_add_to_library_api", lambda ids, typ: (False, "Forbidden"))
        result = server._library_add(album="Real Album")
        assert "Error" in result or "Errors" in result


# ============================================================================
# _library_recently_played
# ============================================================================


class TestLibraryRecentlyPlayed:
    @responses.activate
    def test_success_returns_tracks(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        track = _make_api_song("Recent Song", "Recent Artist", "9999999999")
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/recent/played/tracks",
            json={"data": [track]},
            status=200,
        )
        result = server._library_recently_played(limit=10)
        assert "Recent Song" in result

    @responses.activate
    def test_no_tracks_returns_message(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/recent/played/tracks",
            json={"data": []},
            status=200,
        )
        result = server._library_recently_played(limit=10)
        assert "No recently played" in result

    @responses.activate
    def test_api_error_returns_error(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Connection-level error (not a 4xx/5xx) triggers RequestException."""
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        import requests as _requests

        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/recent/played/tracks",
            body=_requests.exceptions.ConnectionError("connection refused"),
        )
        result = server._library_recently_played(limit=10)
        assert "API Error" in result

    @responses.activate
    def test_non_200_breaks_pagination(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """A non-200 status on the first batch means no tracks."""
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/recent/played/tracks",
            status=401,
        )
        result = server._library_recently_played(limit=10)
        assert "No recently played" in result

    @responses.activate
    def test_paginates_multiple_batches(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        batch1 = [_make_api_song(f"Song {i}", "Artist", str(1000000000 + i)) for i in range(10)]
        batch2 = [_make_api_song(f"Song {i + 10}", "Artist", str(1000000010 + i)) for i in range(5)]
        # First call returns 10, second returns 5
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/recent/played/tracks",
            json={"data": batch1},
            status=200,
        )
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/recent/played/tracks",
            json={"data": batch2},
            status=200,
        )
        result = server._library_recently_played(limit=15)
        assert "Song 0" in result

    def test_valueerror_returns_string(self, monkeypatch):
        monkeypatch.setattr(
            server, "get_headers", lambda: (_ for _ in ()).throw(ValueError("no token"))
        )
        result = server._library_recently_played(limit=10)
        assert "no token" in result


# ============================================================================
# _build_library_track_data
# ============================================================================


class TestBuildLibraryTrackData:
    def test_basic_construction(self):
        songs = [_make_song_data()]
        result, _ = server._build_library_track_data(songs, fetch_explicit=False, clean_only=False)
        assert len(result) == 1
        assert result[0]["name"] == "Song A"
        assert result[0]["explicit"] == "Unknown"

    def test_fetch_explicit_enriches_from_cache(self, monkeypatch):
        songs = [_make_song_data(track_id="i.abc")]
        fake_cache = MagicMock()
        fake_cache.get_explicit.return_value = "Yes"
        monkeypatch.setattr(server, "get_track_cache", lambda: fake_cache)
        result, _ = server._build_library_track_data(songs, fetch_explicit=True, clean_only=False)
        assert result[0]["explicit"] == "Yes"

    def test_clean_only_filters_explicit(self, monkeypatch):
        songs = [
            _make_song_data(name="Clean", track_id="i.c1"),
            _make_song_data(name="Dirty", track_id="i.d1"),
        ]
        fake_cache = MagicMock()

        def get_ex(tid):
            return "Yes" if tid == "i.d1" else "No"

        fake_cache.get_explicit.side_effect = get_ex
        monkeypatch.setattr(server, "get_track_cache", lambda: fake_cache)
        result, _ = server._build_library_track_data(songs, fetch_explicit=False, clean_only=True)
        names = [t["name"] for t in result]
        assert "Clean" in names
        assert "Dirty" not in names

    def test_no_id_skipped_in_cache_lookup(self, monkeypatch):
        """Track with empty id should still appear, explicit stays 'Unknown'."""
        songs = [_make_song_data(track_id="")]
        fake_cache = MagicMock()
        monkeypatch.setattr(server, "get_track_cache", lambda: fake_cache)
        result, _ = server._build_library_track_data(songs, fetch_explicit=True, clean_only=False)
        fake_cache.get_explicit.assert_not_called()
        assert result[0]["explicit"] == "Unknown"


# ============================================================================
# _library_browse
# ============================================================================


class TestLibraryBrowse:
    def test_native_songs_success_with_limit(self, monkeypatch):
        songs = [_make_song_data()]
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_engine", lambda: "native")
        monkeypatch.setattr(
            server.asc, "get_library_songs_page", lambda offset, limit: (True, songs, 100, "")
        )
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: _default_prefs(fetch_explicit=False, clean_only=False),
        )
        result = server._library_browse(item_type="songs", limit=25)
        assert "Song A" in result

    def test_native_songs_empty_library(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_engine", lambda: "native")
        monkeypatch.setattr(
            server.asc, "get_library_songs_page", lambda offset, limit: (True, [], 0, "")
        )
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: _default_prefs(fetch_explicit=False, clean_only=False),
        )
        result = server._library_browse(item_type="songs", limit=25)
        assert "No songs" in result or "No" in result

    def test_native_songs_offset_exceeds_library(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_engine", lambda: "native")
        monkeypatch.setattr(
            server.asc, "get_library_songs_page", lambda offset, limit: (True, [], 10, "")
        )
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: _default_prefs(fetch_explicit=False, clean_only=False),
        )
        result = server._library_browse(item_type="songs", limit=25, offset=100)
        assert "Offset" in result or "exceeds" in result

    def test_native_songs_limit_zero_full_fetch(self, monkeypatch):
        songs = [_make_song_data()]
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_engine", lambda: "native")
        monkeypatch.setattr(server.asc, "get_library_songs", lambda limit: (True, songs))
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: _default_prefs(fetch_explicit=False, clean_only=False),
        )
        result = server._library_browse(item_type="songs", limit=0)
        assert "Song A" in result

    def test_native_songs_full_fetch_empty_library(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_engine", lambda: "native")
        monkeypatch.setattr(server.asc, "get_library_songs", lambda limit: (True, []))
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: _default_prefs(fetch_explicit=False, clean_only=False),
        )
        result = server._library_browse(item_type="songs", limit=0)
        assert "No songs" in result or "No" in result

    def test_native_songs_as_error_surfaces(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_engine", lambda: "native")
        monkeypatch.setattr(
            server.asc,
            "get_library_songs_page",
            lambda offset, limit: (False, [], 0, "Music.app not responding"),
        )
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: _default_prefs(fetch_explicit=False, clean_only=False),
        )
        result = server._library_browse(item_type="songs", limit=25)
        assert "Error" in result

    def test_native_songs_full_fetch_as_error(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_engine", lambda: "native")
        monkeypatch.setattr(server.asc, "get_library_songs", lambda limit: (False, "Script failed"))
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: _default_prefs(fetch_explicit=False, clean_only=False),
        )
        result = server._library_browse(item_type="songs", limit=0)
        assert "Error" in result

    def test_native_clean_only_uses_the_page_path(self, monkeypatch):
        """clean_only rides the O(limit) page path so rating lookups are bounded
        by the page rather than swept across the library."""
        songs = [_make_song_data(name="Clean", track_id="i.c1")]
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_engine", lambda: "native")
        monkeypatch.setattr(
            server.asc, "get_library_songs_page", lambda offset, limit: (True, songs, 1, None)
        )
        fake_cache = MagicMock()
        fake_cache.get_explicit.return_value = "No"
        monkeypatch.setattr(server, "get_track_cache", lambda: fake_cache)
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: _default_prefs(fetch_explicit=False, clean_only=True),
        )
        result = server._library_browse(item_type="songs", limit=25, clean_only=True)
        assert "Clean" in result

    @responses.activate
    def test_api_songs(self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        monkeypatch.setattr(server, "_engine", lambda: "api")
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: _default_prefs(fetch_explicit=False, clean_only=False),
        )
        api_song = _make_api_song("Library Song", "My Artist", "1234567890")
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/songs",
            json={"data": [api_song]},
            status=200,
        )
        result = server._library_browse(item_type="songs", limit=10)
        assert "Library Song" in result

    @responses.activate
    def test_api_albums(self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        monkeypatch.setattr(server, "_engine", lambda: "api")
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: _default_prefs(fetch_explicit=False, clean_only=False),
        )
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/albums",
            json={
                "data": [
                    {
                        "id": "l.abc",
                        "attributes": {
                            "name": "My Album",
                            "artistName": "My Artist",
                            "trackCount": 10,
                            "genreNames": ["Rock"],
                            "releaseDate": "2020-01-01",
                        },
                    }
                ]
            },
            status=200,
        )
        result = server._library_browse(item_type="albums", limit=10)
        assert "My Album" in result

    @responses.activate
    def test_api_artists(self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        monkeypatch.setattr(server, "_engine", lambda: "api")
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: _default_prefs(fetch_explicit=False, clean_only=False),
        )
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/artists",
            json={
                "data": [
                    {
                        "id": "r.abc",
                        "attributes": {"name": "My Artist"},
                    }
                ]
            },
            status=200,
        )
        result = server._library_browse(item_type="artists", limit=10)
        assert "My Artist" in result

    @responses.activate
    def test_api_videos(self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        monkeypatch.setattr(server, "_engine", lambda: "api")
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: _default_prefs(fetch_explicit=False, clean_only=False),
        )
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/music-videos",
            json={
                "data": [
                    {
                        "id": "v.abc",
                        "attributes": {"name": "Cool Video", "artistName": "Video Artist"},
                    }
                ]
            },
            status=200,
        )
        result = server._library_browse(item_type="videos", limit=10)
        assert "Cool Video" in result

    @responses.activate
    def test_api_invalid_type_returns_error(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        monkeypatch.setattr(server, "_engine", lambda: "api")
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: _default_prefs(fetch_explicit=False, clean_only=False),
        )
        result = server._library_browse(item_type="bogus", limit=10)
        assert "Invalid type" in result or "bogus" in result

    @responses.activate
    def test_api_no_items_returns_message(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        monkeypatch.setattr(server, "_engine", lambda: "api")
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: _default_prefs(fetch_explicit=False, clean_only=False),
        )
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/songs",
            json={"data": []},
            status=200,
        )
        result = server._library_browse(item_type="songs", limit=10)
        assert "No songs" in result

    @responses.activate
    def test_api_error_returns_error_message(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        monkeypatch.setattr(server, "_engine", lambda: "api")
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: _default_prefs(fetch_explicit=False, clean_only=False),
        )
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/songs",
            status=503,
        )
        result = server._library_browse(item_type="songs", limit=10)
        assert "API Error" in result

    @responses.activate
    def test_api_404_breaks_pagination(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        monkeypatch.setattr(server, "_engine", lambda: "api")
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: _default_prefs(fetch_explicit=False, clean_only=False),
        )
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/songs",
            status=404,
        )
        result = server._library_browse(item_type="songs", limit=10)
        assert "No songs" in result

    @responses.activate
    def test_api_clean_only_songs_filtered(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        monkeypatch.setattr(server, "_engine", lambda: "api")
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: _default_prefs(fetch_explicit=False, clean_only=True),
        )
        clean = _make_api_song("Clean Track", "Artist", "1111111111", explicit=False)
        dirty = _make_api_song("Dirty Track", "Artist", "2222222222", explicit=True)
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/songs",
            json={"data": [clean, dirty]},
            status=200,
        )
        result = server._library_browse(item_type="songs", limit=10, clean_only=True)
        assert "Clean Track" in result
        assert "Dirty Track" not in result


# ============================================================================
# _library_favorites
# ============================================================================


class TestLibraryFavorites:
    def test_success_returns_songs(self, monkeypatch):
        songs = [_make_song_data(name="Loved Song")]
        monkeypatch.setattr(server.asc, "get_loved_songs", lambda limit: (True, songs))
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: _default_prefs(fetch_explicit=False, clean_only=False),
        )
        result = server._library_favorites(limit=25)
        assert "Loved Song" in result

    def test_no_songs_returns_message(self, monkeypatch):
        monkeypatch.setattr(server.asc, "get_loved_songs", lambda limit: (True, []))
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: _default_prefs(fetch_explicit=False, clean_only=False),
        )
        result = server._library_favorites(limit=25)
        assert "No favorite songs" in result

    def test_asc_error_returns_error_message(self, monkeypatch):
        monkeypatch.setattr(server.asc, "get_loved_songs", lambda limit: (False, "Script error"))
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: _default_prefs(fetch_explicit=False, clean_only=False),
        )
        result = server._library_favorites(limit=25)
        assert "Error" in result

    def test_clean_only_filters_all_explicit_returns_message(self, monkeypatch):
        songs = [_make_song_data(name="Explicit Fav", track_id="i.x1")]
        monkeypatch.setattr(server.asc, "get_loved_songs", lambda limit: (True, songs))
        fake_cache = MagicMock()
        fake_cache.get_explicit.return_value = "Yes"
        monkeypatch.setattr(server, "get_track_cache", lambda: fake_cache)
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: _default_prefs(fetch_explicit=False, clean_only=True),
        )
        result = server._library_favorites(limit=25, clean_only=True)
        assert "No favorite songs" in result


# ============================================================================
# _library_recently_added
# ============================================================================


class TestLibraryRecentlyAdded:
    @responses.activate
    def test_success_returns_items(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        item = {
            "id": "l.abc",
            "type": "library-albums",
            "attributes": {
                "name": "New Album",
                "artistName": "New Artist",
                "trackCount": 12,
                "genreNames": ["Pop"],
                "releaseDate": "2024-01-01",
                "dateAdded": "2024-06-01",
                "artwork": {"url": "https://example.com/{w}x{h}.jpg"},
            },
        }
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/recently-added",
            json={"data": [item]},
            status=200,
        )
        result = server._library_recently_added(limit=25, format="text", export="none", full=False)
        assert "New Album" in result

    @responses.activate
    def test_404_breaks_loop(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/recently-added",
            status=404,
        )
        result = server._library_recently_added(limit=25, format="text", export="none", full=False)
        assert "No recently added" in result

    @responses.activate
    def test_empty_response_returns_message(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/recently-added",
            json={"data": []},
            status=200,
        )
        result = server._library_recently_added(limit=25, format="text", export="none", full=False)
        assert "No recently added" in result

    @responses.activate
    def test_api_error_returns_error(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/recently-added",
            status=503,
        )
        result = server._library_recently_added(limit=25, format="text", export="none", full=False)
        assert "API Error" in result

    def test_valueerror_returns_string(self, monkeypatch):
        monkeypatch.setattr(
            server, "get_headers", lambda: (_ for _ in ()).throw(ValueError("missing token"))
        )
        result = server._library_recently_added(limit=25, format="text", export="none", full=False)
        assert "missing token" in result


# ============================================================================
# _library_rate
# ============================================================================


class TestLibraryRate:
    def test_no_track_returns_error(self):
        result = server._library_rate(action="love", track="")
        assert "Error" in result
        assert "track" in result.lower()

    def test_invalid_action_returns_error(self):
        result = server._library_rate(action="smash", track="Song A")
        assert "Invalid action" in result
        assert "smash" in result

    @responses.activate
    def test_catalog_id_love_via_api(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses.add(
            responses.PUT,
            "https://api.music.apple.com/v1/me/ratings/songs/1234567890",
            status=200,
        )
        result = server._library_rate(action="love", track="1234567890")
        assert "love" in result.lower()
        assert "1234567890" in result

    @responses.activate
    def test_catalog_id_dislike_via_api(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses.add(
            responses.PUT,
            "https://api.music.apple.com/v1/me/ratings/songs/1234567890",
            status=200,
        )
        result = server._library_rate(action="dislike", track="1234567890")
        assert "dislike" in result.lower()

    def test_catalog_id_get_not_native_returns_error(self, monkeypatch):
        monkeypatch.setattr(server, "_engine", lambda: "api")
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        result = server._library_rate(action="get", track="1234567890")
        assert "native" in result.lower() or "Error" in result

    def test_catalog_id_set_not_native_returns_error(self, monkeypatch):
        monkeypatch.setattr(server, "_engine", lambda: "api")
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        result = server._library_rate(action="set", track="1234567890")
        assert "native" in result.lower() or "Error" in result

    @responses.activate
    def test_catalog_id_get_native_success(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(server, "_engine", lambda: "native")
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        # Return track info for catalog ID lookup
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/songs/1234567890",
            json={"data": [{"attributes": {"name": "Catalog Song", "artistName": "Artist"}}]},
            status=200,
        )
        monkeypatch.setattr(server, "get_storefront", lambda: "us")
        monkeypatch.setattr(server.asc, "get_rating", lambda name, artist: (True, 80))
        result = server._library_rate(action="get", track="1234567890")
        assert "★" in result or "Catalog Song" in result

    @responses.activate
    def test_catalog_id_set_native_success(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(server, "_engine", lambda: "native")
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/songs/1234567890",
            json={"data": [{"attributes": {"name": "Catalog Song", "artistName": "Artist"}}]},
            status=200,
        )
        monkeypatch.setattr(server, "get_storefront", lambda: "us")
        monkeypatch.setattr(server.asc, "set_rating", lambda name, val, artist: (True, "Set"))
        result = server._library_rate(action="set", track="1234567890", stars=4)
        assert "★" in result or "Catalog Song" in result

    @responses.activate
    def test_catalog_id_get_track_info_not_found(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(server, "_engine", lambda: "native")
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/songs/1234567890",
            json={"data": []},
            status=200,
        )
        monkeypatch.setattr(server, "get_storefront", lambda: "us")
        result = server._library_rate(action="get", track="1234567890")
        assert "Error" in result

    def test_persistent_id_returns_error(self):
        # Persistent ID: 12+ hex chars
        result = server._library_rate(action="love", track="ABC123DEF456")
        assert "Persistent ID" in result or "not supported" in result

    def test_library_id_returns_error(self):
        # Library ID starts with "i."
        result = server._library_rate(action="love", track="i.ABC123")
        assert "Library ID" in result or "not supported" in result

    def test_name_love_native_success(self, monkeypatch):
        monkeypatch.setattr(server, "_engine", lambda: "native")
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(
            server.asc, "love_track", lambda name, artist: (True, "Loved: Song A by Artist A")
        )
        result = server._library_rate(action="love", track="Song A", artist="Artist A")
        assert "Loved" in result or "loved" in result.lower()

    def test_name_get_native_success(self, monkeypatch):
        monkeypatch.setattr(server, "_engine", lambda: "native")
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server.asc, "get_rating", lambda name, artist: (True, 60))
        result = server._library_rate(action="get", track="Song A", artist="Artist A")
        assert "★" in result

    def test_name_set_native_success(self, monkeypatch):
        monkeypatch.setattr(server, "_engine", lambda: "native")
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server.asc, "set_rating", lambda name, val, artist: (True, "Set"))
        result = server._library_rate(action="set", track="Song A", artist="Artist A", stars=3)
        assert "★" in result

    def test_name_get_not_native_returns_error(self, monkeypatch):
        monkeypatch.setattr(server, "_engine", lambda: "api")
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        result = server._library_rate(action="get", track="Song A")
        assert "native" in result.lower() or "Error" in result

    def test_name_set_not_native_returns_error(self, monkeypatch):
        monkeypatch.setattr(server, "_engine", lambda: "api")
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        result = server._library_rate(action="set", track="Song A", stars=5)
        assert "native" in result.lower() or "Error" in result

    @responses.activate
    def test_name_love_native_as_fail_api_fallback_success(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """AS fails with logic error → API fallback for love."""
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(server, "_engine", lambda: "native")
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        # AS returns failure with unknown error (logic-level = will cascade)
        monkeypatch.setattr(
            server.asc, "love_track", lambda name, artist: (False, "track not found")
        )
        monkeypatch.setattr(server.asc, "classify_error", lambda err: server.asc.ERROR_UNKNOWN)
        # Catalog search returns matching song
        api_song = _make_api_song("Song A", "Artist A", "1234567890")
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/search",
            json={"results": {"songs": {"data": [api_song]}}},
            status=200,
        )
        responses.add(
            responses.PUT,
            "https://api.music.apple.com/v1/me/ratings/songs/1234567890",
            status=200,
        )
        monkeypatch.setattr(server, "get_storefront", lambda: "us")
        result = server._library_rate(action="love", track="Song A", artist="Artist A")
        assert "Loved" in result or "loved" in result.lower()

    @responses.activate
    def test_name_love_as_env_error_no_cascade(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """AS fails with env error (not ERROR_UNKNOWN) → no cascade, return AS error."""
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(server, "_engine", lambda: "native")
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(
            server.asc, "love_track", lambda name, artist: (False, "Music.app not running")
        )
        monkeypatch.setattr(
            server.asc, "classify_error", lambda err: "not_running"
        )  # not ERROR_UNKNOWN
        result = server._library_rate(action="love", track="Song A")
        assert "Error" in result

    @responses.activate
    def test_name_love_not_found_in_catalog(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """After AS fails, catalog search returns nothing."""
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(server, "_engine", lambda: "native")
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server.asc, "love_track", lambda name, artist: (False, "not found"))
        monkeypatch.setattr(server.asc, "classify_error", lambda err: server.asc.ERROR_UNKNOWN)
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/search",
            json={"results": {"songs": {"data": []}}},
            status=200,
        )
        monkeypatch.setattr(server, "get_storefront", lambda: "us")
        result = server._library_rate(action="love", track="Ghost Song")
        assert "not found" in result.lower() or "Track not found" in result

    def test_name_dislike_native_success(self, monkeypatch):
        monkeypatch.setattr(server, "_engine", lambda: "native")
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(
            server.asc, "dislike_track", lambda name, artist: (True, "Disliked: Song B")
        )
        result = server._library_rate(action="dislike", track="Song B")
        assert "Disliked" in result or "dislike" in result.lower()

    def test_name_get_native_error(self, monkeypatch):
        monkeypatch.setattr(server, "_engine", lambda: "native")
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(
            server.asc, "get_rating", lambda name, artist: (False, "track not found")
        )
        result = server._library_rate(action="get", track="Missing Song")
        assert "Error" in result

    def test_name_set_native_error(self, monkeypatch):
        monkeypatch.setattr(server, "_engine", lambda: "native")
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(
            server.asc, "set_rating", lambda name, val, artist: (False, "track not found")
        )
        result = server._library_rate(action="set", track="Missing Song", stars=3)
        assert "Error" in result

    def test_invalid_json_track_produces_error(self, monkeypatch):
        """Malformed JSON track → r.error → 'Error: <msg>' (line 5652)."""
        monkeypatch.setattr(server, "_can_use_library_api", lambda: True)
        # "[invalid" starts with '[', fails JSON parse → ResolvedInput(error="Invalid JSON: ...")
        result = server._library_rate(action="love", track="[invalid json")
        assert "Error" in result

    @responses.activate
    def test_catalog_id_love_api_failure(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Catalog ID rate via API fails → error returned (line 5670)."""
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses.add(
            responses.PUT,
            "https://api.music.apple.com/v1/me/ratings/songs/1234567890",
            status=401,
        )
        result = server._library_rate(action="love", track="1234567890")
        assert "Error" in result

    @responses.activate
    def test_catalog_id_get_exception_during_lookup(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Exception during catalog ID name lookup is swallowed (lines 5688-5689)."""
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(server, "_engine", lambda: "native")
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "get_storefront", lambda: "us")
        # Force exception inside the try block that fetches song name
        import requests as _req

        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/songs/1234567890",
            body=_req.exceptions.ConnectionError("net error"),
        )
        # After the exception is swallowed, track_name stays "" → "Could not find track info"
        result = server._library_rate(action="get", track="1234567890")
        assert "Error" in result

    @responses.activate
    def test_name_love_api_fallback_api_rate_failure(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """API fallback for love by name: song found but rate call fails (line 5774)."""
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(server, "_engine", lambda: "native")
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server.asc, "love_track", lambda name, artist: (False, "not found"))
        monkeypatch.setattr(server.asc, "classify_error", lambda err: server.asc.ERROR_UNKNOWN)
        api_song = _make_api_song("Song A", "Artist A", "1234567890")
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/search",
            json={"results": {"songs": {"data": [api_song]}}},
            status=200,
        )
        # Rate call fails
        responses.add(
            responses.PUT,
            "https://api.music.apple.com/v1/me/ratings/songs/1234567890",
            status=403,
        )
        monkeypatch.setattr(server, "get_storefront", lambda: "us")
        result = server._library_rate(action="love", track="Song A", artist="Artist A")
        assert "Error" in result


# ============================================================================
# Additional coverage for _library_browse and _library_favorites missing lines
# ============================================================================


class TestLibraryBrowseMissingLines:
    def test_native_full_fetch_pagination_error(self, monkeypatch):
        """Offset exceeds count in full-fetch path → pagination error (line 5057)."""
        songs = [_make_song_data(name=f"Song {i}") for i in range(3)]
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_engine", lambda: "native")
        monkeypatch.setattr(server.asc, "get_library_songs", lambda limit: (True, songs))
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: _default_prefs(fetch_explicit=False, clean_only=False),
        )
        # limit=0 triggers full-fetch; offset=100 exceeds 3 songs → pagination error
        result = server._library_browse(item_type="songs", limit=0, offset=100)
        assert "Offset" in result or "exceeds" in result

    @responses.activate
    def test_api_pagination_error(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Offset exceeds count in API path → pagination error returned (line 5155)."""
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        monkeypatch.setattr(server, "_engine", lambda: "api")
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: _default_prefs(fetch_explicit=False, clean_only=False),
        )
        api_song = _make_api_song("Only Song", "Artist", "1234567890")
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/songs",
            json={"data": [api_song]},
            status=200,
        )
        # offset=100 exceeds 1 item in response
        result = server._library_browse(item_type="songs", limit=25, offset=100)
        assert "Offset" in result or "exceeds" in result

    def test_api_value_error_handler(self, monkeypatch):
        """ValueError from get_headers → str(e) returned (lines 5169-5170)."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        monkeypatch.setattr(server, "_engine", lambda: "api")
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: _default_prefs(fetch_explicit=False, clean_only=False),
        )
        monkeypatch.setattr(
            server, "get_headers", lambda: (_ for _ in ()).throw(ValueError("token missing"))
        )
        result = server._library_browse(item_type="songs", limit=10)
        assert "token missing" in result


class TestLibraryFavoritesMissingLines:
    def test_pagination_error_when_offset_exceeds(self, monkeypatch):
        """Offset beyond total in _library_favorites → pagination error (line 5206)."""
        songs = [_make_song_data(name="Loved Song")]
        monkeypatch.setattr(server.asc, "get_loved_songs", lambda limit: (True, songs))
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: _default_prefs(fetch_explicit=False, clean_only=False),
        )
        # offset=100 exceeds 1 song → _apply_pagination returns error
        result = server._library_favorites(limit=25, offset=100)
        assert "Offset" in result or "exceeds" in result


class TestLibraryRecentlyAddedMissingLines:
    @responses.activate
    def test_full_batch_continues_pagination(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """When first batch fills up, offset increments and loop continues (line 5330)."""
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        # First batch: exactly 25 items (fills batch_limit) → offset += 25
        batch1 = [
            {
                "id": f"l.{i}",
                "type": "library-albums",
                "attributes": {
                    "name": f"Album {i}",
                    "artistName": "Artist",
                    "genreNames": [],
                    "artwork": {"url": ""},
                },
            }
            for i in range(25)
        ]
        # Second batch: fewer items → break
        batch2 = [
            {
                "id": "l.99",
                "type": "library-albums",
                "attributes": {
                    "name": "Last Album",
                    "artistName": "Artist",
                    "genreNames": [],
                    "artwork": {"url": ""},
                },
            }
        ]
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/recently-added",
            json={"data": batch1},
            status=200,
        )
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/recently-added",
            json={"data": batch2},
            status=200,
        )
        result = server._library_recently_added(limit=26, format="text", export="none", full=False)
        assert "Album 0" in result
        assert "Last Album" in result


# ===========================================================================
# clean_only must not answer "clean" when it means "not known to be explicit"
# ===========================================================================
def test_parse_explicit_is_three_state():
    """ "Could not read it" must not collapse to "No" -- that is the one wrong
    answer for a caller filtering for children."""
    from applemusic_mcp.applescript import _parse_explicit

    assert _parse_explicit("true") == "Yes"
    assert _parse_explicit("false") == "No"
    for cannot_tell in ("unknown", "", "   ", "missing value"):
        assert _parse_explicit(cannot_tell) == "Unknown", cannot_tell


def test_clean_filter_removes_explicit_and_discloses_unverified(monkeypatch):
    """Old filter was `!= "Yes"` with uncached ratings set to "Unknown", so a
    cold cache passed everything through while reporting success."""
    from applemusic_mcp import server as srv

    monkeypatch.setattr(srv, "_resolve_explicit_ratings", lambda t, budget=40: {})
    kept, note = srv._apply_clean_filter(
        [
            {"name": "Clean", "explicit": "No"},
            {"name": "Explicit", "explicit": "Yes"},
            {"name": "Unrated", "explicit": "Unknown"},
        ],
        True,
    )

    assert [t["name"] for t in kept] == ["Clean", "Unrated"]
    assert kept[1]["explicit"] == "Unknown", "kept, but not relabelled as clean"
    assert "1 verified clean" in note and "could NOT be verified" in note


def test_clean_filter_resolves_unknown_against_catalog(monkeypatch):
    """The case that let 8 explicit tracks read as clean: Music.app says
    Unknown, the catalog says explicit."""
    from applemusic_mcp import server as srv

    saved = {}
    cache = types.SimpleNamespace(
        get_explicit=lambda tid: None,
        get_track_by_name=lambda n, a="": None,
        set_track_metadata=lambda **kw: saved.update(kw),
    )
    monkeypatch.setattr(srv, "get_track_cache", lambda: cache)
    monkeypatch.setattr(
        srv,
        "_search_catalog_songs",
        lambda q, n: [
            # Same title, different artist: must not be accepted as this
            # track's rating (#50 was this bug class in the queue path).
            {
                "attributes": {
                    "name": "Fat Lip",
                    "artistName": "Someone Else",
                    "contentRating": "clean",
                }
            },
            {
                "attributes": {
                    "name": "Fat Lip",
                    "artistName": "Sum 41",
                    "contentRating": "explicit",
                }
            },
        ],
    )

    kept, note = srv._apply_clean_filter(
        [{"name": "Fat Lip", "artist": "Sum 41", "id": "9", "explicit": "Unknown"}], True
    )
    assert kept == [] and "1 explicit removed" in note
    assert saved["explicit"] == "Yes", "resolved rating should be cached"


def test_clean_filter_will_not_rate_a_track_from_a_different_artist(monkeypatch):
    """No match for this artist means unverified, not clean."""
    from applemusic_mcp import server as srv

    cache = types.SimpleNamespace(
        get_explicit=lambda tid: None,
        get_track_by_name=lambda n, a="": None,
        set_track_metadata=lambda **kw: None,
    )
    monkeypatch.setattr(srv, "get_track_cache", lambda: cache)
    monkeypatch.setattr(
        srv,
        "_search_catalog_songs",
        lambda q, n: [
            {"attributes": {"name": "Fat Lip", "artistName": "Nobody", "contentRating": "clean"}}
        ],
    )

    kept, note = srv._apply_clean_filter(
        [{"name": "Fat Lip", "artist": "Sum 41", "explicit": "Unknown"}], True
    )
    assert kept[0]["explicit"] == "Unknown", "a mismatched hit must not mark it clean"
    assert "could NOT be verified" in note


def test_clean_filter_is_noop_when_disabled():
    from applemusic_mcp import server as srv

    kept, note = srv._apply_clean_filter([{"name": "X", "explicit": "Yes"}], False)
    assert len(kept) == 1 and note == ""


def test_cached_unknown_does_not_permanently_block_verification(monkeypatch):
    """The enrichment path caches "Unknown" for unmatched tracks and the cache
    refuses to overwrite an entry, so treating Unknown as a cache hit would make
    one failed lookup permanent."""
    from applemusic_mcp import server as srv

    cache = types.SimpleNamespace(
        get_explicit=lambda tid: "Unknown",
        get_track_by_name=lambda n, a="": None,
        set_track_metadata=lambda **kw: None,
    )
    monkeypatch.setattr(srv, "get_track_cache", lambda: cache)
    monkeypatch.setattr(
        srv,
        "_search_catalog_songs",
        lambda q, n: [
            {"attributes": {"name": "T", "artistName": "A", "contentRating": "explicit"}}
        ],
    )

    kept, note = srv._apply_clean_filter(
        [{"name": "T", "artist": "A", "id": "1", "explicit": "Unknown"}], True
    )
    assert kept == [], "a cached Unknown must not stop the catalog lookup"


def test_unverified_tracks_are_named_not_just_counted(monkeypatch):
    """A count tells a parent five of twenty-five are unchecked without telling
    them which five."""
    from applemusic_mcp import server as srv

    monkeypatch.setattr(srv, "_resolve_explicit_ratings", lambda t, budget=40: {})
    _, note = srv._apply_clean_filter(
        [{"name": "Mystery", "artist": "Nobody", "explicit": "Unknown"}], True
    )
    assert "Mystery - Nobody" in note


@pytest.mark.parametrize(
    "failure, expected_reason",
    [
        (FileNotFoundError("Developer token not found"), "not signed in"),
        (requests.exceptions.ConnectionError("offline"), "catalog unreachable"),
        (requests.exceptions.Timeout("slow"), "catalog timed out"),
    ],
)
def test_clean_filter_degrades_without_crashing(monkeypatch, failure, expected_reason):
    """No token, no network, no catalog: keep the tracks, mark them unverified,
    and say which of those it was. These paths used to work with no network at
    all, so they must not start raising."""
    from applemusic_mcp import server as srv

    cache = types.SimpleNamespace(
        get_explicit=lambda tid: None,
        get_track_by_name=lambda n, a="": None,
        set_track_metadata=lambda **kw: None,
    )
    monkeypatch.setattr(srv, "get_track_cache", lambda: cache)

    def boom(q, n):
        raise failure

    monkeypatch.setattr(srv, "_search_catalog_songs", boom)

    kept, note = srv._apply_clean_filter(
        [{"name": "T", "artist": "A", "id": "1", "explicit": "Unknown"}], True
    )
    assert kept and kept[0]["explicit"] == "Unknown"
    assert expected_reason in note


def test_library_song_without_content_rating_is_unknown_not_clean():
    """`/me/library/songs` never returns `contentRating`. Reading absent as "No"
    reported every library track as clean without checking anything, and on
    Windows/Linux the library API is the only source."""
    from applemusic_mcp import server as srv

    library = {"type": "library-songs", "id": "i.abc", "attributes": {"name": "X"}}
    catalog_clean = {"type": "songs", "id": "123", "attributes": {"name": "X"}}
    catalog_explicit = {"type": "songs", "id": "124", "attributes": {"contentRating": "explicit"}}

    assert srv._rating_from_song_object(library) == "Unknown"
    # The catalog *does* omit the field on clean tracks, so absent means No there.
    assert srv._rating_from_song_object(catalog_clean) == "No"
    assert srv._rating_from_song_object(catalog_explicit) == "Yes"


def test_unverified_rating_is_never_cached(monkeypatch):
    """The cache is consulted as authoritative and refuses to overwrite, so
    caching an unverified guess would make it permanent."""
    from applemusic_mcp import server as srv

    writes = []
    monkeypatch.setattr(
        srv,
        "get_track_cache",
        lambda: types.SimpleNamespace(set_track_metadata=lambda **kw: writes.append(kw)),
    )
    srv.extract_track_data(
        {"type": "library-songs", "id": "i.abc", "attributes": {"name": "N", "artistName": "A"}},
        False,
    )
    assert writes == [], "an Unknown rating must not be written to the cache"


def test_ratings_are_resolved_by_catalog_id_before_any_name_search(monkeypatch):
    """Batching by id is exact and one request; the fuzzy search should only
    ever see what has no catalog id."""
    from applemusic_mcp import server as srv

    searched = []
    monkeypatch.setattr(
        srv,
        "get_track_cache",
        lambda: types.SimpleNamespace(
            get_explicit=lambda tid: None,
            get_track_by_name=lambda n, a="": None,
            set_track_metadata=lambda **kw: None,
        ),
    )
    monkeypatch.setattr(
        srv.amp_api, "catalog_content_ratings", lambda ids, storefront="us": {"111": "Yes"}
    )
    monkeypatch.setattr(srv, "_search_catalog_songs", lambda q, n: searched.append(q) or [])

    tracks = [
        {"name": "Batched", "artist": "A", "catalog_id": "111", "explicit": "Unknown"},
        {"name": "NoId", "artist": "B", "catalog_id": "", "explicit": "Unknown"},
    ]
    srv._resolve_explicit_ratings(tracks)

    assert tracks[0]["explicit"] == "Yes", "resolved exactly, by id"
    assert len(searched) == 1 and "NoId" in searched[0], "only the id-less track is searched"


def test_batched_resolutions_do_not_consume_the_search_budget(monkeypatch):
    """The budget caps expensive per-track searches. Counting cheap batched
    resolutions against it would let one well-populated page disable the
    fallback for exactly the tracks that need it -- the ones with no id."""
    from applemusic_mcp import server as srv

    monkeypatch.setattr(
        srv,
        "get_track_cache",
        lambda: types.SimpleNamespace(
            get_explicit=lambda tid: None,
            get_track_by_name=lambda n, a="": None,
            set_track_metadata=lambda **kw: None,
        ),
    )
    batched = {str(i): "No" for i in range(50)}
    monkeypatch.setattr(
        srv.amp_api, "catalog_content_ratings", lambda ids, storefront="us": batched
    )
    searched = []
    monkeypatch.setattr(srv, "_search_catalog_songs", lambda q, n: searched.append(q) or [])

    tracks = [
        {"name": f"B{i}", "artist": "A", "catalog_id": str(i), "explicit": "Unknown"}
        for i in range(50)
    ] + [{"name": "NoId", "artist": "A", "catalog_id": "", "explicit": "Unknown"}]

    srv._resolve_explicit_ratings(tracks, budget=5)
    assert searched, "50 batched tracks must not exhaust a 5-search budget"


def test_rate_limiting_is_named_as_the_reason(monkeypatch):
    from applemusic_mcp import server as srv

    monkeypatch.setattr(
        srv,
        "get_track_cache",
        lambda: types.SimpleNamespace(
            get_explicit=lambda tid: None,
            get_track_by_name=lambda n, a="": None,
            set_track_metadata=lambda **kw: None,
        ),
    )
    monkeypatch.setattr(srv.amp_api, "catalog_content_ratings", lambda ids, storefront="us": {})
    monkeypatch.setattr(srv, "_search_catalog_songs", lambda q, n: [])
    monkeypatch.setattr(srv.amp_api, "throttled_recently", lambda *a, **k: True)

    _, note = srv._apply_clean_filter(
        [{"name": "T", "artist": "A", "catalog_id": "1", "explicit": "Unknown"}], True
    )
    assert "rate limited by Apple" in note


@pytest.mark.parametrize(
    "name", ["../../.ssh/id_rsa", "/etc/passwd", "..", "../../../etc/hosts", "sub/../../x"]
)
def test_read_export_rejects_traversal(name):
    """`is_relative_to` is lexical: cache_dir/"../../.ssh/id_rsa" satisfies it
    while resolving outside the cache. What kept this unexploitable was the MCP
    SDK binding {filename} to one path segment -- a control this server does not
    own. GHSA-82fm-fh3q-54fj."""
    from applemusic_mcp import server as srv

    assert srv.read_export(name) == "Invalid path"


def test_read_export_still_reads_a_real_export():
    from applemusic_mcp import server as srv

    cache = srv.get_cache_dir().resolve()
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "_guard_probe.json").write_text('{"ok":1}', encoding="utf-8")
    try:
        assert srv.read_export("_guard_probe.json") == '{"ok":1}'
    finally:
        (cache / "_guard_probe.json").unlink()


def test_csv_carries_explicit_so_a_clean_only_export_is_not_silently_unlabelled():
    """The note is suppressed for csv on the grounds that rows carry the rating.
    They did not. GHSA-82fm-fh3q-54fj."""
    from applemusic_mcp import server as srv

    rows = [
        {
            "name": "A",
            "duration": "3:00",
            "artist": "X",
            "album": "Al",
            "year": "2020",
            "genre": "Pop",
            "id": "1",
            "explicit": "Unknown",
        }
    ]
    out = srv.format_output(rows, "csv", "none", False, "t")
    assert "explicit" in out.splitlines()[0]
    assert "Unknown" in out
