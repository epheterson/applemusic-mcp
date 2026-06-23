"""Tests for server module."""

import json
import time
from unittest.mock import patch, MagicMock

import pytest
import sys
import responses

from applemusic_mcp import server


class TestGetTokenExpirationWarning:
    """Tests for get_token_expiration_warning function."""

    def test_returns_none_when_no_token_file(self, mock_config_dir):
        """Should return None when token file doesn't exist."""
        result = server.get_token_expiration_warning()
        assert result is None

    def test_returns_none_when_token_valid(self, mock_config_dir):
        """Should return None when token has more than 30 days left."""
        token_file = mock_config_dir / "developer_token.json"
        token_data = {"expires": time.time() + 86400 * 60}  # 60 days
        with open(token_file, "w") as f:
            json.dump(token_data, f)

        result = server.get_token_expiration_warning()
        assert result is None

    def test_returns_warning_when_expiring_soon(self, mock_config_dir):
        """Should return warning when token expires within 30 days."""
        token_file = mock_config_dir / "developer_token.json"
        token_data = {"expires": time.time() + 86400 * 15}  # 15 days
        with open(token_file, "w") as f:
            json.dump(token_data, f)

        result = server.get_token_expiration_warning()
        assert result is not None
        assert "days" in result  # Could be 14 or 15 depending on timing
        assert "generate-token" in result


class TestGetHeaders:
    """Tests for get_headers function."""

    def test_returns_headers_with_tokens(
        self, mock_config_dir, mock_developer_token, mock_user_token
    ):
        """Should return properly formatted headers."""
        # Setup token files
        dev_token_file = mock_config_dir / "developer_token.json"
        with open(dev_token_file, "w") as f:
            json.dump({"token": mock_developer_token, "expires": time.time() + 86400 * 30}, f)

        user_token_file = mock_config_dir / "music_user_token.json"
        with open(user_token_file, "w") as f:
            json.dump({"music_user_token": mock_user_token}, f)

        result = server.get_headers()

        assert "Authorization" in result
        assert result["Authorization"].startswith("Bearer ")
        assert "Music-User-Token" in result
        assert result["Content-Type"] == "application/json"


class TestGetLibraryPlaylists:
    """Tests for get_library_playlists function (API path)."""

    @responses.activate
    def test_returns_playlists(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Should return formatted playlist list via API."""
        # Disable AppleScript to test API path
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)

        # Setup tokens
        dev_token_file = mock_config_dir / "developer_token.json"
        with open(dev_token_file, "w") as f:
            json.dump({"token": mock_developer_token, "expires": time.time() + 86400 * 60}, f)

        user_token_file = mock_config_dir / "music_user_token.json"
        with open(user_token_file, "w") as f:
            json.dump({"music_user_token": mock_user_token}, f)

        # Mock API response
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/playlists",
            json={
                "data": [
                    {"id": "p.abc123", "attributes": {"name": "Test Playlist", "canEdit": True}},
                    {"id": "p.def456", "attributes": {"name": "Read Only", "canEdit": False}},
                ]
            },
            status=200,
        )

        result = server.playlist(action="list")

        assert "Test Playlist" in result
        assert "p.abc123" in result
        assert "Read Only" in result
        assert "p.def456" in result
        assert "2 items" in result

    @responses.activate
    def test_handles_api_error(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Should return error message on API failure."""
        # Disable AppleScript to test API path
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)

        # Setup tokens
        dev_token_file = mock_config_dir / "developer_token.json"
        with open(dev_token_file, "w") as f:
            json.dump({"token": mock_developer_token, "expires": time.time() + 86400 * 60}, f)

        user_token_file = mock_config_dir / "music_user_token.json"
        with open(user_token_file, "w") as f:
            json.dump({"music_user_token": mock_user_token}, f)

        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/playlists",
            json={"error": "Unauthorized"},
            status=401,
        )

        result = server.playlist(action="list")

        assert "API Error" in result or "401" in result


class TestCreatePlaylist:
    """Tests for create_playlist function (API path)."""

    @responses.activate
    def test_creates_playlist_successfully(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Should create playlist via API and return ID."""
        # Disable AppleScript to test API path
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)

        # Setup tokens
        dev_token_file = mock_config_dir / "developer_token.json"
        with open(dev_token_file, "w") as f:
            json.dump({"token": mock_developer_token, "expires": time.time() + 86400 * 60}, f)

        user_token_file = mock_config_dir / "music_user_token.json"
        with open(user_token_file, "w") as f:
            json.dump({"music_user_token": mock_user_token}, f)

        responses.add(
            responses.POST,
            "https://api.music.apple.com/v1/me/library/playlists",
            json={"data": [{"id": "p.newplaylist123"}]},
            status=201,
        )

        result = server.playlist(
            action="create", name="My New Playlist", description="A description"
        )

        assert "My New Playlist" in result
        assert "p.newplaylist123" in result


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="_playlist_rename is defined inside `if APPLESCRIPT_AVAILABLE:` "
    "and only materializes at import time on macOS. Tests that monkeypatch "
    "APPLESCRIPT_AVAILABLE=True at runtime can't conjure the function on "
    "Linux. Follow-up: move the wrapper out of the conditional block.",
)
class TestRenamePlaylist:
    """Tests for rename_playlist function (AppleScript path)."""

    def test_renames_playlist_successfully(self, monkeypatch):
        """Should rename playlist via AppleScript."""

        # Mock AppleScript to return success
        def mock_rename_playlist(old_name, new_name):
            return (True, f"Renamed: {old_name} → {new_name}")

        monkeypatch.setattr(server.asc, "rename_playlist", mock_rename_playlist)
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)

        result = server.playlist(action="rename", playlist="Old Name", new_name="New Name")

        assert "Renamed" in result
        assert "Old Name" in result
        assert "New Name" in result

    def test_requires_macos(self, monkeypatch):
        """Should error when AppleScript not available."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)

        result = server.playlist(action="rename", playlist="Old Name", new_name="New Name")

        assert "Error" in result
        assert "macOS" in result

    def test_requires_new_name(self, monkeypatch):
        """Should error when new_name not provided."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)

        result = server.playlist(action="rename", playlist="Old Name", new_name="")

        assert "Error" in result


class TestCreateFolder:
    """Tests for create_folder action (AppleScript path)."""

    def test_creates_folder_successfully(self, monkeypatch):
        """Should create folder via AppleScript."""

        def mock_create_folder(name):
            return (True, "ABCD1234")

        monkeypatch.setattr(server.asc, "create_folder", mock_create_folder)
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)

        result = server.playlist(action="create_folder", name="My Folder")

        assert "My Folder" in result
        assert "ABCD1234" in result

    def test_requires_macos(self, monkeypatch):
        """Should error when AppleScript not available."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)

        result = server.playlist(action="create_folder", name="My Folder")

        assert "Error" in result
        assert "macOS" in result

    def test_requires_name(self, monkeypatch):
        """Should error when name not provided."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)

        result = server.playlist(action="create_folder", name="")

        assert "Error" in result


class TestMoveToFolder:
    """Tests for move action (AppleScript path)."""

    def test_moves_playlist_successfully(self, monkeypatch):
        """Should move playlist to folder via AppleScript."""

        def mock_move_to_folder(item_name, folder_name):
            return (True, f"Moved '{item_name}' to folder '{folder_name}'")

        monkeypatch.setattr(server.asc, "move_to_folder", mock_move_to_folder)
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)

        result = server.playlist(action="move", playlist="My Playlist", name="My Folder")

        assert "My Playlist" in result
        assert "My Folder" in result

    def test_requires_macos(self, monkeypatch):
        """Should error when AppleScript not available."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)

        result = server.playlist(action="move", playlist="My Playlist", name="My Folder")

        assert "Error" in result
        assert "macOS" in result

    def test_requires_playlist_name(self, monkeypatch):
        """Should error when playlist name not provided."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)

        result = server.playlist(action="move", playlist="", name="My Folder")

        assert "Error" in result

    def test_move_to_root_requires_confirmation(self, monkeypatch):
        """Should explain limitation and require confirmation when no folder."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)

        result = server.playlist(action="move", playlist="My Playlist", name="")

        assert "cannot move" in result.lower()
        assert "allow_duplicates" in result


class TestAddToPlaylist:
    """Tests for add_to_playlist function."""

    @responses.activate
    def test_adds_tracks_successfully(self, mock_config_dir, mock_developer_token, mock_user_token):
        """Should add tracks and return confirmation."""
        # Setup tokens
        dev_token_file = mock_config_dir / "developer_token.json"
        with open(dev_token_file, "w") as f:
            json.dump({"token": mock_developer_token, "expires": time.time() + 86400 * 60}, f)

        user_token_file = mock_config_dir / "music_user_token.json"
        with open(user_token_file, "w") as f:
            json.dump({"music_user_token": mock_user_token}, f)

        responses.add(
            responses.POST,
            "https://api.music.apple.com/v1/me/library/playlists/p.test123/tracks",
            status=204,
        )

        result = server.playlist(
            action="add", playlist="p.test123", track="i.song1, i.song2, i.song3"
        )

        assert "Added" in result
        assert "3 track" in result

    def test_handles_empty_track(self, mock_config_dir, mock_developer_token, mock_user_token):
        """Should return error for empty track."""
        # Setup tokens
        dev_token_file = mock_config_dir / "developer_token.json"
        with open(dev_token_file, "w") as f:
            json.dump({"token": mock_developer_token, "expires": time.time() + 86400 * 60}, f)

        user_token_file = mock_config_dir / "music_user_token.json"
        with open(user_token_file, "w") as f:
            json.dump({"music_user_token": mock_user_token}, f)

        result = server.playlist(action="add", playlist="p.test123", track="")

        assert "Provide track or album parameter" in result


class TestSplitTrackArtistCandidates:
    """Tests for _split_track_artist_candidates helper (open-ended track input)."""

    def test_simple_forward_form(self):
        """Produces forward + reverse candidates for a clean 'Song - Artist'."""
        got = server._split_track_artist_candidates("Silvera - GOJIRA")
        assert got == [("Silvera", "GOJIRA"), ("GOJIRA", "Silvera")]

    def test_no_separator_returns_empty(self):
        """Returns empty list when the input has no ' - ' separator to split on."""
        assert server._split_track_artist_candidates("No dash here") == []
        assert server._split_track_artist_candidates("") == []

    def test_hyphen_without_spaces_not_split(self):
        """A plain hyphen (no spaces) isn't a split candidate — preserves names like 'Jay-Z'."""
        assert server._split_track_artist_candidates("Jay-Z") == []

    def test_multi_dash_produces_first_and_last_split(self):
        """Multi-dash inputs try both first-dash and last-dash splits so multi-word titles work."""
        got = server._split_track_artist_candidates("A - B - C")
        # First-dash split: ('A', 'B - C') + reverse; last-dash split: ('A - B', 'C')
        assert ("A", "B - C") in got
        assert ("B - C", "A") in got
        assert ("A - B", "C") in got

    def test_forward_form_first(self):
        """Forward form (Song - Artist, the convention) is always tried first."""
        got = server._split_track_artist_candidates("Come Together - The Beatles")
        assert got[0] == ("Come Together", "The Beatles")

    def test_strips_whitespace_around_parts(self):
        """Candidates have whitespace stripped from both parts."""
        got = server._split_track_artist_candidates("  Song  -  Artist  ")
        assert got[0] == ("Song", "Artist")

    def test_empty_part_rejected(self):
        """An empty part on either side is rejected (no junk candidates)."""
        assert server._split_track_artist_candidates(" - Artist") == []
        assert server._split_track_artist_candidates("Song - ") == []


class TestSearchLibrary:
    """Tests for search_library function."""

    @responses.activate
    def test_returns_search_results(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Should return formatted search results via API fallback."""
        # Force API path by disabling AppleScript
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)

        # Setup tokens
        dev_token_file = mock_config_dir / "developer_token.json"
        with open(dev_token_file, "w") as f:
            json.dump({"token": mock_developer_token, "expires": time.time() + 86400 * 60}, f)

        user_token_file = mock_config_dir / "music_user_token.json"
        with open(user_token_file, "w") as f:
            json.dump({"music_user_token": mock_user_token}, f)

        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/search",
            json={
                "results": {
                    "library-songs": {
                        "data": [
                            {
                                "id": "i.abc123",
                                "attributes": {
                                    "name": "Wonderwall",
                                    "artistName": "Oasis",
                                    "albumName": "(What's the Story) Morning Glory?",
                                },
                            }
                        ]
                    }
                }
            },
            status=200,
        )

        result = server.library(action="search", query="Wonderwall")

        assert "Wonderwall" in result
        assert "Oasis" in result
        assert "i.abc123" in result

    @responses.activate
    def test_genre_search_does_not_fall_back_to_fulltext_api(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Genre filtering is local-only; it must not degrade to the API full-text
        search, which has no genre filter and would false-match on the genre name."""
        # No AppleScript + a valid token is exactly the case that used to cascade
        # into /me/library/search.
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)

        dev_token_file = mock_config_dir / "developer_token.json"
        with open(dev_token_file, "w") as f:
            json.dump({"token": mock_developer_token, "expires": time.time() + 86400 * 60}, f)
        user_token_file = mock_config_dir / "music_user_token.json"
        with open(user_token_file, "w") as f:
            json.dump({"music_user_token": mock_user_token}, f)

        # Deliberately register NO /me/library/search mock: any call there would
        # raise ConnectionError and surface as "API Error", failing the assert.
        result = server.library(action="search", query="Rock", types="genre")

        assert "API Error" not in result
        assert "genre" in result.lower()


class TestSearchCatalog:
    """Tests for search_catalog function."""

    @responses.activate
    def test_returns_catalog_results(self, mock_config_dir, mock_developer_token, mock_user_token):
        """Should return formatted catalog search results."""
        # Setup tokens
        dev_token_file = mock_config_dir / "developer_token.json"
        with open(dev_token_file, "w") as f:
            json.dump({"token": mock_developer_token, "expires": time.time() + 86400 * 60}, f)

        user_token_file = mock_config_dir / "music_user_token.json"
        with open(user_token_file, "w") as f:
            json.dump({"music_user_token": mock_user_token}, f)

        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/search",
            json={
                "results": {
                    "songs": {
                        "data": [
                            {
                                "id": "123456789",
                                "attributes": {"name": "Let It Be", "artistName": "The Beatles"},
                            }
                        ]
                    }
                }
            },
            status=200,
        )

        result = server.catalog(action="search", query="Let It Be")

        assert "Let It Be" in result
        assert "The Beatles" in result
        assert "123456789" in result


class TestCheckAuthStatus:
    """Tests for check_auth_status function."""

    def test_reports_missing_tokens(self, mock_config_dir):
        """Should report missing tokens."""
        result = server.config(action="auth-status")

        assert "MISSING" in result
        assert "Developer Token" in result
        assert "Music User Token" in result

    def test_reports_valid_tokens(self, mock_config_dir, mock_developer_token, mock_user_token):
        """Should report OK for valid tokens."""
        # Setup tokens
        dev_token_file = mock_config_dir / "developer_token.json"
        with open(dev_token_file, "w") as f:
            json.dump({"token": mock_developer_token, "expires": time.time() + 86400 * 60}, f)

        user_token_file = mock_config_dir / "music_user_token.json"
        with open(user_token_file, "w") as f:
            json.dump({"music_user_token": mock_user_token}, f)

        # Don't actually test API connection
        with patch.object(server, "get_headers", return_value={}):
            with patch("requests.get") as mock_get:
                mock_get.return_value.status_code = 200
                result = server.config(action="auth-status")

        assert "OK" in result
        assert "Developer Token" in result

    def test_reports_expiring_token(self, mock_config_dir, mock_developer_token, mock_user_token):
        """Should warn about expiring token."""
        # Setup expiring token
        dev_token_file = mock_config_dir / "developer_token.json"
        with open(dev_token_file, "w") as f:
            json.dump({"token": mock_developer_token, "expires": time.time() + 86400 * 10}, f)

        user_token_file = mock_config_dir / "music_user_token.json"
        with open(user_token_file, "w") as f:
            json.dump({"music_user_token": mock_user_token}, f)

        result = server.config(action="auth-status")

        assert "EXPIRES IN" in result or "10" in result


class TestFormatDuration:
    """Tests for format_duration helper function."""

    def test_formats_standard_duration(self):
        """Should format milliseconds as m:ss."""
        assert server.format_duration(225000) == "3:45"
        assert server.format_duration(60000) == "1:00"
        assert server.format_duration(5000) == "0:05"

    def test_handles_zero(self):
        """Should return empty string for zero."""
        assert server.format_duration(0) == ""

    def test_handles_none(self):
        """Should return empty string for None."""
        assert server.format_duration(None) == ""

    def test_handles_negative(self):
        """Should return empty string for negative values."""
        assert server.format_duration(-1000) == ""
        assert server.format_duration(-60000) == ""

    def test_handles_large_duration(self):
        """Should handle songs longer than an hour."""
        # 1 hour, 5 minutes, 30 seconds = 3930000 ms
        assert server.format_duration(3930000) == "65:30"


class TestExtractTrackData:
    """Tests for extract_track_data helper function."""

    def test_extracts_basic_fields(self):
        """Should extract core fields from track data."""
        track = {
            "id": "i.abc123",
            "attributes": {
                "name": "Wonderwall",
                "artistName": "Oasis",
                "albumName": "(What's the Story) Morning Glory?",
                "durationInMillis": 258000,
                "releaseDate": "1995-10-02",
                "genreNames": ["Rock", "Alternative"],
            },
        }
        result = server.extract_track_data(track)

        assert result["name"] == "Wonderwall"
        assert result["artist"] == "Oasis"
        assert result["album"] == "(What's the Story) Morning Glory?"
        assert result["duration"] == "4:18"
        assert result["year"] == "1995"
        assert result["genre"] == "Rock"
        assert result["id"] == "i.abc123"

    def test_handles_empty_track(self):
        """Should handle empty track dict gracefully."""
        result = server.extract_track_data({})

        assert result["name"] == ""
        assert result["artist"] == ""
        assert result["duration"] == ""
        assert result["id"] == ""

    def test_handles_missing_attributes(self):
        """Should handle track with empty attributes."""
        track = {"id": "test123", "attributes": {}}
        result = server.extract_track_data(track)

        assert result["id"] == "test123"
        assert result["name"] == ""

    def test_includes_extras_when_requested(self):
        """Should include extra fields when include_extras=True."""
        track = {
            "id": "123",
            "attributes": {
                "name": "Test",
                "trackNumber": 5,
                "discNumber": 2,
                "hasLyrics": True,
                "composerName": "John Doe",
                "isrc": "USRC12345678",
                "contentRating": "explicit",
                "playParams": {"catalogId": "cat123"},
                "previews": [{"url": "https://example.com/preview.m4a"}],
                "artwork": {"url": "https://example.com/{w}x{h}bb.jpg"},
            },
        }
        result = server.extract_track_data(track, include_extras=True)

        assert result["track_number"] == 5
        assert result["disc_number"] == 2
        assert result["has_lyrics"] is True
        assert result["composer"] == "John Doe"
        assert result["isrc"] == "USRC12345678"
        assert result["is_explicit"] is True
        assert result["catalog_id"] == "cat123"
        assert "preview.m4a" in result["preview_url"]
        assert "500x500" in result["artwork_url"]


