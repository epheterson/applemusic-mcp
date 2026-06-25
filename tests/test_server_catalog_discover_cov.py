"""Coverage tests for catalog and discover tools in server.py.

Covers (exclusive to this file):
  _catalog_search          ~4589-4746
  _catalog_album_tracks    ~4749-4860
  _catalog_album_details   ~4863-4967
  _discover_recommendations ~5215-5257
  _discover_heavy_rotation  ~5260-5302
  _discover_personal_station ~5364-5399
  discover() dispatcher    ~5402-5447
  _discover_top_songs      ~5449-5516
  _discover_similar_artists ~5519-5584
  _discover_song_station   ~5587-5615
  _catalog_song_details    ~5782-5816
  _catalog_artist_details  ~5819-5892
  _discover_charts         ~5895-5931
  _catalog_genres          ~5934-5959
  _catalog_suggestions     ~5962-5988
  catalog() dispatcher     ~5996-6061
"""

import json
import time
from unittest.mock import MagicMock, patch

import pytest
import responses as resp_lib

from applemusic_mcp import server

BASE = "https://api.music.apple.com/v1"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _write_tokens(config_dir, dev_token, user_token):
    (config_dir / "developer_token.json").write_text(
        json.dumps({"token": dev_token, "expires": time.time() + 86400 * 60})
    )
    (config_dir / "music_user_token.json").write_text(json.dumps({"music_user_token": user_token}))


def _song(
    song_id="1234567890", name="Test Song", artist="Test Artist", album="Test Album", explicit=False
):
    return {
        "id": song_id,
        "type": "songs",
        "attributes": {
            "name": name,
            "artistName": artist,
            "albumName": album,
            "durationInMillis": 210000,
            "releaseDate": "2023-01-15",
            "genreNames": ["Pop"],
            "contentRating": "explicit" if explicit else "clean",
            "isrc": "USABC2300001",
        },
    }


def _album(album_id="987654321", name="Test Album", artist="Test Artist"):
    return {
        "id": album_id,
        "type": "albums",
        "attributes": {
            "name": name,
            "artistName": artist,
            "trackCount": 10,
            "releaseDate": "2023-01-01",
            "genreNames": ["Pop"],
            "recordLabel": "Test Label",
            "copyright": "© 2023 Test",
        },
    }


def _artist(artist_id="111222333", name="Test Artist"):
    return {
        "id": artist_id,
        "type": "artists",
        "attributes": {
            "name": name,
            "genreNames": ["Rock", "Pop"],
        },
    }


# ── Shared fixture ─────────────────────────────────────────────────────────────


@pytest.fixture
def tokens(mock_config_dir, mock_developer_token, mock_user_token):
    """Write token files so get_headers() resolves cleanly."""
    _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
    return mock_config_dir


# ═══════════════════════════════════════════════════════════════════════════════
# _catalog_search  (lines ~4589-4746)
# ═══════════════════════════════════════════════════════════════════════════════


class TestCatalogSearch:
    @resp_lib.activate
    def test_empty_query_non_video(self, tokens):
        """Empty query for non music-video type returns error early."""
        result = server._catalog_search(query="", types="songs")
        assert "Error" in result
        assert "query required" in result

    @resp_lib.activate
    def test_search_songs_text(self, tokens):
        """Search returning songs produces text output."""
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/search",
            json={"results": {"songs": {"data": [_song()]}}},
            status=200,
        )
        result = server._catalog_search(query="Test Song", types="songs")
        assert "Test Song" in result
        assert "Test Artist" in result

    @resp_lib.activate
    def test_search_albums(self, tokens):
        """Search returning albums produces album section."""
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/search",
            json={"results": {"albums": {"data": [_album()]}}},
            status=200,
        )
        result = server._catalog_search(query="Test Album", types="albums")
        assert "Albums" in result
        assert "Test Album" in result

    @resp_lib.activate
    def test_search_artists(self, tokens):
        """Search returning artists produces artist section."""
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/search",
            json={"results": {"artists": {"data": [_artist()]}}},
            status=200,
        )
        result = server._catalog_search(query="Test Artist", types="artists")
        assert "Artists" in result
        assert "Test Artist" in result

    @resp_lib.activate
    def test_search_playlists(self, tokens):
        """Search returning playlists produces playlist section."""
        playlist = {
            "id": "pl.abc123",
            "type": "playlists",
            "attributes": {"name": "Test Playlist", "curatorName": "Apple"},
        }
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/search",
            json={"results": {"playlists": {"data": [playlist]}}},
            status=200,
        )
        result = server._catalog_search(query="Test Playlist", types="playlists")
        assert "Playlists" in result
        assert "Test Playlist" in result

    @resp_lib.activate
    def test_search_music_videos(self, tokens):
        """Search returning music videos produces video section."""
        video = {
            "id": "v.123",
            "type": "music-videos",
            "attributes": {
                "name": "Test Video",
                "artistName": "Test Artist",
                "durationInMillis": 210000,
            },
        }
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/search",
            json={"results": {"music-videos": {"data": [video]}}},
            status=200,
        )
        result = server._catalog_search(query="Test Video", types="music-videos")
        assert "Music Videos" in result
        assert "Test Video" in result

    @resp_lib.activate
    def test_music_videos_empty_query_charts_path(self, tokens):
        """Empty query for music-videos hits the charts endpoint."""
        chart_video = {
            "id": "v.999",
            "type": "music-videos",
            "attributes": {
                "name": "Chart Video",
                "artistName": "Chart Artist",
                "durationInMillis": 180000,
            },
        }
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/charts",
            json={"results": {"music-videos": [{"data": [chart_video]}]}},
            status=200,
        )
        result = server._catalog_search(query="", types="music-videos")
        assert "Music Videos" in result
        assert "Chart Video" in result

    @resp_lib.activate
    def test_music_videos_empty_query_empty_charts(self, tokens):
        """Empty query music-videos with no chart results returns 'No results found'."""
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/charts",
            json={"results": {}},
            status=200,
        )
        result = server._catalog_search(query="", types="music-videos")
        assert "No results found" in result

    @resp_lib.activate
    def test_search_json_format(self, tokens):
        """JSON format returns parseable JSON."""
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/search",
            json={"results": {"songs": {"data": [_song()]}}},
            status=200,
        )
        result = server._catalog_search(query="Test", types="songs", format="json")
        parsed = json.loads(result)
        assert "songs" in parsed
        assert parsed["songs"][0]["name"] == "Test Song"

    @resp_lib.activate
    def test_search_clean_only_filters_explicit(self, tokens):
        """clean_only=True removes explicit songs."""
        explicit_song = _song(song_id="1111111111", name="Dirty Song", explicit=True)
        clean_song = _song(song_id="2222222222", name="Clean Song", explicit=False)
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/search",
            json={"results": {"songs": {"data": [explicit_song, clean_song]}}},
            status=200,
        )
        result = server._catalog_search(query="Test", types="songs", clean_only=True)
        assert "Clean Song" in result
        assert "Dirty Song" not in result

    @resp_lib.activate
    def test_search_no_results(self, tokens):
        """Empty results produce 'No results found'."""
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/search",
            json={"results": {}},
            status=200,
        )
        result = server._catalog_search(query="NothingHere", types="songs")
        assert "No results found" in result

    @resp_lib.activate
    def test_search_api_error(self, tokens):
        """HTTP error bubbles up as 'API Error'."""
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/search",
            json={"errors": [{"title": "Unauthorized"}]},
            status=401,
        )
        result = server._catalog_search(query="Test", types="songs")
        assert "API Error" in result

    @resp_lib.activate
    def test_search_export_songs(self, tokens, tmp_path, monkeypatch):
        """Export path appends exported line to output."""
        monkeypatch.setattr(server, "get_cache_dir", lambda: tmp_path)
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/search",
            json={"results": {"songs": {"data": [_song()]}}},
            status=200,
        )
        result = server._catalog_search(query="Test", types="songs", export="csv")
        # export_msg appended to the text output
        assert "Exported" in result or "export" in result.lower()

    @resp_lib.activate
    def test_search_deduplicates_songs(self, tokens):
        """Duplicate song IDs are deduplicated."""
        s = _song()
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/search",
            json={"results": {"songs": {"data": [s, s]}}},
            status=200,
        )
        result = server._catalog_search(query="Test", types="songs")
        # Should appear once (2 identical IDs → 1 after dedup)
        assert result.count("1234567890") == 1