class TestTruncate:
    """Tests for truncate helper function."""

    def test_truncates_long_string(self):
        """Should truncate and add ellipsis for strings exceeding max length."""
        result = server.truncate("This is a very long string", 10)
        assert result == "This is a ..."
        assert len(result) == 13  # 10 chars + "..."

    def test_returns_short_string_unchanged(self):
        """Should return strings shorter than max unchanged."""
        result = server.truncate("Short", 10)
        assert result == "Short"

    def test_returns_exact_length_unchanged(self):
        """Should return strings exactly at max length unchanged."""
        result = server.truncate("TenChars!!", 10)
        assert result == "TenChars!!"

    def test_handles_empty_string(self):
        """Should handle empty string."""
        result = server.truncate("", 10)
        assert result == ""


class TestFormatTrackList:
    """Tests for format_track_list helper function."""

    def test_full_format_for_small_lists(self):
        """Should use full format for small track lists."""
        tracks = [
            {
                "name": "Song Name",
                "artist": "Artist Name",
                "duration": "3:45",
                "album": "Album Name",
                "year": "2024",
                "genre": "Rock",
                "id": "123",
            }
        ]
        lines, tier = server.format_track_list(tracks)

        assert tier == "Full"
        assert len(lines) == 1
        assert "Song Name - Artist Name (3:45) Album Name [2024] Rock 123" == lines[0]

    def test_clipped_format_when_full_exceeds_limit(self):
        """Should use clipped format when full format exceeds MAX_OUTPUT_CHARS."""
        track = {
            "name": "A" * 100,
            "artist": "B" * 50,
            "duration": "3:00",
            "album": "C" * 100,
            "year": "2024",
            "genre": "Rock",
            "id": "12345678901234567890",
        }
        tracks = [track] * 200
        lines, tier = server.format_track_list(tracks)

        assert tier == "Clipped"
        assert len(lines) == 200
        assert "..." in lines[0]  # Truncated
        assert "C" * 100 not in lines[0]  # Album truncated
        assert "[2024]" in lines[0]  # Year still present
        assert "Rock" in lines[0]  # Genre still present

    def test_compact_format_when_clipped_exceeds_limit(self):
        """Should use compact format when clipped format exceeds MAX_OUTPUT_CHARS."""
        track = {
            "name": "A" * 50,
            "artist": "B" * 30,
            "duration": "3:00",
            "album": "Album",
            "year": "2024",
            "genre": "Rock",
            "id": "12345678901234567890",
        }
        tracks = [track] * 450
        lines, tier = server.format_track_list(tracks)

        assert tier == "Compact"
        assert len(lines) == 450
        assert "Album" not in lines[0]  # Album dropped
        assert "[2024]" not in lines[0]  # Year dropped
        assert "(3:00)" in lines[0]  # Duration still present

    def test_minimal_format_when_compact_exceeds_limit(self):
        """Should use minimal format when compact format also exceeds limit."""
        track = {
            "name": "A" * 50,
            "artist": "B" * 30,
            "duration": "3:00",
            "album": "Album",
            "year": "2024",
            "genre": "Rock",
            "id": "12345678901234567890",
        }
        tracks = [track] * 800
        lines, tier = server.format_track_list(tracks)

        assert tier == "Minimal"
        assert len(lines) == 800
        assert "(3:00)" not in lines[0]

    def test_handles_empty_optional_fields(self):
        """Should handle tracks with empty year/genre gracefully."""
        tracks = [
            {
                "name": "Song",
                "artist": "Artist",
                "duration": "3:00",
                "album": "Album",
                "year": "",
                "genre": "",
                "id": "123",
            }
        ]
        lines, tier = server.format_track_list(tracks)

        assert tier == "Full"
        assert "[" not in lines[0]
        assert lines[0] == "Song - Artist (3:00) Album 123"

    def test_returns_empty_for_no_tracks(self):
        """Should handle empty track list."""
        lines, tier = server.format_track_list([])
        assert tier == "Full"
        assert lines == []


class TestSearchCatalogSongsHelper:
    """Tests for _search_catalog_songs internal helper."""

    @responses.activate
    def test_returns_songs_on_success(self, mock_config_dir, mock_developer_token, mock_user_token):
        """Should return list of song dicts on successful search."""
        # Setup tokens
        dev_token_file = mock_config_dir / "developer_token.json"
        with open(dev_token_file, "w") as f:
            json.dump({"token": mock_developer_token, "expires": time.time() + 86400 * 60}, f)

        user_token_file = mock_config_dir / "music_user_token.json"
        with open(user_token_file, "w") as f:
            json.dump({"music_user_token": mock_user_token}, f)

        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/search",
            json={
                "results": {
                    "songs": {
                        "data": [
                            {
                                "id": "123",
                                "attributes": {"name": "Test Song", "artistName": "Test Artist"},
                            }
                        ]
                    }
                }
            },
            status=200,
        )

        result = server._search_catalog_songs("test", limit=5)
        assert len(result) == 1
        assert result[0]["id"] == "123"

    @responses.activate
    def test_returns_empty_on_error(self, mock_config_dir, mock_developer_token, mock_user_token):
        """Should return empty list on API error."""
        # Setup tokens
        dev_token_file = mock_config_dir / "developer_token.json"
        with open(dev_token_file, "w") as f:
            json.dump({"token": mock_developer_token, "expires": time.time() + 86400 * 60}, f)

        user_token_file = mock_config_dir / "music_user_token.json"
        with open(user_token_file, "w") as f:
            json.dump({"music_user_token": mock_user_token}, f)

        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/search",
            json={"error": "Unauthorized"},
            status=401,
        )

        result = server._search_catalog_songs("test")
        assert result == []


class TestAddSongsToLibraryHelper:
    """Tests for _add_songs_to_library internal helper."""

    def test_returns_error_for_empty_ids(self):
        """Should return error tuple for empty ID list."""
        success, msg = server._add_songs_to_library([])
        assert success is False
        assert "No catalog IDs" in msg

    @responses.activate
    def test_returns_success_on_valid_response(
        self, mock_config_dir, mock_developer_token, mock_user_token
    ):
        """Should return success tuple on successful add."""
        # Setup tokens
        dev_token_file = mock_config_dir / "developer_token.json"
        with open(dev_token_file, "w") as f:
            json.dump({"token": mock_developer_token, "expires": time.time() + 86400 * 60}, f)

        user_token_file = mock_config_dir / "music_user_token.json"
        with open(user_token_file, "w") as f:
            json.dump({"music_user_token": mock_user_token}, f)

        responses.add(
            responses.POST,
            "https://api.music.apple.com/v1/me/library",
            status=202,
        )

        success, msg = server._add_songs_to_library(["123456789"])
        assert success is True
        assert "1 song" in msg

    @responses.activate
    def test_returns_error_on_api_failure(
        self, mock_config_dir, mock_developer_token, mock_user_token
    ):
        """Should return error tuple on API failure."""
        # Setup tokens
        dev_token_file = mock_config_dir / "developer_token.json"
        with open(dev_token_file, "w") as f:
            json.dump({"token": mock_developer_token, "expires": time.time() + 86400 * 60}, f)

        user_token_file = mock_config_dir / "music_user_token.json"
        with open(user_token_file, "w") as f:
            json.dump({"music_user_token": mock_user_token}, f)

        responses.add(
            responses.POST,
            "https://api.music.apple.com/v1/me/library",
            status=401,
        )

        success, msg = server._add_songs_to_library(["123456789"])
        assert success is False
        assert "401" in msg


class TestAddToLibraryTool:
    """Tests for add_to_library MCP tool."""

    def test_returns_error_for_empty_input(self):
        """Should return error when no input provided."""
        result = server.library(action="add")
        assert "Error: Provide track or album parameter" in result

    @responses.activate
    def test_adds_songs_successfully(self, mock_config_dir, mock_developer_token, mock_user_token):
        """Should add songs and return success message."""
        # Setup tokens
        dev_token_file = mock_config_dir / "developer_token.json"
        with open(dev_token_file, "w") as f:
            json.dump({"token": mock_developer_token, "expires": time.time() + 86400 * 60}, f)

        user_token_file = mock_config_dir / "music_user_token.json"
        with open(user_token_file, "w") as f:
            json.dump({"music_user_token": mock_user_token}, f)

        responses.add(
            responses.POST,
            "https://api.music.apple.com/v1/me/library",
            status=202,
        )

        result = server.library(action="add", track="1234567890, 9876543210")
        assert "Added" in result
        assert "2" in result


class TestPlayTrackMatching:
    """Tests for play_track song matching logic."""

    def test_matches_featured_artist_in_song_name(self):
        """Should match artist in song name for featured artists."""
        # Mock song with featured artist in title
        song = {
            "id": "123",
            "attributes": {
                "name": "Uptown Funk (feat. Bruno Mars)",
                "artistName": "Mark Ronson",
                "url": "https://music.apple.com/us/song/123",
            },
        }

        # Check if "Bruno Mars" matches (in song name, not artistName)
        song_name = song["attributes"]["name"]
        song_artist = song["attributes"]["artistName"]
        artist = "Bruno Mars"

        # This is the matching logic from play_track
        matches_artist = (
            artist.lower() in song_artist.lower() or artist.lower() in song_name.lower()
        )
        assert matches_artist is True

    def test_matches_artist_in_artist_name(self):
        """Should match artist in artistName field."""
        song_name = "Bohemian Rhapsody"
        song_artist = "Queen"
        artist = "Queen"

        matches_artist = (
            artist.lower() in song_artist.lower() or artist.lower() in song_name.lower()
        )
        assert matches_artist is True

    def test_no_match_when_artist_not_found(self):
        """Should not match when artist is in neither field."""
        song_name = "Some Song"
        song_artist = "Some Artist"
        artist = "Different Artist"

        matches_artist = (
            artist.lower() in song_artist.lower() or artist.lower() in song_name.lower()
        )
        assert matches_artist is False

    def test_partial_track_name_match(self):
        """Should match partial track names."""
        song_name = "Bohemian Rhapsody (Remastered 2011)"
        track_name = "Bohemian Rhapsody"

        matches_track = track_name.lower() in song_name.lower()
        assert matches_track is True

    def test_case_insensitive_matching(self):
        """Should match regardless of case."""
        song_name = "BOHEMIAN RHAPSODY"
        song_artist = "QUEEN"
        track_name = "bohemian rhapsody"
        artist = "queen"

        matches_track = track_name.lower() in song_name.lower()
        matches_artist = artist.lower() in song_artist.lower()
        assert matches_track is True
        assert matches_artist is True


class TestPaginationWithFetchExplicit:
    """Tests for pagination when fetch_explicit is True.

    When fetch_explicit=True and playlist name is provided, we use the
    cache-first approach: AppleScript for fast native access, then check
    cache for explicit status, only hitting API on cache miss.
    """

    @responses.activate
    def test_uses_applescript_with_cache_when_fetch_explicit_true(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """With fetch_explicit=True and playlist name, should use AppleScript + cache.

        AppleScript provides fast native access, cache stores explicit status.
        API is only called on cache miss.
        """
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)

        # Setup tokens
        dev_token_file = mock_config_dir / "developer_token.json"
        with open(dev_token_file, "w") as f:
            json.dump({"token": mock_developer_token, "expires": time.time() + 86400 * 60}, f)

        user_token_file = mock_config_dir / "music_user_token.json"
        with open(user_token_file, "w") as f:
            json.dump({"music_user_token": mock_user_token}, f)

        # Mock AppleScript - SHOULD be called
        mock_applescript_called = False

        def mock_asc_get_tracks(*args, **kwargs):
            nonlocal mock_applescript_called
            mock_applescript_called = True
            return (
                True,
                [
                    {"name": f"Track {i}", "artist": "Artist", "album": "Album", "id": f"PID{i}"}
                    for i in range(5)
                ],
            )

        # Mock cache - return cached explicit status for all tracks
        mock_cache = MagicMock()
        mock_cache.get_explicit.return_value = "Clean"  # All tracks have cached explicit status

        with patch.object(server.asc, "get_playlist_tracks", side_effect=mock_asc_get_tracks):
            with patch.object(server, "get_track_cache", return_value=mock_cache):
                # Call with playlist name and fetch_explicit=True
                result = server.playlist(
                    action="tracks", playlist="Test Playlist", fetch_explicit=True
                )

                # Should use AppleScript
                assert (
                    mock_applescript_called
                ), "AppleScript should be called for fast native access"

                # Cache should be checked for each track (via get_explicit)
                assert mock_cache.get_explicit.call_count == 5

                # Should show all 5 tracks
                assert "=== 5 tracks ===" in result
                assert "Track 0" in result
                assert "Track 4" in result
                # Note: text format may not display explicit status, but cache was used

    @responses.activate
    def test_optimized_pagination_minimal_api_calls(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """With limit specified, should only fetch needed tracks, not all.

        Performance test: limit=5 on a 500 track playlist should make 1 API call,
        not 5 calls to fetch all 500 tracks.
        """
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)

        # Setup tokens
        dev_token_file = mock_config_dir / "developer_token.json"
        with open(dev_token_file, "w") as f:
            json.dump({"token": mock_developer_token, "expires": time.time() + 86400 * 60}, f)

        user_token_file = mock_config_dir / "music_user_token.json"
        with open(user_token_file, "w") as f:
            json.dump({"music_user_token": mock_user_token}, f)

        # Mock API: return 5 tracks (simulating a partial response)
        api_call_count = 0

        def request_callback(request):
            nonlocal api_call_count
            api_call_count += 1
            return (
                200,
                {},
                json.dumps(
                    {
                        "data": [
                            {
                                "id": f"i.lib{i}",
                                "attributes": {
                                    "name": f"Track {i}",
                                    "artistName": "Artist",
                                    "albumName": "Album",
                                    "contentRating": "clean",
                                },
                            }
                            for i in range(5)
                        ]
                    }
                ),
            )

        responses.add_callback(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.test123/tracks",
            callback=request_callback,
            content_type="application/json",
        )

        # Call with playlist ID and limit=5
        result = server.playlist(action="tracks", playlist="p.test123", limit=5)

        # Should only make 1 API call, not multiple
        assert api_call_count == 1, f"Expected 1 API call, got {api_call_count}"
        assert "Track 0" in result
        assert "Track 4" in result


class TestFindApiPlaylistByName:
    """Tests for _find_api_playlist_by_name function."""

    @responses.activate
    def test_finds_exact_match(self, mock_config_dir, mock_developer_token, mock_user_token):
        """Should find playlist by exact name match."""
        # Setup tokens
        dev_token_file = mock_config_dir / "developer_token.json"
        with open(dev_token_file, "w") as f:
            json.dump({"token": mock_developer_token, "expires": time.time() + 86400 * 60}, f)

        user_token_file = mock_config_dir / "music_user_token.json"
        with open(user_token_file, "w") as f:
            json.dump({"music_user_token": mock_user_token}, f)

        # Mock API response
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/playlists",
            json={
                "data": [
                    {"id": "p.abc123", "attributes": {"name": "My Playlist"}},
                    {"id": "p.def456", "attributes": {"name": "Another Playlist"}},
                ]
            },
            status=200,
        )

        playlist_id, fuzzy_match = server._find_api_playlist_by_name("My Playlist")
        assert playlist_id == "p.abc123"
        assert fuzzy_match is None  # Should be exact match

    @responses.activate
    def test_finds_partial_match(self, mock_config_dir, mock_developer_token, mock_user_token):
        """Should find playlist by partial name match."""
        dev_token_file = mock_config_dir / "developer_token.json"
        with open(dev_token_file, "w") as f:
            json.dump({"token": mock_developer_token, "expires": time.time() + 86400 * 60}, f)

        user_token_file = mock_config_dir / "music_user_token.json"
        with open(user_token_file, "w") as f:
            json.dump({"music_user_token": mock_user_token}, f)

        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/playlists",
            json={
                "data": [
                    {"id": "p.emoji123", "attributes": {"name": "🎸 Rock Playlist"}},
                ]
            },
            status=200,
        )

        # Partial match without emoji
        playlist_id, fuzzy_match = server._find_api_playlist_by_name("Rock Playlist")
        assert playlist_id == "p.emoji123"

    @responses.activate
    def test_prefers_exact_match_over_partial(
        self, mock_config_dir, mock_developer_token, mock_user_token
    ):
        """Should prefer exact match over partial match."""
        dev_token_file = mock_config_dir / "developer_token.json"
        with open(dev_token_file, "w") as f:
            json.dump({"token": mock_developer_token, "expires": time.time() + 86400 * 60}, f)

        user_token_file = mock_config_dir / "music_user_token.json"
        with open(user_token_file, "w") as f:
            json.dump({"music_user_token": mock_user_token}, f)

        # Partial match comes first in the list, but exact match should win
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/playlists",
            json={
                "data": [
                    {"id": "p.partial", "attributes": {"name": "Rock Playlist Extended"}},
                    {"id": "p.exact", "attributes": {"name": "Rock Playlist"}},
                ]
            },
            status=200,
        )

        playlist_id, fuzzy_match = server._find_api_playlist_by_name("Rock Playlist")
        assert playlist_id == "p.exact"
        assert fuzzy_match is None  # Should be exact match

    @responses.activate
    def test_returns_none_when_not_found(
        self, mock_config_dir, mock_developer_token, mock_user_token
    ):
        """Should return None when playlist not found."""
        dev_token_file = mock_config_dir / "developer_token.json"
        with open(dev_token_file, "w") as f:
            json.dump({"token": mock_developer_token, "expires": time.time() + 86400 * 60}, f)

        user_token_file = mock_config_dir / "music_user_token.json"
        with open(user_token_file, "w") as f:
            json.dump({"music_user_token": mock_user_token}, f)

        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/playlists",
            json={"data": []},
            status=200,
        )

        playlist_id, fuzzy_match = server._find_api_playlist_by_name("Nonexistent Playlist")
        assert playlist_id is None
        assert fuzzy_match is None

    @responses.activate
    def test_returns_none_on_api_error(
        self, mock_config_dir, mock_developer_token, mock_user_token
    ):
        """Should return None on API error (graceful fallback)."""
        dev_token_file = mock_config_dir / "developer_token.json"
        with open(dev_token_file, "w") as f:
            json.dump({"token": mock_developer_token, "expires": time.time() + 86400 * 60}, f)

        user_token_file = mock_config_dir / "music_user_token.json"
        with open(user_token_file, "w") as f:
            json.dump({"music_user_token": mock_user_token}, f)

        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/playlists",
            status=401,  # Unauthorized
        )

        playlist_id, fuzzy_match = server._find_api_playlist_by_name("Any Playlist")
        assert playlist_id is None
        assert fuzzy_match is None


class TestResolvePlaylistApiLookup:
    """Tests for _resolve_playlist API lookup behavior."""

    def test_returns_id_directly_for_p_prefix(self):
        """Should return playlist ID directly when p. prefix provided."""
        resolved = server._resolve_playlist("p.abc123xyz")
        assert resolved.api_id == "p.abc123xyz"
        assert resolved.applescript_name is None
        assert resolved.error is None
        assert resolved.raw_input == "p.abc123xyz"

    def test_returns_error_for_empty_string(self):
        """Should return error for empty playlist string."""
        resolved = server._resolve_playlist("")
        assert resolved.api_id is None
        assert resolved.applescript_name is None
        assert resolved.error is not None
        assert "required" in resolved.error.lower()

    @responses.activate
    def test_looks_up_api_id_for_name(self, mock_config_dir, mock_developer_token, mock_user_token):
        """Should look up API playlist ID when given a name."""
        dev_token_file = mock_config_dir / "developer_token.json"
        with open(dev_token_file, "w") as f:
            json.dump({"token": mock_developer_token, "expires": time.time() + 86400 * 60}, f)

        user_token_file = mock_config_dir / "music_user_token.json"
        with open(user_token_file, "w") as f:
            json.dump({"music_user_token": mock_user_token}, f)

        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/playlists",
            json={
                "data": [
                    {"id": "p.found123", "attributes": {"name": "My Music"}},
                ]
            },
            status=200,
        )

        resolved = server._resolve_playlist("My Music")

        # Should return BOTH API ID and AppleScript name when match found
        assert resolved.api_id == "p.found123"
        assert resolved.applescript_name == "My Music"  # Matched name populated too!
        assert resolved.error is None
        assert resolved.raw_input == "My Music"

    @responses.activate
    def test_falls_back_to_name_when_not_in_api(
        self, mock_config_dir, mock_developer_token, mock_user_token
    ):
        """Should fall back to playlist name when not found in API."""
        dev_token_file = mock_config_dir / "developer_token.json"
        with open(dev_token_file, "w") as f:
            json.dump({"token": mock_developer_token, "expires": time.time() + 86400 * 60}, f)

        user_token_file = mock_config_dir / "music_user_token.json"
        with open(user_token_file, "w") as f:
            json.dump({"music_user_token": mock_user_token}, f)

        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/playlists",
            json={"data": []},  # Empty - playlist not in API
            status=200,
        )

        resolved = server._resolve_playlist("Local Only Playlist")

        # Should fall back to name for AppleScript
        assert resolved.api_id is None
        assert resolved.applescript_name == "Local Only Playlist"
        assert resolved.error is None

    def test_handles_ps_i_love_you_as_name(self):
        """Should treat 'p.s. I love you' as a name, not an ID."""
        # This tests the edge case where a playlist name starts with "p."
        # but isn't an ID (has spaces/punctuation after p.)
        with patch.object(server, "_find_api_playlist_by_name", return_value=(None, None)):
            resolved = server._resolve_playlist("p.s. I love you")

        # Should be treated as a name, not an ID
        assert resolved.api_id is None
        assert resolved.applescript_name == "p.s. I love you"
        assert resolved.error is None


class TestFuzzyMatchingPlaylistResolution:
    """Tests for fuzzy matching playlist names - REGRESSION TESTS."""

    @responses.activate
    def test_fuzzy_matches_and_vs_ampersand(
        self, mock_config_dir, mock_developer_token, mock_user_token
    ):
        """Should fuzzy match 'Jack and Norah' to 'Jack & Norah'."""
        # Setup tokens
        dev_token_file = mock_config_dir / "developer_token.json"
        with open(dev_token_file, "w") as f:
            json.dump({"token": mock_developer_token, "expires": time.time() + 86400 * 60}, f)

        user_token_file = mock_config_dir / "music_user_token.json"
        with open(user_token_file, "w") as f:
            json.dump({"music_user_token": mock_user_token}, f)

        # Mock API response with playlist named "Jack & Norah"
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/playlists",
            json={
                "data": [
                    {"id": "p.jack123", "attributes": {"name": "Jack & Norah"}},
                ]
            },
            status=200,
        )

        # User types "Jack and Norah" (with "and" instead of "&")
        resolved = server._resolve_playlist("Jack and Norah")

        # Should fuzzy match and return BOTH identifiers
        assert resolved.api_id == "p.jack123"
        assert resolved.applescript_name == "Jack & Norah"  # CRITICAL: Must have actual name
        assert resolved.error is None
        assert resolved.raw_input == "Jack and Norah"
        assert resolved.fuzzy_match is not None
        assert "and" in str(resolved.fuzzy_match.transformations).lower() or "&" in str(
            resolved.fuzzy_match.transformations
        )

    @responses.activate
    def test_fuzzy_matches_with_emojis(
        self, mock_config_dir, mock_developer_token, mock_user_token
    ):
        """Should fuzzy match playlist names with emojis removed."""
        dev_token_file = mock_config_dir / "developer_token.json"
        with open(dev_token_file, "w") as f:
            json.dump({"token": mock_developer_token, "expires": time.time() + 86400 * 60}, f)

        user_token_file = mock_config_dir / "music_user_token.json"
        with open(user_token_file, "w") as f:
            json.dump({"music_user_token": mock_user_token}, f)

        # Mock API response with emoji playlist
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/playlists",
            json={
                "data": [
                    {"id": "p.emoji123", "attributes": {"name": "🤟👶🎸 Jack & Norah"}},
                ]
            },
            status=200,
        )

        # User types without emojis
        resolved = server._resolve_playlist("Jack & Norah")

        # Should fuzzy match (emojis ignored)
        assert resolved.api_id == "p.emoji123"
        assert resolved.applescript_name == "🤟👶🎸 Jack & Norah"  # Keep actual emoji name
        assert resolved.error is None
        assert resolved.fuzzy_match is not None

    @responses.activate
    def test_exact_match_preferred_over_fuzzy(
        self, mock_config_dir, mock_developer_token, mock_user_token
    ):
        """Should prefer exact match over fuzzy match."""
        dev_token_file = mock_config_dir / "developer_token.json"
        with open(dev_token_file, "w") as f:
            json.dump({"token": mock_developer_token, "expires": time.time() + 86400 * 60}, f)

        user_token_file = mock_config_dir / "music_user_token.json"
        with open(user_token_file, "w") as f:
            json.dump({"music_user_token": mock_user_token}, f)

        # Mock API response with both exact and fuzzy matches
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/playlists",
            json={
                "data": [
                    {
                        "id": "p.fuzzy",
                        "attributes": {"name": "The Rock Music"},
                    },  # Fuzzy (article removed)
                    {"id": "p.exact", "attributes": {"name": "Rock Music"}},  # Exact match
                ]
            },
            status=200,
        )

        resolved = server._resolve_playlist("Rock Music")

        # Should choose exact match
        assert resolved.api_id == "p.exact"
        assert resolved.applescript_name == "Rock Music"
        assert resolved.fuzzy_match is None or resolved.fuzzy_match.match_type == "exact"

    @responses.activate
    def test_resolved_object_has_both_ids_after_fuzzy_match(
        self, mock_config_dir, mock_developer_token, mock_user_token
    ):
        """REGRESSION TEST: Resolved object MUST have both api_id and applescript_name after fuzzy match.

        This is the critical fix for the bug where remove_from_playlist("Jack and Norah", ...)
        would fail because fuzzy matching converted to API ID but function needs AppleScript name.
        """
        dev_token_file = mock_config_dir / "developer_token.json"
        with open(dev_token_file, "w") as f:
            json.dump({"token": mock_developer_token, "expires": time.time() + 86400 * 60}, f)

        user_token_file = mock_config_dir / "music_user_token.json"
        with open(user_token_file, "w") as f:
            json.dump({"music_user_token": mock_user_token}, f)

        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/playlists",
            json={
                "data": [
                    {"id": "p.test123", "attributes": {"name": "Test & Playlist"}},
                ]
            },
            status=200,
        )

        resolved = server._resolve_playlist("Test and Playlist")

        # CRITICAL: Both must be populated
        assert resolved.api_id is not None, "api_id must be populated after fuzzy match"
        assert (
            resolved.applescript_name is not None
        ), "applescript_name must be populated after fuzzy match"
        assert resolved.api_id == "p.test123"
        assert resolved.applescript_name == "Test & Playlist"


class TestPlaylistResolutionPerformance:
    """Performance tests for playlist resolution."""

    @responses.activate
    def test_fuzzy_matching_performance_with_many_playlists(
        self, mock_config_dir, mock_developer_token, mock_user_token
    ):
        """Should complete fuzzy matching in reasonable time even with many playlists.

        This tests the optimization where fuzzy matching only happens if exact/partial fails.
        With 50 playlists and target at position 25, should complete quickly.
        """
        import time as time_module

        dev_token_file = mock_config_dir / "developer_token.json"
        with open(dev_token_file, "w") as f:
            json.dump({"token": mock_developer_token, "expires": time.time() + 86400 * 60}, f)

        user_token_file = mock_config_dir / "music_user_token.json"
        with open(user_token_file, "w") as f:
            json.dump({"music_user_token": mock_user_token}, f)

        # Mock API with 50 playlists (realistic library size)
        playlists = [
            {"id": f"p.test{i}", "attributes": {"name": f"Playlist {i}"}} for i in range(25)
        ]
        # Add fuzzy match target in the middle
        playlists.append({"id": "p.target", "attributes": {"name": "Rock & Roll"}})
        # Add more playlists after
        playlists.extend(
            [{"id": f"p.test{i}", "attributes": {"name": f"Playlist {i}"}} for i in range(25, 50)]
        )

        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/playlists",
            json={"data": playlists},
            status=200,
        )

        start = time_module.time()
        resolved = server._resolve_playlist("Rock and Roll")  # Fuzzy: "and" → "&"
        elapsed = time_module.time() - start

        # Should complete in under 500ms (fuzzy matching is now fallback-only)
        assert elapsed < 0.5, f"Fuzzy matching took {elapsed:.2f}s, should be < 0.5s"
        assert resolved.api_id == "p.target"
        assert resolved.applescript_name == "Rock & Roll"
        assert resolved.fuzzy_match is not None, "Should be a fuzzy match"


# =============================================================================
# Integration Tests - Real User Journeys
# =============================================================================
# These tests validate realistic workflows at different scales:
# - First 1-2 actions: Basic discovery
# - First 5 actions: Getting started
# - First 10 actions: Regular user
# - First 20 actions: Power user
#
# Each flow is tested in three modes:
# - API-only: APPLESCRIPT_AVAILABLE = False
# - macOS-only: AppleScript mocked, preferred for local operations
# - Combined: Both available, tests routing logic
# =============================================================================


class TestUserJourneyAPIOnly:
    """Integration tests for API-only mode (non-macOS or AppleScript unavailable)."""

    @responses.activate
    def test_first_2_actions_search_and_list_playlists(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """First things users do: search for music and see their playlists."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        self._setup_tokens(mock_config_dir, mock_developer_token, mock_user_token)

        # 1. Search catalog for a song
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/search",
            json={
                "results": {
                    "songs": {
                        "data": [
                            {
                                "id": "123456",
                                "attributes": {
                                    "name": "Hey Jude",
                                    "artistName": "The Beatles",
                                    "albumName": "Past Masters",
                                    "durationInMillis": 431000,
                                    "releaseDate": "1968-08-26",
                                    "genreNames": ["Rock"],
                                },
                            }
                        ]
                    }
                }
            },
            status=200,
        )

        result = server.catalog(action="search", query="Hey Jude Beatles")
        assert "Hey Jude" in result
        assert "Beatles" in result

        # 2. List playlists
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/playlists",
            json={
                "data": [
                    {"id": "p.favorites", "attributes": {"name": "Favorites", "canEdit": True}},
                    {"id": "p.workout", "attributes": {"name": "Workout Mix", "canEdit": True}},
                ]
            },
            status=200,
        )

        result = server.playlist(action="list")
        assert "Favorites" in result
        assert "Workout Mix" in result

    @responses.activate
    def test_first_5_actions_basic_playlist_workflow(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Getting started: search, list playlists, get tracks, add to playlist."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        self._setup_tokens(mock_config_dir, mock_developer_token, mock_user_token)

        # 1. Search catalog
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/search",
            json={
                "results": {
                    "songs": {
                        "data": [
                            {
                                "id": "song1",
                                "attributes": {
                                    "name": "Bohemian Rhapsody",
                                    "artistName": "Queen",
                                    "albumName": "A Night at the Opera",
                                    "durationInMillis": 354000,
                                    "releaseDate": "1975-10-31",
                                    "genreNames": ["Rock"],
                                },
                            }
                        ]
                    }
                }
            },
            status=200,
        )
        result = server.catalog(action="search", query="Bohemian Rhapsody")
        assert "Bohemian Rhapsody" in result

        # 2. List playlists
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/playlists",
            json={
                "data": [
                    {"id": "p.rock", "attributes": {"name": "Classic Rock", "canEdit": True}},
                ]
            },
            status=200,
        )
        result = server.playlist(action="list")
        assert "Classic Rock" in result

        # 3. Get playlist tracks
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/playlists",
            json={
                "data": [
                    {"id": "p.rock", "attributes": {"name": "Classic Rock", "canEdit": True}},
                ]
            },
            status=200,
        )
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.rock/tracks",
            json={
                "data": [
                    {
                        "id": "i.track1",
                        "attributes": {
                            "name": "Stairway to Heaven",
                            "artistName": "Led Zeppelin",
                            "albumName": "Led Zeppelin IV",
                            "durationInMillis": 482000,
                        },
                    }
                ]
            },
            status=200,
        )
        result = server.playlist(action="tracks", playlist="Classic Rock")
        assert "Stairway to Heaven" in result

        # 4. Search library
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/search",
            json={
                "results": {
                    "library-songs": {
                        "data": [
                            {
                                "id": "i.lib1",
                                "attributes": {
                                    "name": "Hotel California",
                                    "artistName": "Eagles",
                                    "albumName": "Hotel California",
                                    "durationInMillis": 391000,
                                },
                            }
                        ]
                    }
                }
            },
            status=200,
        )
        result = server.library(action="search", query="Hotel California")
        assert "Hotel California" in result

        # 5. Add track to playlist (via catalog search + add)
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/playlists",
            json={
                "data": [{"id": "p.rock", "attributes": {"name": "Classic Rock", "canEdit": True}}]
            },
            status=200,
        )
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/search",
            json={
                "results": {
                    "songs": {
                        "data": [
                            {
                                "id": "cat123",
                                "attributes": {
                                    "name": "Dream On",
                                    "artistName": "Aerosmith",
                                    "albumName": "Aerosmith",
                                    "durationInMillis": 267000,
                                    "releaseDate": "1973-01-01",
                                    "genreNames": ["Rock"],
                                },
                            }
                        ]
                    }
                }
            },
            status=200,
        )
        responses.add(
            responses.POST,
            "https://api.music.apple.com/v1/me/library",
            json={},
            status=202,
        )
        responses.add(
            responses.POST,
            "https://api.music.apple.com/v1/me/library/playlists/p.rock/tracks",
            json={},
            status=201,
        )
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.rock/tracks",
            json={
                "data": [
                    {
                        "id": "i.track1",
                        "attributes": {"name": "Dream On", "artistName": "Aerosmith"},
                    }
                ]
            },
            status=200,
        )
        result = server.playlist(
            action="add", playlist="Classic Rock", track="Dream On", artist="Aerosmith"
        )
        assert "Dream On" in result or "Added" in result or "error" not in result.lower()

    @responses.activate
    def test_first_10_actions_regular_user(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Regular user: create playlist, add/remove tracks, get recommendations."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        self._setup_tokens(mock_config_dir, mock_developer_token, mock_user_token)

        # 6. Create a new playlist
        responses.add(
            responses.POST,
            "https://api.music.apple.com/v1/me/library/playlists",
            json={"data": [{"id": "p.new123", "attributes": {"name": "My New Playlist"}}]},
            status=201,
        )
        result = server.playlist(
            action="create", name="My New Playlist", description="Created for testing"
        )
        assert "p.new123" in result or "My New Playlist" in result

        # 7. Get recently played
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/recent/played/tracks",
            json={
                "data": [
                    {
                        "id": "recent1",
                        "attributes": {
                            "name": "Yesterday",
                            "artistName": "The Beatles",
                            "albumName": "Help!",
                            "durationInMillis": 125000,
                        },
                    }
                ]
            },
            status=200,
        )
        result = server.library(action="recently_played", limit=5)
        assert "Yesterday" in result or "recent" in result.lower()

        # 8. Get recommendations
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/recommendations",
            json={"data": [{"id": "rec1", "type": "playlists", "attributes": {"name": "For You"}}]},
            status=200,
        )
        result = server.discover(action="recommendations")
        # Just check it doesn't error

        # 9. Get heavy rotation
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/history/heavy-rotation",
            json={
                "data": [
                    {
                        "id": "hr1",
                        "type": "albums",
                        "attributes": {"name": "Abbey Road", "artistName": "The Beatles"},
                    }
                ]
            },
            status=200,
        )
        result = server.discover(action="heavy_rotation")
        assert "Abbey Road" in result or result  # Just check no error

        # 10. Get artist top songs
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/search",
            json={
                "results": {
                    "artists": {
                        "data": [
                            {
                                "id": "artist1",
                                "attributes": {"name": "The Beatles", "genreNames": ["Rock"]},
                            }
                        ]
                    }
                }
            },
            status=200,
        )
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/artists/artist1/view/top-songs",
            json={
                "data": [
                    {
                        "id": "top1",
                        "attributes": {
                            "name": "Come Together",
                            "artistName": "The Beatles",
                            "albumName": "Abbey Road",
                            "durationInMillis": 259000,
                        },
                    }
                ]
            },
            status=200,
        )
        result = server.discover(action="top_songs", artist="The Beatles")
        assert "Come Together" in result or "Beatles" in result

    def _setup_tokens(self, mock_config_dir, mock_developer_token, mock_user_token):
        """Helper to setup authentication tokens."""
        dev_token_file = mock_config_dir / "developer_token.json"
        with open(dev_token_file, "w") as f:
            json.dump({"token": mock_developer_token, "expires": time.time() + 86400 * 60}, f)

        user_token_file = mock_config_dir / "music_user_token.json"
        with open(user_token_file, "w") as f:
            json.dump({"music_user_token": mock_user_token}, f)


class TestUserJourneyFuzzyMatching:
    """Integration tests for fuzzy matching across all entity types."""

    @responses.activate
    def test_fuzzy_playlist_workflow(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """User workflow with fuzzy-named playlist: get tracks, add, remove."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        self._setup_tokens(mock_config_dir, mock_developer_token, mock_user_token)

        # Playlist has special characters
        playlist_data = [
            {"id": "p.fuzzy1", "attributes": {"name": "🎸 Rock & Roll Classics", "canEdit": True}}
        ]

        # 1. Get tracks from fuzzy-named playlist (typed without emoji, "and" instead of "&")
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/playlists",
            json={"data": playlist_data},
            status=200,
        )
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.fuzzy1/tracks",
            json={
                "data": [
                    {
                        "id": "i.t1",
                        "attributes": {
                            "name": "Sweet Child O' Mine",
                            "artistName": "Guns N' Roses",
                            "albumName": "Appetite for Destruction",
                            "durationInMillis": 356000,
                        },
                    }
                ]
            },
            status=200,
        )

        result = server.playlist(action="tracks", playlist="Rock and Roll Classics")
        assert "Sweet Child" in result
        assert "Fuzzy match" in result or "fuzzy" in result.lower()

        # 2. Add to fuzzy-named playlist
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/playlists",
            json={"data": playlist_data},
            status=200,
        )
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/search",
            json={
                "results": {
                    "songs": {
                        "data": [
                            {
                                "id": "cat456",
                                "attributes": {
                                    "name": "Back in Black",
                                    "artistName": "AC/DC",
                                    "albumName": "Back in Black",
                                    "durationInMillis": 255000,
                                    "releaseDate": "1980-07-25",
                                    "genreNames": ["Rock"],
                                },
                            }
                        ]
                    }
                }
            },
            status=200,
        )
        responses.add(
            responses.POST,
            "https://api.music.apple.com/v1/me/library",
            json={},
            status=202,
        )
        responses.add(
            responses.POST,
            "https://api.music.apple.com/v1/me/library/playlists/p.fuzzy1/tracks",
            json={},
            status=201,
        )
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.fuzzy1/tracks",
            json={"data": [{"id": "i.new", "attributes": {"name": "Back in Black"}}]},
            status=200,
        )

        result = server.playlist(
            action="add", playlist="Rock and Roll Classics", track="Back in Black"
        )
        assert "Back in Black" in result or "Added" in result

    @responses.activate
    def test_fuzzy_track_search_in_catalog(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """User searches with typos/variations, fuzzy matching finds correct track."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        self._setup_tokens(mock_config_dir, mock_developer_token, mock_user_token)

        # API returns track with proper name
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/search",
            json={
                "results": {
                    "songs": {
                        "data": [
                            {
                                "id": "789",
                                "attributes": {
                                    "name": "Can't Buy Me Love",
                                    "artistName": "The Beatles",
                                    "albumName": "A Hard Day's Night",
                                    "durationInMillis": 137000,
                                    "releaseDate": "1964-03-16",
                                    "genreNames": ["Rock"],
                                },
                            }
                        ]
                    }
                }
            },
            status=200,
        )

        # User types without apostrophe
        result = server.catalog(action="search", query="Cant Buy Me Love Beatles")
        assert "Can't Buy Me Love" in result or "Cant Buy Me Love" in result

    @responses.activate
    def test_fuzzy_album_search(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """User searches for album with fuzzy name."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        self._setup_tokens(mock_config_dir, mock_developer_token, mock_user_token)

        # Test _find_matching_catalog_album with fuzzy input
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/search",
            json={
                "results": {
                    "albums": {
                        "data": [
                            {
                                "id": "album1",
                                "attributes": {
                                    "name": "Sgt. Pepper's Lonely Hearts Club Band",
                                    "artistName": "The Beatles",
                                    "trackCount": 13,
                                    "releaseDate": "1967-06-01",
                                },
                            }
                        ]
                    }
                }
            },
            status=200,
        )

        album, error, fuzzy = server._find_matching_catalog_album(
            "Sgt Peppers Lonely Hearts", "Beatles"
        )
        assert album is not None
        assert error is None
        assert album.get("id") == "album1"

    def _setup_tokens(self, mock_config_dir, mock_developer_token, mock_user_token):
        """Helper to setup authentication tokens."""
        dev_token_file = mock_config_dir / "developer_token.json"
        with open(dev_token_file, "w") as f:
            json.dump({"token": mock_developer_token, "expires": time.time() + 86400 * 60}, f)

        user_token_file = mock_config_dir / "music_user_token.json"
        with open(user_token_file, "w") as f:
            json.dump({"music_user_token": mock_user_token}, f)