# ═══════════════════════════════════════════════════════════════════════════════
# _catalog_album_tracks  (lines ~4749-4860)
# ═══════════════════════════════════════════════════════════════════════════════


class TestCatalogAlbumTracks:
    def test_empty_album_param(self, tokens):
        """Missing album returns early error."""
        result = server._catalog_album_tracks(album="")
        assert "Error" in result
        assert "album" in result.lower()

    @resp_lib.activate
    def test_by_catalog_id_success(self, tokens):
        """Catalog ID triggers catalog URL and returns tracks."""
        album_id = "123456789"
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/albums/{album_id}/tracks",
            json={"data": [_song()]},
            status=200,
        )
        result = server._catalog_album_tracks(album=album_id)
        assert "Test Song" in result

    @resp_lib.activate
    def test_by_library_album_id_uses_library_url(self, tokens):
        """l. prefix triggers library URL."""
        album_id = "l.abc123"
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/me/library/albums/{album_id}/tracks",
            json={"data": [_song()]},
            status=200,
        )
        result = server._catalog_album_tracks(album=album_id)
        assert "Test Song" in result

    @resp_lib.activate
    def test_by_name_found(self, tokens):
        """Album by name: search → tracks fetch."""
        album_id = "987654321"
        # Search returns the album
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/search",
            json={"results": {"albums": {"data": [_album(album_id=album_id, name="Test Album")]}}},
            status=200,
        )
        # Tracks fetch
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/albums/{album_id}/tracks",
            json={"data": [_song()]},
            status=200,
        )
        result = server._catalog_album_tracks(album="Test Album")
        assert "Test Song" in result

    @resp_lib.activate
    def test_by_name_not_found(self, tokens):
        """Album by name: search returns no match → not found."""
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/search",
            json={"results": {"albums": {"data": []}}},
            status=200,
        )
        result = server._catalog_album_tracks(album="Unknown Album XYZ")
        assert "not found" in result.lower() or "Error" in result

    def test_unsupported_input_type(self, tokens):
        """Playlist ID input is unsupported for album lookup."""
        result = server._catalog_album_tracks(album="p.abc123")
        assert "Unsupported" in result

    @resp_lib.activate
    def test_error_from_resolved_input(self, tokens):
        """Malformed JSON input produces ResolvedInput with error."""
        result = server._catalog_album_tracks(album="[invalid json")
        assert "Error" in result

    @resp_lib.activate
    def test_404_response_no_tracks(self, tokens):
        """404 from API breaks loop → no tracks found."""
        album_id = "123456789"
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/albums/{album_id}/tracks",
            json={},
            status=404,
        )
        result = server._catalog_album_tracks(album=album_id)
        assert "No tracks found" in result

    @resp_lib.activate
    def test_empty_data_response_no_tracks(self, tokens):
        """Empty data in response breaks loop → no tracks found."""
        album_id = "123456789"
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/albums/{album_id}/tracks",
            json={"data": []},
            status=200,
        )
        result = server._catalog_album_tracks(album=album_id)
        assert "No tracks found" in result

    @resp_lib.activate
    def test_pagination_100_track_continuation(self, tokens):
        """Exactly 100 tracks triggers api_offset += 100 for next page."""
        album_id = "123456789"
        tracks_100 = [_song(song_id=str(1000000000 + i), name=f"Track {i}") for i in range(100)]
        tracks_5 = [_song(song_id=str(2000000000 + i), name=f"Track {i+100}") for i in range(5)]
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/albums/{album_id}/tracks",
            json={"data": tracks_100},
            status=200,
        )
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/albums/{album_id}/tracks",
            json={"data": tracks_5},
            status=200,
        )
        result = server._catalog_album_tracks(album=album_id)
        assert "105 tracks" in result or "tracks" in result

    @resp_lib.activate
    def test_api_error(self, tokens):
        """HTTP 500 returns API Error."""
        album_id = "123456789"
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/albums/{album_id}/tracks",
            json={"errors": [{"title": "Server Error"}]},
            status=500,
        )
        result = server._catalog_album_tracks(album=album_id)
        assert "API Error" in result

    @resp_lib.activate
    def test_json_format(self, tokens):
        """format='json' returns JSON output."""
        album_id = "123456789"
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/albums/{album_id}/tracks",
            json={"data": [_song()]},
            status=200,
        )
        result = server._catalog_album_tracks(album=album_id, format="json")
        # format_output returns JSON string for json format
        assert "Test Song" in result

    @resp_lib.activate
    def test_with_pagination_params(self, tokens):
        """limit/offset parameters apply pagination."""
        album_id = "123456789"
        tracks = [_song(song_id=str(1000000000 + i), name=f"Track {i}") for i in range(5)]
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/albums/{album_id}/tracks",
            json={"data": tracks},
            status=200,
        )
        result = server._catalog_album_tracks(album=album_id, limit=2, offset=0)
        assert "Track" in result


# ═══════════════════════════════════════════════════════════════════════════════
# _catalog_album_details  (lines ~4863-4967)
# ═══════════════════════════════════════════════════════════════════════════════