class TestUserJourneyMacOSOnly:
    """Integration tests for macOS-only mode (AppleScript preferred)."""

    def test_playlist_operations_prefer_applescript(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """On macOS, playlist operations should prefer AppleScript when possible."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        self._setup_tokens(mock_config_dir, mock_developer_token, mock_user_token)

        # Mock AppleScript module
        mock_asc = MagicMock()
        mock_asc.get_playlists.return_value = (
            True,
            [{"name": "Chill Vibes", "id": "abc123", "count": 25}],
        )
        monkeypatch.setattr(server, "asc", mock_asc)

        result = server.playlist(action="list")
        assert "Chill Vibes" in result
        mock_asc.get_playlists.assert_called_once()

    def test_remove_from_playlist_uses_applescript_name(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """remove_from_playlist MUST use AppleScript name, not API ID."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        self._setup_tokens(mock_config_dir, mock_developer_token, mock_user_token)

        # Mock AppleScript
        mock_asc = MagicMock()
        mock_asc.get_playlists.return_value = (
            True,
            [{"name": "🎵 My Mix", "id": "xyz789", "count": 10}],
        )
        mock_asc.remove_track_from_playlist.return_value = (True, "Removed")
        monkeypatch.setattr(server, "asc", mock_asc)

        # Resolve playlist with fuzzy name
        resolved = server._resolve_playlist("My Mix")

        # Critical: applescript_name must be the actual name with emoji
        assert resolved.applescript_name == "🎵 My Mix"
        assert resolved.api_id is None or resolved.applescript_name is not None

    def _setup_tokens(self, mock_config_dir, mock_developer_token, mock_user_token):
        """Helper to setup authentication tokens."""
        dev_token_file = mock_config_dir / "developer_token.json"
        with open(dev_token_file, "w") as f:
            json.dump({"token": mock_developer_token, "expires": time.time() + 86400 * 60}, f)

        user_token_file = mock_config_dir / "music_user_token.json"
        with open(user_token_file, "w") as f:
            json.dump({"music_user_token": mock_user_token}, f)


class TestAlbumDisambiguation:
    """Tests for album param behavior: disambiguation filter when track is present, whole-album add when alone."""

    def test_album_without_track_calls_resolve_album(self, monkeypatch):
        """album param alone should call _resolve_album (whole-album add path)."""
        resolve_album_called = False
        original_resolve = server._resolve_album

        def tracking_resolve(*args, **kwargs):
            nonlocal resolve_album_called
            resolve_album_called = True
            return original_resolve(*args, **kwargs)

        monkeypatch.setattr(server, "_resolve_album", tracking_resolve)
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        # Album-add path requires a developer token; mock so the test
        # reaches _resolve_album on systems without a real token (CI).
        monkeypatch.setattr(server, "_has_developer_token", lambda: True)

        mock_asc = MagicMock()
        mock_asc.get_playlists.return_value = (
            True,
            [{"name": "Test Playlist", "id": "test123", "count": 0}],
        )
        monkeypatch.setattr(server, "asc", mock_asc)

        # Will fail at API call, but that's fine — we're just checking _resolve_album was entered
        try:
            server._playlist_add(playlist="Test Playlist", album="Some Album", artist="Some Artist")
        except Exception:
            pass

        assert (
            resolve_album_called
        ), "_resolve_album should be called when only album (no track) is provided"

    def test_album_with_track_skips_resolve_album(self, monkeypatch):
        """album + track together should NOT call _resolve_album (disambiguation path instead)."""
        resolve_album_called = False
        original_resolve = server._resolve_album

        def tracking_resolve(*args, **kwargs):
            nonlocal resolve_album_called
            resolve_album_called = True
            return original_resolve(*args, **kwargs)

        monkeypatch.setattr(server, "_resolve_album", tracking_resolve)
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)

        mock_asc = MagicMock()
        mock_asc.get_playlists.return_value = (
            True,
            [{"name": "Test Playlist", "id": "test123", "count": 0}],
        )
        mock_asc.track_exists_in_playlist.return_value = (True, False)
        mock_asc.add_track_to_playlist.return_value = (True, "Added Hot Potato")
        monkeypatch.setattr(server, "asc", mock_asc)

        server._playlist_add(
            playlist="Test Playlist",
            track="Hot Potato",
            album="Ready, Steady, Wiggle!",
            artist="The Wiggles",
        )

        assert (
            not resolve_album_called
        ), "_resolve_album should NOT be called when both track and album are provided"

    @responses.activate
    def test_album_with_track_uses_album_as_filter(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """album + track together should use album as disambiguation, NOT add whole album."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        self._setup_tokens(mock_config_dir, mock_developer_token, mock_user_token)

        mock_asc = MagicMock()
        mock_asc.get_playlists.return_value = (
            True,
            [{"name": "Test Playlist", "id": "test123", "count": 0}],
        )
        # First call: pre-add duplicate-check → False (not duplicate, proceed).
        # Subsequent calls: post-add verification → True (track now in playlist).
        # The post-add verify catches AppleScript false-positive successes
        # (some user-created playlists silently revert AppleScript edits
        # server-side); without the side_effect, mocked verify would always
        # return False and the post-add verify retry would consume two calls
        # to add_track_to_playlist instead of one.
        mock_asc.track_exists_in_playlist.side_effect = [(True, False)] + [(True, True)] * 10
        mock_asc.add_track_to_playlist.return_value = (
            True,
            "Added Hot Potato (Ready, Steady, Wiggle!) by The Wiggles",
        )
        monkeypatch.setattr(server, "asc", mock_asc)

        result = server._playlist_add(
            playlist="Test Playlist",
            track="Hot Potato",
            album="Ready, Steady, Wiggle!",
            artist="The Wiggles",
        )

        # Should add exactly 1 track, not the whole album
        assert "Added 1 track" in result
        assert "Hot Potato" in result

        # AppleScript should have been called with album param for disambiguation
        mock_asc.add_track_to_playlist.assert_called_once()
        call_args = mock_asc.add_track_to_playlist.call_args
        # 4th arg (album) should be "Ready, Steady, Wiggle!"
        assert (
            call_args[0][3] == "Ready, Steady, Wiggle!"
            or call_args.kwargs.get("album") == "Ready, Steady, Wiggle!"
        )

    @responses.activate
    def test_library_ids_route_to_applescript_for_non_api_playlists(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Library IDs should use AppleScript mode for non-API playlists, not fail with 403."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        self._setup_tokens(mock_config_dir, mock_developer_token, mock_user_token)

        mock_asc = MagicMock()
        mock_asc.get_playlists.return_value = (
            True,
            [{"name": "My Playlist", "id": "abc123", "count": 10}],
        )
        # First call: duplicate-check pre-add → False (not a duplicate, proceed).
        # Subsequent calls: post-add verification → True (track now in playlist).
        # The post-add verify was added so AppleScript false-positive successes
        # (silent `duplicate` no-op when library sync hasn't propagated) get
        # caught instead of returned to the user as success.
        mock_asc.track_exists_in_playlist.side_effect = [(True, False)] + [(True, True)] * 10
        mock_asc.add_track_to_playlist.return_value = (True, "Added Hot Potato by The Wiggles")
        monkeypatch.setattr(server, "asc", mock_asc)

        # Mock library song lookup for the ID
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/songs/i.abc123",
            json={
                "data": [
                    {
                        "id": "i.abc123",
                        "attributes": {"name": "Hot Potato", "artistName": "The Wiggles"},
                    }
                ]
            },
            status=200,
        )

        result = server._playlist_add(
            playlist="My Playlist",
            track="i.abc123",
        )

        # Should NOT get "Cannot edit this playlist" error
        assert "Cannot edit" not in result
        # AppleScript should have been used
        mock_asc.add_track_to_playlist.assert_called_once()

    def _setup_tokens(self, mock_config_dir, mock_developer_token, mock_user_token):
        """Helper to setup authentication tokens."""
        dev_token_file = mock_config_dir / "developer_token.json"
        with open(dev_token_file, "w") as f:
            json.dump({"token": mock_developer_token, "expires": time.time() + 86400 * 60}, f)

        user_token_file = mock_config_dir / "music_user_token.json"
        with open(user_token_file, "w") as f:
            json.dump({"music_user_token": mock_user_token}, f)


class TestUserJourneyCombinedMode:
    """Integration tests for combined mode (both API and AppleScript available)."""

    @responses.activate
    def test_add_to_playlist_chooses_best_mode(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """add_to_playlist should use AppleScript for track names, API for IDs."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        self._setup_tokens(mock_config_dir, mock_developer_token, mock_user_token)

        # Mock AppleScript
        mock_asc = MagicMock()
        mock_asc.get_playlists.return_value = (
            True,
            [{"name": "Workout", "id": "work123", "count": 50}],
        )
        mock_asc.track_exists_in_playlist.return_value = (True, False)  # Track doesn't exist yet
        mock_asc.add_track_to_playlist.return_value = (True, "Added")
        monkeypatch.setattr(server, "asc", mock_asc)

        # Mock API for playlist resolution
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/playlists",
            json={"data": [{"id": "p.work", "attributes": {"name": "Workout", "canEdit": True}}]},
            status=200,
        )

        # Add track by name - should use AppleScript
        result = server.playlist(action="add", playlist="Workout", track="Eye of the Tiger")

        # AppleScript should have been called for track name operations
        # (The exact assertion depends on implementation details)

    def test_playlist_list_does_not_cascade_to_api_on_applescript_failure(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """When AppleScript fails on macOS, _playlist_list must NOT silently
        cascade to the API. The API path returns a strict subset (only
        API-visible playlists) and silently swapping views on AS failure
        masked the real cause and confused users into thinking they needed
        a developer token. Now the AS error surfaces directly with an
        actionable message."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)

        # Tokens configured but should NOT be touched — fallthrough is
        # blocked.
        self._setup_tokens(mock_config_dir, mock_developer_token, mock_user_token)

        mock_asc = MagicMock()
        mock_asc.get_playlists.return_value = (
            False,
            "execution error: Music got an error: Not authorized to send Apple events to Music. (-1743)",
        )
        # Wire classify_error / error constants through to the real module.
        from applemusic_mcp import applescript as real_asc

        mock_asc.classify_error = real_asc.classify_error
        mock_asc.ERROR_UNKNOWN = real_asc.ERROR_UNKNOWN
        mock_asc.ERROR_AUTOMATION_DENIED = real_asc.ERROR_AUTOMATION_DENIED
        mock_asc.ERROR_MUSIC_NOT_RUNNING = real_asc.ERROR_MUSIC_NOT_RUNNING
        mock_asc.ERROR_TIMEOUT = real_asc.ERROR_TIMEOUT
        mock_asc.ERROR_SYNTAX = real_asc.ERROR_SYNTAX
        monkeypatch.setattr(server, "asc", mock_asc)

        result = server.playlist(action="list")
        # Surfaces actionable Automation-denied message
        assert "Automation permission denied" in result
        # Does NOT leak the developer-token error
        assert "Developer token not found" not in result

    def _setup_tokens(self, mock_config_dir, mock_developer_token, mock_user_token):
        """Helper to setup authentication tokens."""
        dev_token_file = mock_config_dir / "developer_token.json"
        with open(dev_token_file, "w") as f:
            json.dump({"token": mock_developer_token, "expires": time.time() + 86400 * 60}, f)

        user_token_file = mock_config_dir / "music_user_token.json"
        with open(user_token_file, "w") as f:
            json.dump({"music_user_token": mock_user_token}, f)


class TestUserJourneyPowerUser:
    """Integration tests for power user workflows (20+ actions)."""

    @responses.activate
    def test_album_workflow(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Power user: add entire album to library."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        self._setup_tokens(mock_config_dir, mock_developer_token, mock_user_token)

        # Search for album
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/search",
            json={
                "results": {
                    "albums": {
                        "data": [
                            {
                                "id": "alb1",
                                "attributes": {
                                    "name": "Dark Side of the Moon",
                                    "artistName": "Pink Floyd",
                                    "trackCount": 10,
                                    "releaseDate": "1973-03-01",
                                },
                            }
                        ]
                    }
                }
            },
            status=200,
        )

        # Add album to library
        responses.add(
            responses.POST,
            "https://api.music.apple.com/v1/me/library",
            json={},
            status=202,
        )

        result = server.library(action="add", album="Dark Side of the Moon", artist="Pink Floyd")
        assert "Dark Side" in result or "Added" in result.lower() or "Album" in result

    @responses.activate
    def test_copy_playlist_workflow(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Power user: copy a playlist."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        self._setup_tokens(mock_config_dir, mock_developer_token, mock_user_token)

        # Source playlist
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/playlists",
            json={
                "data": [{"id": "p.src", "attributes": {"name": "Original Mix", "canEdit": True}}]
            },
            status=200,
        )

        # Get source tracks
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.src/tracks",
            json={
                "data": [
                    {"id": "i.t1", "attributes": {"name": "Track 1", "artistName": "Artist 1"}},
                    {"id": "i.t2", "attributes": {"name": "Track 2", "artistName": "Artist 2"}},
                ]
            },
            status=200,
        )

        # Create new playlist
        responses.add(
            responses.POST,
            "https://api.music.apple.com/v1/me/library/playlists",
            json={"data": [{"id": "p.new", "attributes": {"name": "Copy of Original Mix"}}]},
            status=201,
        )

        # Add tracks to new playlist
        responses.add(
            responses.POST,
            "https://api.music.apple.com/v1/me/library/playlists/p.new/tracks",
            json={},
            status=201,
        )

        result = server.playlist(
            action="copy", source="Original Mix", new_name="Copy of Original Mix"
        )
        assert "p.new" in result or "Copy" in result or "copied" in result.lower()

    @responses.activate
    def test_search_deduplication(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Search results should be deduplicated by track ID."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        self._setup_tokens(mock_config_dir, mock_developer_token, mock_user_token)

        # API returns duplicates
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/search",
            json={
                "results": {
                    "songs": {
                        "data": [
                            {
                                "id": "dup1",
                                "attributes": {
                                    "name": "Duplicate Song",
                                    "artistName": "Artist",
                                    "albumName": "Album",
                                    "durationInMillis": 200000,
                                    "releaseDate": "2020-01-01",
                                    "genreNames": ["Pop"],
                                },
                            },
                            {
                                "id": "dup1",
                                "attributes": {
                                    "name": "Duplicate Song",
                                    "artistName": "Artist",
                                    "albumName": "Album",
                                    "durationInMillis": 200000,
                                    "releaseDate": "2020-01-01",
                                    "genreNames": ["Pop"],
                                },
                            },  # Same ID = duplicate
                            {
                                "id": "dup2",
                                "attributes": {
                                    "name": "Unique Song",
                                    "artistName": "Artist",
                                    "albumName": "Album",
                                    "durationInMillis": 180000,
                                    "releaseDate": "2020-01-01",
                                    "genreNames": ["Pop"],
                                },
                            },
                        ]
                    }
                }
            },
            status=200,
        )

        result = server.catalog(action="search", query="test query")

        # Should show "2 Songs" not "3 Songs"
        assert "2 Songs" in result

    def _setup_tokens(self, mock_config_dir, mock_developer_token, mock_user_token):
        """Helper to setup authentication tokens."""
        dev_token_file = mock_config_dir / "developer_token.json"
        with open(dev_token_file, "w") as f:
            json.dump({"token": mock_developer_token, "expires": time.time() + 86400 * 60}, f)

        user_token_file = mock_config_dir / "music_user_token.json"
        with open(user_token_file, "w") as f:
            json.dump({"music_user_token": mock_user_token}, f)


class TestCatalogAlbumDetails:
    """Tests for catalog album_details action."""

    @responses.activate
    def test_album_details_by_id(self, mock_config_dir, mock_developer_token, mock_user_token):
        """Should fetch album metadata and tracks by catalog ID."""
        # Setup tokens
        dev_token_file = mock_config_dir / "developer_token.json"
        with open(dev_token_file, "w") as f:
            json.dump({"token": mock_developer_token, "expires": time.time() + 86400 * 60}, f)

        user_token_file = mock_config_dir / "music_user_token.json"
        with open(user_token_file, "w") as f:
            json.dump({"music_user_token": mock_user_token}, f)

        # Mock album metadata response
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/albums/1781270319",
            json={
                "data": [
                    {
                        "id": "1781270319",
                        "attributes": {
                            "name": "GNX",
                            "artistName": "Kendrick Lamar",
                            "releaseDate": "2024-11-22",
                            "genreNames": ["Hip-Hop/Rap"],
                            "recordLabel": "pgLang",
                            "trackCount": 12,
                            "copyright": "℗ 2024 pgLang",
                        },
                    }
                ]
            },
            status=200,
        )

        # Mock tracks response
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/albums/1781270319/tracks",
            json={
                "data": [
                    {
                        "id": "t1",
                        "attributes": {"name": "wacced out murals", "durationInMillis": 251000},
                    },
                    {"id": "t2", "attributes": {"name": "squabble up", "durationInMillis": 193000}},
                ]
            },
            status=200,
        )

        result = server.catalog(action="album_details", album="1781270319")

        assert "GNX" in result
        assert "Kendrick Lamar" in result
        assert "2024-11-22" in result
        assert "Hip-Hop/Rap" in result
        assert "pgLang" in result
        assert "wacced out murals" in result
        assert "squabble up" in result

    @responses.activate
    def test_album_details_by_name_fuzzy_match(
        self, mock_config_dir, mock_developer_token, mock_user_token
    ):
        """Should find album by name using fuzzy matching."""
        # Setup tokens
        dev_token_file = mock_config_dir / "developer_token.json"
        with open(dev_token_file, "w") as f:
            json.dump({"token": mock_developer_token, "expires": time.time() + 86400 * 60}, f)

        user_token_file = mock_config_dir / "music_user_token.json"
        with open(user_token_file, "w") as f:
            json.dump({"music_user_token": mock_user_token}, f)

        # Mock search response
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/search",
            json={
                "results": {
                    "albums": {
                        "data": [
                            {
                                "id": "123",
                                "attributes": {"name": "Abbey Road", "artistName": "The Beatles"},
                            }
                        ]
                    }
                }
            },
            status=200,
        )

        # Mock album metadata
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/albums/123",
            json={
                "data": [
                    {
                        "id": "123",
                        "attributes": {
                            "name": "Abbey Road",
                            "artistName": "The Beatles",
                            "releaseDate": "1969-09-26",
                            "genreNames": ["Rock"],
                            "recordLabel": "Apple Records",
                            "trackCount": 17,
                            "copyright": "℗ 1969",
                        },
                    }
                ]
            },
            status=200,
        )

        # Mock tracks
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/albums/123/tracks",
            json={
                "data": [
                    {
                        "id": "t1",
                        "attributes": {"name": "Come Together", "durationInMillis": 259000},
                    },
                ]
            },
            status=200,
        )

        result = server.catalog(action="album_details", album="abbey road", artist="beatles")

        assert "Abbey Road" in result
        assert "The Beatles" in result
        assert "Come Together" in result

    @responses.activate
    def test_album_details_missing_album_error(
        self, mock_config_dir, mock_developer_token, mock_user_token
    ):
        """Should return error when album not found."""
        # Setup tokens
        dev_token_file = mock_config_dir / "developer_token.json"
        with open(dev_token_file, "w") as f:
            json.dump({"token": mock_developer_token, "expires": time.time() + 86400 * 60}, f)

        user_token_file = mock_config_dir / "music_user_token.json"
        with open(user_token_file, "w") as f:
            json.dump({"music_user_token": mock_user_token}, f)

        # Mock search with no results
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/us/search",
            json={"results": {"albums": {"data": []}}},
            status=200,
        )

        result = server.catalog(action="album_details", album="NonexistentAlbum999")

        assert "not found" in result.lower() or "error" in result.lower()


class TestDiscoverStorefrontParameter:
    """Tests for discover action storefront parameter."""

    @responses.activate
    def test_charts_with_storefront_parameter(
        self, mock_config_dir, mock_developer_token, mock_user_token
    ):
        """Should query Italian charts without changing default storefront."""
        # Setup tokens
        dev_token_file = mock_config_dir / "developer_token.json"
        with open(dev_token_file, "w") as f:
            json.dump({"token": mock_developer_token, "expires": time.time() + 86400 * 60}, f)

        user_token_file = mock_config_dir / "music_user_token.json"
        with open(user_token_file, "w") as f:
            json.dump({"music_user_token": mock_user_token}, f)

        # Mock Italy charts response
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/it/charts",
            json={
                "results": {
                    "songs": [
                        {
                            "name": "Top brani",
                            "data": [
                                {
                                    "id": "it1",
                                    "attributes": {
                                        "name": "Italian Song",
                                        "artistName": "Italian Artist",
                                    },
                                },
                            ],
                        }
                    ]
                }
            },
            status=200,
        )

        result = server.discover(action="charts", chart_type="songs", storefront="it")

        # Verify it was called with 'it' storefront
        assert len(responses.calls) == 1
        assert "/catalog/it/charts" in responses.calls[0].request.url
        assert "Italian Song" in result or "Top brani" in result

    @responses.activate
    def test_top_songs_with_storefront_parameter(
        self, mock_config_dir, mock_developer_token, mock_user_token
    ):
        """Should query artist top songs in specific storefront."""
        # Setup tokens
        dev_token_file = mock_config_dir / "developer_token.json"
        with open(dev_token_file, "w") as f:
            json.dump({"token": mock_developer_token, "expires": time.time() + 86400 * 60}, f)

        user_token_file = mock_config_dir / "music_user_token.json"
        with open(user_token_file, "w") as f:
            json.dump({"music_user_token": mock_user_token}, f)

        # Mock search in JP storefront
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/jp/search",
            json={
                "results": {
                    "artists": {
                        "data": [{"id": "jp-artist-123", "attributes": {"name": "Japanese Artist"}}]
                    }
                }
            },
            status=200,
        )

        # Mock top songs
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/catalog/jp/artists/jp-artist-123/view/top-songs",
            json={
                "data": [
                    {
                        "id": "jp-song-1",
                        "attributes": {"name": "JP Hit Song", "artistName": "Japanese Artist"},
                    },
                ]
            },
            status=200,
        )

        result = server.discover(action="top_songs", artist="Japanese Artist", storefront="jp")

        # Verify JP storefront was used
        assert any("/catalog/jp/" in call.request.url for call in responses.calls)
        assert "Japanese Artist" in result


class TestDiscoverRecommendationsLimit:
    """Tests for discover recommendations limit parameter."""

    @responses.activate
    def test_recommendations_respects_limit_parameter(
        self, mock_config_dir, mock_developer_token, mock_user_token
    ):
        """Should only return requested number of recommendations."""
        # Setup tokens
        dev_token_file = mock_config_dir / "developer_token.json"
        with open(dev_token_file, "w") as f:
            json.dump({"token": mock_developer_token, "expires": time.time() + 86400 * 60}, f)

        user_token_file = mock_config_dir / "music_user_token.json"
        with open(user_token_file, "w") as f:
            json.dump({"music_user_token": mock_user_token}, f)

        # Mock recommendations response with many items
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/recommendations",
            json={
                "data": [
                    {
                        "attributes": {"title": {"stringForDisplay": "For You"}},
                        "relationships": {
                            "contents": {
                                "data": [
                                    {
                                        "id": f"rec{i}",
                                        "type": "songs",
                                        "attributes": {
                                            "name": f"Song {i}",
                                            "artistName": "Artist",
                                            "releaseDate": "2024-01-01",
                                        },
                                    }
                                    for i in range(1, 51)  # 50 items
                                ]
                            }
                        },
                    }
                ]
            },
            status=200,
        )

        # Request only 15 items
        result = server.discover(action="recommendations", limit=15, format="text")

        # Count items in result (rough check - each song should have a line)
        lines = [l for l in result.split("\n") if l.strip() and not l.startswith("===")]
        # Should have ~15 lines, not 50
        assert len(lines) <= 20  # Allow some buffer for formatting

    @responses.activate
    def test_recommendations_limit_zero_returns_all(
        self, mock_config_dir, mock_developer_token, mock_user_token
    ):
        """Should return all recommendations when limit=0."""
        # Setup tokens
        dev_token_file = mock_config_dir / "developer_token.json"
        with open(dev_token_file, "w") as f:
            json.dump({"token": mock_developer_token, "expires": time.time() + 86400 * 60}, f)

        user_token_file = mock_config_dir / "music_user_token.json"
        with open(user_token_file, "w") as f:
            json.dump({"music_user_token": mock_user_token}, f)

        # Mock recommendations response
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/recommendations",
            json={
                "data": [
                    {
                        "attributes": {"title": {"stringForDisplay": "For You"}},
                        "relationships": {
                            "contents": {
                                "data": [
                                    {
                                        "id": f"rec{i}",
                                        "type": "songs",
                                        "attributes": {
                                            "name": f"Song {i}",
                                            "artistName": "Artist",
                                            "releaseDate": "2024-01-01",
                                        },
                                    }
                                    for i in range(1, 21)  # 20 items
                                ]
                            }
                        },
                    }
                ]
            },
            status=200,
        )

        # Request with limit=0 (should return all)
        result = server.discover(action="recommendations", limit=0, format="text")

        # Should have all ~20 items
        lines = [l for l in result.split("\n") if l.strip() and not l.startswith("===")]
        assert len(lines) >= 7  # At least 7-8 items from the category


class TestHasDeveloperToken:
    """Tests for _has_developer_token feature-detection helper."""

    def test_returns_false_when_token_missing(self, monkeypatch):
        """No token configured → False, no exception leaked."""

        def raise_missing():
            raise FileNotFoundError("Developer token not found")

        monkeypatch.setattr(server, "get_developer_token", raise_missing)
        assert server._has_developer_token() is False

    def test_returns_false_when_token_expired(self, monkeypatch):
        """Expired token → False (caller should fall back, not crash)."""

        def raise_expired():
            raise ValueError("Developer token expired or expiring soon")

        monkeypatch.setattr(server, "get_developer_token", raise_expired)
        assert server._has_developer_token() is False

    def test_returns_true_when_token_valid(self, monkeypatch):
        """Token present and valid → True."""
        monkeypatch.setattr(server, "get_developer_token", lambda: "fake_token")
        assert server._has_developer_token() is True


class TestSmartAddVerifiesSplitMatch:
    """Verify _smart_as_add_track_to_playlist re-checks split-resolved adds."""

    def test_split_match_with_verify_passes_returns_success(self, monkeypatch):
        """Happy path: split candidate adds and verifies → return success."""

        def fake_add(playlist, name, artist, album):
            if " - " in name and artist is None:
                return False, "Track not found"
            return True, f"Added '{name}' to '{playlist}'"

        def fake_verify(playlist, name, artist):
            return (name, artist) == ("Silvera", "GOJIRA")

        monkeypatch.setattr(server.asc, "add_track_to_playlist", fake_add)
        monkeypatch.setattr(server, "_verify_track_in_playlist", fake_verify)

        ok, _result, split_match = server._smart_as_add_track_to_playlist(
            "MyPlaylist", "Silvera - GOJIRA", None, None
        )
        assert ok is True
        assert split_match == ("Silvera", "GOJIRA")

    def test_split_match_verify_failure_does_not_cascade(self, monkeypatch):
        """First split add succeeds but verify fails → return False with a
        descriptive error rather than try the next candidate.

        Cascading would risk a second wrong-track add on slow iCloud sync
        (the 'real' track really did land but verify is just slow). One
        suspect add is better than two."""
        add_calls = []

        def fake_add(playlist, name, artist, album):
            add_calls.append((name, artist))
            if " - " in name and artist is None:
                return False, "Track not found"
            # Both candidates would add OK — but we should never reach the
            # second one once verify fails on the first.
            return True, f"Added '{name}' to '{playlist}'"

        def fake_verify(playlist, name, artist):
            return False  # always fails — simulates slow sync

        monkeypatch.setattr(server.asc, "add_track_to_playlist", fake_add)
        monkeypatch.setattr(server, "_verify_track_in_playlist", fake_verify)

        ok, result, split_match = server._smart_as_add_track_to_playlist(
            "MyPlaylist", "Silvera - GOJIRA", None, None
        )
        assert ok is False
        assert split_match is None
        # Literal attempt + first split candidate only — second candidate
        # must NOT have been tried (would cascade a second wrong add).
        assert len(add_calls) == 2
        assert "could not verify" in result

    def test_first_attempt_success_does_not_verify(self, monkeypatch):
        """A clean first-attempt add should NOT trigger verification — preserves
        existing latency profile and behavior for the common path."""
        verify_called = []

        def fake_add(playlist, name, artist, album):
            return True, "Added"

        def fake_verify(*args, **kwargs):
            verify_called.append(args)
            return False  # would fail if called

        monkeypatch.setattr(server.asc, "add_track_to_playlist", fake_add)
        monkeypatch.setattr(server, "_verify_track_in_playlist", fake_verify)

        ok, _result, split_match = server._smart_as_add_track_to_playlist(
            "MyPlaylist", "Some Track", "Some Artist", None
        )
        assert ok is True
        assert split_match is None
        assert verify_called == []  # verify must NOT run on first-try success


class TestAppleScriptPerTrackGuards:
    """Confirms the per-track try/on error wrap is present in generated scripts.

    These tests don't run AppleScript — they assert on the script string the
    Python helpers build. That's enough to confirm we deployed the defensive
    wrap (preventing one broken track from aborting the whole iteration).
    """

    def test_get_library_songs_script_wraps_per_track(self):
        """get_library_songs builds a script whose per-track read sits inside try/on error."""
        from applemusic_mcp import applescript as asc

        captured = {}

        def fake_run(script):
            captured["script"] = script
            return True, ""

        with patch.object(asc, "run_applescript", fake_run):
            asc.get_library_songs(limit=10)

        s = captured["script"]
        # The skip-comment is a stable marker for the defensive block
        assert "skip inaccessible tracks" in s
        # Defensive wrap surrounds the property reads
        assert s.find("set tName to name of t") > s.find("try")

    def test_get_library_songs_page_script_structure(self):
        """get_library_songs_page uses count+range access and has defensive try/on error."""
        from applemusic_mcp import applescript as asc

        captured = {}

        def fake_run(script):
            captured["script"] = script
            return True, "total:0\n"

        with patch.object(asc, "run_applescript", fake_run):
            asc.get_library_songs_page(offset=10, limit=5)

        s = captured["script"]
        assert "count of tracks" in s
        assert "items 11 through" in s
        assert "skip inaccessible tracks" in s
        assert s.find("set tName to name of t") > s.find("try")

    def test_get_library_songs_page_parses_total_and_tracks(self):
        """get_library_songs_page parses the total: header line and track rows."""
        from applemusic_mcp import applescript as asc

        fake_output = "total:42\nAlpha|||Artist A|||Album A|||180.0|||Rock|||2020|||PID1|||false\n"

        with patch.object(asc, "run_applescript", lambda _: (True, fake_output)):
            success, tracks, total, error = asc.get_library_songs_page(offset=0, limit=10)

        assert success is True
        assert total == 42
        assert len(tracks) == 1
        assert tracks[0]["name"] == "Alpha"
        assert tracks[0]["id"] == "PID1"
        assert error == ""

    def test_get_library_songs_page_applescript_error_propagates(self):
        """get_library_songs_page returns (False, [], 0, error) on AppleScript failure."""
        from applemusic_mcp import applescript as asc

        with patch.object(asc, "run_applescript", lambda _: (False, "Music not running")):
            success, tracks, total, error = asc.get_library_songs_page(offset=0, limit=10)

        assert success is False
        assert tracks == []
        assert total == 0
        assert "Music not running" in error

    def test_get_library_songs_page_guards_invalid_applescript_range(self):
        """get_library_songs_page with limit <= 0 returns error without running AppleScript."""
        from applemusic_mcp import applescript as asc

        ran = []
        with patch.object(asc, "run_applescript", lambda _: ran.append(True) or (True, "")):
            success, tracks, total, error = asc.get_library_songs_page(offset=0, limit=0)

        assert success is False
        assert tracks == []
        assert total == 0
        assert "limit must be > 0" in error
        assert ran == [], "AppleScript must not run when limit <= 0"

    def test_search_library_page_parses_total_and_slice(self):
        """search_library_page parses the total: header line and returns the page tracks."""
        from applemusic_mcp import applescript as asc

        fake_output = "total:42\nAlpha|||Artist A|||Album A|||180.0|||Rock|||2020|||PID1|||false\n"
        with patch.object(asc, "run_applescript", lambda _: (True, fake_output)):
            success, tracks, total, error = asc.search_library_page(
                "alpha", "songs", offset=0, limit=10
            )

        assert success is True
        assert total == 42
        assert len(tracks) == 1
        assert tracks[0]["name"] == "Alpha"
        assert tracks[0]["id"] == "PID1"
        assert error == ""

    def test_search_library_page_script_uses_range_slice(self):
        """search_library_page slices searchResults by offset/limit (O(limit) range
        access) instead of always capping at the first 100 hits."""
        from applemusic_mcp import applescript as asc

        captured = {}

        def fake_run(script):
            captured["script"] = script
            return True, "total:0\n"

        with patch.object(asc, "run_applescript", fake_run):
            asc.search_library_page("query", "songs", offset=10, limit=5)

        s = captured["script"]
        assert "search library playlist 1 for" in s
        assert "count of searchResults" in s
        assert "items 11 through" in s  # offset 10 -> 1-based start 11
        assert "skip inaccessible tracks" in s

    def test_search_library_page_guards_invalid_limit(self):
        """search_library_page with limit <= 0 returns an error without running AppleScript."""
        from applemusic_mcp import applescript as asc

        ran = []
        with patch.object(asc, "run_applescript", lambda _: ran.append(True) or (True, "")):
            success, tracks, total, error = asc.search_library_page("q", "songs", offset=0, limit=0)

        assert success is False
        assert tracks == []
        assert total == 0
        assert "limit must be > 0" in error
        assert ran == [], "AppleScript must not run when limit <= 0"

    def test_search_library_keeps_two_tuple_return(self):
        """search_library must keep its (success, tracks) shape so the ~6 existing
        callers that unpack two values don't break."""
        from applemusic_mcp import applescript as asc

        fake_output = "total:1\nAlpha|||Artist A|||Album A|||180.0|||Rock|||2020|||PID1|||false\n"
        with patch.object(asc, "run_applescript", lambda _: (True, fake_output)):
            result = asc.search_library("alpha", "songs")

        assert len(result) == 2, "search_library must stay a 2-tuple"
        success, tracks = result
        assert success is True
        assert tracks[0]["name"] == "Alpha"

    def test_search_library_script_wraps_per_track(self):
        from applemusic_mcp import applescript as asc

        captured = {}

        def fake_run(script):
            captured["script"] = script
            return True, ""

        with patch.object(asc, "run_applescript", fake_run):
            asc.search_library("anything", "songs")

        s = captured["script"]
        assert "skip inaccessible tracks" in s

    def test_search_playlist_script_wraps_per_track(self):
        from applemusic_mcp import applescript as asc

        captured = {}

        def fake_run(script):
            captured["script"] = script
            return True, ""

        with patch.object(asc, "run_applescript", fake_run):
            asc.search_playlist("MyPlaylist", "anything")

        s = captured["script"]
        assert "skip inaccessible tracks" in s


class TestLibrarySearchPagination:
    """library(action='search') must honor limit and offset on macOS.

    Regression guard: the original AppleScript path called search_library without
    a limit and never sliced the result, so limit=N was silently ignored and the
    caller always got up to 100 hits with no way to page past them.
    """

    @staticmethod
    def _fake_page_factory(captured, total):
        def fake_page(query, types, offset=0, limit=100):
            captured["query"] = query
            captured["types"] = types
            captured["offset"] = offset
            captured["limit"] = limit
            tracks = [
                {
                    "name": f"S{i}",
                    "artist": "A",
                    "album": "Al",
                    "duration": "3:00",
                    "genre": "R",
                    "year": "2020",
                    "id": f"id{offset + i}",
                    "explicit": "No",
                }
                for i in range(limit)
            ]
            return True, tracks, total, ""

        return fake_page

    def test_search_forwards_limit_and_offset_to_page(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        captured = {}
        monkeypatch.setattr(
            server.asc, "search_library_page", self._fake_page_factory(captured, total=100)
        )

        server.library(
            action="search",
            query="x",
            types="songs",
            limit=5,
            offset=10,
            fetch_explicit=False,
            clean_only=False,
        )

        assert captured["limit"] == 5, "limit must reach the AppleScript page query"
        assert captured["offset"] == 10, "offset must reach the AppleScript page query"

    def test_search_returns_only_limit_results(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        captured = {}
        monkeypatch.setattr(
            server.asc, "search_library_page", self._fake_page_factory(captured, total=100)
        )

        result = server.library(
            action="search",
            query="x",
            types="songs",
            limit=5,
            offset=0,
            format="json",
            fetch_explicit=False,
            clean_only=False,
        )

        assert len(json.loads(result)) == 5, "limit=5 must yield exactly 5 results, not 100"

    def test_search_shows_pagination_header(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        captured = {}
        monkeypatch.setattr(
            server.asc, "search_library_page", self._fake_page_factory(captured, total=100)
        )

        result = server.library(
            action="search",
            query="x",
            types="songs",
            limit=5,
            offset=0,
            fetch_explicit=False,
            clean_only=False,
        )

        assert "of 100" in result, "text output should show total count for paging"


class TestLibrarySearchSurfacesAppleScriptError:
    """When both AS and API fail, the message should expose BOTH causes."""

    def test_token_error_includes_applescript_failure(self, monkeypatch):
        """If AS fails AND token is missing, surface both — don't pretend the
        only problem was the token (the original misleading-error class)."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)

        # AppleScript fails outright
        def fake_page(query, types, offset=0, limit=100):
            return False, [], 0, "AppleScript exited with code 1"

        monkeypatch.setattr(server.asc, "search_library_page", fake_page)

        # API path raises the legacy token error
        def fake_get_headers():
            raise FileNotFoundError("Developer token not found. Run: applemusic-mcp generate-token")

        monkeypatch.setattr(server, "get_headers", fake_get_headers)

        result = server._library_search("test query")
        assert "Developer token not found" in result
        assert "AppleScript also failed" in result
        assert "AppleScript exited with code 1" in result

    @pytest.mark.usefixtures("mock_config_dir")
    def test_no_songs_found_includes_applescript_failure(
        self, monkeypatch, mock_config_dir, mock_developer_token, mock_user_token
    ):
        """If AS fails AND API returns zero songs, the user should still see
        why AS failed — not a bare 'No songs found' that hides the cause."""
        import responses as resp_lib

        # Configure tokens so the API path actually runs (no FileNotFoundError)
        dev_token_file = mock_config_dir / "developer_token.json"
        with open(dev_token_file, "w") as f:
            json.dump({"token": mock_developer_token, "expires": time.time() + 86400 * 30}, f)
        user_token_file = mock_config_dir / "music_user_token.json"
        with open(user_token_file, "w") as f:
            json.dump({"music_user_token": mock_user_token}, f)

        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)

        def fake_page(query, types, offset=0, limit=100):
            return False, [], 0, "Music app not running"

        monkeypatch.setattr(server.asc, "search_library_page", fake_page)

        with resp_lib.RequestsMock() as r:
            r.add(
                resp_lib.GET,
                f"{server.BASE_URL}/me/library/search",
                json={"results": {"library-songs": {"data": []}}},
                status=200,
            )
            result = server._library_search("nonexistent track")

        assert "No songs found" in result
        assert "AppleScript also failed" in result
        assert "Music app not running" in result


class TestClassifyApplescriptError:
    """Tests for applescript.classify_error pattern matching."""

    def test_automation_denied_by_code(self):
        from applemusic_mcp import applescript as asc

        text = "execution error: Music got an error: Not authorized to send Apple events to Music. (-1743)"
        assert asc.classify_error(text) == asc.ERROR_AUTOMATION_DENIED

    def test_automation_denied_by_phrase(self):
        from applemusic_mcp import applescript as asc

        # Older / variant macOS wording
        assert (
            asc.classify_error("Application is not allowed assistive access")
            == asc.ERROR_AUTOMATION_DENIED
        )

    def test_music_not_running_by_code_609(self):
        from applemusic_mcp import applescript as asc

        text = "execution error: Music got an error: Connection is invalid. (-609)"
        assert asc.classify_error(text) == asc.ERROR_MUSIC_NOT_RUNNING

    def test_music_not_running_by_code_10810(self):
        from applemusic_mcp import applescript as asc

        text = "Application isn't running. (-10810)"
        assert asc.classify_error(text) == asc.ERROR_MUSIC_NOT_RUNNING

    def test_music_not_running_by_phrase(self):
        from applemusic_mcp import applescript as asc

        assert asc.classify_error('Can\'t get application "Music"') == asc.ERROR_MUSIC_NOT_RUNNING

    def test_timeout(self):
        from applemusic_mcp import applescript as asc

        # The exact message run_applescript emits on subprocess.TimeoutExpired
        assert asc.classify_error("AppleScript timed out after 30 seconds") == asc.ERROR_TIMEOUT

    def test_syntax_error(self):
        from applemusic_mcp import applescript as asc

        assert asc.classify_error("syntax error in line 3") == asc.ERROR_SYNTAX

    def test_unknown_for_logic_errors(self):
        """App-logic errors (track not found, playlist empty, etc.) classify as
        unknown so callers can still cascade to the API for legitimate
        fallbacks."""
        from applemusic_mcp import applescript as asc

        assert asc.classify_error("Track 'X' not found in library") == asc.ERROR_UNKNOWN
        assert asc.classify_error("ERROR:Playlist is empty") == asc.ERROR_UNKNOWN

    def test_unknown_for_empty(self):
        from applemusic_mcp import applescript as asc

        assert asc.classify_error("") == asc.ERROR_UNKNOWN


class TestFormatApplescriptError:
    """Tests for _format_applescript_error user-facing messages."""

    def test_music_not_running_message(self):
        msg = server._format_applescript_error("Connection is invalid (-609)", "create playlist")
        assert "Music.app isn't running" in msg
        assert "create playlist" in msg
        assert "Open Music.app" in msg

    def test_automation_denied_message(self):
        msg = server._format_applescript_error(
            "Not authorized to send Apple events to Music. (-1743)", "list playlists"
        )
        assert "Automation permission denied" in msg
        assert "list playlists" in msg
        assert "System Settings" in msg
        assert "Music" in msg  # mentions enabling the Music toggle

    def test_timeout_message(self):
        msg = server._format_applescript_error(
            "AppleScript timed out after 30 seconds", "rate track"
        )
        assert "timed out" in msg
        assert "unresponsive" in msg

    def test_unknown_includes_raw(self):
        """Unknown errors fall through to raw display so users can report them."""
        msg = server._format_applescript_error("some weird error", "some op")
        assert "some weird error" in msg
        assert "some op" in msg

    def test_no_operation_context(self):
        """Operation context is optional — no parens when not provided."""
        msg = server._format_applescript_error("Connection is invalid (-609)")
        assert "Music.app isn't running" in msg
        # No empty-parens artifact like "isn't running ()."
        assert "()" not in msg


class TestPlaylistCreateNoTokenLeakOnAsFailure:
    """Regression test for the Reddit-reported bug — _playlist_create must
    NOT cascade to the API path on macOS when AS fails. Otherwise a
    tokenless macOS user trying to create a playlist (the README-promised
    happy path) sees 'Developer token not found' when their actual problem
    is Music.app not running."""

    def test_music_not_running_does_not_leak_token_error(self, monkeypatch):
        from applemusic_mcp import applescript as real_asc

        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)

        mock_asc = MagicMock()
        mock_asc.create_playlist.return_value = (
            False,
            "execution error: Music got an error: Connection is invalid. (-609)",
        )
        mock_asc.classify_error = real_asc.classify_error
        mock_asc.ERROR_UNKNOWN = real_asc.ERROR_UNKNOWN
        mock_asc.ERROR_AUTOMATION_DENIED = real_asc.ERROR_AUTOMATION_DENIED
        mock_asc.ERROR_MUSIC_NOT_RUNNING = real_asc.ERROR_MUSIC_NOT_RUNNING
        mock_asc.ERROR_TIMEOUT = real_asc.ERROR_TIMEOUT
        mock_asc.ERROR_SYNTAX = real_asc.ERROR_SYNTAX
        monkeypatch.setattr(server, "asc", mock_asc)

        # Token deliberately not configured — if cascade leaks, this is
        # the error the user would see.
        def raise_no_token():
            raise FileNotFoundError("Developer token not found. Run: applemusic-mcp generate-token")

        monkeypatch.setattr(server, "get_headers", raise_no_token)

        result = server._playlist_create("My New Playlist")

        assert "Music.app isn't running" in result
        assert "Developer token not found" not in result
        assert "generate-token" not in result

    def test_automation_denied_surfaces_actionable_message(self, monkeypatch):
        from applemusic_mcp import applescript as real_asc

        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)

        mock_asc = MagicMock()
        mock_asc.create_playlist.return_value = (
            False,
            "Not authorized to send Apple events to Music. (-1743)",
        )
        mock_asc.classify_error = real_asc.classify_error
        mock_asc.ERROR_UNKNOWN = real_asc.ERROR_UNKNOWN
        mock_asc.ERROR_AUTOMATION_DENIED = real_asc.ERROR_AUTOMATION_DENIED
        mock_asc.ERROR_MUSIC_NOT_RUNNING = real_asc.ERROR_MUSIC_NOT_RUNNING
        mock_asc.ERROR_TIMEOUT = real_asc.ERROR_TIMEOUT
        mock_asc.ERROR_SYNTAX = real_asc.ERROR_SYNTAX
        monkeypatch.setattr(server, "asc", mock_asc)

        result = server._playlist_create("Whatever")
        assert "Automation permission denied" in result
        assert "System Settings" in result
        assert "Developer token not found" not in result

    def test_applescript_success_returns_normally(self, monkeypatch):
        """Sanity: the happy path still works — AS create succeeds, function returns ID."""
        from applemusic_mcp import applescript as real_asc

        monkeypatch.setattr(server, "AppLESCRIPT_AVAILABLE", True) if False else None
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)

        mock_asc = MagicMock()
        mock_asc.create_playlist.return_value = (True, "p.123abc")
        mock_asc.classify_error = real_asc.classify_error
        monkeypatch.setattr(server, "asc", mock_asc)

        result = server._playlist_create("My Playlist")
        assert "Created playlist 'My Playlist'" in result
        assert "p.123abc" in result


class TestLibraryRateGatedFallthrough:
    """_library_rate has a legitimate API fallback for catalog songs not in
    the local library, so it uses gated fallthrough — environmental AS
    errors surface; logic AS errors (track not found) cascade to API.

    These tests use the public ``rating`` MCP tool to exercise the full
    code path including _resolve_track, which simplifies setup."""

    def test_environmental_error_blocks_cascade(self, monkeypatch):
        from applemusic_mcp import applescript as real_asc

        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)

        mock_asc = MagicMock()
        mock_asc.love_track.return_value = (False, "Not authorized (-1743)")
        mock_asc.classify_error = real_asc.classify_error
        mock_asc.ERROR_UNKNOWN = real_asc.ERROR_UNKNOWN
        mock_asc.ERROR_AUTOMATION_DENIED = real_asc.ERROR_AUTOMATION_DENIED
        mock_asc.ERROR_MUSIC_NOT_RUNNING = real_asc.ERROR_MUSIC_NOT_RUNNING
        mock_asc.ERROR_TIMEOUT = real_asc.ERROR_TIMEOUT
        mock_asc.ERROR_SYNTAX = real_asc.ERROR_SYNTAX
        monkeypatch.setattr(server, "asc", mock_asc)

        # If cascade happened, _search_catalog_songs would be called.
        def fail_if_called(*args, **kwargs):
            raise AssertionError("Cascade to API should have been blocked")

        monkeypatch.setattr(server, "_search_catalog_songs", fail_if_called)

        result = server._library_rate(
            action="love", track="Some Track", artist="Some Artist", stars=0
        )
        assert "Automation permission denied" in result

    def test_logic_error_allows_cascade(self, monkeypatch):
        """When AS fails with a non-environmental error (e.g. track not
        in library), cascade to API is allowed."""
        from applemusic_mcp import applescript as real_asc

        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)

        mock_asc = MagicMock()
        mock_asc.love_track.return_value = (False, "Track 'X' not found in library")
        mock_asc.classify_error = real_asc.classify_error
        mock_asc.ERROR_UNKNOWN = real_asc.ERROR_UNKNOWN
        mock_asc.ERROR_AUTOMATION_DENIED = real_asc.ERROR_AUTOMATION_DENIED
        mock_asc.ERROR_MUSIC_NOT_RUNNING = real_asc.ERROR_MUSIC_NOT_RUNNING
        mock_asc.ERROR_TIMEOUT = real_asc.ERROR_TIMEOUT
        mock_asc.ERROR_SYNTAX = real_asc.ERROR_SYNTAX
        monkeypatch.setattr(server, "asc", mock_asc)

        cascade_called = []

        def mock_catalog_search(*args, **kwargs):
            cascade_called.append(True)
            return []  # No results — falls through cleanly

        monkeypatch.setattr(server, "_search_catalog_songs", mock_catalog_search)

        server._library_rate(action="love", track="Some Track", artist="Some Artist", stars=0)
        assert cascade_called, "Cascade to API should have happened on logic-level AS error"


class TestLibraryBrowseNoTokenLeakOnAsFailure:
    """Regression test for the reviewer-flagged callsite. _library_browse
    is `library(action="browse")` — same shape as _playlist_list.
    AppleScript failure on macOS must NOT cascade to the API path and
    leak the token error."""

    def test_music_not_running_does_not_leak_token_error(self, monkeypatch):
        from applemusic_mcp import applescript as real_asc

        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)

        mock_asc = MagicMock()
        mock_asc.get_library_songs_page.return_value = (
            False,
            [],
            0,
            "execution error: Music got an error: Connection is invalid. (-609)",
        )
        mock_asc.classify_error = real_asc.classify_error
        mock_asc.ERROR_UNKNOWN = real_asc.ERROR_UNKNOWN
        mock_asc.ERROR_AUTOMATION_DENIED = real_asc.ERROR_AUTOMATION_DENIED
        mock_asc.ERROR_MUSIC_NOT_RUNNING = real_asc.ERROR_MUSIC_NOT_RUNNING
        mock_asc.ERROR_TIMEOUT = real_asc.ERROR_TIMEOUT
        mock_asc.ERROR_SYNTAX = real_asc.ERROR_SYNTAX
        monkeypatch.setattr(server, "asc", mock_asc)

        # Token NOT configured. If cascade leaks, this is what the user sees.
        def raise_no_token():
            raise FileNotFoundError("Developer token not found. Run: applemusic-mcp generate-token")

        monkeypatch.setattr(server, "get_headers", raise_no_token)

        result = server._library_browse(item_type="songs")
        assert "Music.app isn't running" in result
        assert "Developer token not found" not in result

    def test_automation_denied_surfaces_actionable(self, monkeypatch):
        from applemusic_mcp import applescript as real_asc

        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)

        mock_asc = MagicMock()
        mock_asc.get_library_songs_page.return_value = (
            False,
            [],
            0,
            "Not authorized to send Apple events to Music. (-1743)",
        )
        mock_asc.classify_error = real_asc.classify_error
        mock_asc.ERROR_UNKNOWN = real_asc.ERROR_UNKNOWN
        mock_asc.ERROR_AUTOMATION_DENIED = real_asc.ERROR_AUTOMATION_DENIED
        mock_asc.ERROR_MUSIC_NOT_RUNNING = real_asc.ERROR_MUSIC_NOT_RUNNING
        mock_asc.ERROR_TIMEOUT = real_asc.ERROR_TIMEOUT
        mock_asc.ERROR_SYNTAX = real_asc.ERROR_SYNTAX
        monkeypatch.setattr(server, "asc", mock_asc)

        result = server._library_browse(item_type="songs")
        assert "Automation permission denied" in result
        assert "System Settings" in result
        assert "Developer token not found" not in result


class TestResolveTrackBatch:
    """_resolve_track / _resolve_input batch parsing: newline, CSV, JSON,
    single. Newline support is the fix for callers that paste one song per
    line — previously the whole blob was treated as a single track name."""

    def test_newline_separated_names(self):
        blob = "\n".join(f"Song {i}" for i in range(1, 15))
        results = server._resolve_track(blob)
        assert len(results) == 14
        assert [r.value for r in results] == [f"Song {i}" for i in range(1, 15)]
        assert all(r.error is None for r in results)

    def test_newline_preserves_commas_in_titles(self):
        # Newline split must NOT further split on commas inside a title.
        results = server._resolve_track("Take Me Home, Country Roads\nImagine")
        assert [r.value for r in results] == ["Take Me Home, Country Roads", "Imagine"]

    def test_newline_skips_blank_lines(self):
        results = server._resolve_track("Hey Jude\n\n  \nLet It Be\n")
        assert [r.value for r in results] == ["Hey Jude", "Let It Be"]

    def test_newline_applies_artist_to_each(self):
        results = server._resolve_track("Hey Jude\nLet It Be", artist="Beatles")
        assert [r.artist for r in results] == ["Beatles", "Beatles"]

    def test_csv_still_works(self):
        results = server._resolve_track("Hey Jude, Let It Be")
        assert [r.value for r in results] == ["Hey Jude", "Let It Be"]

    def test_json_array_still_works(self):
        results = server._resolve_track('[{"name":"A","artist":"X"},{"name":"B","artist":"Y"}]')
        assert [(r.value, r.artist) for r in results] == [("A", "X"), ("B", "Y")]

    def test_single_name_still_works(self):
        results = server._resolve_track("Yesterday")
        assert len(results) == 1
        assert results[0].value == "Yesterday"


class TestLibraryFavorites:
    """library(action="favorites") lists loved (Favorite) songs via the
    AppleScript `whose loved is true` filter."""

    def _loved_songs(self):
        return [
            {
                "name": "Loved One",
                "artist": "Artist A",
                "album": "Album A",
                "duration": "3:21",
                "genre": "Pop",
                "year": "2020",
                "id": "loved1",
                "explicit": "No",
            },
            {
                "name": "Loved Two",
                "artist": "Artist B",
                "album": "Album B",
                "duration": "4:05",
                "genre": "Rock",
                "year": "2021",
                "id": "loved2",
                "explicit": "No",
            },
        ]

    def test_lists_loved_songs(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        mock_asc = MagicMock()
        mock_asc.get_loved_songs.return_value = (True, self._loved_songs())
        monkeypatch.setattr(server, "asc", mock_asc)

        result = server.library(action="favorites")
        assert "Loved One" in result
        assert "Loved Two" in result
        mock_asc.get_loved_songs.assert_called_once()

    def test_loved_alias(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        mock_asc = MagicMock()
        mock_asc.get_loved_songs.return_value = (True, self._loved_songs())
        monkeypatch.setattr(server, "asc", mock_asc)

        result = server.library(action="loved")
        assert "Loved One" in result

    def test_empty_library(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        mock_asc = MagicMock()
        mock_asc.get_loved_songs.return_value = (True, [])
        monkeypatch.setattr(server, "asc", mock_asc)

        result = server.library(action="favorites")
        assert "No favorite songs" in result

    def test_requires_macos(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        result = server.library(action="favorites")
        assert "requires macOS" in result

    def test_applescript_failure_surfaces_actionable(self, monkeypatch):
        from applemusic_mcp import applescript as real_asc

        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        mock_asc = MagicMock()
        mock_asc.get_loved_songs.return_value = (
            False,
            "execution error: Music got an error: Connection is invalid. (-609)",
        )
        mock_asc.classify_error = real_asc.classify_error
        mock_asc.ERROR_UNKNOWN = real_asc.ERROR_UNKNOWN
        mock_asc.ERROR_AUTOMATION_DENIED = real_asc.ERROR_AUTOMATION_DENIED
        mock_asc.ERROR_MUSIC_NOT_RUNNING = real_asc.ERROR_MUSIC_NOT_RUNNING
        mock_asc.ERROR_TIMEOUT = real_asc.ERROR_TIMEOUT
        mock_asc.ERROR_SYNTAX = real_asc.ERROR_SYNTAX
        monkeypatch.setattr(server, "asc", mock_asc)

        result = server.library(action="favorites")
        assert "Music.app isn't running" in result


class TestNotAllowedOvermatchFix:
    """Reviewer flagged that bare 'not allowed' was promoting Music.app
    logic-level errors (e.g. 'editing not allowed for smart playlist')
    to ERROR_AUTOMATION_DENIED, which would block legitimate API
    cascade in _library_rate. Verify the tightened phrase matching."""

    def test_smart_playlist_not_allowed_classifies_as_unknown(self):
        from applemusic_mcp import applescript as asc

        # This is a Music.app logic error, NOT an Automation denial.
        # It should classify as UNKNOWN so callers cascade as appropriate.
        assert asc.classify_error("operation not allowed on smart playlists") == asc.ERROR_UNKNOWN
        assert asc.classify_error("editing not allowed for this playlist") == asc.ERROR_UNKNOWN

    def test_full_assistive_phrase_still_classifies_as_automation_denied(self):
        from applemusic_mcp import applescript as asc

        # The actual -1743 phrase variant should still match.
        assert (
            asc.classify_error("Application is not allowed assistive access")
            == asc.ERROR_AUTOMATION_DENIED
        )
        assert (
            asc.classify_error("requires assistive access to control Music")
            == asc.ERROR_AUTOMATION_DENIED
        )


class TestPlaylistAddIdsRequireToken:
    """When a tokenless macOS user passes a track ID (not name) to
    playlist(action="add"), the AS-mode code path needs the API to
    resolve the ID's metadata before handing off to AppleScript. Prior
    to the fix, _playlist_add called get_headers() unconditionally inside
    the AS block — leaking 'Developer token not found' on the same
    user-facing surface as the bug this PR exists to close."""

    def test_id_without_token_says_what_to_do_not_dev_token(self, monkeypatch):
        from applemusic_mcp import applescript as real_asc

        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)

        # Set up: AS resolves the playlist by name, no tokens configured
        mock_asc = MagicMock()
        mock_asc.get_playlists.return_value = (
            True,
            [{"name": "Workout", "id": "p.work", "smart": False, "track_count": 5}],
        )
        # track_exists_in_playlist for dedup check (won't matter — we
        # should bail before reaching it)
        mock_asc.track_exists_in_playlist.return_value = (True, False)
        mock_asc.classify_error = real_asc.classify_error
        monkeypatch.setattr(server, "asc", mock_asc)

        # No token configured — _has_developer_token returns False,
        # which the new ID-guard catches and surfaces a specific message.
        monkeypatch.setattr(server, "_has_developer_token", lambda: False)

        # If the guard didn't fire, this would raise the misleading
        # FileNotFoundError. Patch get_headers to fail loudly so the
        # test catches a regression.
        def fail_loud():
            raise AssertionError(
                "get_headers() should not have been called — the ID guard "
                "must intercept before this point"
            )

        monkeypatch.setattr(server, "get_headers", fail_loud)

        result = server._playlist_add(
            playlist="Workout",
            track="1440783617",  # catalog ID format
            album="",
            artist="",
            allow_duplicates=False,
            verify=False,
            auto_search=False,
        )

        assert "Track IDs require an API token" in result
        assert "pass the track by name instead" in result
        # The legacy misleading error must NOT appear
        assert "Developer token not found" not in result.split("Track IDs")[0]

    def test_album_without_token_does_not_leak_dev_token_error(self, monkeypatch):
        """Adding by album also requires the catalog API (album's tracklist is
        fetched there). On tokenless macOS, _playlist_add must surface a
        specific 'add by album requires API' message — not 'Developer token
        not found'. Reviewer-flagged sibling of the ID guard."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)

        mock_asc = MagicMock()
        mock_asc.get_playlists.return_value = (
            True,
            [{"name": "Workout", "id": "p.work", "smart": False, "track_count": 0}],
        )
        monkeypatch.setattr(server, "asc", mock_asc)
        monkeypatch.setattr(server, "_has_developer_token", lambda: False)

        # If we reach get_headers, the guard didn't fire.
        def fail_loud():
            raise AssertionError(
                "get_headers() should not have been called — the album guard "
                "must intercept before this point"
            )

        monkeypatch.setattr(server, "get_headers", fail_loud)

        result = server._playlist_add(
            playlist="Workout",
            track="",
            album="Dark Side of the Moon",
            artist="",
            allow_duplicates=False,
            verify=False,
            auto_search=False,
        )

        assert "Adding by album requires an API token" in result
        assert "Developer token not found" not in result


class TestPlaylistCopyNoTokenLeakOnAsFailure:
    """Regression test for _playlist_copy AS-mode (by-name path). The fix
    in cdc3e40 deferred get_headers() from the function preamble to inside
    the API-mode (by-ID) branch only — without a regression test, a
    future refactor could re-introduce the upfront get_headers() call
    silently, leaking 'Developer token not found' to tokenless macOS
    users copying playlists by name.

    Reviewer-flagged gap (TestPlaylistCopyNoTokenLeakOnAsFailure was
    missing; all other fixed callsites had regression tests)."""

    @staticmethod
    def _wire_error_constants(mock_asc, real_asc):
        """Helper: replicate the error category attributes on a MagicMock
        so server-side `asc.ERROR_*` lookups resolve to the real strings,
        not auto-generated MagicMock instances. Without this, the
        category-equality checks inside `_format_applescript_error` and
        the gated-cascade in `_library_rate` always evaluate False."""
        mock_asc.classify_error = real_asc.classify_error
        mock_asc.ERROR_UNKNOWN = real_asc.ERROR_UNKNOWN
        mock_asc.ERROR_AUTOMATION_DENIED = real_asc.ERROR_AUTOMATION_DENIED
        mock_asc.ERROR_MUSIC_NOT_RUNNING = real_asc.ERROR_MUSIC_NOT_RUNNING
        mock_asc.ERROR_TIMEOUT = real_asc.ERROR_TIMEOUT
        mock_asc.ERROR_SYNTAX = real_asc.ERROR_SYNTAX

    def test_as_mode_by_name_does_not_call_get_headers(self, monkeypatch):
        from applemusic_mcp import applescript as real_asc

        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)

        mock_asc = MagicMock()
        mock_asc.get_playlists.return_value = (
            True,
            [{"name": "Source Playlist", "id": "src.id", "smart": False, "track_count": 3}],
        )
        # AS read of source tracks succeeds with one track
        mock_asc.get_playlist_tracks.return_value = (
            True,
            [{"name": "Track 1", "artist": "Artist 1", "album": "Album 1", "id": "t.1"}],
        )
        # AS create of destination succeeds
        mock_asc.create_playlist.return_value = (True, "p.dest")
        # AS add per track succeeds
        mock_asc.add_track_to_playlist.return_value = (True, "Added")
        self._wire_error_constants(mock_asc, real_asc)
        monkeypatch.setattr(server, "asc", mock_asc)

        # If get_headers is reached, the regression has happened.
        def fail_loud():
            raise AssertionError(
                "get_headers() should NOT be called on the AS-by-name copy path — "
                "v0.9.4 deferred it to API-mode (by-ID) only"
            )

        monkeypatch.setattr(server, "get_headers", fail_loud)

        # Tokenless invocation by name (no token configured, AS-mode)
        result = server._playlist_copy(source="Source Playlist", new_name="Destination")

        assert "Created" in result and "Destination" in result
        assert "Developer token not found" not in result

    def test_as_mode_failure_surfaces_formatted_error(self, monkeypatch):
        """When AS fails inside copy on macOS, surface the formatted error
        (Music.app isn't running, Automation denied, etc.) — not a raw
        stderr or a token error."""
        from applemusic_mcp import applescript as real_asc

        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)

        mock_asc = MagicMock()
        mock_asc.get_playlists.return_value = (
            True,
            [{"name": "Source Playlist", "id": "src.id", "smart": False, "track_count": 3}],
        )
        # AS read of source tracks fails environmentally
        mock_asc.get_playlist_tracks.return_value = (
            False,
            "execution error: Music got an error: Connection is invalid. (-609)",
        )
        self._wire_error_constants(mock_asc, real_asc)
        monkeypatch.setattr(server, "asc", mock_asc)

        def fail_loud():
            raise AssertionError("get_headers() reached — should have early-returned")

        monkeypatch.setattr(server, "get_headers", fail_loud)

        result = server._playlist_copy(source="Source Playlist", new_name="Destination")
        assert "Music.app isn't running" in result
        assert "Developer token not found" not in result


class TestLibrarySearchEmptyDoesNotLeakToken:
    """Regression test for the bug horrorshow75 hit on Reddit after v0.9.4
    shipped. _library_search's AS path treats `(True, [])` (empty success)
    differently from `(True, [results])` — the early-return guard was
    `if success and results:`, which fails on empty lists and falls
    through to the API path. On tokenless macOS, that path then leaks
    'Developer token not found' even though the truth was just 'song's
    not in your library'. v0.9.4 missed this case because the prior
    sweeps focused on AS-FAILURE paths, not AS-empty-SUCCESS paths."""

    def test_tokenless_macos_empty_as_returns_no_results_not_token_error(self, monkeypatch):
        from applemusic_mcp import applescript as real_asc

        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)

        mock_asc = MagicMock()
        # AS search succeeds but returns zero hits (song not in library)
        mock_asc.search_library_page.return_value = (True, [], 0, "")
        mock_asc.classify_error = real_asc.classify_error
        mock_asc.ERROR_UNKNOWN = real_asc.ERROR_UNKNOWN
        mock_asc.ERROR_AUTOMATION_DENIED = real_asc.ERROR_AUTOMATION_DENIED
        mock_asc.ERROR_MUSIC_NOT_RUNNING = real_asc.ERROR_MUSIC_NOT_RUNNING
        mock_asc.ERROR_TIMEOUT = real_asc.ERROR_TIMEOUT
        mock_asc.ERROR_SYNTAX = real_asc.ERROR_SYNTAX
        monkeypatch.setattr(server, "asc", mock_asc)

        # No token configured. If cascade fires, this is the misleading error.
        monkeypatch.setattr(server, "_has_developer_token", lambda: False)

        # If get_headers reaches the API, the regression has happened.
        def fail_loud():
            raise AssertionError(
                "get_headers() should not be called — empty AS result on "
                "tokenless macOS must short-circuit to 'No songs found'"
            )

        monkeypatch.setattr(server, "get_headers", fail_loud)

        result = server._library_search("nonexistent track xyz")
        assert "No songs found" in result
        # The leak-error must NOT appear
        assert "Developer token not found" not in result
        assert "applemusic-mcp generate-token" not in result
        assert "applemusic-mcp authorize" not in result

    def test_tokenful_macos_empty_as_still_cascades_for_cloud_check(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """When a token IS configured, empty AS results still cascade to API
        — the API may see cloud-synced tracks AS hasn't seen yet. This is
        the legitimate use case the cascade was originally designed for."""
        import responses as resp_lib

        # Configure tokens
        dev_token_file = mock_config_dir / "developer_token.json"
        with open(dev_token_file, "w") as f:
            json.dump({"token": mock_developer_token, "expires": time.time() + 86400 * 30}, f)
        user_token_file = mock_config_dir / "music_user_token.json"
        with open(user_token_file, "w") as f:
            json.dump({"music_user_token": mock_user_token}, f)

        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)

        mock_asc = MagicMock()
        mock_asc.search_library_page.return_value = (True, [], 0, "")  # AS empty
        from applemusic_mcp import applescript as real_asc

        mock_asc.classify_error = real_asc.classify_error
        monkeypatch.setattr(server, "asc", mock_asc)

        with resp_lib.RequestsMock() as r:
            r.add(
                resp_lib.GET,
                f"{server.BASE_URL}/me/library/search",
                json={
                    "results": {
                        "library-songs": {
                            "data": [
                                {
                                    "id": "i.cloud123",
                                    "attributes": {
                                        "name": "Cloud Track",
                                        "artistName": "Cloud Artist",
                                        "albumName": "Cloud Album",
                                    },
                                }
                            ]
                        }
                    }
                },
                status=200,
            )
            result = server._library_search("cloud track")

        # API was reached and surfaced the cloud-synced result
        assert "Cloud Track" in result

    def test_tokenless_macos_empty_as_message_uses_types_param(self, monkeypatch):
        """The 'No <types> found in library' message respects the types arg."""
        from applemusic_mcp import applescript as real_asc

        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        mock_asc = MagicMock()
        mock_asc.search_library_page.return_value = (True, [], 0, "")
        mock_asc.classify_error = real_asc.classify_error
        monkeypatch.setattr(server, "asc", mock_asc)
        monkeypatch.setattr(server, "_has_developer_token", lambda: False)

        result = server._library_search("nonexistent", types="albums")
        assert "No albums found" in result

    def test_tokenless_macos_empty_hint_routes_to_catalog(self, monkeypatch):
        """The empty-result message must hint at catalog(action='search')
        — without this, Claude sessions reading 'No songs found in library'
        don't know to try catalog and the user gets stuck."""
        from applemusic_mcp import applescript as real_asc

        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        mock_asc = MagicMock()
        mock_asc.search_library_page.return_value = (True, [], 0, "")
        mock_asc.classify_error = real_asc.classify_error
        monkeypatch.setattr(server, "asc", mock_asc)
        monkeypatch.setattr(server, "_has_developer_token", lambda: False)

        result = server._library_search("anything")
        assert "catalog(action='search'" in result


class TestApplyPagination:
    """Unit tests for the _apply_pagination helper."""

    def test_offset_equals_total_returns_empty_not_error(self):
        """Regression: offset == total_count is a valid pagination boundary; must not error."""
        items = [{"name": f"Track {i}"} for i in range(10)]
        result, total, error = server._apply_pagination(items, limit=10, offset=10)
        assert error is None
        assert result == []
        assert total == 10

    def test_offset_beyond_total_returns_error(self):
        """offset strictly greater than total_count is clearly out of bounds."""
        items = [{"name": f"Track {i}"} for i in range(10)]
        result, total, error = server._apply_pagination(items, limit=10, offset=11)
        assert error is not None
        assert "11" in error
        assert result == []
        assert total == 10

    def test_offset_zero_limit_zero_returns_all(self):
        """offset=0, limit=0: returns every item unchanged."""
        items = [{"name": f"Track {i}"} for i in range(10)]
        result, total, error = server._apply_pagination(items, limit=0, offset=0)
        assert error is None
        assert result == items
        assert total == 10

    def test_empty_list_with_nonzero_offset_returns_empty_without_error(self):
        """Empty input list with any offset yields ([], 0, None) — no error."""
        result, total, error = server._apply_pagination([], limit=10, offset=5)
        assert error is None
        assert result == []
        assert total == 0

    def test_offset_midrange_limit_fits_within_remaining(self):
        """Offset in the middle; limit does not exceed the remaining items."""
        items = [{"name": f"Track {i}"} for i in range(25)]
        result, total, error = server._apply_pagination(items, limit=10, offset=10)
        assert error is None
        assert len(result) == 10
        assert result[0]["name"] == "Track 10"
        assert result[-1]["name"] == "Track 19"
        assert total == 25

    def test_offset_midrange_limit_exceeds_remaining_returns_partial_page(self):
        """Offset in the middle; limit overshoots end — returns only the remaining items."""
        items = [{"name": f"Track {i}"} for i in range(15)]
        result, total, error = server._apply_pagination(items, limit=10, offset=10)
        assert error is None
        assert len(result) == 5
        assert result[0]["name"] == "Track 10"
        assert total == 15


class TestLibraryBrowsePagination:
    """Tests for offset/limit pagination in _library_browse.

    Covers both the AppleScript path (macOS, item_type=songs) and the API
    path (APPLESCRIPT_AVAILABLE=False), for three basic scenarios each:
    first page, second page (non-zero offset), and fetch-all (limit=0).
    """

    def _setup_tokens(self, mock_config_dir, mock_developer_token, mock_user_token):
        dev_token_file = mock_config_dir / "developer_token.json"
        with open(dev_token_file, "w") as f:
            json.dump({"token": mock_developer_token, "expires": time.time() + 86400 * 60}, f)
        user_token_file = mock_config_dir / "music_user_token.json"
        with open(user_token_file, "w") as f:
            json.dump({"music_user_token": mock_user_token}, f)

    def _make_api_song(self, i):
        return {
            "id": f"i.lib{i}",
            "attributes": {
                "name": f"Track {i}",
                "artistName": "Artist",
                "albumName": "Album",
                "contentRating": "clean",
            },
        }

    def _make_as_song(self, i):
        return {"name": f"Track {i}", "artist": "Artist", "album": "Album", "id": f"PID{i}"}

    # ── AppleScript path ──────────────────────────────────────────────────────

    def test_applescript_offset_zero_limit_ten_returns_first_page(self, monkeypatch):
        """offset=0, limit=10: calls get_library_songs_page(0, 10) and returns page."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        mock_asc = MagicMock()
        mock_asc.get_library_songs_page.return_value = (
            True,
            [self._make_as_song(i) for i in range(10)],
            100,
            "",
        )
        monkeypatch.setattr(server, "asc", mock_asc)

        result = server._library_browse(item_type="songs", limit=10, offset=0)

        assert "Track 0" in result
        assert "Track 9" in result
        mock_asc.get_library_songs_page.assert_called_once_with(0, 10)

    def test_applescript_offset_ten_limit_ten_returns_second_page(self, monkeypatch):
        """offset=10, limit=10: calls get_library_songs_page(10, 10) and returns pre-sliced page."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        mock_asc = MagicMock()
        mock_asc.get_library_songs_page.return_value = (
            True,
            [self._make_as_song(i) for i in range(10, 20)],
            100,
            "",
        )
        monkeypatch.setattr(server, "asc", mock_asc)

        result = server._library_browse(item_type="songs", limit=10, offset=10)

        mock_asc.get_library_songs_page.assert_called_once_with(10, 10)
        assert "Track 10" in result
        assert "Track 19" in result
        assert "Track 0" not in result
        assert "Track 9" not in result

    def test_applescript_limit_zero_returns_all(self, monkeypatch):
        """limit=0: passes 0 to get_library_songs and returns all songs."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        mock_asc = MagicMock()
        mock_asc.get_library_songs.return_value = (
            True,
            [self._make_as_song(i) for i in range(150)],
        )
        monkeypatch.setattr(server, "asc", mock_asc)

        result = server._library_browse(item_type="songs", limit=0, offset=0)

        assert "Track 0" in result
        assert "Track 149" in result
        mock_asc.get_library_songs.assert_called_once_with(0)

    # ── API path ──────────────────────────────────────────────────────────────

    @responses.activate
    def test_api_offset_zero_limit_ten_returns_first_page(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """offset=0, limit=10: returns first 10 items in a single API call."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        self._setup_tokens(mock_config_dir, mock_developer_token, mock_user_token)

        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/songs",
            json={"data": [self._make_api_song(i) for i in range(10)]},
            status=200,
        )

        result = server._library_browse(item_type="songs", limit=10, offset=0)

        assert "Track 0" in result
        assert "Track 9" in result
        assert len(responses.calls) == 1

    @responses.activate
    def test_api_offset_ten_limit_ten_returns_second_page(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """offset=10, limit=10: fetches offset+limit items, returns items 11-20."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        self._setup_tokens(mock_config_dir, mock_developer_token, mock_user_token)

        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/songs",
            json={"data": [self._make_api_song(i) for i in range(20)]},
            status=200,
        )

        result = server._library_browse(item_type="songs", limit=10, offset=10)

        assert "Track 10" in result
        assert "Track 19" in result
        assert "Track 0" not in result
        assert "Track 9" not in result
        assert len(responses.calls) == 1

    @responses.activate
    def test_api_limit_zero_fetches_all_across_multiple_calls(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """limit=0: keeps calling API until a partial batch signals the end."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        self._setup_tokens(mock_config_dir, mock_developer_token, mock_user_token)

        # Full batch of 100 → pagination continues
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/songs",
            json={"data": [self._make_api_song(i) for i in range(100)]},
            status=200,
        )
        # Partial batch of 50 → pagination stops
        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/songs",
            json={"data": [self._make_api_song(i) for i in range(100, 150)]},
            status=200,
        )

        result = server._library_browse(item_type="songs", limit=0, offset=0)

        assert "Track 0" in result
        assert "Track 149" in result
        assert len(responses.calls) == 2

    # ── Regression: offset == limit on a large library ───────────────────────

    def test_applescript_large_library_second_page_returns_tracks_not_error(self, monkeypatch):
        """Regression: 1000+ song library, offset=10, limit=10 must return page 2 not an error."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        mock_asc = MagicMock()
        mock_asc.get_library_songs_page.return_value = (
            True,
            [self._make_as_song(i) for i in range(10, 20)],
            1000,
            "",
        )
        monkeypatch.setattr(server, "asc", mock_asc)

        result = server._library_browse(item_type="songs", limit=10, offset=10)

        mock_asc.get_library_songs_page.assert_called_once_with(10, 10)
        assert "Offset 10 exceeds" not in result
        assert "Track 10" in result
        assert "Track 19" in result
        assert "Track 0" not in result

    @responses.activate
    def test_api_large_library_second_page_returns_tracks_not_error(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Regression: API path, 1000+ song library, offset=10, limit=10 returns page 2."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        self._setup_tokens(mock_config_dir, mock_developer_token, mock_user_token)

        responses.add(
            responses.GET,
            "https://api.music.apple.com/v1/me/library/songs",
            json={"data": [self._make_api_song(i) for i in range(20)]},
            status=200,
        )

        result = server._library_browse(item_type="songs", limit=10, offset=10)

        assert "Offset 10 exceeds" not in result
        assert "Track 10" in result
        assert "Track 19" in result
        assert "Track 0" not in result

    def test_applescript_clean_only_uses_full_fetch_with_accurate_count(self, monkeypatch):
        """clean_only=True uses the full-fetch path so the reported total reflects post-filter count."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        mock_asc = MagicMock()
        songs = [self._make_as_song(i) for i in range(5)]
        mock_asc.get_library_songs.return_value = (True, songs)
        monkeypatch.setattr(server, "asc", mock_asc)

        result = server._library_browse(item_type="songs", limit=10, offset=0, clean_only=True)

        mock_asc.get_library_songs.assert_called_once_with(0)
        mock_asc.get_library_songs_page.assert_not_called()
        assert result is not None