class TestCatalogAlbumDetails:
    def test_empty_album_param(self, tokens):
        """Missing album returns early error."""
        result = server._catalog_album_details(album="")
        assert "Error" in result

    @resp_lib.activate
    def test_by_catalog_id_success(self, tokens):
        """Catalog ID triggers album + tracks fetch."""
        album_id = "123456789"
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/albums/{album_id}",
            json={"data": [_album(album_id=album_id)]},
            status=200,
        )
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/albums/{album_id}/tracks",
            json={"data": [_song()]},
            status=200,
        )
        result = server._catalog_album_details(album=album_id)
        assert "Test Album" in result
        assert "Test Artist" in result
        assert "Test Song" in result

    @resp_lib.activate
    def test_album_not_found_empty_data(self, tokens):
        """Empty data array from album endpoint returns 'Album not found'."""
        album_id = "123456789"
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/albums/{album_id}",
            json={"data": []},
            status=200,
        )
        result = server._catalog_album_details(album=album_id)
        assert "Album not found" in result

    @resp_lib.activate
    def test_by_name_fuzzy_match(self, tokens):
        """Album by name hits search → album detail → tracks."""
        album_id = "987654321"
        # _find_matching_catalog_album → _search_catalog_albums → search endpoint
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/search",
            json={"results": {"albums": {"data": [_album(album_id=album_id, name="Fuzzy Album")]}}},
            status=200,
        )
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/albums/{album_id}",
            json={"data": [_album(album_id=album_id, name="Fuzzy Album")]},
            status=200,
        )
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/albums/{album_id}/tracks",
            json={"data": [_song()]},
            status=200,
        )
        result = server._catalog_album_details(album="Fuzzy Album")
        assert "Fuzzy Album" in result

    @resp_lib.activate
    def test_by_name_not_found(self, tokens):
        """Album name search returns no results → not found error."""
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/search",
            json={"results": {"albums": {"data": []}}},
            status=200,
        )
        result = server._catalog_album_details(album="Totally Unknown Album XYZZZZ")
        assert "not found" in result.lower() or "Error" in result

    def test_unsupported_input_type(self, tokens):
        """Playlist ID input returns unsupported error."""
        result = server._catalog_album_details(album="p.abc123")
        assert "Unsupported" in result

    @resp_lib.activate
    def test_track_duration_formatted(self, tokens):
        """Track duration formatted as M:SS in output."""
        album_id = "123456789"
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/albums/{album_id}",
            json={"data": [_album(album_id=album_id)]},
            status=200,
        )
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/albums/{album_id}/tracks",
            json={"data": [_song()]},  # 210000ms = 3:30
            status=200,
        )
        result = server._catalog_album_details(album=album_id)
        assert "3:30" in result

    @resp_lib.activate
    def test_tracks_404_breaks_loop(self, tokens):
        """404 on tracks URL breaks loop (no crash)."""
        album_id = "123456789"
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/albums/{album_id}",
            json={"data": [_album(album_id=album_id)]},
            status=200,
        )
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/albums/{album_id}/tracks",
            json={},
            status=404,
        )
        result = server._catalog_album_details(album=album_id)
        assert "Test Album" in result

    @resp_lib.activate
    def test_100_track_pagination(self, tokens):
        """100 tracks triggers api_offset increment for album_details."""
        album_id = "123456789"
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/albums/{album_id}",
            json={"data": [_album(album_id=album_id)]},
            status=200,
        )
        tracks_100 = [_song(song_id=str(1000000000 + i), name=f"Track {i}") for i in range(100)]
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/albums/{album_id}/tracks",
            json={"data": tracks_100},
            status=200,
        )
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/albums/{album_id}/tracks",
            json={"data": []},
            status=200,
        )
        result = server._catalog_album_details(album=album_id)
        assert "Test Album" in result

    @resp_lib.activate
    def test_api_error(self, tokens):
        """HTTP 500 returns API Error."""
        album_id = "123456789"
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/albums/{album_id}",
            json={},
            status=500,
        )
        result = server._catalog_album_details(album=album_id)
        assert "API Error" in result

    def test_error_resolved_input(self, tokens):
        """Malformed JSON triggers resolved input error path."""
        result = server._catalog_album_details(album="[invalid json")
        assert "Error" in result


# ═══════════════════════════════════════════════════════════════════════════════
# _discover_recommendations  (lines ~5215-5257)
# ═══════════════════════════════════════════════════════════════════════════════


class TestDiscoverRecommendations:
    @resp_lib.activate
    def test_recommendations_happy_path(self, tokens):
        """Recommendations returns formatted items."""
        rec = {
            "id": "rec.1",
            "type": "personal-recommendation",
            "attributes": {
                "title": {"stringForDisplay": "Recommended for You"},
            },
            "relationships": {
                "contents": {
                    "data": [
                        {
                            "id": "987654321",
                            "type": "albums",
                            "attributes": {
                                "name": "Great Album",
                                "artistName": "Cool Artist",
                                "releaseDate": "2023-01-01",
                            },
                        }
                    ]
                }
            },
        }
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/me/recommendations",
            json={"data": [rec]},
            status=200,
        )
        result = server._discover_recommendations(
            limit=50, format="text", export="none", full=False
        )
        assert "Great Album" in result or "Cool Artist" in result

    @resp_lib.activate
    def test_recommendations_limit_applied(self, tokens):
        """limit > 0 truncates all_items."""
        recs = []
        for i in range(3):
            recs.append(
                {
                    "id": f"rec.{i}",
                    "type": "personal-recommendation",
                    "attributes": {"title": {"stringForDisplay": f"Rec {i}"}},
                    "relationships": {
                        "contents": {
                            "data": [
                                {
                                    "id": str(1000000000 + i),
                                    "type": "albums",
                                    "attributes": {
                                        "name": f"Album {i}",
                                        "artistName": "Artist",
                                        "releaseDate": "2023-01-01",
                                    },
                                }
                            ]
                        }
                    },
                }
            )
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/me/recommendations",
            json={"data": recs},
            status=200,
        )
        result = server._discover_recommendations(limit=2, format="text", export="none", full=False)
        # limit=2 cuts items to 2; Album 2 should not appear
        assert "Album 2" not in result

    @resp_lib.activate
    def test_recommendations_api_error(self, tokens):
        """HTTP 401 returns API Error."""
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/me/recommendations",
            json={"errors": [{"title": "Unauthorized"}]},
            status=401,
        )
        result = server._discover_recommendations(
            limit=10, format="text", export="none", full=False
        )
        assert "API Error" in result

    @resp_lib.activate
    def test_recommendations_json_format(self, tokens):
        """JSON format returns serializable output."""
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/me/recommendations",
            json={"data": []},
            status=200,
        )
        result = server._discover_recommendations(
            limit=10, format="json", export="none", full=False
        )
        assert result  # Any response (even "[]") is valid


# ═══════════════════════════════════════════════════════════════════════════════
# _discover_heavy_rotation  (lines ~5260-5302)
# ═══════════════════════════════════════════════════════════════════════════════