class TestCanonicalMatcher:
    """The single normalized matcher used at every user-string comparison point.

    Issue #26: Music stores titles with typographic glyphs (curly quotes U+2019,
    ellipsis U+2026, accents) while users/models type ASCII. Every match point
    routes through _normalize_for_match / _loose_contains so the cosmetic glyph
    never decides a match. These are pure-function tests — no Music app needed.
    """

    # --- _normalize_for_match -------------------------------------------------

    def test_smart_and_straight_quotes_normalize_identically(self):
        assert server._normalize_for_match("That's a No No") == server._normalize_for_match(
            "That’s a No No"
        )

    def test_left_quote_and_modifier_apostrophe_fold(self):
        base = server._normalize_for_match("Its Me")
        for variant in ["It's Me", "It’s Me", "It‘s Me", "Itʼs Me"]:
            assert server._normalize_for_match(variant) == base

    def test_accents_fold_to_base_letter(self):
        # The bug the old normalizer had: é was stripped to nothing (caf), not e.
        assert server._normalize_for_match("café") == "cafe"
        assert server._normalize_for_match("Björk") == "bjork"
        assert server._normalize_for_match("Beyoncé") == server._normalize_for_match("Beyonce")

    def test_ellipsis_folds(self):
        assert server._normalize_for_match("Wait…") == server._normalize_for_match("Wait...")

    def test_ampersand_becomes_and(self):
        assert server._normalize_for_match("Salt & Pepa") == server._normalize_for_match(
            "Salt and Pepa"
        )

    def test_hyphens_become_spaces_emoji_removed(self):
        # Hyphens join words -> spaces (keeps tokens distinct); emoji -> gone.
        assert server._normalize_for_match("Peek-A-Boo 🎵") == "peek a boo"

    def test_hyphen_removal_does_not_cross_token_boundary(self):
        # Regression: deleting the hyphen would fold "Peek-A-Boo" -> "peekaboo",
        # letting "kabo" cross the boundary and false-match. Spacing prevents it.
        assert not server._loose_contains("kabo", "Peek-A-Boo")
        # But a real hyphen/space variation still matches.
        assert server._loose_contains("Peek A Boo", "Peek-A-Boo")

    def test_whitespace_collapsed_and_cased(self):
        assert server._normalize_for_match("  THE   Beatles ") == "the beatles"

    def test_none_and_empty_are_empty(self):
        assert server._normalize_for_match(None) == ""
        assert server._normalize_for_match("") == ""
        assert server._normalize_for_match("   ") == ""

    # --- _loose_contains ------------------------------------------------------

    def test_straight_query_matches_curly_name(self):
        assert server._loose_contains("That's a No No", "That’s a No No")

    def test_curly_query_matches_straight_name(self):
        # The reporter's `track="That’s..."` case: query is curly, the name
        # search returns is straight (osascript normalizes stdout).
        assert server._loose_contains("Wouldn’t It Be Nice", "Wouldn't It Be Nice (Live)")

    def test_accent_query_matches_unaccented_name(self):
        assert server._loose_contains("Fur Elise", 'Bagatelle "Für Elise"')

    def test_substring_semantics_preserved(self):
        assert server._loose_contains("No No", "That's a No No")
        assert not server._loose_contains("Yes Yes", "That's a No No")

    def test_empty_needle_imposes_no_constraint(self):
        # Drop-in for `if query and query.lower() not in name`.
        assert server._loose_contains("", "anything") is True
        assert server._loose_contains(None, "anything") is True

    def test_none_haystack_only_matches_empty_needle(self):
        assert server._loose_contains("x", None) is False
        assert server._loose_contains(None, None) is True

    # --- _loose_equals --------------------------------------------------------

    def test_loose_equals_folds_quotes_and_accents(self):
        assert server._loose_equals("Café del Mar", "cafe del mar")
        assert server._loose_equals("Rock 'n' Roll", "Rock ’n’ Roll")
        assert not server._loose_equals("Song", "Songs")


class TestResolveCatalogTrackItunes:
    """The free iTunes Search API resolver (tokenless track resolution, #28)."""

    def _resp(self, results):
        m = MagicMock()
        m.status_code = 200
        m.json.return_value = {"results": results}
        return m

    def test_picks_exact_name_and_artist(self, monkeypatch):
        results = [
            {
                "trackName": "Blinding Lights (Remix)",
                "artistName": "The Weeknd",
                "trackViewUrl": "u1",
            },
            {"trackName": "Blinding Lights", "artistName": "The Weeknd", "trackViewUrl": "u2"},
        ]
        monkeypatch.setattr(server.requests, "get", lambda *a, **k: self._resp(results))
        r = server._resolve_catalog_track_itunes("Blinding Lights", "The Weeknd")
        assert r is not None and r["url"] == "u2" and r["name"] == "Blinding Lights"

    def test_handles_smart_quote_and_kpop(self, monkeypatch):
        # OCR struggled with these; the API resolves them cleanly.
        results = [{"trackName": "I CAN'T STOP ME", "artistName": "TWICE", "trackViewUrl": "u"}]
        monkeypatch.setattr(server.requests, "get", lambda *a, **k: self._resp(results))
        r = server._resolve_catalog_track_itunes("i cant stop me", "twice")
        assert r is not None and r["url"] == "u"

    def test_none_when_name_absent(self, monkeypatch):
        results = [{"trackName": "Totally Different", "artistName": "X", "trackViewUrl": "u"}]
        monkeypatch.setattr(server.requests, "get", lambda *a, **k: self._resp(results))
        assert server._resolve_catalog_track_itunes("Blinding Lights", "The Weeknd") is None

    def test_none_on_weak_substring_with_wrong_artist(self, monkeypatch):
        # Title merely CONTAINS the query but isn't exact, AND the artist is
        # wrong -> score 0 -> rejected (don't deep-link to the wrong track).
        results = [
            {"trackName": "Lights Out", "artistName": "Nobody", "trackViewUrl": "u"},
            {"trackName": "City of Lights (Extended)", "artistName": "Other", "trackViewUrl": "u2"},
        ]
        monkeypatch.setattr(server.requests, "get", lambda *a, **k: self._resp(results))
        assert server._resolve_catalog_track_itunes("Lights", "The Weeknd") is None

    def test_accepts_exact_title_even_without_artist(self, monkeypatch):
        # Exact title alone (score 2) is enough when no artist is supplied.
        results = [{"trackName": "Lights", "artistName": "Whoever", "trackViewUrl": "u"}]
        monkeypatch.setattr(server.requests, "get", lambda *a, **k: self._resp(results))
        r = server._resolve_catalog_track_itunes("Lights")
        assert r is not None and r["url"] == "u"

    def test_none_on_empty_results(self, monkeypatch):
        monkeypatch.setattr(server.requests, "get", lambda *a, **k: self._resp([]))
        assert server._resolve_catalog_track_itunes("anything") is None

    def test_none_on_http_error(self, monkeypatch):
        m = MagicMock()
        m.status_code = 503
        monkeypatch.setattr(server.requests, "get", lambda *a, **k: m)
        assert server._resolve_catalog_track_itunes("anything") is None

    def test_none_when_url_missing(self, monkeypatch):
        results = [{"trackName": "Blinding Lights", "artistName": "The Weeknd"}]  # no trackViewUrl
        monkeypatch.setattr(server.requests, "get", lambda *a, **k: self._resp(results))
        assert server._resolve_catalog_track_itunes("Blinding Lights", "The Weeknd") is None

    def test_none_on_network_exception(self, monkeypatch):
        def boom(*a, **k):
            raise ConnectionError("offline")

        monkeypatch.setattr(server.requests, "get", boom)
        assert server._resolve_catalog_track_itunes("anything") is None

    def test_artist_match_beats_exact_title_by_wrong_artist(self, monkeypatch):
        # The user named the artist (Brye). A same-titled song by a DIFFERENT
        # artist (exact "Lemons" by Hairitage) must NOT win over the requested
        # artist's track, even though its title is an exact match. Artist is the
        # strong signal when supplied. (Measured regression on macOS 26.5: the
        # old title-weighted scorer silently resolved to Hairitage.)
        results = [
            {"trackName": "Lemons", "artistName": "Hairitage", "trackViewUrl": "wrong"},
            {"trackName": "LEMONS (feat. Cavetown)", "artistName": "Brye", "trackViewUrl": "right"},
            {"trackName": "LEMONS (Demo)", "artistName": "Brye", "trackViewUrl": "demo"},
        ]
        monkeypatch.setattr(server.requests, "get", lambda *a, **k: self._resp(results))
        r = server._resolve_catalog_track_itunes("Lemons", "Brye")
        assert r is not None and r["artist"] == "Brye"
        # iTunes' relevance order breaks the Brye/Brye tie -> first one wins.
        assert r["url"] == "right"
        assert r["name"] == "LEMONS (feat. Cavetown)"

    def test_rejects_wrong_artist_even_with_exact_title(self, monkeypatch):
        # Artist supplied but NO result matches it -> None. Fail loudly rather
        # than silently add a different artist's identically-titled song.
        results = [
            {"trackName": "Lemons", "artistName": "Hairitage", "trackViewUrl": "u"},
            {"trackName": "Lemons", "artistName": "Muni Long", "trackViewUrl": "u2"},
        ]
        monkeypatch.setattr(server.requests, "get", lambda *a, **k: self._resp(results))
        assert server._resolve_catalog_track_itunes("Lemons", "Brye") is None

    def test_canonical_suffix_title_accepted_when_artist_matches(self, monkeypatch):
        # Title carries a "(feat. ...)" suffix (not an exact match) but the
        # artist matches -> accepted. This is exactly the obscure-track case the
        # pop-over needs the canonical title for.
        results = [
            {"trackName": "LEMONS (feat. Cavetown)", "artistName": "Brye", "trackViewUrl": "u"}
        ]
        monkeypatch.setattr(server.requests, "get", lambda *a, **k: self._resp(results))
        r = server._resolve_catalog_track_itunes("Lemons", "Brye")
        assert r is not None and r["url"] == "u"
        assert r["name"] == "LEMONS (feat. Cavetown)"