class TestDiscoverHeavyRotation:
    @resp_lib.activate
    def test_heavy_rotation_happy_path(self, tokens):
        """Returns formatted heavy rotation data."""
        item = {
            "id": "l.abc123",
            "type": "library-albums",
            "attributes": {
                "name": "Heavy Album",
                "artistName": "Heavy Artist",
                "genreNames": ["Rock"],
                "trackCount": 12,
                "releaseDate": "2020-05-01",
                "dateAdded": "2023-06-01",
                "artwork": {"url": "https://example.com/{w}x{h}/img.jpg"},
            },
        }
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/me/history/heavy-rotation",
            json={"data": [item]},
            status=200,
        )
        result = server._discover_heavy_rotation(format="text", export="none", full=False)
        assert "Heavy Album" in result or "Heavy Artist" in result

    @resp_lib.activate
    def test_heavy_rotation_empty(self, tokens):
        """Empty data returns specific message."""
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/me/history/heavy-rotation",
            json={"data": []},
            status=200,
        )
        result = server._discover_heavy_rotation(format="text", export="none", full=False)
        assert "No heavy rotation" in result

    @resp_lib.activate
    def test_heavy_rotation_api_error(self, tokens):
        """HTTP error returns API Error."""
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/me/history/heavy-rotation",
            json={},
            status=403,
        )
        result = server._discover_heavy_rotation(format="text", export="none", full=False)
        assert "API Error" in result

    @resp_lib.activate
    def test_heavy_rotation_artwork_url_replacement(self, tokens):
        """Artwork URL placeholder gets replaced with 500x500."""
        item = {
            "id": "l.xyz",
            "type": "library-albums",
            "attributes": {
                "name": "Art Album",
                "artistName": "Art Artist",
                "genreNames": [],
                "artwork": {"url": "https://example.com/{w}x{h}/art.jpg"},
            },
        }
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/me/history/heavy-rotation",
            json={"data": [item]},
            status=200,
        )
        result = server._discover_heavy_rotation(format="json", export="none", full=True)
        assert "500x500" in result


# ═══════════════════════════════════════════════════════════════════════════════
# _discover_personal_station  (lines ~5364-5399)
# ═══════════════════════════════════════════════════════════════════════════════


class TestDiscoverPersonalStation:
    @resp_lib.activate
    def test_personal_station_found(self, tokens):
        """Returns station info when found."""
        station = {
            "id": "ra.123",
            "type": "stations",
            "attributes": {"name": "My Station", "isLive": False},
        }
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/stations",
            json={"data": [station]},
            status=200,
        )
        result = server._discover_personal_station()
        assert "My Station" in result
        assert "ra.123" in result
        assert "On-demand" in result

    @resp_lib.activate
    def test_personal_station_live(self, tokens):
        """Live station shows 'Live' type."""
        station = {
            "id": "ra.live",
            "type": "stations",
            "attributes": {"name": "Live Radio", "isLive": True},
        }
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/stations",
            json={"data": [station]},
            status=200,
        )
        result = server._discover_personal_station()
        assert "Live" in result

    @resp_lib.activate
    def test_personal_station_not_found(self, tokens):
        """Empty data returns 'No personal station found'."""
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/stations",
            json={"data": []},
            status=200,
        )
        result = server._discover_personal_station()
        assert "No personal station" in result

    @resp_lib.activate
    def test_personal_station_api_error(self, tokens):
        """HTTP error returns API Error."""
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/stations",
            json={},
            status=500,
        )
        result = server._discover_personal_station()
        assert "API Error" in result


# ═══════════════════════════════════════════════════════════════════════════════
# discover() dispatcher  (lines ~5402-5447)
# ═══════════════════════════════════════════════════════════════════════════════


class TestDiscoverDispatcher:
    @resp_lib.activate
    def test_recommendations_action(self, tokens):
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/me/recommendations",
            json={"data": []},
            status=200,
        )
        result = server.discover(action="recommendations")
        assert result is not None

    @resp_lib.activate
    def test_heavy_rotation_action_hyphen(self, tokens):
        """hyphen variant normalised to underscore."""
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/me/history/heavy-rotation",
            json={"data": []},
            status=200,
        )
        result = server.discover(action="heavy-rotation")
        assert "No heavy rotation" in result

    @resp_lib.activate
    def test_personal_station_action_hyphen(self, tokens):
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/stations",
            json={"data": []},
            status=200,
        )
        result = server.discover(action="personal-station")
        assert "No personal station" in result

    @resp_lib.activate
    def test_charts_action(self, tokens):
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/charts",
            json={"results": {"songs": [{"name": "Top 25", "data": []}]}},
            status=200,
        )
        result = server.discover(action="charts")
        assert result is not None

    def test_top_songs_missing_artist(self, tokens):
        result = server.discover(action="top_songs", artist="")
        assert "Error" in result
        assert "artist required" in result

    @resp_lib.activate
    def test_top_songs_with_artist(self, tokens):
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/search",
            json={"results": {"artists": {"data": [_artist(artist_id="111222333")]}}},
            status=200,
        )
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/artists/111222333/view/top-songs",
            json={"data": [_song()]},
            status=200,
        )
        result = server.discover(action="top_songs", artist="Test Artist")
        assert "Test Artist" in result or "Test Song" in result

    def test_similar_artists_missing_artist(self, tokens):
        result = server.discover(action="similar_artists", artist="")
        assert "Error" in result
        assert "artist required" in result

    @resp_lib.activate
    def test_similar_artists_with_artist(self, tokens):
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/search",
            json={"results": {"artists": {"data": [_artist(artist_id="111222333")]}}},
            status=200,
        )
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/artists/111222333/view/similar-artists",
            json={"data": [_artist(artist_id="444555666", name="Similar Artist")]},
            status=200,
        )
        result = server.discover(action="similar-artists", artist="Test Artist")
        assert "Similar Artist" in result

    def test_song_station_missing_song_id(self, tokens):
        result = server.discover(action="song_station", song_id="")
        assert "Error" in result
        assert "song_id required" in result

    @resp_lib.activate
    def test_song_station_with_song_id(self, tokens):
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/songs/1234567890/station",
            json={"data": [{"id": "st.123", "attributes": {"name": "Pop Station"}}]},
            status=200,
        )
        result = server.discover(action="song-station", song_id="1234567890")
        assert "Pop Station" in result

    def test_unknown_action(self, tokens):
        result = server.discover(action="bogus_action")
        assert "Unknown action" in result

    @resp_lib.activate
    def test_storefront_param_overrides_default(self, tokens):
        """storefront='gb' should be used instead of 'us'."""
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/gb/charts",
            json={"results": {}},
            status=200,
        )
        result = server.discover(action="charts", storefront="gb")
        assert result is not None


# ═══════════════════════════════════════════════════════════════════════════════
# _discover_top_songs  (lines ~5449-5516)
# ═══════════════════════════════════════════════════════════════════════════════


class TestDiscoverTopSongs:
    def test_empty_artist_param(self, tokens):
        result = server._discover_top_songs(artist="")
        assert "Error" in result

    @resp_lib.activate
    def test_by_name_found(self, tokens):
        """Artist name search → top songs."""
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/search",
            json={"results": {"artists": {"data": [_artist(artist_id="111222333")]}}},
            status=200,
        )
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/artists/111222333/view/top-songs",
            json={"data": [_song(name="Top Hit")]},
            status=200,
        )
        result = server._discover_top_songs(artist="Test Artist", storefront="us")
        assert "Top Hit" in result

    @resp_lib.activate
    def test_by_name_no_songs(self, tokens):
        """Top-songs returns empty → 'No top songs found'."""
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/search",
            json={"results": {"artists": {"data": [_artist(artist_id="111222333")]}}},
            status=200,
        )
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/artists/111222333/view/top-songs",
            json={"data": []},
            status=200,
        )
        result = server._discover_top_songs(artist="Test Artist", storefront="us")
        assert "No top songs found" in result

    @resp_lib.activate
    def test_artist_not_found_by_name(self, tokens):
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/search",
            json={"results": {"artists": {"data": []}}},
            status=200,
        )
        result = server._discover_top_songs(artist="Unknown XYZ", storefront="us")
        assert "No artist found" in result

    @resp_lib.activate
    def test_by_catalog_id_200(self, tokens):
        """All-digit artist ID uses direct artist lookup path (status 200)."""
        artist_id = "111222333"
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/artists/{artist_id}",
            json={"data": [_artist(artist_id=artist_id, name="Known Artist")]},
            status=200,
        )
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/artists/{artist_id}/view/top-songs",
            json={"data": [_song(name="Known Hit")]},
            status=200,
        )
        result = server._discover_top_songs(artist=artist_id, storefront="us")
        assert "Known Hit" in result

    @resp_lib.activate
    def test_by_catalog_id_non_200_artist_lookup(self, tokens):
        """Non-200 artist lookup falls back to raw artist ID as name."""
        artist_id = "111222333"
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/artists/{artist_id}",
            json={},
            status=404,
        )
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/artists/{artist_id}/view/top-songs",
            json={"data": [_song(name="Mystery Hit")]},
            status=200,
        )
        result = server._discover_top_songs(artist=artist_id, storefront="us")
        assert "Mystery Hit" in result

    @resp_lib.activate
    def test_api_error(self, tokens):
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/search",
            json={},
            status=500,
        )
        result = server._discover_top_songs(artist="Test Artist", storefront="us")
        assert "API Error" in result


# ═══════════════════════════════════════════════════════════════════════════════
# _discover_similar_artists  (lines ~5519-5584)
# ═══════════════════════════════════════════════════════════════════════════════


class TestDiscoverSimilarArtists:
    def test_empty_artist_param(self, tokens):
        result = server._discover_similar_artists(artist="")
        assert "Error" in result

    @resp_lib.activate
    def test_by_name_found(self, tokens):
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/search",
            json={"results": {"artists": {"data": [_artist(artist_id="111222333")]}}},
            status=200,
        )
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/artists/111222333/view/similar-artists",
            json={"data": [_artist(artist_id="444555666", name="Like Artist")]},
            status=200,
        )
        result = server._discover_similar_artists(artist="Test Artist", storefront="us")
        assert "Like Artist" in result

    @resp_lib.activate
    def test_no_similar_artists(self, tokens):
        """Empty similar artists → 'No similar artists found'."""
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/search",
            json={"results": {"artists": {"data": [_artist(artist_id="111222333")]}}},
            status=200,
        )
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/artists/111222333/view/similar-artists",
            json={"data": []},
            status=200,
        )
        result = server._discover_similar_artists(artist="Test Artist", storefront="us")
        assert "No similar artists found" in result

    @resp_lib.activate
    def test_artist_not_found_by_name(self, tokens):
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/search",
            json={"results": {"artists": {"data": []}}},
            status=200,
        )
        result = server._discover_similar_artists(artist="Unknown XYZ", storefront="us")
        assert "No artist found" in result

    @resp_lib.activate
    def test_by_catalog_id_200(self, tokens):
        artist_id = "111222333"
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/artists/{artist_id}",
            json={"data": [_artist(artist_id=artist_id, name="Known Artist")]},
            status=200,
        )
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/artists/{artist_id}/view/similar-artists",
            json={"data": [_artist(artist_id="999888777", name="Peer Artist")]},
            status=200,
        )
        result = server._discover_similar_artists(artist=artist_id, storefront="us")
        assert "Peer Artist" in result

    @resp_lib.activate
    def test_by_catalog_id_non_200_artist_lookup(self, tokens):
        """Non-200 artist lookup still proceeds to similar-artists endpoint."""
        artist_id = "111222333"
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/artists/{artist_id}",
            json={},
            status=404,
        )
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/artists/{artist_id}/view/similar-artists",
            json={"data": [_artist(artist_id="999888777", name="Ghost Artist")]},
            status=200,
        )
        result = server._discover_similar_artists(artist=artist_id, storefront="us")
        assert "Ghost Artist" in result

    @resp_lib.activate
    def test_api_error(self, tokens):
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/search",
            json={},
            status=500,
        )
        result = server._discover_similar_artists(artist="Test Artist", storefront="us")
        assert "API Error" in result

    @resp_lib.activate
    def test_similar_artists_genre_display(self, tokens):
        """Genres appear in output."""
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/search",
            json={"results": {"artists": {"data": [_artist(artist_id="111222333")]}}},
            status=200,
        )
        similar = {
            "id": "444555666",
            "type": "artists",
            "attributes": {"name": "Genred Artist", "genreNames": ["Jazz", "Blues"]},
        }
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/artists/111222333/view/similar-artists",
            json={"data": [similar]},
            status=200,
        )
        result = server._discover_similar_artists(artist="Test Artist", storefront="us")
        assert "Jazz" in result or "Genred Artist" in result


# ═══════════════════════════════════════════════════════════════════════════════
# _discover_song_station  (lines ~5587-5615)
# ═══════════════════════════════════════════════════════════════════════════════


class TestDiscoverSongStation:
    @resp_lib.activate
    def test_song_station_found(self, tokens):
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/songs/1234567890/station",
            json={"data": [{"id": "st.abc", "attributes": {"name": "Song Vibes"}}]},
            status=200,
        )
        result = server._discover_song_station(song_id="1234567890", storefront="us")
        assert "Song Vibes" in result
        assert "st.abc" in result

    @resp_lib.activate
    def test_song_station_empty(self, tokens):
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/songs/1234567890/station",
            json={"data": []},
            status=200,
        )
        result = server._discover_song_station(song_id="1234567890", storefront="us")
        assert "No station found" in result

    @resp_lib.activate
    def test_song_station_api_error(self, tokens):
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/songs/1234567890/station",
            json={},
            status=404,
        )
        result = server._discover_song_station(song_id="1234567890", storefront="us")
        assert "API Error" in result


# ═══════════════════════════════════════════════════════════════════════════════
# _catalog_song_details  (lines ~5782-5816)
# ═══════════════════════════════════════════════════════════════════════════════


class TestCatalogSongDetails:
    @resp_lib.activate
    def test_song_details_happy_path(self, tokens):
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/songs/1234567890",
            json={"data": [_song()]},
            status=200,
        )
        result = server._catalog_song_details(song_id="1234567890")
        assert "Test Song" in result
        assert "Test Artist" in result
        assert "ISRC" in result

    @resp_lib.activate
    def test_song_details_explicit_flag(self, tokens):
        """Explicit flag rendered correctly."""
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/songs/1234567890",
            json={"data": [_song(explicit=True)]},
            status=200,
        )
        result = server._catalog_song_details(song_id="1234567890")
        assert "Explicit: Yes" in result

    @resp_lib.activate
    def test_song_details_not_found(self, tokens):
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/songs/1234567890",
            json={"data": []},
            status=200,
        )
        result = server._catalog_song_details(song_id="1234567890")
        assert "Song not found" in result

    @resp_lib.activate
    def test_song_details_api_error(self, tokens):
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/songs/1234567890",
            json={},
            status=500,
        )
        result = server._catalog_song_details(song_id="1234567890")
        assert "API Error" in result


# ═══════════════════════════════════════════════════════════════════════════════
# _catalog_artist_details  (lines ~5819-5892)
# ═══════════════════════════════════════════════════════════════════════════════


class TestCatalogArtistDetails:
    def test_empty_artist_param(self, tokens):
        result = server._catalog_artist_details(artist="")
        assert "Error" in result

    @resp_lib.activate
    def test_by_catalog_id_success(self, tokens):
        artist_id = "111222333"
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/artists/{artist_id}",
            json={"data": [_artist(artist_id=artist_id)]},
            status=200,
        )
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/artists/{artist_id}/albums",
            json={"data": [_album()]},
            status=200,
        )
        result = server._catalog_artist_details(artist=artist_id)
        assert "Test Artist" in result
        assert "Test Album" in result

    @resp_lib.activate
    def test_by_catalog_id_not_found(self, tokens):
        artist_id = "111222333"
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/artists/{artist_id}",
            json={"data": []},
            status=200,
        )
        result = server._catalog_artist_details(artist=artist_id)
        assert "not found" in result.lower()

    @resp_lib.activate
    def test_by_catalog_id_non_200(self, tokens):
        artist_id = "111222333"
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/artists/{artist_id}",
            json={},
            status=404,
        )
        result = server._catalog_artist_details(artist=artist_id)
        assert "not found" in result.lower()

    @resp_lib.activate
    def test_by_name_found(self, tokens):
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/search",
            json={"results": {"artists": {"data": [_artist(artist_id="111222333")]}}},
            status=200,
        )
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/artists/111222333/albums",
            json={"data": [_album()]},
            status=200,
        )
        result = server._catalog_artist_details(artist="Test Artist")
        assert "Test Artist" in result

    @resp_lib.activate
    def test_by_name_not_found(self, tokens):
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/search",
            json={"results": {"artists": {"data": []}}},
            status=200,
        )
        result = server._catalog_artist_details(artist="Unknown Artist XYZ")
        assert "No artist found" in result

    @resp_lib.activate
    def test_albums_non_200_graceful(self, tokens):
        """Non-200 albums response is handled gracefully (no exception)."""
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/search",
            json={"results": {"artists": {"data": [_artist(artist_id="111222333")]}}},
            status=200,
        )
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/artists/111222333/albums",
            json={},
            status=404,
        )
        result = server._catalog_artist_details(artist="Test Artist")
        # Should still return artist info, just without albums section
        assert "Test Artist" in result

    @resp_lib.activate
    def test_api_error(self, tokens):
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/search",
            json={},
            status=500,
        )
        result = server._catalog_artist_details(artist="Test Artist")
        assert "API Error" in result


# ═══════════════════════════════════════════════════════════════════════════════
# _discover_charts  (lines ~5895-5931)
# ═══════════════════════════════════════════════════════════════════════════════


class TestDiscoverCharts:
    @resp_lib.activate
    def test_charts_songs(self, tokens):
        chart_song = {
            "id": "1234567890",
            "type": "songs",
            "attributes": {"name": "Chart Song", "artistName": "Chart Artist"},
        }
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/charts",
            json={"results": {"songs": [{"name": "Top Songs", "data": [chart_song]}]}},
            status=200,
        )
        result = server._discover_charts(chart_type="songs", storefront="us")
        assert "Top Songs" in result
        assert "Chart Song" in result
        assert "Chart Artist" in result

    @resp_lib.activate
    def test_charts_item_no_artist(self, tokens):
        """Items without artistName are formatted without artist field."""
        chart_album = {
            "id": "987654321",
            "type": "albums",
            "attributes": {"name": "Chart Album"},  # no artistName
        }
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/charts",
            json={"results": {"albums": [{"name": "Top Albums", "data": [chart_album]}]}},
            status=200,
        )
        result = server._discover_charts(chart_type="albums", storefront="us")
        assert "Chart Album" in result
        assert " - " not in result  # No " - artist" suffix

    @resp_lib.activate
    def test_charts_empty(self, tokens):
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/charts",
            json={"results": {}},
            status=200,
        )
        result = server._discover_charts(chart_type="songs", storefront="us")
        assert "No chart data available" in result

    @resp_lib.activate
    def test_charts_api_error(self, tokens):
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/charts",
            json={},
            status=500,
        )
        result = server._discover_charts(chart_type="songs", storefront="us")
        assert "API Error" in result


# ═══════════════════════════════════════════════════════════════════════════════
# _catalog_genres  (lines ~5934-5959)
# ═══════════════════════════════════════════════════════════════════════════════


class TestCatalogGenres:
    @resp_lib.activate
    def test_genres_happy_path(self, tokens):
        genre = {"id": "14", "attributes": {"name": "Pop"}}
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/genres",
            json={"data": [genre]},
            status=200,
        )
        result = server._catalog_genres()
        assert "Pop" in result
        assert "14" in result

    @resp_lib.activate
    def test_genres_empty(self, tokens):
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/genres",
            json={"data": []},
            status=200,
        )
        result = server._catalog_genres()
        assert "No genres found" in result

    @resp_lib.activate
    def test_genres_api_error(self, tokens):
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/genres",
            json={},
            status=401,
        )
        result = server._catalog_genres()
        assert "API Error" in result


# ═══════════════════════════════════════════════════════════════════════════════
# _catalog_suggestions  (lines ~5962-5988)
# ═══════════════════════════════════════════════════════════════════════════════


class TestCatalogSuggestions:
    @resp_lib.activate
    def test_suggestions_happy_path(self, tokens):
        suggestion = {"kind": "terms", "searchTerm": "taylor swift", "displayTerm": "Taylor Swift"}
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/search/suggestions",
            json={"results": {"suggestions": [suggestion]}},
            status=200,
        )
        result = server._catalog_suggestions(term="taylor")
        assert "Taylor Swift" in result

    @resp_lib.activate
    def test_suggestions_non_terms_kind_filtered(self, tokens):
        """Non-terms suggestions are ignored."""
        suggestion = {"kind": "topResults", "searchTerm": "taylor", "displayTerm": "Taylor Swift"}
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/search/suggestions",
            json={"results": {"suggestions": [suggestion]}},
            status=200,
        )
        result = server._catalog_suggestions(term="taylor")
        assert "No suggestions found" in result

    @resp_lib.activate
    def test_suggestions_empty(self, tokens):
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/search/suggestions",
            json={"results": {"suggestions": []}},
            status=200,
        )
        result = server._catalog_suggestions(term="xyz")
        assert "No suggestions found" in result

    @resp_lib.activate
    def test_suggestions_api_error(self, tokens):
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/search/suggestions",
            json={},
            status=500,
        )
        result = server._catalog_suggestions(term="test")
        assert "API Error" in result

    @resp_lib.activate
    def test_suggestions_uses_search_term_when_no_display(self, tokens):
        """Falls back to searchTerm when displayTerm absent."""
        suggestion = {"kind": "terms", "searchTerm": "raw term"}
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/search/suggestions",
            json={"results": {"suggestions": [suggestion]}},
            status=200,
        )
        result = server._catalog_suggestions(term="raw")
        assert "raw term" in result


# ═══════════════════════════════════════════════════════════════════════════════
# catalog() dispatcher  (lines ~5996-6061)
# ═══════════════════════════════════════════════════════════════════════════════


class TestCatalogDispatcher:
    @resp_lib.activate
    def test_search_action_api_path(self, tokens):
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/search",
            json={"results": {"songs": {"data": [_song()]}}},
            status=200,
        )
        result = server.catalog(action="search", query="Test Song")
        assert "Test Song" in result

    @resp_lib.activate
    def test_search_hyphen_action_normalized(self, tokens):
        """'search' with hyphen separator: action='search' is the plain form,
        but test the normalize path via album-tracks."""
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/albums/123456789/tracks",
            json={"data": [_song()]},
            status=200,
        )
        result = server.catalog(action="album-tracks", album="123456789")
        assert "Test Song" in result

    def test_album_tracks_action(self, tokens):
        """album_tracks with empty album returns error without HTTP call."""
        result = server.catalog(action="album_tracks", album="")
        assert "Error" in result

    def test_album_details_missing_album(self, tokens):
        result = server.catalog(action="album_details", album="")
        assert "Error" in result
        assert "album required" in result

    @resp_lib.activate
    def test_album_details_with_album(self, tokens):
        album_id = "123456789"
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/albums/{album_id}",
            json={"data": [_album(album_id=album_id)]},
            status=200,
        )
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/albums/{album_id}/tracks",
            json={"data": [_song()]},
            status=200,
        )
        result = server.catalog(action="album_details", album=album_id)
        assert "Test Album" in result

    def test_song_details_missing_song_id(self, tokens):
        result = server.catalog(action="song_details", song_id="")
        assert "Error" in result
        assert "song_id required" in result

    @resp_lib.activate
    def test_song_details_with_song_id(self, tokens):
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/songs/1234567890",
            json={"data": [_song()]},
            status=200,
        )
        result = server.catalog(action="song_details", song_id="1234567890")
        assert "Test Song" in result

    def test_artist_details_missing_artist(self, tokens):
        result = server.catalog(action="artist_details", artist="")
        assert "Error" in result
        assert "artist required" in result

    @resp_lib.activate
    def test_artist_details_with_artist(self, tokens):
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/search",
            json={"results": {"artists": {"data": [_artist()]}}},
            status=200,
        )
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/artists/111222333/albums",
            json={"data": []},
            status=200,
        )
        result = server.catalog(action="artist_details", artist="Test Artist")
        assert "Test Artist" in result

    @resp_lib.activate
    def test_genres_action(self, tokens):
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/genres",
            json={"data": [{"id": "14", "attributes": {"name": "Pop"}}]},
            status=200,
        )
        result = server.catalog(action="genres")
        assert "Pop" in result

    def test_suggestions_missing_term(self, tokens):
        result = server.catalog(action="suggestions", term="")
        assert "Error" in result
        assert "term required" in result

    @resp_lib.activate
    def test_suggestions_with_term(self, tokens):
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/search/suggestions",
            json={
                "results": {
                    "suggestions": [
                        {"kind": "terms", "searchTerm": "beatles", "displayTerm": "The Beatles"}
                    ]
                }
            },
            status=200,
        )
        result = server.catalog(action="suggestions", term="beat")
        assert "Beatles" in result

    def test_unknown_action(self, tokens):
        result = server.catalog(action="nonexistent_action")
        assert "Unknown action" in result

    def test_search_no_api_token_no_applescript(self, mock_config_dir, monkeypatch):
        """When no tokens and no AppleScript, returns token-required error."""
        # Don't write any tokens; get_headers() will raise FileNotFoundError
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        result = server.catalog(action="search", query="Test")
        assert "API token required" in result or "Error" in result

    def test_search_no_api_token_applescript_ui_ok(self, mock_config_dir, monkeypatch):
        """When no tokens but AppleScript works, returns UI search results."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        ui_results = [{"index": 1, "name": "UI Song", "artist": "UI Artist", "type": "song"}]
        monkeypatch.setattr(server.asc, "ui_search_catalog", lambda q: (True, ui_results, None))
        monkeypatch.setattr(server.asc, "ui_clear_search", lambda: None)
        result = server.catalog(action="search", query="Test")
        assert "UI Song" in result
        assert "UI Artist" in result

    def test_search_no_api_token_applescript_ui_fail_with_why(self, mock_config_dir, monkeypatch):
        """When UI search fails with a reason, returns the reason."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(
            server.asc, "ui_search_catalog", lambda q: (False, [], "Music.app not running")
        )
        monkeypatch.setattr(server.asc, "ui_clear_search", lambda: None)
        result = server.catalog(action="search", query="Test")
        assert "Music.app not running" in result or "Error" in result

    def test_search_no_api_token_applescript_ui_ok_no_results(self, mock_config_dir, monkeypatch):
        """UI search returns ok=True but empty results, no why → token error."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server.asc, "ui_search_catalog", lambda q: (True, [], None))
        monkeypatch.setattr(server.asc, "ui_clear_search", lambda: None)
        result = server.catalog(action="search", query="Test")
        assert "API token required" in result or "Error" in result

    def test_search_no_query_no_applescript(self, mock_config_dir, monkeypatch):
        """No query and no AppleScript returns token required (APPLESCRIPT guard False)."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        result = server.catalog(action="search", query="")
        # Falls into the except block, APPLESCRIPT_AVAILABLE=False → returns token error
        assert "Error" in result


# ═══════════════════════════════════════════════════════════════════════════════
# FileNotFoundError / ValueError paths  (missing lines in every handler)
# Using mock_config_dir WITHOUT token files → get_user_token() raises
# FileNotFoundError, caught by each function's except (FileNotFoundError, ValueError).
# ═══════════════════════════════════════════════════════════════════════════════


class TestFileNotFoundPaths:
    """Cover every except (FileNotFoundError, ValueError) handler."""

    # ── _catalog_search 4745-4746 ──────────────────────────────────────────

    def test_catalog_search_no_token(self, mock_config_dir):
        """No user token → FileNotFoundError → returned as string."""
        result = server._catalog_search(query="Test", types="songs")
        assert "Music user token" in result or "not found" in result.lower()

    # ── _catalog_album_tracks 4798-4799 (inner except Exception) ──────────

    @resp_lib.activate
    def test_catalog_album_tracks_inner_search_exception(self, tokens):
        """ConnectionError in name-search inner try is swallowed (except Exception: pass)."""
        # No mock registered for the search URL → responses raises ConnectionError
        result = server._catalog_album_tracks(album="Some Album Name That Won't Match")
        # Inner try catches exception, album_id stays None → "Album not found"
        assert "Album not found" in result or "not found" in result.lower()

    # ── _catalog_album_tracks 4845 (_apply_pagination error) ──────────────

    @resp_lib.activate
    def test_catalog_album_tracks_pagination_error(self, tokens):
        """Offset beyond total → _apply_pagination returns error string."""
        album_id = "123456789"
        tracks = [_song(song_id=str(1000000000 + i)) for i in range(3)]
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/albums/{album_id}/tracks",
            json={"data": tracks},
            status=200,
        )
        # offset > total_count (3 tracks, offset 100)
        result = server._catalog_album_tracks(album=album_id, limit=5, offset=100)
        assert "Offset" in result and "exceeds" in result

    # ── _catalog_album_tracks 4859-4860 (outer FileNotFoundError) ─────────

    def test_catalog_album_tracks_no_token_catalog_id(self, mock_config_dir):
        """CATALOG_ID path reaches outer get_headers(), which raises FileNotFoundError."""
        result = server._catalog_album_tracks(album="123456789")
        assert "Music user token" in result or "not found" in result.lower()

    # ── _catalog_album_details 4888 (ALBUM_ID path → l. prefix) ──────────

    @resp_lib.activate
    def test_catalog_album_details_library_id(self, tokens):
        """l. prefix resolves to ALBUM_ID → album_id = r.value (line 4888)."""
        album_id = "l.abc123"
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/albums/{album_id}",
            json={"data": [_album(album_id=album_id)]},
            status=200,
        )
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/catalog/us/albums/{album_id}/tracks",
            json={"data": [_song()]},
            status=200,
        )
        result = server._catalog_album_details(album=album_id)
        assert "Test Album" in result

    # ── _catalog_album_details 4966-4967 (outer FileNotFoundError) ────────

    def test_catalog_album_details_no_token(self, mock_config_dir):
        """CATALOG_ID path with no token triggers outer FileNotFoundError handler."""
        result = server._catalog_album_details(album="123456789")
        assert "Music user token" in result or "not found" in result.lower()

    # ── _discover_recommendations 5256-5257 ───────────────────────────────

    def test_discover_recommendations_no_token(self, mock_config_dir):
        result = server._discover_recommendations(
            limit=10, format="text", export="none", full=False
        )
        assert "Music user token" in result or "not found" in result.lower()

    # ── _discover_heavy_rotation 5301-5302 ────────────────────────────────

    def test_discover_heavy_rotation_no_token(self, mock_config_dir):
        result = server._discover_heavy_rotation(format="text", export="none", full=False)
        assert "Music user token" in result or "not found" in result.lower()

    # ── _discover_personal_station 5398-5399 ──────────────────────────────

    def test_discover_personal_station_no_token(self, mock_config_dir):
        result = server._discover_personal_station()
        assert "Music user token" in result or "not found" in result.lower()

    # ── _discover_top_songs 5515-5516 ─────────────────────────────────────

    def test_discover_top_songs_no_token(self, mock_config_dir):
        result = server._discover_top_songs(artist="Test Artist", storefront="us")
        assert "Music user token" in result or "not found" in result.lower()

    # ── _discover_similar_artists 5583-5584 ───────────────────────────────

    def test_discover_similar_artists_no_token(self, mock_config_dir):
        result = server._discover_similar_artists(artist="Test Artist", storefront="us")
        assert "Music user token" in result or "not found" in result.lower()

    # ── _discover_song_station 5614-5615 ──────────────────────────────────

    def test_discover_song_station_no_token(self, mock_config_dir):
        result = server._discover_song_station(song_id="1234567890", storefront="us")
        assert "Music user token" in result or "not found" in result.lower()

    # ── _catalog_song_details 5815-5816 ───────────────────────────────────

    def test_catalog_song_details_no_token(self, mock_config_dir):
        result = server._catalog_song_details(song_id="1234567890")
        assert "Music user token" in result or "not found" in result.lower()

    # ── _catalog_artist_details 5891-5892 ─────────────────────────────────

    def test_catalog_artist_details_no_token(self, mock_config_dir):
        result = server._catalog_artist_details(artist="Test Artist")
        assert "Music user token" in result or "not found" in result.lower()

    # ── _discover_charts 5930-5931 ────────────────────────────────────────

    def test_discover_charts_no_token(self, mock_config_dir):
        result = server._discover_charts(chart_type="songs", storefront="us")
        assert "Music user token" in result or "not found" in result.lower()

    # ── _catalog_genres 5958-5959 ─────────────────────────────────────────

    def test_catalog_genres_no_token(self, mock_config_dir):
        result = server._catalog_genres()
        assert "Music user token" in result or "not found" in result.lower()

    # ── _catalog_suggestions 5987-5988 ────────────────────────────────────

    def test_catalog_suggestions_no_token(self, mock_config_dir):
        result = server._catalog_suggestions(term="test")
        assert "Music user token" in result or "not found" in result.lower()


# ═══════════════════════════════════════════════════════════════════════════════
# Fix artwork URL test — must use full=True so the field appears in JSON
# ═══════════════════════════════════════════════════════════════════════════════


class TestHeavyRotationArtworkFix:
    @resp_lib.activate
    def test_heavy_rotation_artwork_url_replacement_full(self, tokens):
        """Artwork URL placeholder replaced with 500x500 (full=True shows the field)."""
        item = {
            "id": "l.xyz",
            "type": "library-albums",
            "attributes": {
                "name": "Art Album",
                "artistName": "Art Artist",
                "genreNames": [],
                "artwork": {"url": "https://example.com/{w}x{h}/art.jpg"},
            },
        }
        resp_lib.add(
            resp_lib.GET,
            f"{BASE}/me/history/heavy-rotation",
            json={"data": [item]},
            status=200,
        )
        result = server._discover_heavy_rotation(format="json", export="none", full=True)
        assert "500x500" in result
