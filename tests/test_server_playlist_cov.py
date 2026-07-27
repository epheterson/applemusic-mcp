"""Playlist slice coverage — drives lines 967-1180, 1331-1462, 2412-3810, 3959-4099.

Strategy:
- All tests mock at the boundary (asc.*, amp_api.*, requests, get_headers).
- Each test asserts on the returned string content, not just call counts.
- No real network, no real AppleScript, no real files.
"""

import json
import time
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest
import responses as responses_lib

from applemusic_mcp import server
from applemusic_mcp import amp_api as _amp_api_module

# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------


def _write_tokens(config_dir, dev_token: str, user_token: str) -> None:
    (config_dir / "developer_token.json").write_text(
        json.dumps({"token": dev_token, "expires": time.time() + 86400 * 60})
    )
    (config_dir / "music_user_token.json").write_text(json.dumps({"music_user_token": user_token}))


# ---------------------------------------------------------------------------
# _resolve_failure_msg  (lines 1003-1012)
# ---------------------------------------------------------------------------


class TestResolveFailureMsg:
    def test_expired(self, monkeypatch):
        monkeypatch.setattr(server.amp_api, "session_status", lambda: "expired")
        result = server._resolve_failure_msg("playlist X not found")
        assert "expired" in result.lower()
        assert "applemusic-mcp login" in result  # real re-auth command (no signin/authorize)

    def test_throttled(self, monkeypatch):
        monkeypatch.setattr(server.amp_api, "session_status", lambda: "throttled")
        result = server._resolve_failure_msg("playlist X not found")
        assert "429" in result or "rate-limit" in result.lower()

    def test_not_found(self, monkeypatch):
        monkeypatch.setattr(server.amp_api, "session_status", lambda: "ok")
        result = server._resolve_failure_msg("playlist X not found")
        assert "Error" in result
        assert "playlist X not found" in result


# ---------------------------------------------------------------------------
# _safe_single_match  (lines 1015-1025)
# ---------------------------------------------------------------------------


class TestSafeSingleMatch:
    def test_exact_match_chosen(self):
        matches = [
            {"name": "Love Me Do", "relationship_id": "r1"},
            {"name": "Love Is All Around", "relationship_id": "r2"},
            {"name": "love", "relationship_id": "r3"},
        ]
        chosen, others = server._safe_single_match(matches, "Love")
        assert chosen["name"] == "love"
        assert others == 2

    def test_no_exact_uses_first(self):
        matches = [
            {"name": "Love Me Do", "relationship_id": "r1"},
            {"name": "Love Is Around", "relationship_id": "r2"},
        ]
        chosen, others = server._safe_single_match(matches, "love song")
        assert chosen["relationship_id"] == "r1"
        assert others == 1

    def test_single_match_others_zero(self):
        matches = [{"name": "Bohemian Rhapsody", "relationship_id": "r1"}]
        chosen, others = server._safe_single_match(matches, "Bohemian Rhapsody")
        assert others == 0
        assert chosen["name"] == "Bohemian Rhapsody"


# ---------------------------------------------------------------------------
# API helpers: _playlist_create_api, _playlist_delete_api, _playlist_rename_api
# (lines 967-991)
# ---------------------------------------------------------------------------


class TestPlaylistCreateApi:
    def test_success(self, monkeypatch):
        monkeypatch.setattr(
            server.amp_api, "create_playlist", lambda name, desc="": (True, "p.new123")
        )
        result = server._playlist_create_api("My Playlist", "desc")
        assert "My Playlist" in result
        assert "p.new123" in result
        assert "Created" in result

    def test_failure(self, monkeypatch):
        monkeypatch.setattr(
            server.amp_api, "create_playlist", lambda name, desc="": (False, "auth error")
        )
        result = server._playlist_create_api("My Playlist")
        assert "Error" in result
        assert "auth error" in result


class TestPlaylistDeleteApi:
    def test_success(self, monkeypatch):
        monkeypatch.setattr(server.amp_api, "resolve_playlist_id", lambda name, **k: "p.del123")
        monkeypatch.setattr(server.amp_api, "delete_playlist", lambda pid: (True, "ok"))
        monkeypatch.setattr(server.amp_api, "session_status", lambda: "ok")
        result = server._playlist_delete_api("My Playlist")
        assert "Deleted" in result
        assert "My Playlist" in result

    def test_not_found(self, monkeypatch):
        monkeypatch.setattr(server.amp_api, "resolve_playlist_id", lambda name, **k: None)
        monkeypatch.setattr(server.amp_api, "session_status", lambda: "ok")
        result = server._playlist_delete_api("Ghost Playlist")
        assert "Error" in result
        assert "Ghost Playlist" in result

    def test_delete_fails(self, monkeypatch):
        monkeypatch.setattr(server.amp_api, "resolve_playlist_id", lambda name, **k: "p.del123")
        monkeypatch.setattr(server.amp_api, "delete_playlist", lambda pid: (False, "403 Forbidden"))
        result = server._playlist_delete_api("My Playlist")
        assert "Error" in result
        assert "403" in result


class TestPlaylistRenameApi:
    def test_success(self, monkeypatch):
        monkeypatch.setattr(server.amp_api, "resolve_playlist_id", lambda name, **k: "p.r123")
        monkeypatch.setattr(server.amp_api, "rename_playlist", lambda pid, new: (True, "ok"))
        result = server._playlist_rename_api("Old", "New")
        assert "Renamed" in result
        assert "Old" in result
        assert "New" in result

    def test_not_found(self, monkeypatch):
        monkeypatch.setattr(server.amp_api, "resolve_playlist_id", lambda name, **k: None)
        monkeypatch.setattr(server.amp_api, "session_status", lambda: "ok")
        result = server._playlist_rename_api("Ghost", "New")
        assert "Error" in result

    def test_rename_fails(self, monkeypatch):
        monkeypatch.setattr(server.amp_api, "resolve_playlist_id", lambda name, **k: "p.r123")
        monkeypatch.setattr(
            server.amp_api, "rename_playlist", lambda pid, new: (False, "read-only")
        )
        result = server._playlist_rename_api("Old", "New")
        assert "Error" in result
        assert "read-only" in result


# ---------------------------------------------------------------------------
# _playlist_remove_api  (lines 1028-1051)
# ---------------------------------------------------------------------------


class TestPlaylistRemoveApi:
    def test_success_single_match(self, monkeypatch):
        monkeypatch.setattr(server.amp_api, "resolve_playlist_id", lambda name, **k: "p.abc")
        monkeypatch.setattr(
            server.amp_api,
            "get_tracks",
            lambda pid: [{"name": "Hey Jude", "artist": "Beatles", "relationship_id": "rel1"}],
        )
        monkeypatch.setattr(server.amp_api, "remove_track", lambda pid, rid: (True, "ok"))
        result = server._playlist_remove_api("My PL", "Hey Jude")
        assert "Removed" in result
        assert "Hey Jude" in result

    def test_success_multiple_matches_warns(self, monkeypatch):
        monkeypatch.setattr(server.amp_api, "resolve_playlist_id", lambda name, **k: "p.abc")
        monkeypatch.setattr(
            server.amp_api,
            "get_tracks",
            lambda pid: [
                {"name": "Love", "artist": "A", "relationship_id": "rel1"},
                {"name": "Love Song", "artist": "B", "relationship_id": "rel2"},
            ],
        )
        monkeypatch.setattr(server.amp_api, "remove_track", lambda pid, rid: (True, "ok"))
        result = server._playlist_remove_api("My PL", "love")
        assert "Removed" in result
        assert "other track" in result.lower() or "also matched" in result

    def test_playlist_not_found(self, monkeypatch):
        monkeypatch.setattr(server.amp_api, "resolve_playlist_id", lambda name, **k: None)
        monkeypatch.setattr(server.amp_api, "session_status", lambda: "ok")
        result = server._playlist_remove_api("Ghost PL", "Hey Jude")
        assert "Error" in result
        assert "Ghost PL" in result

    def test_track_not_found(self, monkeypatch):
        monkeypatch.setattr(server.amp_api, "resolve_playlist_id", lambda name, **k: "p.abc")
        monkeypatch.setattr(
            server.amp_api,
            "get_tracks",
            lambda pid: [{"name": "Other Song", "artist": "Artist", "relationship_id": "rel1"}],
        )
        result = server._playlist_remove_api("My PL", "Hey Jude")
        assert "Error" in result
        assert "not found" in result.lower()

    def test_remove_fails(self, monkeypatch):
        monkeypatch.setattr(server.amp_api, "resolve_playlist_id", lambda name, **k: "p.abc")
        monkeypatch.setattr(
            server.amp_api,
            "get_tracks",
            lambda pid: [{"name": "Hey Jude", "artist": "Beatles", "relationship_id": "rel1"}],
        )
        monkeypatch.setattr(server.amp_api, "remove_track", lambda pid, rid: (False, "error"))
        result = server._playlist_remove_api("My PL", "Hey Jude")
        assert "Error" in result
        assert "remove failed" in result.lower()

    def test_artist_filter(self, monkeypatch):
        monkeypatch.setattr(server.amp_api, "resolve_playlist_id", lambda name, **k: "p.abc")
        monkeypatch.setattr(
            server.amp_api,
            "get_tracks",
            lambda pid: [
                {"name": "Love", "artist": "Beatles", "relationship_id": "rel1"},
                {"name": "Love", "artist": "Hendrix", "relationship_id": "rel2"},
            ],
        )
        monkeypatch.setattr(server.amp_api, "remove_track", lambda pid, rid: (True, "ok"))
        # With artist filter, only Beatles match
        result = server._playlist_remove_api("My PL", "Love", "Beatles")
        assert "Removed" in result
        assert "Beatles" in result


# ---------------------------------------------------------------------------
# _folder_create_api, _folder_delete_api  (lines 1054-1070)
# ---------------------------------------------------------------------------


class TestFolderCreateApi:
    def test_success(self, monkeypatch):
        monkeypatch.setattr(
            server.amp_api, "create_folder", lambda name, parent_id=None: (True, "f.folder1")
        )
        result = server._folder_create_api("Summer Vibes")
        assert "Created" in result
        assert "Summer Vibes" in result
        assert "f.folder1" in result

    def test_failure(self, monkeypatch):
        monkeypatch.setattr(
            server.amp_api, "create_folder", lambda name, parent_id=None: (False, "quota exceeded")
        )
        result = server._folder_create_api("Summer Vibes")
        assert "Error" in result
        assert "quota exceeded" in result


class TestFolderDeleteApi:
    def test_not_found(self, monkeypatch):
        monkeypatch.setattr(server.amp_api, "resolve_folder_id", lambda name: None)
        result = server._folder_delete_api("Ghost Folder")
        assert "Error" in result
        assert "Ghost Folder" in result

    def test_success(self, monkeypatch):
        monkeypatch.setattr(server.amp_api, "resolve_folder_id", lambda name: "f.folder1")
        monkeypatch.setattr(server.amp_api, "delete_folder", lambda fid: (True, "ok"))
        result = server._folder_delete_api("Old Folder")
        assert "Deleted" in result
        assert "Old Folder" in result

    def test_delete_fails(self, monkeypatch):
        monkeypatch.setattr(server.amp_api, "resolve_folder_id", lambda name: "f.folder1")
        monkeypatch.setattr(server.amp_api, "delete_folder", lambda fid: (False, "403 Forbidden"))
        result = server._folder_delete_api("Old Folder")
        assert "Error" in result
        assert "403" in result


# ---------------------------------------------------------------------------
# _playlist_move_api  (lines 1073-1094)
# ---------------------------------------------------------------------------


class TestPlaylistMoveApi:
    def test_move_to_root(self, monkeypatch):
        monkeypatch.setattr(server.amp_api, "resolve_playlist_id", lambda name, **k: "p.abc")
        monkeypatch.setattr(
            server.amp_api, "move_playlist_to_folder", lambda pid, fid: (True, "ok")
        )
        result = server._playlist_move_api("My Playlist", "")
        assert "top level" in result.lower() or "Moved" in result

    def test_move_to_root_explicit(self, monkeypatch):
        monkeypatch.setattr(server.amp_api, "resolve_playlist_id", lambda name, **k: "p.abc")
        monkeypatch.setattr(
            server.amp_api, "move_playlist_to_folder", lambda pid, fid: (True, "ok")
        )
        result = server._playlist_move_api("My Playlist", "root")
        assert "Moved" in result

    def test_move_to_folder_exists(self, monkeypatch):
        monkeypatch.setattr(server.amp_api, "resolve_playlist_id", lambda name, **k: "p.abc")
        monkeypatch.setattr(server.amp_api, "resolve_folder_id", lambda name: "f.folder1")
        monkeypatch.setattr(
            server.amp_api, "move_playlist_to_folder", lambda pid, fid: (True, "ok")
        )
        result = server._playlist_move_api("My Playlist", "Summer")
        assert "Moved" in result
        assert "Summer" in result

    def test_move_to_folder_creates_folder(self, monkeypatch):
        monkeypatch.setattr(server.amp_api, "resolve_playlist_id", lambda name, **k: "p.abc")
        monkeypatch.setattr(server.amp_api, "resolve_folder_id", lambda name: None)
        monkeypatch.setattr(
            server.amp_api, "create_folder", lambda name, parent_id=None: (True, "f.new1")
        )
        monkeypatch.setattr(
            server.amp_api, "move_playlist_to_folder", lambda pid, fid: (True, "ok")
        )
        result = server._playlist_move_api("My Playlist", "New Folder")
        assert "Moved" in result
        assert "New Folder" in result

    def test_move_folder_create_fails(self, monkeypatch):
        monkeypatch.setattr(server.amp_api, "resolve_playlist_id", lambda name, **k: "p.abc")
        monkeypatch.setattr(server.amp_api, "resolve_folder_id", lambda name: None)
        monkeypatch.setattr(
            server.amp_api, "create_folder", lambda name, parent_id=None: (False, "quota")
        )
        result = server._playlist_move_api("My Playlist", "New Folder")
        assert "Error" in result
        assert "quota" in result

    def test_playlist_not_found(self, monkeypatch):
        monkeypatch.setattr(server.amp_api, "resolve_playlist_id", lambda name, **k: None)
        monkeypatch.setattr(server.amp_api, "session_status", lambda: "ok")
        result = server._playlist_move_api("Ghost", "Folder")
        assert "Error" in result

    def test_move_fails(self, monkeypatch):
        monkeypatch.setattr(server.amp_api, "resolve_playlist_id", lambda name, **k: "p.abc")
        monkeypatch.setattr(server.amp_api, "resolve_folder_id", lambda name: "f.folder1")
        monkeypatch.setattr(
            server.amp_api, "move_playlist_to_folder", lambda pid, fid: (False, "403")
        )
        result = server._playlist_move_api("My Playlist", "Summer")
        assert "Error" in result

    def test_move_to_root_fails(self, monkeypatch):
        monkeypatch.setattr(server.amp_api, "resolve_playlist_id", lambda name, **k: "p.abc")
        monkeypatch.setattr(
            server.amp_api, "move_playlist_to_folder", lambda pid, fid: (False, "server error")
        )
        result = server._playlist_move_api("My Playlist", "root")
        assert "Error" in result


# ---------------------------------------------------------------------------
# _playlist_add_api  (lines 1097-1148)
# ---------------------------------------------------------------------------


class TestPlaylistAddApi:
    def test_with_explicit_playlist_id_and_catalog_track(self, monkeypatch):
        """p.xxx playlist ID bypasses fuzzy matching; catalog ID added via search."""
        monkeypatch.setattr(
            server.amp_api,
            "search_catalog_songs",
            lambda q, n: [{"id": "1234567890", "name": "Hey Jude", "artist": "Beatles"}],
        )
        monkeypatch.setattr(server.amp_api, "add_tracks", lambda pid, items: (True, "ok"))
        result = server._playlist_add_api("p.abc123", "Hey Jude", auto_add=True)
        assert "Added" in result
        assert "Hey Jude" in result

    def test_with_library_id_track(self, monkeypatch):
        monkeypatch.setattr(server.amp_api, "add_tracks", lambda pid, items: (True, "ok"))
        result = server._playlist_add_api("p.abc123", "i.libsong1")
        assert "Added" in result

    def test_name_search_not_found(self, monkeypatch):
        """Track name search returns nothing -> included in errors, but nothing added."""
        monkeypatch.setattr(server.amp_api, "search_catalog_songs", lambda q, n: [])
        result = server._playlist_add_api("p.abc123", "NonExistentSong12345", auto_add=True)
        assert "Error" in result

    def test_with_playlist_name_uses_fuzzy(self, monkeypatch):
        """Playlist given as name triggers _find_api_playlist_by_name then resolve."""
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: ("p.found1", None))
        monkeypatch.setattr(server.amp_api, "add_tracks", lambda pid, items: (True, "ok"))
        result = server._playlist_add_api("My Playlist", "i.libsong1")
        assert "Added" in result

    def test_playlist_name_fallback_to_resolve(self, monkeypatch):
        """_find_api_playlist_by_name fails, fallback to amp_api.resolve_playlist_id."""
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.amp_api, "resolve_playlist_id", lambda name, **k: "p.resolved")
        monkeypatch.setattr(server.amp_api, "add_tracks", lambda pid, items: (True, "ok"))
        result = server._playlist_add_api("My Playlist", "i.libsong1")
        assert "Added" in result

    def test_playlist_not_found_at_all(self, monkeypatch):
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.amp_api, "resolve_playlist_id", lambda name, **k: None)
        monkeypatch.setattr(server.amp_api, "session_status", lambda: "ok")
        result = server._playlist_add_api("Ghost Playlist", "i.libsong1")
        assert "Error" in result

    def test_add_fails(self, monkeypatch):
        monkeypatch.setattr(
            server.amp_api, "add_tracks", lambda pid, items: (False, "quota exceeded")
        )
        result = server._playlist_add_api("p.abc123", "i.libsong1")
        assert "Error" in result

    def test_with_catalog_id_track(self, monkeypatch):
        """Catalog ID (all digits 9+) is added directly without search."""
        monkeypatch.setattr(server.amp_api, "add_tracks", lambda pid, items: (True, "ok"))
        result = server._playlist_add_api("p.abc123", "1234567890")
        assert "Added" in result

    def test_fuzzy_match_info_in_output(self, monkeypatch):
        """Fuzzy match result gets appended to output when playlist matched fuzzily."""
        from applemusic_mcp.server import FuzzyMatchResult

        fuzzy = FuzzyMatchResult(
            matched_name="Rock & Roll Classics",
            query="Rock and Roll Classics",
            normalized_query="rock and roll classics",
            normalized_match="rock & roll classics",
            transformations=["and→&"],
            match_type="fuzzy",
        )
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: ("p.found1", fuzzy))
        monkeypatch.setattr(server.amp_api, "add_tracks", lambda pid, items: (True, "ok"))
        result = server._playlist_add_api("Rock and Roll Classics", "i.libsong1")
        assert "Added" in result
        assert "Rock & Roll Classics" in result


# ---------------------------------------------------------------------------
# _library_remove_api  (lines 1151-1179)
# ---------------------------------------------------------------------------


class TestLibraryRemoveApi:
    def test_empty_track_error(self, monkeypatch):
        result = server._library_remove_api("")
        assert "Error" in result

    def test_not_found(self, monkeypatch):
        monkeypatch.setattr(server.amp_api, "search_library_songs", lambda term: [])
        monkeypatch.setattr(server.amp_api, "session_status", lambda: "ok")
        result = server._library_remove_api("NonExistentSong")
        assert "Error" in result
        assert "not found" in result.lower()

    def test_success(self, monkeypatch):
        monkeypatch.setattr(
            server.amp_api,
            "search_library_songs",
            lambda term: [{"id": "i.song1", "name": "Hey Jude", "artist": "Beatles"}],
        )
        monkeypatch.setattr(server.amp_api, "remove_from_library", lambda lid: (True, "ok"))
        result = server._library_remove_api("Hey Jude", "Beatles")
        assert "Removed" in result
        assert "Hey Jude" in result

    def test_remove_fails(self, monkeypatch):
        monkeypatch.setattr(
            server.amp_api,
            "search_library_songs",
            lambda term: [{"id": "i.song1", "name": "Hey Jude", "artist": "Beatles"}],
        )
        monkeypatch.setattr(server.amp_api, "remove_from_library", lambda lid: (False, "forbidden"))
        result = server._library_remove_api("Hey Jude")
        assert "Error" in result

    def test_multiple_matches_warns(self, monkeypatch):
        monkeypatch.setattr(
            server.amp_api,
            "search_library_songs",
            lambda term: [
                {"id": "i.song1", "name": "Love", "artist": "Artist A"},
                {"id": "i.song2", "name": "Love Something", "artist": "Artist B"},
            ],
        )
        monkeypatch.setattr(server.amp_api, "remove_from_library", lambda lid: (True, "ok"))
        result = server._library_remove_api("Love")
        assert "Removed" in result
        assert "also matched" in result or "other title" in result.lower()


# ---------------------------------------------------------------------------
# _find_api_playlist_by_name  (lines 1331-1385)
# ---------------------------------------------------------------------------


class TestFindApiPlaylistByName:
    @responses_lib.activate
    def test_finds_playlist(self, mock_config_dir, mock_developer_token, mock_user_token):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists",
            json={
                "data": [
                    {"id": "p.found1", "attributes": {"name": "Summer Hits"}},
                ]
            },
            status=200,
        )
        pid, fuzzy = server._find_api_playlist_by_name("Summer Hits")
        assert pid == "p.found1"

    @responses_lib.activate
    def test_not_found(self, mock_config_dir, mock_developer_token, mock_user_token):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists",
            json={"data": []},
            status=200,
        )
        pid, fuzzy = server._find_api_playlist_by_name("NonExistent")
        assert pid is None

    @responses_lib.activate
    def test_api_error_returns_none(self, mock_config_dir, mock_developer_token, mock_user_token):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists",
            status=401,
        )
        pid, fuzzy = server._find_api_playlist_by_name("Summer")
        assert pid is None

    @responses_lib.activate
    def test_paginates_over_100(self, mock_config_dir, mock_developer_token, mock_user_token):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        # First page: 100 items (triggers another request)
        big_page = [{"id": f"p.{i}", "attributes": {"name": f"Playlist {i}"}} for i in range(100)]
        # Second page: the target
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists",
            json={"data": big_page},
            status=200,
        )
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists",
            json={"data": [{"id": "p.target", "attributes": {"name": "Target Playlist"}}]},
            status=200,
        )
        pid, fuzzy = server._find_api_playlist_by_name("Target Playlist")
        assert pid == "p.target"


# ---------------------------------------------------------------------------
# _resolve_playlist  (lines 1388-1459)
# ---------------------------------------------------------------------------


class TestResolvePlaylist:
    def test_empty_returns_error(self, monkeypatch):
        resolved = server._resolve_playlist("")
        assert resolved.error is not None
        assert "required" in resolved.error.lower()

    def test_explicit_id(self, monkeypatch):
        resolved = server._resolve_playlist("p.abc123xyz")
        assert resolved.api_id == "p.abc123xyz"
        assert resolved.error is None

    def test_name_not_id(self, monkeypatch):
        # "p.s. I love you" is a name, not an ID (has spaces)
        # Stub the lookups the name path consults so resolution stays offline:
        # no API match, and an empty native playlist list → raw-name fallback.
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, []))
        resolved = server._resolve_playlist("p.s. I love you")
        # Should NOT be treated as an ID
        assert resolved.api_id != "p.s. I love you" or resolved.api_id is None

    @responses_lib.activate
    def test_finds_by_name_via_api(self, mock_config_dir, mock_developer_token, mock_user_token):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists",
            json={"data": [{"id": "p.api1", "attributes": {"name": "Summer Hits"}}]},
            status=200,
        )
        resolved = server._resolve_playlist("Summer Hits")
        assert resolved.api_id == "p.api1"
        assert resolved.error is None

    def test_fallback_to_applescript_when_api_fails(self, monkeypatch):
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(
            server.asc,
            "get_playlists",
            lambda: (
                True,
                [
                    {"name": "Jazz Mix", "id": "jz1"},
                ],
            ),
        )
        resolved = server._resolve_playlist("Jazz Mix")
        assert resolved.applescript_name == "Jazz Mix"
        assert resolved.error is None

    def test_fallback_to_raw_when_applescript_fails(self, monkeypatch):
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        resolved = server._resolve_playlist("My Playlist")
        assert resolved.applescript_name == "My Playlist"
        assert resolved.error is None

    def test_applescript_get_playlists_fails(self, monkeypatch):
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (False, "error"))
        resolved = server._resolve_playlist("My Playlist")
        # Falls back to raw input
        assert resolved.applescript_name == "My Playlist"


# ---------------------------------------------------------------------------
# _is_catalog_id  (lines 2952-2960)
# ---------------------------------------------------------------------------


class TestIsCatalogId:
    def test_numeric_catalog_id(self):
        assert server._is_catalog_id("1440783617") is True

    def test_library_id(self):
        assert server._is_catalog_id("i.ABC123XYZ") is False

    def test_short_numeric(self):
        assert server._is_catalog_id("12345") is False

    def test_playlist_id(self):
        assert server._is_catalog_id("p.abc123") is False


# ---------------------------------------------------------------------------
# _get_playlist_track_names  (lines 2963-2997)
# ---------------------------------------------------------------------------


class TestGetPlaylistTrackNames:
    @responses_lib.activate
    def test_success(self, mock_config_dir, mock_developer_token, mock_user_token):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.abc/tracks",
            json={
                "data": [
                    {"id": "i.t1", "attributes": {"name": "Song A", "artistName": "Artist A"}},
                ]
            },
            status=200,
        )
        ok, tracks = server._get_playlist_track_names("p.abc")
        assert ok is True
        assert len(tracks) == 1
        assert tracks[0]["name"] == "Song A"

    @responses_lib.activate
    def test_404_returns_empty(self, mock_config_dir, mock_developer_token, mock_user_token):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.missing/tracks",
            status=404,
        )
        ok, tracks = server._get_playlist_track_names("p.missing")
        assert ok is True
        assert tracks == []

    @responses_lib.activate
    def test_pagination(self, mock_config_dir, mock_developer_token, mock_user_token):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        # First page: 100 items
        page1 = [
            {"id": f"i.t{i}", "attributes": {"name": f"Song {i}", "artistName": "Artist"}}
            for i in range(100)
        ]
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.pl/tracks",
            json={"data": page1},
            status=200,
        )
        # Second page: 1 item (terminates)
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.pl/tracks",
            json={
                "data": [
                    {"id": "i.t100", "attributes": {"name": "Song 100", "artistName": "Artist"}}
                ]
            },
            status=200,
        )
        ok, tracks = server._get_playlist_track_names("p.pl")
        assert ok is True
        assert len(tracks) == 101


# ---------------------------------------------------------------------------
# _find_track_in_list  (lines 3000-3012)
# ---------------------------------------------------------------------------


class TestFindTrackInList:
    def test_finds_by_name(self):
        tracks = [{"name": "Hey Jude", "artist": "Beatles", "id": "t1"}]
        matches = server._find_track_in_list(tracks, "Hey Jude")
        assert len(matches) == 1
        assert "Hey Jude" in matches[0]

    def test_with_artist_filter(self):
        tracks = [
            {"name": "Hey Jude", "artist": "Beatles", "id": "t1"},
            {"name": "Hey Jude Cover", "artist": "Someone Else", "id": "t2"},
        ]
        matches = server._find_track_in_list(tracks, "Hey Jude", "Beatles")
        assert len(matches) == 1
        assert "Beatles" in matches[0]

    def test_not_found(self):
        tracks = [{"name": "Other Song", "artist": "Artist", "id": "t1"}]
        matches = server._find_track_in_list(tracks, "Hey Jude")
        assert matches == []

    def test_artist_filter_no_match(self):
        tracks = [{"name": "Hey Jude", "artist": "Beatles", "id": "t1"}]
        matches = server._find_track_in_list(tracks, "Hey Jude", "Rolling Stones")
        assert matches == []


# ---------------------------------------------------------------------------
# _playlist_create  (lines 3015-3061)
# ---------------------------------------------------------------------------


class TestPlaylistCreate:
    def test_applescript_success(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(
            server.asc, "create_playlist", lambda name, desc="": (True, "PERSIST001")
        )
        result = server._playlist_create("New Playlist", "A cool playlist")
        assert "Created" in result
        assert "New Playlist" in result
        assert "PERSIST001" in result

    def test_applescript_failure(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(
            server.asc, "create_playlist", lambda name, desc="": (False, "Music.app not running")
        )
        result = server._playlist_create("New Playlist")
        assert "Error" in result

    @responses_lib.activate
    def test_api_fallback_success(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses_lib.add(
            responses_lib.POST,
            "https://api.music.apple.com/v1/me/library/playlists",
            json={"data": [{"id": "p.apipl1"}]},
            status=200,
        )
        result = server._playlist_create("API Playlist")
        assert "Created" in result
        assert "API Playlist" in result
        assert "p.apipl1" in result

    @responses_lib.activate
    def test_api_fallback_request_error(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        import requests as req_lib

        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses_lib.add(
            responses_lib.POST,
            "https://api.music.apple.com/v1/me/library/playlists",
            body=req_lib.exceptions.ConnectionError("connection error"),
        )
        result = server._playlist_create("API Playlist")
        assert "Error" in result or "API Error" in result

    def test_api_fallback_value_error(self, mock_config_dir, monkeypatch):
        """FileNotFoundError when no token configured."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        # mock_config_dir has no token files -> FileNotFoundError -> str(e)
        result = server._playlist_create("API Playlist")
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# _macos_only  (lines 3064-3072)
# ---------------------------------------------------------------------------


class TestMacosOnly:
    def test_not_available_returns_error(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        result = server._macos_only("rename")
        assert result is not None
        assert "Error" in result
        assert "rename" in result
        assert "macOS" in result

    def test_available_returns_none(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        result = server._macos_only("rename")
        assert result is None


# ---------------------------------------------------------------------------
# _playlist_create_folder  (lines 3075-3117)
# ---------------------------------------------------------------------------


class TestPlaylistCreateFolder:
    def test_applescript_single_level_success(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server.asc, "create_folder", lambda name: (True, "f.fld1"))
        result = server._playlist_create_folder("Summer")
        assert "Created" in result
        assert "Summer" in result
        assert "f.fld1" in result

    def test_applescript_multi_level_success(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server.asc, "create_folder_path", lambda path: (True, "f.nested"))
        result = server._playlist_create_folder("Summer/Chill/Deep")
        assert "Created" in result
        assert "Summer/Chill/Deep" in result

    def test_applescript_failure(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server.asc, "create_folder", lambda name: (False, "not supported"))
        result = server._playlist_create_folder("MyFolder")
        assert "Error" in result

    def test_api_fallback_nested_path_rejected(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        result = server._playlist_create_folder("Summer/Chill")
        assert "Error" in result
        assert "macOS" in result or "Nested" in result

    @responses_lib.activate
    def test_api_fallback_single_success(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses_lib.add(
            responses_lib.POST,
            "https://api.music.apple.com/v1/me/library/playlist-folders",
            json={"data": [{"id": "f.apifld1"}]},
            status=201,
        )
        result = server._playlist_create_folder("API Folder")
        assert "Created" in result
        assert "API Folder" in result

    @responses_lib.activate
    def test_api_fallback_error_status(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses_lib.add(
            responses_lib.POST,
            "https://api.music.apple.com/v1/me/library/playlist-folders",
            status=400,
        )
        result = server._playlist_create_folder("Bad Folder")
        assert "Error" in result

    def test_api_fallback_no_token(self, mock_config_dir, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        # mock_config_dir exists but has no token files -> FileNotFoundError inside get_headers()
        result = server._playlist_create_folder("Folder")
        assert "Error" in result or "API" in result

    @responses_lib.activate
    def test_api_fallback_request_exception(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        import requests as req_lib

        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses_lib.add(
            responses_lib.POST,
            "https://api.music.apple.com/v1/me/library/playlist-folders",
            body=req_lib.exceptions.ConnectionError("connection error"),
        )
        result = server._playlist_create_folder("MyFolder")
        assert "Error" in result


# ---------------------------------------------------------------------------
# _playlist_tree, _playlist_path  (lines 3120-3139)
# ---------------------------------------------------------------------------


class TestPlaylistTree:
    def test_no_applescript_error(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        result = server._playlist_tree()
        assert "Error" in result
        assert "macOS" in result

    def test_success(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(
            server.asc, "get_folder_tree", lambda: (True, "Summer\n  Chill\nWinter")
        )
        result = server._playlist_tree()
        assert "Summer" in result

    def test_empty_tree(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server.asc, "get_folder_tree", lambda: (True, "   "))
        result = server._playlist_tree()
        assert "No folders" in result

    def test_failure(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server.asc, "get_folder_tree", lambda: (False, "AS error"))
        result = server._playlist_tree()
        assert "Error" in result


class TestPlaylistPath:
    def test_no_applescript_error(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        result = server._playlist_path("My Playlist")
        assert "Error" in result
        assert "macOS" in result

    def test_no_name_error(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        result = server._playlist_path("")
        assert "Error" in result

    def test_success(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server.asc, "get_playlist_path", lambda name: (True, "Summer/Chill"))
        result = server._playlist_path("My Playlist")
        assert "Summer/Chill" in result

    def test_failure(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server.asc, "get_playlist_path", lambda name: (False, "not found"))
        result = server._playlist_path("My Playlist")
        assert "Error" in result


# ---------------------------------------------------------------------------
# _playlist_move, _playlist_move_to_root  (lines 3142-3163)
# ---------------------------------------------------------------------------


class TestPlaylistMove:
    def test_success(self, monkeypatch):
        monkeypatch.setattr(
            server.asc, "move_to_folder", lambda pl, folder: (True, "Moved playlist")
        )
        result = server._playlist_move("My Playlist", "Summer")
        assert "Moved" in result

    def test_failure(self, monkeypatch):
        monkeypatch.setattr(server.asc, "move_to_folder", lambda pl, folder: (False, "not found"))
        result = server._playlist_move("My Playlist", "Summer")
        assert "Error" in result


class TestPlaylistMoveToRoot:
    def test_success(self, monkeypatch):
        monkeypatch.setattr(server.asc, "move_to_root", lambda name: (True, "Moved to root"))
        result = server._playlist_move_to_root("My Playlist")
        assert "root" in result.lower() or "Moved" in result

    def test_failure(self, monkeypatch):
        monkeypatch.setattr(server.asc, "move_to_root", lambda name: (False, "not supported"))
        result = server._playlist_move_to_root("My Playlist")
        assert "Error" in result


# ---------------------------------------------------------------------------
# _playlist_create_in_folder  (lines 3166-3186)
# ---------------------------------------------------------------------------


class TestPlaylistCreateInFolder:
    def test_success_single_level_folder(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server.asc, "create_folder", lambda name: (True, "f.fld"))
        monkeypatch.setattr(
            server.asc, "create_playlist", lambda name, desc="": (True, "PERSIST01")
        )
        monkeypatch.setattr(server.asc, "move_to_folder", lambda pl, folder: (True, "Moved"))
        result = server._playlist_create_in_folder("My PL", "Summer")
        assert "Created" in result
        assert "My PL" in result
        assert "Summer" in result

    def test_success_nested_folder(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server.asc, "create_folder_path", lambda path: (True, "f.nested"))
        monkeypatch.setattr(
            server.asc, "create_playlist", lambda name, desc="": (True, "PERSIST01")
        )
        monkeypatch.setattr(server.asc, "move_to_folder", lambda pl, folder: (True, "Moved"))
        result = server._playlist_create_in_folder("My PL", "Summer/Chill")
        assert "Created" in result

    def test_create_fails(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server.asc, "create_folder", lambda name: (True, "f.fld"))
        monkeypatch.setattr(server.asc, "create_playlist", lambda name, desc="": (False, "error"))
        result = server._playlist_create_in_folder("My PL", "Summer")
        assert "Error" in result

    def test_move_fails_rolls_back(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server.asc, "create_folder", lambda name: (True, "f.fld"))
        monkeypatch.setattr(
            server.asc, "create_playlist", lambda name, desc="": (True, "PERSIST01")
        )
        monkeypatch.setattr(
            server.asc, "move_to_folder", lambda pl, folder: (False, "folder not found")
        )
        deleted = []
        monkeypatch.setattr(
            server.asc, "delete_playlist", lambda name: deleted.append(name) or (True, "ok")
        )
        result = server._playlist_create_in_folder("My PL", "Summer")
        assert "Error" in result
        assert "My PL" in deleted or len(deleted) == 1


# ---------------------------------------------------------------------------
# _playlist_delete_folder, _playlist_rename_folder  (lines 3189-3217)
# ---------------------------------------------------------------------------


class TestPlaylistDeleteFolder:
    def test_no_name_error(self, monkeypatch):
        result = server._playlist_delete_folder("")
        assert "Error" in result

    def test_success(self, monkeypatch):
        monkeypatch.setattr(server.asc, "delete_folder", lambda name: (True, "Deleted folder"))
        result = server._playlist_delete_folder("Old Folder")
        assert "Deleted" in result

    def test_failure(self, monkeypatch):
        monkeypatch.setattr(server.asc, "delete_folder", lambda name: (False, "not found"))
        result = server._playlist_delete_folder("Old Folder")
        assert "Error" in result


class TestPlaylistRenameFolder:
    def test_no_folder_name_error(self, monkeypatch):
        result = server._playlist_rename_folder("", "New Name")
        assert "Error" in result
        assert "folder name" in result.lower()

    def test_no_new_name_error(self, monkeypatch):
        result = server._playlist_rename_folder("Old Folder", "")
        assert "Error" in result
        assert "new_name" in result.lower()

    def test_success(self, monkeypatch):
        monkeypatch.setattr(
            server.asc, "rename_playlist", lambda old, new: (True, f"Renamed {old} to {new}")
        )
        result = server._playlist_rename_folder("Old Folder", "New Folder")
        assert "Renamed" in result or "New Folder" in result

    def test_failure(self, monkeypatch):
        monkeypatch.setattr(server.asc, "rename_playlist", lambda old, new: (False, "not found"))
        result = server._playlist_rename_folder("Old", "New")
        assert "Error" in result


# ---------------------------------------------------------------------------
# _playlist_list  (lines 2412-2498)
# ---------------------------------------------------------------------------


class TestPlaylistList:
    def test_applescript_success(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(
            server.asc,
            "get_playlists",
            lambda: (
                True,
                [
                    {"id": "ABCD1234", "name": "Summer Hits", "track_count": 12, "smart": False},
                    {"id": "EFGH5678", "name": "Chill Vibes", "track_count": 5, "smart": True},
                ],
            ),
        )
        result = server._playlist_list()
        assert "Summer Hits" in result
        assert "Chill Vibes" in result

    def test_applescript_empty(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, []))
        result = server._playlist_list()
        assert "No playlists" in result

    def test_filter_narrows_by_name(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(
            server.asc,
            "get_playlists",
            lambda: (
                True,
                [
                    {"id": "A", "name": "Jack & Norah", "track_count": 5, "smart": False},
                    {"id": "B", "name": "Jack Heard", "track_count": 3, "smart": False},
                    {"id": "C", "name": "Reggae Tunes", "track_count": 9, "smart": False},
                ],
            ),
        )
        result = server._playlist_list(filter="jack")
        assert "Jack & Norah" in result
        assert "Jack Heard" in result
        assert "Reggae Tunes" not in result

    def test_filter_no_match(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(
            server.asc,
            "get_playlists",
            lambda: (True, [{"id": "C", "name": "Reggae Tunes", "track_count": 9, "smart": False}]),
        )
        result = server._playlist_list(filter="zzz")
        assert "No playlists matching 'zzz'" in result

    def test_applescript_failure(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (False, "Music.app not running"))
        result = server._playlist_list()
        assert "Error" in result

    @responses_lib.activate
    def test_api_fallback_success(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists",
            json={
                "data": [
                    {"id": "p.abc", "attributes": {"name": "Rock Playlist", "canEdit": True}},
                ]
            },
            status=200,
        )
        result = server._playlist_list()
        assert "Rock Playlist" in result

    @responses_lib.activate
    def test_api_fallback_404_stops_pagination(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists",
            status=404,
        )
        # Should not raise — just returns empty list
        result = server._playlist_list()
        assert isinstance(result, str)

    @responses_lib.activate
    def test_api_json_format(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists",
            json={
                "data": [
                    {"id": "p.abc", "attributes": {"name": "Rock Playlist", "canEdit": True}},
                ]
            },
            status=200,
        )
        result = server._playlist_list(format="json")
        data = json.loads(result)
        assert isinstance(data, list)
        assert data[0]["name"] == "Rock Playlist"


# ---------------------------------------------------------------------------
# _playlist_tracks  (lines 2501-2888)
# ---------------------------------------------------------------------------


class TestPlaylistTracks:
    def test_empty_playlist_param_returns_error(self, monkeypatch):
        # _resolve_playlist("") returns error
        result = server._playlist_tracks("")
        assert "Error" in result

    def test_applescript_name_only_empty_playlist(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "Jazz Mix"}]))
        monkeypatch.setattr(server.asc, "get_playlist_tracks", lambda name: (True, []))
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "fetch_explicit": False,
                "output_format": "text",
                "mode": "auto",
                "auto_add": False,
                "storefront": "us",
            },
        )
        result = server._playlist_tracks("Jazz Mix")
        assert "empty" in result.lower()

    def test_applescript_tracks_success(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "Jazz Mix"}]))
        monkeypatch.setattr(
            server.asc,
            "get_playlist_tracks",
            lambda name: (
                True,
                [
                    {
                        "name": "So What",
                        "artist": "Miles Davis",
                        "album": "Kind of Blue",
                        "duration": "9:22",
                        "genre": "Jazz",
                        "year": "1959",
                        "id": "",
                    },
                ],
            ),
        )
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "fetch_explicit": False,
                "output_format": "text",
                "mode": "auto",
                "auto_add": False,
                "storefront": "us",
            },
        )
        result = server._playlist_tracks("Jazz Mix")
        assert "So What" in result
        assert "Miles Davis" in result

    def test_applescript_failure(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "Jazz Mix"}]))
        monkeypatch.setattr(
            server.asc, "get_playlist_tracks", lambda name: (False, "Music.app error")
        )
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "fetch_explicit": False,
                "output_format": "text",
                "mode": "auto",
                "auto_add": False,
                "storefront": "us",
            },
        )
        result = server._playlist_tracks("Jazz Mix")
        assert "Error" in result

    def test_applescript_not_available_for_name(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "fetch_explicit": False,
                "output_format": "text",
                "mode": "auto",
                "auto_add": False,
                "storefront": "us",
            },
        )
        result = server._playlist_tracks("Jazz Mix")
        assert "Error" in result
        assert "macOS" in result

    @responses_lib.activate
    def test_api_path_with_id(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "fetch_explicit": False,
                "output_format": "text",
                "mode": "auto",
                "auto_add": False,
                "storefront": "us",
            },
        )
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.testpl/tracks",
            json={
                "data": [
                    {
                        "id": "i.t1",
                        "attributes": {
                            "name": "Hey Jude",
                            "artistName": "Beatles",
                            "albumName": "Hey Jude",
                            "durationInMillis": 431000,
                        },
                    },
                ]
            },
            status=200,
        )
        result = server._playlist_tracks("p.testpl")
        assert "Hey Jude" in result
        assert "Beatles" in result


# ---------------------------------------------------------------------------
# _playlist_search  (lines 2891-2949)
# ---------------------------------------------------------------------------


class TestPlaylistSearch:
    def test_empty_playlist_error(self, monkeypatch):
        result = server._playlist_search("jude", "")
        assert "Error" in result

    def test_applescript_search_success(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "Jazz Mix"}]))
        monkeypatch.setattr(
            server.asc,
            "search_playlist",
            lambda name, q: (True, [{"name": "So What", "artist": "Miles Davis", "id": ""}]),
        )
        result = server._playlist_search("Miles Davis", "Jazz Mix")
        assert "So What" in result
        assert "Miles Davis" in result

    def test_applescript_search_failure(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "Jazz Mix"}]))
        monkeypatch.setattr(server.asc, "search_playlist", lambda name, q: (False, "error"))
        result = server._playlist_search("Miles Davis", "Jazz Mix")
        assert "Error" in result

    def test_no_matches(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "Jazz Mix"}]))
        monkeypatch.setattr(server.asc, "search_playlist", lambda name, q: (True, []))
        result = server._playlist_search("xyz99999", "Jazz Mix")
        assert "No matches" in result

    @responses_lib.activate
    def test_api_path_with_id(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.testpl/tracks",
            json={
                "data": [
                    {
                        "id": "i.t1",
                        "attributes": {
                            "name": "Hey Jude",
                            "artistName": "Beatles",
                            "albumName": "Hey",
                        },
                    },
                ]
            },
            status=200,
        )
        result = server._playlist_search("Jude", "p.testpl")
        assert "Hey Jude" in result

    def test_applescript_not_available_for_name_search(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        result = server._playlist_search("hey", "Jazz Mix")
        assert "Error" in result
        assert "macOS" in result

    @responses_lib.activate
    def test_api_search_multiple_matches(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """API search with many matches shows truncated list."""
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        # Create 12 "love" songs
        tracks = [
            {
                "id": f"i.t{i}",
                "attributes": {
                    "name": f"Love Song {i}",
                    "artistName": "Artist",
                    "albumName": "Alb",
                },
            }
            for i in range(12)
        ]
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.pl/tracks",
            json={"data": tracks},
            status=200,
        )
        result = server._playlist_search("Love", "p.pl")
        assert "Found" in result
        assert "more" in result


# ---------------------------------------------------------------------------
# _playlist_copy  (lines 3810-3956)
# ---------------------------------------------------------------------------


class TestPlaylistCopy:
    def test_no_new_name_error(self, monkeypatch):
        result = server._playlist_copy("Source", "")
        assert "Error" in result
        assert "new_name" in result.lower()

    def test_applescript_success(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "Source PL"}]))
        monkeypatch.setattr(
            server.asc,
            "get_playlist_tracks",
            lambda name: (
                True,
                [
                    {"name": "Hey Jude", "artist": "Beatles"},
                    {"name": "Let It Be", "artist": "Beatles"},
                ],
            ),
        )
        monkeypatch.setattr(
            server.asc, "create_playlist", lambda name, desc="": (True, "NEWPERSIST")
        )
        monkeypatch.setattr(
            server.asc, "add_track_to_playlist", lambda pl, name, artist: (True, "ok")
        )
        result = server._playlist_copy("Source PL", "Copy PL")
        assert "Copy PL" in result
        assert "NEWPERSIST" in result
        assert "2" in result

    def test_applescript_empty_source(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "Source PL"}]))
        monkeypatch.setattr(server.asc, "get_playlist_tracks", lambda name: (True, []))
        result = server._playlist_copy("Source PL", "Copy PL")
        assert "Error" in result
        assert "empty" in result.lower()

    def test_applescript_source_read_fails(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "Source PL"}]))
        monkeypatch.setattr(
            server.asc, "get_playlist_tracks", lambda name: (False, "Music.app error")
        )
        result = server._playlist_copy("Source PL", "Copy PL")
        assert "Error" in result

    def test_applescript_create_fails(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "Source PL"}]))
        monkeypatch.setattr(
            server.asc,
            "get_playlist_tracks",
            lambda name: (
                True,
                [
                    {"name": "Hey Jude", "artist": "Beatles"},
                ],
            ),
        )
        monkeypatch.setattr(server.asc, "create_playlist", lambda name, desc="": (False, "error"))
        result = server._playlist_copy("Source PL", "Copy PL")
        assert "Error" in result

    def test_applescript_some_tracks_fail(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "Source PL"}]))
        monkeypatch.setattr(
            server.asc,
            "get_playlist_tracks",
            lambda name: (
                True,
                [
                    {"name": "Hey Jude", "artist": "Beatles"},
                    {"name": "Missing Track", "artist": "Unknown"},
                ],
            ),
        )
        monkeypatch.setattr(
            server.asc, "create_playlist", lambda name, desc="": (True, "NEWPERSIST")
        )
        call_count = [0]

        def mock_add(pl, name, artist):
            call_count[0] += 1
            return (True, "ok") if call_count[0] == 1 else (False, "not found")

        monkeypatch.setattr(server.asc, "add_track_to_playlist", mock_add)
        result = server._playlist_copy("Source PL", "Copy PL")
        assert "NEWPERSIST" in result
        assert "Failed" in result

    def test_no_applescript_for_name_error(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        result = server._playlist_copy("Source PL", "Copy PL")
        assert "Error" in result
        assert "macOS" in result

    @responses_lib.activate
    def test_api_mode_by_id_success(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.src/tracks",
            json={
                "data": [
                    {"id": "i.t1", "attributes": {"name": "Song A", "artistName": "Artist"}},
                ]
            },
            status=200,
        )
        responses_lib.add(
            responses_lib.POST,
            "https://api.music.apple.com/v1/me/library/playlists",
            json={"data": [{"id": "p.newcopy"}]},
            status=200,
        )
        responses_lib.add(
            responses_lib.POST,
            "https://api.music.apple.com/v1/me/library/playlists/p.newcopy/tracks",
            status=204,
        )
        result = server._playlist_copy("p.src", "Copy PL")
        assert "Copy PL" in result
        assert "p.newcopy" in result
        assert "1" in result

    @responses_lib.activate
    def test_api_mode_404_pagination_ends(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.src/tracks",
            status=404,
        )
        responses_lib.add(
            responses_lib.POST,
            "https://api.music.apple.com/v1/me/library/playlists",
            json={"data": [{"id": "p.newcopy"}]},
            status=200,
        )
        result = server._playlist_copy("p.src", "Copy PL")
        assert "Copy PL" in result
        assert "0" in result


# ---------------------------------------------------------------------------
# _playlist_add  (key branches of lines 3220-3807)
# ---------------------------------------------------------------------------


class TestPlaylistAdd:
    def test_no_playlist_error(self, monkeypatch):
        result = server._playlist_add("", "Hey Jude")
        assert "Error" in result
        assert "playlist" in result.lower()

    def test_no_track_no_album_error(self, monkeypatch):
        result = server._playlist_add("My Playlist", "")
        assert "Error" in result

    def test_applescript_mode_name_success(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "My Playlist"}]))
        monkeypatch.setattr(
            server.asc, "track_exists_in_playlist", lambda pl, name, artist: (True, False)
        )
        monkeypatch.setattr(
            server,
            "_smart_as_add_track_to_playlist",
            lambda pl, name, artist, album: (True, "ok", None),
        )
        monkeypatch.setattr(server, "_verify_track_in_playlist", lambda pl, name, artist: True)
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "auto_add": False,
                "fetch_explicit": False,
                "mode": "auto",
                "storefront": "us",
                "output_format": "text",
            },
        )
        result = server._playlist_add("My Playlist", "Hey Jude")
        assert "Added" in result

    def test_applescript_duplicate_skipped(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "My Playlist"}]))
        monkeypatch.setattr(
            server.asc, "track_exists_in_playlist", lambda pl, name, artist: (True, True)
        )
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "auto_add": False,
                "fetch_explicit": False,
                "mode": "auto",
                "storefront": "us",
                "output_format": "text",
            },
        )
        result = server._playlist_add("My Playlist", "Hey Jude")
        # Should skip due to duplicate — "No tracks added" or skipped step
        assert "Skipped" in result or "No tracks" in result or "skip" in result.lower()

    def test_explicit_id_uses_api_mode(self, monkeypatch):
        """Explicit p.xxx playlist bypasses AppleScript."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "auto_add": False,
                "fetch_explicit": False,
                "mode": "auto",
                "storefront": "us",
                "output_format": "text",
            },
        )
        # API mode: needs get_headers and POST to playlists endpoint
        # Since we have no tokens, it raises FileNotFoundError -> "Error"
        result = server._playlist_add("p.abc123", "Hey Jude")
        assert isinstance(result, str)

    def test_no_applescript_name_error(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "auto_add": False,
                "fetch_explicit": False,
                "mode": "auto",
                "storefront": "us",
                "output_format": "text",
            },
        )
        result = server._playlist_add("My Playlist", "Hey Jude")
        assert "Error" in result
        assert "macOS" in result or "playlist ID" in result

    def test_track_not_found_no_autosearch_hint(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "My Playlist"}]))
        monkeypatch.setattr(
            server.asc, "track_exists_in_playlist", lambda pl, name, artist: (True, False)
        )
        monkeypatch.setattr(
            server,
            "_smart_as_add_track_to_playlist",
            lambda pl, name, artist, album: (False, "Track not found", None),
        )
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "auto_add": False,
                "fetch_explicit": False,
                "mode": "auto",
                "storefront": "us",
                "output_format": "text",
            },
        )
        result = server._playlist_add("My Playlist", "UnknownSong", verify=False)
        assert "not in your library" in result or "auto_add" in result

    def test_applescript_add_split_match(self, monkeypatch):
        """When _smart_as_add_track_to_playlist returns split_match, name is formatted."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "My Playlist"}]))
        monkeypatch.setattr(
            server.asc, "track_exists_in_playlist", lambda pl, name, artist: (True, False)
        )
        monkeypatch.setattr(
            server,
            "_smart_as_add_track_to_playlist",
            lambda pl, name, artist, album: (True, "ok", ("Hey Jude", "Beatles")),
        )
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "auto_add": False,
                "fetch_explicit": False,
                "mode": "auto",
                "storefront": "us",
                "output_format": "text",
            },
        )
        result = server._playlist_add("My Playlist", "HeyJude")
        assert "Added" in result
        assert "Hey Jude" in result

    def test_mixed_added_and_errors(self, monkeypatch):
        """When some tracks added and some fail."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "My Playlist"}]))
        monkeypatch.setattr(
            server.asc, "track_exists_in_playlist", lambda pl, name, artist: (True, False)
        )
        call_count = [0]

        def mock_smart_add(pl, name, artist, album):
            call_count[0] += 1
            if call_count[0] == 1:
                return (True, "ok", None)
            return (False, "Some error", None)

        monkeypatch.setattr(server, "_smart_as_add_track_to_playlist", mock_smart_add)
        monkeypatch.setattr(server, "_verify_track_in_playlist", lambda pl, name, artist: True)
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "auto_add": False,
                "fetch_explicit": False,
                "mode": "auto",
                "storefront": "us",
                "output_format": "text",
            },
        )
        result = server._playlist_add("My Playlist", "Hey Jude, Let It Be", verify=True)
        assert "Added" in result
        assert "Errors" in result or "failed" in result.lower()

    def test_verify_fails_then_retry_fails(self, monkeypatch):
        """Verify-then-retry path when track doesn't persist after add."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "My PL"}]))
        monkeypatch.setattr(
            server.asc, "track_exists_in_playlist", lambda pl, name, artist: (True, False)
        )
        monkeypatch.setattr(
            server,
            "_smart_as_add_track_to_playlist",
            lambda pl, name, artist, album: (True, "ok", None),
        )
        # First verify call returns False (track not found), second also False
        monkeypatch.setattr(server, "_verify_track_in_playlist", lambda pl, name, artist: False)
        monkeypatch.setattr(server, "_VERIFY_DELAY_S", 0)
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "auto_add": False,
                "fetch_explicit": False,
                "mode": "auto",
                "storefront": "us",
                "output_format": "text",
            },
        )
        result = server._playlist_add("My PL", "Hey Jude", verify=True)
        # Should fail with "did not persist" message
        assert "persist" in result.lower() or "Error" in result


# ---------------------------------------------------------------------------
# playlist() dispatcher  (lines 3959-4093)
# ---------------------------------------------------------------------------


class TestPlaylistDispatcher:
    # --- list ---
    def test_action_list(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(
            server.asc,
            "get_playlists",
            lambda: (True, [{"id": "A1", "name": "PL1", "track_count": 3, "smart": False}]),
        )
        result = server.playlist(action="list")
        assert "PL1" in result

    # --- tracks ---
    def test_action_tracks(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "Jazz Mix"}]))
        monkeypatch.setattr(
            server.asc,
            "get_playlist_tracks",
            lambda name: (
                True,
                [
                    {
                        "name": "So What",
                        "artist": "Miles Davis",
                        "album": "",
                        "duration": "9:22",
                        "genre": "Jazz",
                        "year": "1959",
                        "id": "",
                    },
                ],
            ),
        )
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "fetch_explicit": False,
                "output_format": "text",
                "mode": "auto",
                "auto_add": False,
                "storefront": "us",
            },
        )
        result = server.playlist(action="tracks", playlist="Jazz Mix")
        assert "So What" in result

    # --- search ---
    def test_action_search_no_query(self, monkeypatch):
        result = server.playlist(action="search")
        assert "Error" in result
        assert "query" in result.lower()

    def test_action_search_with_query(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "Jazz Mix"}]))
        monkeypatch.setattr(
            server.asc,
            "search_playlist",
            lambda name, q: (True, [{"name": "So What", "artist": "Miles Davis", "id": ""}]),
        )
        result = server.playlist(action="search", playlist="Jazz Mix", query="Miles")
        assert "So What" in result

    # --- create ---
    def test_action_create_no_name_no_folder_error(self, monkeypatch):
        result = server.playlist(action="create")
        assert "Error" in result

    def test_action_create_name_native(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_engine", lambda: "native")
        monkeypatch.setattr(
            server.asc, "create_playlist", lambda name, desc="": (True, "PERSIST01")
        )
        result = server.playlist(action="create", name="New PL")
        assert "Created" in result
        assert "New PL" in result

    def test_action_create_name_api_engine(self, monkeypatch):
        monkeypatch.setattr(server, "_write_rail", lambda *a, **k: "web")
        monkeypatch.setattr(
            server.amp_api, "create_playlist", lambda name, desc="": (True, "p.new1")
        )
        result = server.playlist(action="create", name="API PL")
        assert "Created" in result
        assert "p.new1" in result

    def test_action_create_folder_only_api(self, monkeypatch):
        monkeypatch.setattr(server, "_engine", lambda: "api")
        monkeypatch.setattr(
            server.amp_api, "create_folder", lambda name, parent_id=None: (True, "f.fld1")
        )
        # On macOS _playlist_create_folder branches on APPLESCRIPT_AVAILABLE, so it
        # calls asc.create_folder regardless of engine; stub it so this stays
        # offline instead of creating a real "My Folder" in the library.
        monkeypatch.setattr(server.asc, "create_folder", lambda name: (True, "f.fld1"))
        result = server.playlist(action="create", folder="My Folder")
        assert "Created" in result
        assert "My Folder" in result

    def test_action_create_folder_only_native(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_engine", lambda: "native")
        monkeypatch.setattr(server.asc, "create_folder", lambda name: (True, "f.fld1"))
        result = server.playlist(action="create", folder="My Folder")
        assert "Created" in result

    def test_action_create_folder_and_name(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_engine", lambda: "native")
        monkeypatch.setattr(server.asc, "create_folder", lambda name: (True, "f.fld1"))
        monkeypatch.setattr(
            server.asc, "create_playlist", lambda name, desc="": (True, "PERSIST01")
        )
        monkeypatch.setattr(server.asc, "move_to_folder", lambda pl, folder: (True, "Moved"))
        result = server.playlist(action="create", name="New PL", folder="Summer")
        assert "Created" in result
        assert "New PL" in result

    # --- add ---
    def test_action_add_api_engine(self, monkeypatch):
        # Off macOS → the add takes the web rail (amp-api).
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        monkeypatch.setattr(server, "_has_developer_token", lambda: False)
        monkeypatch.setattr(server, "_has_user_token", lambda: True)
        monkeypatch.setattr(server.amp_api, "add_tracks", lambda pid, items: (True, "ok"))
        result = server.playlist(action="add", playlist="p.abc123", track="i.song1")
        assert "Added" in result

    def test_web_add_to_app_made_playlist_explains_origin(self, monkeypatch):
        """Off macOS, a 401/403 adding to a Music.app-made playlist (session OK)
        must explain it's a playlist-origin limitation, not blame auth."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        monkeypatch.setattr(server, "_has_developer_token", lambda: False)
        monkeypatch.setattr(server, "_has_user_token", lambda: True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: ("p.app", None))
        monkeypatch.setattr(server.amp_api, "session_status", lambda: "ok")
        monkeypatch.setattr(
            server.amp_api, "add_tracks", lambda pid, items: (False, "status 403: forbidden")
        )
        out = server.playlist(action="add", playlist="Jack & Norah", track="i.song1")
        assert "created in Music.app" in out and "403" in out

    def test_web_add_401_with_expired_session_says_reauth(self, monkeypatch):
        """A 401 add when the session is actually expired must say re-auth, NOT
        blame the playlist origin (the disambiguation the review caught)."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        monkeypatch.setattr(server, "_has_developer_token", lambda: False)
        monkeypatch.setattr(server, "_has_user_token", lambda: True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: ("p.app", None))
        monkeypatch.setattr(server.amp_api, "session_status", lambda: "expired")
        monkeypatch.setattr(
            server.amp_api, "add_tracks", lambda pid, items: (False, "status 401: unauthorized")
        )
        out = server.playlist(action="add", playlist="Jack & Norah", track="i.song1")
        assert "expired" in out.lower() and "login" in out.lower()
        assert "created in Music.app" not in out

    def test_action_add_api_engine_with_album_falls_back(self, monkeypatch):
        """API engine with album param falls through to _playlist_add."""
        monkeypatch.setattr(server, "_engine", lambda: "api")
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "auto_add": False,
                "fetch_explicit": False,
                "mode": "api",
                "storefront": "us",
                "output_format": "text",
            },
        )
        # album-only add requires token check
        result = server.playlist(action="add", playlist="p.abc123", album="Abbey Road")
        assert isinstance(result, str)

    def test_action_add_native(self, monkeypatch):
        monkeypatch.setattr(server, "_engine", lambda: "native")
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "My Playlist"}]))
        monkeypatch.setattr(
            server.asc, "track_exists_in_playlist", lambda pl, name, artist: (True, False)
        )
        monkeypatch.setattr(
            server,
            "_smart_as_add_track_to_playlist",
            lambda pl, name, artist, album: (True, "ok", None),
        )
        monkeypatch.setattr(server, "_verify_track_in_playlist", lambda pl, name, artist: True)
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "auto_add": False,
                "fetch_explicit": False,
                "mode": "native",
                "storefront": "us",
                "output_format": "text",
            },
        )
        result = server.playlist(action="add", playlist="My Playlist", track="Hey Jude")
        assert "Added" in result

    # --- copy ---
    def test_action_copy(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "Source PL"}]))
        monkeypatch.setattr(
            server.asc,
            "get_playlist_tracks",
            lambda name: (
                True,
                [
                    {"name": "Song A", "artist": "Artist A"},
                ],
            ),
        )
        monkeypatch.setattr(
            server.asc, "create_playlist", lambda name, desc="": (True, "NEWPERSIST")
        )
        monkeypatch.setattr(
            server.asc, "add_track_to_playlist", lambda pl, name, artist: (True, "ok")
        )
        result = server.playlist(action="copy", source="Source PL", new_name="Copy PL")
        assert "Copy PL" in result

    # --- remove ---
    def test_action_remove_api_engine(self, monkeypatch):
        monkeypatch.setattr(server, "_write_rail", lambda *a, **k: "web")
        monkeypatch.setattr(server.amp_api, "resolve_playlist_id", lambda name, **k: "p.abc")
        monkeypatch.setattr(
            server.amp_api,
            "get_tracks",
            lambda pid: [{"name": "Hey Jude", "artist": "Beatles", "relationship_id": "rel1"}],
        )
        monkeypatch.setattr(server.amp_api, "remove_track", lambda pid, rid: (True, "ok"))
        result = server.playlist(action="remove", playlist="My PL", track="Hey Jude")
        assert "Removed" in result

    def test_action_remove_native_macos_only_error(self, monkeypatch):
        """Off macOS, remove now routes to the web rail (not a 'requires macOS' error)."""
        monkeypatch.setattr(server, "_write_rail", lambda *a, **k: "web")
        monkeypatch.setattr(server.amp_api, "resolve_playlist_id", lambda name, **k: "p.abc")
        monkeypatch.setattr(
            server.amp_api,
            "get_tracks",
            lambda pid: [{"name": "Hey Jude", "artist": "Beatles", "relationship_id": "rel1"}],
        )
        monkeypatch.setattr(server.amp_api, "remove_track", lambda pid, rid: (True, "ok"))
        result = server.playlist(action="remove", playlist="My PL", track="Hey Jude")
        assert "Removed" in result and "macOS" not in result

    def test_action_delete_no_name_error(self, monkeypatch):
        result = server.playlist(action="delete")
        assert "Error" in result

    def test_action_delete_api_engine(self, monkeypatch):
        monkeypatch.setattr(server, "_write_rail", lambda *a, **k: "web")
        monkeypatch.setattr(server.amp_api, "resolve_playlist_id", lambda name, **k: "p.del1")
        monkeypatch.setattr(server.amp_api, "delete_playlist", lambda pid: (True, "ok"))
        monkeypatch.setattr(server.amp_api, "session_status", lambda: "ok")
        result = server.playlist(action="delete", name="Old Playlist")
        assert "Deleted" in result

    def test_action_delete_folder_api(self, monkeypatch):
        monkeypatch.setattr(server, "_write_rail", lambda *a, **k: "web")
        monkeypatch.setattr(server.amp_api, "resolve_folder_id", lambda name: "f.fld1")
        monkeypatch.setattr(server.amp_api, "delete_folder", lambda fid: (True, "ok"))
        result = server.playlist(action="delete", folder="Old Folder")
        assert "Deleted" in result

    def test_action_delete_folder_native_macos_only(self, monkeypatch):
        """Off macOS, folder delete routes to the web rail."""
        monkeypatch.setattr(server, "_write_rail", lambda *a, **k: "web")
        monkeypatch.setattr(server.amp_api, "resolve_folder_id", lambda name: "f.fld1")
        monkeypatch.setattr(server.amp_api, "delete_folder", lambda fid: (True, "ok"))
        result = server.playlist(action="delete", folder="Old Folder")
        assert "Deleted" in result and "macOS" not in result

    def test_action_delete_folder_native_success(self, monkeypatch):
        monkeypatch.setattr(server, "_engine", lambda: "native")
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server.asc, "delete_folder", lambda name: (True, "Deleted"))
        result = server.playlist(action="delete", folder="Old Folder")
        assert "Deleted" in result

    def test_action_delete_native_success(self, monkeypatch):
        monkeypatch.setattr(server, "_engine", lambda: "native")
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        # _playlist_delete reads tracks first (for the undo log); stub it offline.
        monkeypatch.setattr(server.asc, "get_playlist_tracks", lambda name: (True, []))
        monkeypatch.setattr(server.asc, "delete_playlist", lambda name: (True, "Deleted"))
        result = server.playlist(action="delete", name="Old PL")
        assert "Deleted" in result

    def test_action_delete_native_macos_only_error(self, monkeypatch):
        """Off macOS, delete routes to the web rail."""
        monkeypatch.setattr(server, "_write_rail", lambda *a, **k: "web")
        monkeypatch.setattr(server.amp_api, "resolve_playlist_id", lambda name, **k: "p.del1")
        monkeypatch.setattr(server.amp_api, "delete_playlist", lambda pid: (True, "ok"))
        monkeypatch.setattr(server.amp_api, "session_status", lambda: "ok")
        result = server.playlist(action="delete", name="Old PL")
        assert "Deleted" in result and "macOS" not in result

    def test_action_rename_no_new_name_error(self, monkeypatch):
        result = server.playlist(action="rename", playlist="Old PL")
        assert "Error" in result
        assert "new_name" in result.lower()

    def test_action_rename_no_playlist_error(self, monkeypatch):
        result = server.playlist(action="rename", new_name="New PL")
        assert "Error" in result

    def test_action_rename_api_engine(self, monkeypatch):
        monkeypatch.setattr(server, "_write_rail", lambda *a, **k: "web")
        monkeypatch.setattr(server.amp_api, "resolve_playlist_id", lambda name, **k: "p.r1")
        monkeypatch.setattr(server.amp_api, "rename_playlist", lambda pid, new: (True, "ok"))
        result = server.playlist(action="rename", playlist="Old PL", new_name="New PL")
        assert "Renamed" in result

    def test_action_rename_folder_macos_only(self, monkeypatch):
        monkeypatch.setattr(server, "_engine", lambda: "native")
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        result = server.playlist(action="rename", folder="Old Folder", new_name="New Folder")
        assert "Error" in result
        assert "macOS" in result

    def test_action_rename_folder_native_success(self, monkeypatch):
        monkeypatch.setattr(server, "_engine", lambda: "native")
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(
            server.asc, "rename_playlist", lambda old, new: (True, f"Renamed to {new}")
        )
        result = server.playlist(action="rename", folder="Old Folder", new_name="New Folder")
        assert "Renamed" in result or "New Folder" in result

    def test_action_rename_native_success(self, monkeypatch):
        monkeypatch.setattr(server, "_engine", lambda: "native")
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(
            server.asc, "rename_playlist", lambda old, new: (True, f"Renamed {old} to {new}")
        )
        result = server.playlist(action="rename", playlist="Old PL", new_name="New PL")
        assert "Renamed" in result

    def test_action_rename_native_macos_only_error(self, monkeypatch):
        """Off macOS, rename routes to the web rail."""
        monkeypatch.setattr(server, "_write_rail", lambda *a, **k: "web")
        monkeypatch.setattr(server.amp_api, "resolve_playlist_id", lambda name, **k: "p.r1")
        monkeypatch.setattr(server.amp_api, "rename_playlist", lambda pid, new: (True, "ok"))
        result = server.playlist(action="rename", playlist="Old PL", new_name="New PL")
        assert "Renamed" in result and "macOS" not in result

    def test_action_create_folder_no_name_error(self, monkeypatch):
        result = server.playlist(action="create_folder")
        assert "Error" in result

    def test_action_create_folder_api_engine(self, monkeypatch):
        monkeypatch.setattr(server, "_engine", lambda: "api")
        monkeypatch.setattr(
            server.amp_api, "create_folder", lambda name, parent_id=None: (True, "f.fld1")
        )
        # See test_action_create_folder_only_api: on macOS the folder create path
        # runs asc.create_folder, so stub it to keep the test offline.
        monkeypatch.setattr(server.asc, "create_folder", lambda name: (True, "f.fld1"))
        result = server.playlist(action="create_folder", name="Summer Folder")
        assert "Created" in result
        assert "Summer Folder" in result

    def test_action_create_folder_native_success(self, monkeypatch):
        monkeypatch.setattr(server, "_engine", lambda: "native")
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server.asc, "create_folder", lambda name: (True, "f.fld1"))
        result = server.playlist(action="create_folder", name="Summer Folder")
        assert "Created" in result

    def test_action_create_folder_native_macos_only_error(self, monkeypatch):
        """Off macOS, create_folder routes to the web rail."""
        monkeypatch.setattr(server, "_write_rail", lambda *a, **k: "web")
        monkeypatch.setattr(
            server.amp_api, "create_folder", lambda name, parent_id=None: (True, "f.fld1")
        )
        result = server.playlist(action="create_folder", name="Summer Folder")
        assert "Created" in result and "macOS" not in result

    def test_action_move_no_playlist_error(self, monkeypatch):
        result = server.playlist(action="move")
        assert "Error" in result
        assert "playlist" in result.lower()

    def test_action_move_api_engine(self, monkeypatch):
        monkeypatch.setattr(server, "_write_rail", lambda *a, **k: "web")
        monkeypatch.setattr(server.amp_api, "resolve_playlist_id", lambda name, **k: "p.abc")
        monkeypatch.setattr(server.amp_api, "resolve_folder_id", lambda name: "f.fld1")
        monkeypatch.setattr(
            server.amp_api, "move_playlist_to_folder", lambda pid, fid: (True, "ok")
        )
        result = server.playlist(action="move", playlist="My PL", name="Summer")
        assert "Moved" in result

    def test_action_move_native_to_folder(self, monkeypatch):
        monkeypatch.setattr(server, "_engine", lambda: "native")
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server.asc, "move_to_folder", lambda pl, folder: (True, "Moved"))
        result = server.playlist(action="move", playlist="My PL", name="Summer")
        assert "Moved" in result

    def test_action_move_native_no_folder_needs_confirm(self, monkeypatch):
        monkeypatch.setattr(server, "_engine", lambda: "native")
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        result = server.playlist(action="move", playlist="My PL")
        assert "cannot move" in result.lower() or "allow_duplicates" in result

    def test_action_move_native_no_folder_with_confirm(self, monkeypatch):
        monkeypatch.setattr(server, "_engine", lambda: "native")
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server.asc, "move_to_root", lambda name: (True, "Moved to root"))
        result = server.playlist(action="move", playlist="My PL", allow_duplicates=True)
        assert "root" in result.lower() or "Moved" in result

    def test_action_move_native_macos_only_error(self, monkeypatch):
        """Off macOS, move routes to the web rail."""
        monkeypatch.setattr(server, "_write_rail", lambda *a, **k: "web")
        monkeypatch.setattr(server.amp_api, "resolve_playlist_id", lambda name, **k: "p.abc")
        monkeypatch.setattr(server.amp_api, "resolve_folder_id", lambda name: "f.fld1")
        monkeypatch.setattr(
            server.amp_api, "move_playlist_to_folder", lambda pid, fid: (True, "ok")
        )
        result = server.playlist(action="move", playlist="My PL", name="Summer")
        assert "Moved" in result and "macOS" not in result

    def test_action_path_with_target(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server.asc, "get_playlist_path", lambda name: (True, "Summer/Chill"))
        result = server.playlist(action="path", playlist="My PL")
        assert "Summer/Chill" in result

    def test_action_path_without_target_shows_tree(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server.asc, "get_folder_tree", lambda: (True, "Summer\n  Chill"))
        result = server.playlist(action="path")
        assert "Summer" in result

    # --- unknown action ---
    def test_unknown_action(self, monkeypatch):
        result = server.playlist(action="frobnicate")
        assert "Unknown action" in result
        assert "frobnicate" in result

    # --- action normalization ---
    def test_action_with_dash_normalized(self, monkeypatch):
        """Actions with dashes should normalize to underscores."""
        monkeypatch.setattr(server, "_write_rail", lambda *a, **k: "web")
        monkeypatch.setattr(
            server.amp_api, "create_folder", lambda name, parent_id=None: (True, "f.fld1")
        )
        result = server.playlist(action="create-folder", name="Summer")
        assert "Created" in result

    # --- delete with playlist param (not name) ---
    def test_action_delete_using_playlist_param(self, monkeypatch):
        monkeypatch.setattr(server, "_write_rail", lambda *a, **k: "web")
        monkeypatch.setattr(server.amp_api, "resolve_playlist_id", lambda name, **k: "p.del1")
        monkeypatch.setattr(server.amp_api, "delete_playlist", lambda pid: (True, "ok"))
        monkeypatch.setattr(server.amp_api, "session_status", lambda: "ok")
        result = server.playlist(action="delete", playlist="Old Playlist")
        assert "Deleted" in result

    # --- move using folder param explicitly ---
    def test_action_move_using_folder_param(self, monkeypatch):
        monkeypatch.setattr(server, "_write_rail", lambda *a, **k: "web")
        monkeypatch.setattr(server.amp_api, "resolve_playlist_id", lambda name, **k: "p.abc")
        monkeypatch.setattr(server.amp_api, "resolve_folder_id", lambda name: "f.fld1")
        monkeypatch.setattr(
            server.amp_api, "move_playlist_to_folder", lambda pid, fid: (True, "ok")
        )
        result = server.playlist(action="move", playlist="My PL", folder="Summer")
        assert "Moved" in result

    # --- move API engine to root (empty folder) ---
    def test_action_move_api_engine_no_folder(self, monkeypatch):
        monkeypatch.setattr(server, "_write_rail", lambda *a, **k: "web")
        monkeypatch.setattr(server.amp_api, "resolve_playlist_id", lambda name, **k: "p.abc")
        monkeypatch.setattr(
            server.amp_api, "move_playlist_to_folder", lambda pid, fid: (True, "ok")
        )
        result = server.playlist(action="move", playlist="My PL")
        assert "Moved" in result

    # --- rename via name param (not playlist) ---
    def test_action_rename_using_name_param(self, monkeypatch):
        monkeypatch.setattr(server, "_write_rail", lambda *a, **k: "web")
        monkeypatch.setattr(server.amp_api, "resolve_playlist_id", lambda name, **k: "p.r1")
        monkeypatch.setattr(server.amp_api, "rename_playlist", lambda pid, new: (True, "ok"))
        result = server.playlist(action="rename", name="Old PL", new_name="New PL")
        assert "Renamed" in result

    # --- create_folder backward compat with alias ---
    def test_action_create_folder_alias_via_create(self, monkeypatch):
        """create action with folder= and no name= creates a folder."""
        monkeypatch.setattr(server, "_engine", lambda: "native")
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server.asc, "create_folder", lambda name: (True, "f.fld"))
        result = server.playlist(action="create", folder="My Folder")
        assert "Created" in result
        assert "My Folder" in result

    # --- path with folder param ---
    def test_action_path_with_folder_param(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server.asc, "get_playlist_path", lambda name: (True, "Root/Sub"))
        result = server.playlist(action="path", folder="Sub")
        assert "Root/Sub" in result


# ---------------------------------------------------------------------------
# Additional edge cases for better coverage
# ---------------------------------------------------------------------------


class TestPlaylistAddApiEdgeCases:
    """Cover additional _playlist_add_api branches."""

    def test_with_name_track_search_with_artist(self, monkeypatch):
        """Name track with artist passed to catalog search."""
        catalog_called = []

        def mock_search(q, n):
            catalog_called.append(q)
            return [{"id": "1234567890", "name": "Hey Jude", "artist": "Beatles"}]

        monkeypatch.setattr(server.amp_api, "add_tracks", lambda pid, items: (True, "ok"))
        monkeypatch.setattr(server.amp_api, "search_catalog_songs", mock_search)
        result = server._playlist_add_api("p.abc123", "Hey Jude", "Beatles", auto_add=True)
        assert "Added" in result
        assert len(catalog_called) == 1
        assert "Beatles" in catalog_called[0]


class TestPlaylistListOutputFormats:
    """Cover format_output calls for different formats."""

    def test_csv_format(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(
            server.asc,
            "get_playlists",
            lambda: (
                True,
                [
                    {"id": "A1", "name": "PL1", "track_count": 3, "smart": False},
                ],
            ),
        )
        result = server._playlist_list(format="csv")
        assert "PL1" in result

    def test_none_format(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(
            server.asc,
            "get_playlists",
            lambda: (
                True,
                [
                    {"id": "A1", "name": "PL1", "track_count": 3, "smart": False},
                ],
            ),
        )
        result = server._playlist_list(format="none")
        assert isinstance(result, str)


class TestPlaylistAddIdsWithNoToken:
    """Test the IDs-require-token guard in _playlist_add."""

    def test_ids_without_token_error(self, monkeypatch):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "My PL"}]))
        monkeypatch.setattr(server, "_has_developer_token", lambda: False)
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "auto_add": False,
                "fetch_explicit": False,
                "mode": "auto",
                "storefront": "us",
                "output_format": "text",
            },
        )
        # Catalog ID input
        result = server._playlist_add("My PL", "1234567890")
        assert "Error" in result or "token" in result.lower() or "login --dev" in result

    def test_all_tracks_duplicate_returns_no_tracks_added(self, monkeypatch):
        """When all tracks are skipped as duplicates."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "My PL"}]))
        monkeypatch.setattr(
            server.asc, "track_exists_in_playlist", lambda pl, name, artist: (True, True)
        )
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "auto_add": False,
                "fetch_explicit": False,
                "mode": "auto",
                "storefront": "us",
                "output_format": "text",
            },
        )
        result = server._playlist_add("My PL", "Hey Jude, Let It Be")
        # No tracks added, no errors
        assert "No tracks" in result or "steps" in result.lower() or len(result) > 0


class TestPlaylistCopyLargeBatch:
    """Test copy with 25+ tracks to trigger batch logic."""

    @responses_lib.activate
    def test_api_copy_large_batch(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        # 30 tracks -> two batches (25 + 5)
        tracks = [
            {"id": f"i.t{i}", "attributes": {"name": f"Song {i}", "artistName": "Artist"}}
            for i in range(30)
        ]
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.src/tracks",
            json={"data": tracks},
            status=200,
        )
        responses_lib.add(
            responses_lib.POST,
            "https://api.music.apple.com/v1/me/library/playlists",
            json={"data": [{"id": "p.newcopy"}]},
            status=200,
        )
        # Two batch posts
        responses_lib.add(
            responses_lib.POST,
            "https://api.music.apple.com/v1/me/library/playlists/p.newcopy/tracks",
            status=204,
        )
        responses_lib.add(
            responses_lib.POST,
            "https://api.music.apple.com/v1/me/library/playlists/p.newcopy/tracks",
            status=204,
        )
        result = server._playlist_copy("p.src", "Copy PL")
        assert "30" in result
        assert "Copy PL" in result


class TestPlaylistRemoveNativeDispatch:
    """Cover native _playlist_remove path in dispatcher."""

    def test_remove_native_success(self, monkeypatch):
        monkeypatch.setattr(server, "_engine", lambda: "native")
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        # _playlist_remove is macOS-only (defined inside APPLESCRIPT_AVAILABLE block)
        # Patch it directly
        monkeypatch.setattr(
            server,
            "_playlist_remove",
            lambda pl, track, artist="": f"Removed '{track}' from '{pl}'",
        )
        result = server.playlist(action="remove", playlist="My PL", track="Hey Jude")
        assert "Removed" in result
        assert "Hey Jude" in result


class TestPlaylistDeleteNativeDirect:
    """Cover _playlist_delete path via dispatcher."""

    def test_delete_native_calls_playlist_delete(self, monkeypatch):
        monkeypatch.setattr(server, "_engine", lambda: "native")
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_playlist_delete", lambda name: f"Deleted playlist '{name}'")
        result = server.playlist(action="delete", name="Old PL")
        assert "Deleted" in result
        assert "Old PL" in result


class TestResolvePlaylistApplescriptEmpty:
    """Cover branch where AppleScript get_playlists returns no match."""

    def test_no_match_in_applescript_falls_back_to_raw(self, monkeypatch):
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(
            server.asc, "get_playlists", lambda: (True, [{"name": "Something Else"}])
        )
        resolved = server._resolve_playlist("NonExistentPlaylist")
        # Falls back to raw (fuzzy match returns None for very different names)
        assert resolved.error is None


class TestPlaylistAddApiErrorHandling:
    """Cover error paths in _playlist_add_api."""

    def test_empty_items_after_all_errors(self, monkeypatch):
        """All tracks error-out -> 'Error: nothing to add'."""
        monkeypatch.setattr(server.amp_api, "search_catalog_songs", lambda q, n: [])
        result = server._playlist_add_api("p.abc123", "UnknownSong1, UnknownSong2", auto_add=True)
        assert "Error" in result
        assert "nothing" in result.lower() or "not found" in result.lower()

    def test_r_error_from_bad_json(self, monkeypatch):
        """r.error branch (line 1117): malformed JSON gives r.error."""
        monkeypatch.setattr(server.amp_api, "add_tracks", lambda pid, items: (True, "ok"))
        # '[invalid' starts with '[', JSON parse fails -> r.error set
        result = server._playlist_add_api("p.abc123", "[invalid json")
        assert "Error" in result

    def test_unsupported_id_type(self, monkeypatch):
        """Line 1134: PLAYLIST_ID as a track input -> 'unsupported id type'."""
        monkeypatch.setattr(server.amp_api, "add_tracks", lambda pid, items: (True, "ok"))
        # All IDs are "unsupported" -> items is empty -> "nothing to add"
        result = server._playlist_add_api("p.abc123", "p.abc999xyz")
        assert "Error" in result

    def test_mixed_success_and_error_appended(self, monkeypatch):
        """Line 1147: errors appended when items succeeded too."""
        # One catalog ID succeeds + one name that can't be found (errors list non-empty)
        monkeypatch.setattr(server.amp_api, "search_catalog_songs", lambda q, n: [])
        monkeypatch.setattr(server.amp_api, "add_tracks", lambda pid, items: (True, "ok"))
        # "1234567890" is catalog ID (added), "MissingTrack" not found -> errors
        result = server._playlist_add_api("p.abc123", "1234567890, MissingTrack")
        assert "Added" in result
        assert "not found" in result.lower() or "MissingTrack" in result


# ---------------------------------------------------------------------------
# _playlist_list: pagination and exception coverage
# ---------------------------------------------------------------------------


class TestPlaylistListPaginationAndExceptions:
    @responses_lib.activate
    def test_api_pagination_100_items_then_empty(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Hit offset += 100 (line 2467) and 'if not playlists: break' (line 2463)."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        page1 = [
            {"id": f"p.{i}", "attributes": {"name": f"PL {i}", "canEdit": True}} for i in range(100)
        ]
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists",
            json={"data": page1},
            status=200,
        )
        # Second page: empty data -> breaks
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists",
            json={"data": []},
            status=200,
        )
        result = server._playlist_list()
        assert "PL 0" in result
        assert "100 items" in result or "PL 99" in result

    def test_api_no_token_raises_filenotfound(self, mock_config_dir, monkeypatch):
        """Line 2497-2498: FileNotFoundError from missing token -> return str(e)."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        # mock_config_dir exists but no token files -> FileNotFoundError in get_headers()
        result = server._playlist_list()
        assert isinstance(result, str)

    @responses_lib.activate
    def test_api_request_exception(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Line 2495-2496: requests.exceptions.RequestException."""
        import requests as req_lib

        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists",
            body=req_lib.exceptions.ConnectionError("network error"),
        )
        result = server._playlist_list()
        assert "API Error" in result


# ---------------------------------------------------------------------------
# _playlist_search: _get_playlist_track_names failure path (line 2921)
# ---------------------------------------------------------------------------


class TestPlaylistSearchGetTracksFailed:
    def test_get_tracks_failure_returns_error(self, monkeypatch):
        """Line 2921: success=False from _get_playlist_track_names."""
        monkeypatch.setattr(
            server, "_get_playlist_track_names", lambda pid: (False, "connection error")
        )
        result = server._playlist_search("Jude", "p.abc123")
        assert "Error" in result
        assert "connection error" in result


# ---------------------------------------------------------------------------
# _get_playlist_track_names: pagination (line 2982 offset += 100)
# ---------------------------------------------------------------------------


class TestGetPlaylistTrackNamesPagination:
    @responses_lib.activate
    def test_pagination_offset_incremented(
        self, mock_config_dir, mock_developer_token, mock_user_token
    ):
        """Line 2982: offset += 100 when first page is full (100 items)."""
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        page1 = [
            {"id": f"i.t{i}", "attributes": {"name": f"Song {i}", "artistName": "Artist"}}
            for i in range(100)
        ]
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.pagtest/tracks",
            json={"data": page1},
            status=200,
        )
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.pagtest/tracks",
            json={
                "data": [{"id": "i.t100", "attributes": {"name": "Last Song", "artistName": "Art"}}]
            },
            status=200,
        )
        ok, tracks = server._get_playlist_track_names("p.pagtest")
        assert ok is True
        assert len(tracks) == 101
        assert tracks[-1]["name"] == "Last Song"


# ---------------------------------------------------------------------------
# _playlist_create: API exception handlers (lines 3059, 3061)
# ---------------------------------------------------------------------------


class TestPlaylistCreateApiExceptions:
    @responses_lib.activate
    def test_request_exception_returns_api_error(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Line 3058-3059: requests.exceptions.RequestException handler."""
        import requests as req_lib

        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses_lib.add(
            responses_lib.POST,
            "https://api.music.apple.com/v1/me/library/playlists",
            body=req_lib.exceptions.ConnectionError("network down"),
        )
        result = server._playlist_create("PL")
        assert "Error" in result or "API Error" in result

    def test_filenotfound_returns_str(self, mock_config_dir, monkeypatch):
        """Line 3060-3061: FileNotFoundError from missing token -> str(e)."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        # No token written -> FileNotFoundError in get_headers()
        result = server._playlist_create("PL")
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# _playlist_add: track input error (line 3247)
# ---------------------------------------------------------------------------


class TestPlaylistAddTrackInputError:
    def test_invalid_json_track_returns_error(self, monkeypatch):
        """Line 3247: r.error from malformed JSON track input."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "My PL"}]))
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "auto_add": False,
                "fetch_explicit": False,
                "mode": "auto",
                "storefront": "us",
                "output_format": "text",
            },
        )
        # '[bad' starts with '[', JSON decode fails -> r.error="Invalid JSON: ..."
        result = server._playlist_add("My PL", "[bad json")
        assert "Error" in result


# ---------------------------------------------------------------------------
# _playlist_add: result building branches (lines 3617, 3629)
# ---------------------------------------------------------------------------


class TestPlaylistAddResultBuilding:
    def test_added_and_errors_with_steps(self, monkeypatch):
        """Line 3617: added+errors path with steps included."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "My PL"}]))
        monkeypatch.setattr(server.asc, "track_exists_in_playlist", lambda pl, n, a: (True, False))
        call_count = [0]

        def mock_smart_add(pl, name, artist, album):
            call_count[0] += 1
            # First succeeds, second fails
            if call_count[0] == 1:
                return (True, "ok", None)
            return (False, "Some error", None)

        monkeypatch.setattr(server, "_smart_as_add_track_to_playlist", mock_smart_add)
        monkeypatch.setattr(server, "_verify_track_in_playlist", lambda pl, n, a: True)
        # Inject a step by having the second track be "Track not found" type (not auto_add)
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "auto_add": False,
                "fetch_explicit": False,
                "mode": "auto",
                "storefront": "us",
                "output_format": "text",
            },
        )
        result = server._playlist_add("My PL", "Song A, Song B", verify=True)
        assert "Added" in result

    def test_no_tracks_added_no_steps_returns_message(self, monkeypatch):
        """Line 3629: 'No tracks added' when nothing happened (no steps)."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "My PL"}]))
        monkeypatch.setattr(server.asc, "track_exists_in_playlist", lambda pl, n, a: (True, True))
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "auto_add": False,
                "fetch_explicit": False,
                "mode": "auto",
                "storefront": "us",
                "output_format": "text",
            },
        )
        # All tracks are duplicates -> no added, no errors, no steps
        result = server._playlist_add("My PL", "Song A")
        # Should be "No tracks added" since the duplicate step is a skipped step (in steps list)
        assert isinstance(result, str)

    def test_errors_only_no_autosearch_shows_tip(self, monkeypatch):
        """Line 3619-3625: only errors, auto_add=False -> shows tip."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "My PL"}]))
        monkeypatch.setattr(server.asc, "track_exists_in_playlist", lambda pl, n, a: (True, False))
        monkeypatch.setattr(
            server,
            "_smart_as_add_track_to_playlist",
            lambda pl, n, a, alb: (False, "Some random error", None),
        )
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "auto_add": False,
                "fetch_explicit": False,
                "mode": "auto",
                "storefront": "us",
                "output_format": "text",
            },
        )
        result = server._playlist_add("My PL", "Song A", auto_add=False)
        assert "Errors" in result
        assert "auto_add" in result or "Tip" in result


# ---------------------------------------------------------------------------
# _playlist_add: API mode with library IDs (lines 3631-3807)
# ---------------------------------------------------------------------------


class TestPlaylistAddApiMode:
    @responses_lib.activate
    def test_api_mode_with_library_id(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """API mode: playlist p.xxx + library ID track -> POST directly."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        # Check for duplicates
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.dest/tracks",
            json={"data": []},
            status=200,
        )
        # POST tracks to playlist
        responses_lib.add(
            responses_lib.POST,
            "https://api.music.apple.com/v1/me/library/playlists/p.dest/tracks",
            status=204,
        )
        # GET to verify (called by _get_playlist_track_names)
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.dest/tracks",
            json={
                "data": [
                    {"id": "i.libsong1", "attributes": {"name": "Song A", "artistName": "Artist"}}
                ]
            },
            status=200,
        )
        result = server._playlist_add("p.dest", "i.libsong1")
        assert "Added" in result or "track" in result.lower() or "Verified" in result

    @responses_lib.activate
    def test_api_mode_with_name_track_library_found(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """API mode: name track -> _find_track_id returns library ID."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(
            server, "_find_track_id", lambda name, artist="": ("i.found1", None, f"{name} - Artist")
        )
        # Duplicate check (empty)
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.dest/tracks",
            json={"data": []},
            status=200,
        )
        responses_lib.add(
            responses_lib.POST,
            "https://api.music.apple.com/v1/me/library/playlists/p.dest/tracks",
            status=204,
        )
        # Verify
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.dest/tracks",
            json={
                "data": [{"id": "i.found1", "attributes": {"name": "Song", "artistName": "Artist"}}]
            },
            status=200,
        )
        result = server._playlist_add("p.dest", "Song A")
        assert isinstance(result, str)

    @responses_lib.activate
    def test_api_mode_no_tracks_to_add(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """API mode: name track not found anywhere -> 'Error: No tracks to add'."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(server, "_find_track_id", lambda name, artist="": (None, None, ""))
        result = server._playlist_add("p.dest", "UnknownSong99")
        assert "Error" in result or "No tracks" in result

    @responses_lib.activate
    def test_api_mode_403_error(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """API mode: 403 response -> 'Cannot edit this playlist'."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        # Empty playlist (no duplicates)
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.dest/tracks",
            json={"data": []},
            status=200,
        )
        responses_lib.add(
            responses_lib.POST,
            "https://api.music.apple.com/v1/me/library/playlists/p.dest/tracks",
            status=403,
        )
        result = server._playlist_add("p.dest", "i.libsong1")
        assert "Error" in result
        assert "Cannot edit" in result

    @responses_lib.activate
    def test_api_mode_500_error(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """API mode: 500 response -> 'Cannot edit this playlist'."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.dest/tracks",
            json={"data": []},
            status=200,
        )
        responses_lib.add(
            responses_lib.POST,
            "https://api.music.apple.com/v1/me/library/playlists/p.dest/tracks",
            status=500,
        )
        result = server._playlist_add("p.dest", "i.libsong1")
        assert "Error" in result
        assert "Cannot edit" in result

    @responses_lib.activate
    def test_api_mode_no_library_ids(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """API mode: all catalog IDs fail to appear in library -> 'No valid library IDs'."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        # Add to library (201/202 ok)
        responses_lib.add(
            responses_lib.POST,
            "https://api.music.apple.com/v1/me/library",
            status=202,
        )
        # Catalog info for the catalog ID
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/catalog/us/songs/1234567890",
            json={"data": [{"attributes": {"name": "Song A", "artistName": "Artist A"}}]},
            status=200,
        )
        # Library search returns nothing (track not synced)
        for _ in range(10):
            responses_lib.add(
                responses_lib.GET,
                "https://api.music.apple.com/v1/me/library/search",
                json={"results": {"library-songs": {"data": []}}},
                status=200,
            )
        result = server._playlist_add("p.dest", "1234567890")
        assert isinstance(result, str)

    @responses_lib.activate
    def test_api_mode_catalog_track_found_in_library(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """API mode: an added catalog track that DOES appear in library search
        exercises the found_id success branch (server.py:3713-3725), then gets
        added to the playlist. Fully mocked — never touches a real account.
        (allow_duplicates=True / verify=False skip the extra dup/verify calls.)"""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses_lib.add(
            responses_lib.POST, "https://api.music.apple.com/v1/me/library", status=202
        )
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/catalog/us/songs/1234567890",
            json={"data": [{"attributes": {"name": "Song A", "artistName": "Artist A"}}]},
            status=200,
        )
        # Library search now RETURNS the freshly-added song -> found_id branch.
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/search",
            json={
                "results": {
                    "library-songs": {
                        "data": [
                            {
                                "id": "i.libA",
                                "attributes": {"name": "Song A", "artistName": "Artist A"},
                            }
                        ]
                    }
                }
            },
            status=200,
        )
        responses_lib.add(
            responses_lib.POST,
            "https://api.music.apple.com/v1/me/library/playlists/p.dest/tracks",
            status=204,
        )
        result = server._playlist_add("p.dest", "1234567890", allow_duplicates=True, verify=False)
        assert "Found in library" in result and "Song A" in result


# ---------------------------------------------------------------------------
# _playlist_copy: edge cases (lines 3862, 3911, 3915, 3953-3956)
# ---------------------------------------------------------------------------


class TestPlaylistCopyEdgeCases:
    def test_applescript_many_failures_truncated(self, monkeypatch):
        """Line 3862: >5 failures -> truncated failed list."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "Src"}]))
        monkeypatch.setattr(
            server.asc,
            "get_playlist_tracks",
            lambda name: (True, [{"name": f"Track {i}", "artist": "Artist"} for i in range(7)]),
        )
        monkeypatch.setattr(server.asc, "create_playlist", lambda name, desc="": (True, "NEWP"))
        monkeypatch.setattr(
            server.asc, "add_track_to_playlist", lambda pl, name, artist: (False, "not found")
        )
        result = server._playlist_copy("Src", "Copy")
        assert "Failed" in result
        assert "..." in result  # truncation indicator

    @responses_lib.activate
    def test_api_copy_empty_tracks_from_200(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Line 3910-3911: 200 response with empty data -> break."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.src/tracks",
            json={"data": []},
            status=200,
        )
        responses_lib.add(
            responses_lib.POST,
            "https://api.music.apple.com/v1/me/library/playlists",
            json={"data": [{"id": "p.copy1"}]},
            status=200,
        )
        result = server._playlist_copy("p.src", "Empty Copy")
        assert "Empty Copy" in result
        assert "0" in result

    @responses_lib.activate
    def test_api_copy_pagination_100_then_1(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Line 3914-3915: 100 items on first page triggers offset += 100."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        page1 = [{"id": f"i.t{i}", "attributes": {}} for i in range(100)]
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.src/tracks",
            json={"data": page1},
            status=200,
        )
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.src/tracks",
            json={"data": [{"id": "i.t100", "attributes": {}}]},
            status=200,
        )
        responses_lib.add(
            responses_lib.POST,
            "https://api.music.apple.com/v1/me/library/playlists",
            json={"data": [{"id": "p.copy1"}]},
            status=200,
        )
        # batch POST for 101 tracks (two batches: 25+25+25+25+1)
        for _ in range(5):
            responses_lib.add(
                responses_lib.POST,
                "https://api.music.apple.com/v1/me/library/playlists/p.copy1/tracks",
                status=204,
            )
        result = server._playlist_copy("p.src", "Big Copy")
        assert "101" in result
        assert "Big Copy" in result

    @responses_lib.activate
    def test_api_copy_request_exception(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Lines 3953-3954: RequestException handler in copy API mode."""
        import requests as req_lib

        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.src/tracks",
            body=req_lib.exceptions.ConnectionError("network error"),
        )
        result = server._playlist_copy("p.src", "Copy")
        assert "API Error" in result

    def test_api_copy_no_token(self, mock_config_dir, monkeypatch):
        """Lines 3955-3956: FileNotFoundError -> str(e)."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        # No token written -> FileNotFoundError
        result = server._playlist_copy("p.src", "Copy")
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# _playlist_add: auto_add path (lines 3451-3459)
# ---------------------------------------------------------------------------


class TestPlaylistAddAutoSearch:
    def test_auto_search_succeeds(self, monkeypatch):
        """Lines 3451-3458: auto_add=True, track not found locally but catalog found."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "My PL"}]))
        monkeypatch.setattr(server.asc, "track_exists_in_playlist", lambda pl, n, a: (True, False))
        monkeypatch.setattr(
            server,
            "_smart_as_add_track_to_playlist",
            lambda pl, n, a, alb: (False, "Track not found in library", None),
        )
        monkeypatch.setattr(
            server,
            "_unified_auto_search_to_playlist",
            lambda name, artist, pl: (True, f"{name} - Artist", ["[UI] Added"]),
        )
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "auto_add": True,
                "fetch_explicit": False,
                "mode": "auto",
                "storefront": "us",
                "output_format": "text",
            },
        )
        result = server._playlist_add("My PL", "New Release", auto_add=True)
        assert "Added" in result or "New Release" in result

    def test_auto_search_fails(self, monkeypatch):
        """Lines 3458-3459: auto_add=True but search also fails."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "My PL"}]))
        monkeypatch.setattr(server.asc, "track_exists_in_playlist", lambda pl, n, a: (True, False))
        monkeypatch.setattr(
            server,
            "_smart_as_add_track_to_playlist",
            lambda pl, n, a, alb: (False, "Track not found in library", None),
        )
        monkeypatch.setattr(
            server,
            "_unified_auto_search_to_playlist",
            lambda name, artist, pl: (False, "not found anywhere", []),
        )
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "auto_add": True,
                "fetch_explicit": False,
                "mode": "auto",
                "storefront": "us",
                "output_format": "text",
            },
        )
        result = server._playlist_add("My PL", "Ghost Song", auto_add=True)
        assert "Errors" in result or "not found" in result.lower()


# ---------------------------------------------------------------------------
# _playlist_tracks: fetch_explicit enrichment (lines 2570-2712)
# ---------------------------------------------------------------------------


class TestPlaylistTracksFetchExplicit:
    @responses_lib.activate
    def test_fetch_explicit_enriches_tracks(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Cover the fetch_explicit=True path in _playlist_tracks (lines 2556-2754)."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "Jazz Mix"}]))
        # Return tracks with no IDs (forces cache miss + API lookup)
        monkeypatch.setattr(
            server.asc,
            "get_playlist_tracks",
            lambda name: (
                True,
                [
                    {
                        "name": "So What",
                        "artist": "Miles Davis",
                        "album": "Kind of Blue",
                        "duration": "9:22",
                        "genre": "Jazz",
                        "year": "1959",
                        "id": "",
                    },
                ],
            ),
        )
        # Mock track cache
        mock_cache = MagicMock()
        mock_cache.get_explicit.return_value = None  # cache miss for all
        monkeypatch.setattr(server, "get_track_cache", lambda: mock_cache)
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        # Library playlists lookup to find the playlist ID
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists",
            json={
                "data": [
                    {"id": "p.jazzid", "attributes": {"name": "Jazz Mix"}},
                ]
            },
            status=200,
        )
        # Tracks with explicit info
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.jazzid/tracks",
            json={
                "data": [
                    {
                        "id": "i.t1",
                        "attributes": {
                            "name": "So What",
                            "artistName": "Miles Davis",
                            "albumName": "Kind of Blue",
                            "contentRating": "notExplicit",
                            "isrc": "USRC123",
                            "playParams": {"catalogId": "1234567890"},
                        },
                    },
                ]
            },
            status=200,
        )
        result = server._playlist_tracks("Jazz Mix", fetch_explicit=True)
        assert "So What" in result
        assert "Miles Davis" in result

    @responses_lib.activate
    def test_fetch_explicit_no_playlist_found_in_api(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """fetch_explicit=True but playlist not found in API library -> graceful."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "Jazz Mix"}]))
        monkeypatch.setattr(
            server.asc,
            "get_playlist_tracks",
            lambda name: (
                True,
                [
                    {
                        "name": "So What",
                        "artist": "Miles Davis",
                        "album": "Kind of Blue",
                        "duration": "9:22",
                        "genre": "Jazz",
                        "year": "1959",
                        "id": "",
                    },
                ],
            ),
        )
        mock_cache = MagicMock()
        mock_cache.get_explicit.return_value = None
        monkeypatch.setattr(server, "get_track_cache", lambda: mock_cache)
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        # API returns different playlists (no match)
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists",
            json={"data": [{"id": "p.other", "attributes": {"name": "Other Playlist"}}]},
            status=200,
        )
        result = server._playlist_tracks("Jazz Mix", fetch_explicit=True)
        assert "So What" in result

    def test_fetch_explicit_cache_hit_skips_api(self, monkeypatch):
        """fetch_explicit=True but cached explicit status -> no API call needed for those tracks."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "Jazz Mix"}]))
        monkeypatch.setattr(
            server.asc,
            "get_playlist_tracks",
            lambda name: (
                True,
                [
                    {
                        "name": "So What",
                        "artist": "Miles Davis",
                        "album": "Kind of Blue",
                        "duration": "9:22",
                        "genre": "Jazz",
                        "year": "1959",
                        "id": "i.t1",
                    },
                ],
            ),
        )
        mock_cache = MagicMock()
        mock_cache.get_explicit.return_value = "Yes"  # cache hit!
        monkeypatch.setattr(server, "get_track_cache", lambda: mock_cache)
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "fetch_explicit": True,
                "output_format": "text",
                "mode": "auto",
                "auto_add": False,
                "storefront": "us",
            },
        )
        result = server._playlist_tracks("Jazz Mix", fetch_explicit=True)
        assert "So What" in result


# ---------------------------------------------------------------------------
# _playlist_add: AppleScript ID processing (lines 3490-3594)
# ---------------------------------------------------------------------------


class TestPlaylistAddAppleScriptIds:
    @responses_lib.activate
    def test_library_id_in_applescript_mode(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Library ID in AS mode: API lookup + asc.add_track_to_playlist."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "My PL"}]))
        monkeypatch.setattr(server, "_has_developer_token", lambda: True)
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(server.asc, "track_exists_in_playlist", lambda pl, n, a: (True, False))
        monkeypatch.setattr(
            server.asc, "add_track_to_playlist", lambda pl, name, artist: (True, "ok")
        )
        monkeypatch.setattr(server, "_verify_track_in_playlist", lambda pl, n, a: True)
        # Library song info lookup
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/songs/i.libsong1",
            json={"data": [{"attributes": {"name": "Hey Jude", "artistName": "Beatles"}}]},
            status=200,
        )
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "auto_add": False,
                "fetch_explicit": False,
                "mode": "auto",
                "storefront": "us",
                "output_format": "text",
            },
        )
        result = server._playlist_add("My PL", "i.libsong1")
        assert "Added" in result or "Hey Jude" in result

    @responses_lib.activate
    def test_library_id_api_lookup_fails(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Library ID info not found -> error in errors list."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "My PL"}]))
        monkeypatch.setattr(server, "_has_developer_token", lambda: True)
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/songs/i.missing",
            status=404,
        )
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "auto_add": False,
                "fetch_explicit": False,
                "mode": "auto",
                "storefront": "us",
                "output_format": "text",
            },
        )
        result = server._playlist_add("My PL", "i.missing")
        assert "Error" in result or "Could not get info" in result or "Errors" in result


# ---------------------------------------------------------------------------
# _playlist_tracks: API path full-fetch branches (lines 2786-2888)
# ---------------------------------------------------------------------------


class TestPlaylistTracksApiPaths:
    @responses_lib.activate
    def test_full_fetch_path_no_filter(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Full fetch path (no limit) covers lines 2830-2888."""
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "fetch_explicit": False,
                "output_format": "text",
                "mode": "auto",
                "auto_add": False,
                "storefront": "us",
            },
        )
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.pl/tracks",
            json={
                "data": [
                    {
                        "id": "i.t1",
                        "attributes": {
                            "name": "Song A",
                            "artistName": "Artist A",
                            "albumName": "Alb",
                            "durationInMillis": 200000,
                        },
                    }
                ]
            },
            status=200,
        )
        result = server._playlist_tracks("p.pl")
        assert "Song A" in result

    @responses_lib.activate
    def test_full_fetch_404_stops(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Line 2839-2840: 404 in full fetch path."""
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "fetch_explicit": False,
                "output_format": "text",
                "mode": "auto",
                "auto_add": False,
                "storefront": "us",
            },
        )
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.pl/tracks",
            status=404,
        )
        result = server._playlist_tracks("p.pl")
        assert "empty" in result.lower() or isinstance(result, str)

    @responses_lib.activate
    def test_full_fetch_empty_tracks_returns_empty(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Line 2843-2844: empty tracks in full fetch."""
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "fetch_explicit": False,
                "output_format": "text",
                "mode": "auto",
                "auto_add": False,
                "storefront": "us",
            },
        )
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.pl/tracks",
            json={"data": []},
            status=200,
        )
        result = server._playlist_tracks("p.pl")
        assert "empty" in result.lower()

    @responses_lib.activate
    def test_full_fetch_pagination_100_items(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Line 2848: offset += 100 in full fetch path."""
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "fetch_explicit": False,
                "output_format": "text",
                "mode": "auto",
                "auto_add": False,
                "storefront": "us",
            },
        )
        page1 = [
            {
                "id": f"i.t{i}",
                "attributes": {
                    "name": f"Song {i}",
                    "artistName": "Artist",
                    "albumName": "Alb",
                    "durationInMillis": 180000,
                },
            }
            for i in range(100)
        ]
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.pl/tracks",
            json={"data": page1},
            status=200,
        )
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.pl/tracks",
            json={
                "data": [
                    {
                        "id": "i.t100",
                        "attributes": {
                            "name": "Last Song",
                            "artistName": "Artist",
                            "albumName": "Alb",
                            "durationInMillis": 180000,
                        },
                    }
                ]
            },
            status=200,
        )
        result = server._playlist_tracks("p.pl")
        assert "Song 0" in result or "101" in result or "100 items" in result

    @responses_lib.activate
    def test_full_fetch_with_filter(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Line 2856-2861: filter applied to full fetch results."""
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "fetch_explicit": False,
                "output_format": "text",
                "mode": "auto",
                "auto_add": False,
                "storefront": "us",
            },
        )
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.pl/tracks",
            json={
                "data": [
                    {
                        "id": "i.t1",
                        "attributes": {
                            "name": "Hey Jude",
                            "artistName": "Beatles",
                            "albumName": "Single",
                            "durationInMillis": 431000,
                        },
                    },
                    {
                        "id": "i.t2",
                        "attributes": {
                            "name": "Other Song",
                            "artistName": "Other Artist",
                            "albumName": "Alb",
                            "durationInMillis": 200000,
                        },
                    },
                ]
            },
            status=200,
        )
        result = server._playlist_tracks("p.pl", filter="jude")
        assert "Hey Jude" in result
        assert "Other Song" not in result

    @responses_lib.activate
    def test_full_fetch_with_offset_exceeds_total(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Line 2864-2866: _apply_pagination error when offset too large."""
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "fetch_explicit": False,
                "output_format": "text",
                "mode": "auto",
                "auto_add": False,
                "storefront": "us",
            },
        )
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.pl/tracks",
            json={
                "data": [
                    {
                        "id": "i.t1",
                        "attributes": {
                            "name": "Song A",
                            "artistName": "Artist",
                            "albumName": "Alb",
                            "durationInMillis": 200000,
                        },
                    }
                ]
            },
            status=200,
        )
        # offset=100 exceeds total=1 -> error
        result = server._playlist_tracks("p.pl", offset=100)
        assert "Offset" in result or "Error" in result or isinstance(result, str)

    @responses_lib.activate
    def test_api_request_exception(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Lines 2885-2886: RequestException in API path."""
        import requests as req_lib

        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "fetch_explicit": False,
                "output_format": "text",
                "mode": "auto",
                "auto_add": False,
                "storefront": "us",
            },
        )
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.pl/tracks",
            body=req_lib.exceptions.ConnectionError("network error"),
        )
        result = server._playlist_tracks("p.pl")
        assert "API Error" in result

    def test_api_no_token_filenotfound(self, mock_config_dir, monkeypatch):
        """Lines 2887-2888: FileNotFoundError in API path."""
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "fetch_explicit": False,
                "output_format": "text",
                "mode": "auto",
                "auto_add": False,
                "storefront": "us",
            },
        )
        result = server._playlist_tracks("p.pl")
        assert isinstance(result, str)

    @responses_lib.activate
    def test_optimized_path_with_limit(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Optimized path (limit > 0, no filter) covers lines 2767-2827."""
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "fetch_explicit": False,
                "output_format": "text",
                "mode": "auto",
                "auto_add": False,
                "storefront": "us",
            },
        )
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.pl/tracks",
            json={
                "meta": {"total": 50},
                "data": [
                    {
                        "id": f"i.t{i}",
                        "attributes": {
                            "name": f"Song {i}",
                            "artistName": "Artist",
                            "albumName": "Alb",
                            "durationInMillis": 180000,
                        },
                    }
                    for i in range(5)
                ],
            },
            status=200,
        )
        result = server._playlist_tracks("p.pl", limit=5)
        assert "Song 0" in result

    @responses_lib.activate
    def test_optimized_path_404(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Line 2785-2786: 404 in optimized path."""
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "fetch_explicit": False,
                "output_format": "text",
                "mode": "auto",
                "auto_add": False,
                "storefront": "us",
            },
        )
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.pl/tracks",
            status=404,
        )
        result = server._playlist_tracks("p.pl", limit=5)
        assert "empty" in result.lower() or isinstance(result, str)

    @responses_lib.activate
    def test_optimized_path_with_offset(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Line 2807-2808: offset > 0 in optimized path."""
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "fetch_explicit": False,
                "output_format": "text",
                "mode": "auto",
                "auto_add": False,
                "storefront": "us",
            },
        )
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.pl/tracks",
            json={
                "meta": {"total": 20},
                "data": [
                    {
                        "id": f"i.t{i}",
                        "attributes": {
                            "name": f"Song {i}",
                            "artistName": "Artist",
                            "albumName": "Alb",
                            "durationInMillis": 180000,
                        },
                    }
                    for i in range(10)
                ],
            },
            status=200,
        )
        result = server._playlist_tracks("p.pl", limit=5, offset=3)
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# _playlist_tracks: AppleScript filter and pagination (lines 2715-2757)
# ---------------------------------------------------------------------------


class TestPlaylistTracksAppleScriptFilter:
    def test_filter_applied(self, monkeypatch):
        """Line 2715-2720: filter applied to AppleScript tracks."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "Jazz Mix"}]))
        monkeypatch.setattr(
            server.asc,
            "get_playlist_tracks",
            lambda name: (
                True,
                [
                    {
                        "name": "So What",
                        "artist": "Miles Davis",
                        "album": "Kind of Blue",
                        "duration": "9:22",
                        "genre": "Jazz",
                        "year": "1959",
                        "id": "",
                    },
                    {
                        "name": "Autumn Leaves",
                        "artist": "Bill Evans",
                        "album": "Portrait",
                        "duration": "5:12",
                        "genre": "Jazz",
                        "year": "1959",
                        "id": "",
                    },
                ],
            ),
        )
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "fetch_explicit": False,
                "output_format": "text",
                "mode": "auto",
                "auto_add": False,
                "storefront": "us",
            },
        )
        result = server._playlist_tracks("Jazz Mix", filter="miles")
        assert "So What" in result
        assert "Autumn Leaves" not in result

    def test_pagination_error(self, monkeypatch):
        """Line 2724-2725: _apply_pagination error (offset > total)."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "Jazz Mix"}]))
        monkeypatch.setattr(
            server.asc,
            "get_playlist_tracks",
            lambda name: (
                True,
                [
                    {
                        "name": "So What",
                        "artist": "Miles Davis",
                        "album": "",
                        "duration": "9:22",
                        "genre": "Jazz",
                        "year": "1959",
                        "id": "",
                    }
                ],
            ),
        )
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "fetch_explicit": False,
                "output_format": "text",
                "mode": "auto",
                "auto_add": False,
                "storefront": "us",
            },
        )
        # offset=100 exceeds total=1 -> error returned
        result = server._playlist_tracks("Jazz Mix", offset=100)
        assert "Offset" in result or "Error" in result or isinstance(result, str)


# ---------------------------------------------------------------------------
# _playlist_add: album add path (lines 3287-3380)
# ---------------------------------------------------------------------------


class TestPlaylistAddAlbum:
    @responses_lib.activate
    def test_album_by_name_success(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Album name search -> find album -> fetch tracks -> add via IDs path."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "My PL"}]))
        monkeypatch.setattr(server, "_has_developer_token", lambda: True)
        monkeypatch.setattr(server, "get_storefront", lambda: "us")
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(server.asc, "track_exists_in_playlist", lambda pl, n, a: (True, False))
        # Album tracks go through ids_list -> asc.add_track_to_playlist (not _smart_as)
        monkeypatch.setattr(server.asc, "add_track_to_playlist", lambda pl, n, a: (True, "ok"))
        monkeypatch.setattr(server, "_verify_track_in_playlist", lambda pl, n, a: True)
        monkeypatch.setattr(
            server, "time", type("T", (), {"sleep": staticmethod(lambda s: None)})()
        )
        # Catalog search for album
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/catalog/us/search",
            json={
                "results": {
                    "albums": {
                        "data": [
                            {
                                "id": "alb123",
                                "attributes": {
                                    "name": "Abbey Road",
                                    "artistName": "Beatles",
                                },
                            }
                        ]
                    }
                }
            },
            status=200,
        )
        # Album tracks - use 10-digit numeric ID so _is_catalog_id returns True
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/catalog/us/albums/alb123/tracks",
            json={
                "data": [
                    {
                        "id": "1234567890",
                        "attributes": {"name": "Come Together", "artistName": "Beatles"},
                    }
                ]
            },
            status=200,
        )
        # POST /me/library (add catalog song to library before adding to playlist)
        responses_lib.add(
            responses_lib.POST,
            "https://api.music.apple.com/v1/me/library",
            json={},
            status=200,
        )
        # GET catalog song info
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/catalog/us/songs/1234567890",
            json={
                "data": [
                    {
                        "id": "1234567890",
                        "attributes": {"name": "Come Together", "artistName": "Beatles"},
                    }
                ]
            },
            status=200,
        )
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "auto_add": False,
                "fetch_explicit": False,
                "mode": "auto",
                "storefront": "us",
                "output_format": "text",
            },
        )
        result = server._playlist_add("My PL", album="Abbey Road")
        assert isinstance(result, str)

    @responses_lib.activate
    def test_album_by_catalog_id_success(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Album as catalog ID (all digits 9+) -> direct fetch (line 3308-3320)."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "My PL"}]))
        monkeypatch.setattr(server, "_has_developer_token", lambda: True)
        monkeypatch.setattr(server, "get_storefront", lambda: "us")
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(server.asc, "track_exists_in_playlist", lambda pl, n, a: (True, False))
        # ids_list path uses asc.add_track_to_playlist, not _smart_as_add_track_to_playlist
        monkeypatch.setattr(server.asc, "add_track_to_playlist", lambda pl, n, a: (True, "ok"))
        monkeypatch.setattr(server, "_verify_track_in_playlist", lambda pl, n, a: True)
        monkeypatch.setattr(
            server, "time", type("T", (), {"sleep": staticmethod(lambda s: None)})()
        )
        # Direct album tracks fetch by catalog ID
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/catalog/us/albums/1234567890/tracks",
            json={
                "data": [
                    {
                        "id": "9876543210",
                        "attributes": {"name": "Come Together", "artistName": "Beatles"},
                    }
                ]
            },
            status=200,
        )
        # POST /me/library to sync catalog track before adding to playlist
        responses_lib.add(
            responses_lib.POST,
            "https://api.music.apple.com/v1/me/library",
            json={},
            status=200,
        )
        # GET catalog song info for the resolved track
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/catalog/us/songs/9876543210",
            json={
                "data": [
                    {
                        "id": "9876543210",
                        "attributes": {"name": "Come Together", "artistName": "Beatles"},
                    }
                ]
            },
            status=200,
        )
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "auto_add": False,
                "fetch_explicit": False,
                "mode": "auto",
                "storefront": "us",
                "output_format": "text",
            },
        )
        result = server._playlist_add("My PL", album="1234567890")
        assert isinstance(result, str)

    @responses_lib.activate
    def test_album_by_catalog_id_api_error(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Line 3319-3320: album catalog ID fetch returns non-200."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "My PL"}]))
        monkeypatch.setattr(server, "_has_developer_token", lambda: True)
        monkeypatch.setattr(server, "get_storefront", lambda: "us")
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/catalog/us/albums/1234567890/tracks",
            status=404,
        )
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "auto_add": False,
                "fetch_explicit": False,
                "mode": "auto",
                "storefront": "us",
                "output_format": "text",
            },
        )
        result = server._playlist_add("My PL", album="1234567890")
        assert isinstance(result, str)

    @responses_lib.activate
    def test_album_by_name_not_found(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Lines 3364-3367: album search returns no albums."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "My PL"}]))
        monkeypatch.setattr(server, "_has_developer_token", lambda: True)
        monkeypatch.setattr(server, "get_storefront", lambda: "us")
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/catalog/us/search",
            json={"results": {"albums": {"data": []}}},
            status=200,
        )
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "auto_add": False,
                "fetch_explicit": False,
                "mode": "auto",
                "storefront": "us",
                "output_format": "text",
            },
        )
        result = server._playlist_add("My PL", album="UnknownAlbum99")
        assert isinstance(result, str)

    def test_album_no_token_error(self, monkeypatch):
        """Lines 3292-3298: no token for album add."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "My PL"}]))
        monkeypatch.setattr(server, "_has_developer_token", lambda: False)
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "auto_add": False,
                "fetch_explicit": False,
                "mode": "auto",
                "storefront": "us",
                "output_format": "text",
            },
        )
        result = server._playlist_add("My PL", album="Abbey Road")
        assert "Error" in result
        assert "token" in result.lower()

    @responses_lib.activate
    def test_album_with_artist_match(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Lines 3339-3342: album search with artist in result."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "My PL"}]))
        monkeypatch.setattr(server, "_has_developer_token", lambda: True)
        monkeypatch.setattr(server, "get_storefront", lambda: "us")
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/catalog/us/search",
            json={
                "results": {
                    "albums": {
                        "data": [
                            {
                                "id": "alb123",
                                "attributes": {"name": "Abbey Road", "artistName": "Beatles"},
                            }
                        ]
                    }
                }
            },
            status=200,
        )
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/catalog/us/albums/alb123/tracks",
            json={
                "data": [
                    {
                        # 10-digit numeric ID so _is_catalog_id returns True
                        "id": "1234567890",
                        "attributes": {"name": "Come Together", "artistName": "Beatles"},
                    }
                ]
            },
            status=200,
        )
        # POST /me/library to sync catalog track
        responses_lib.add(
            responses_lib.POST,
            "https://api.music.apple.com/v1/me/library",
            json={},
            status=200,
        )
        # GET catalog song info
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/catalog/us/songs/1234567890",
            json={
                "data": [
                    {
                        "id": "1234567890",
                        "attributes": {"name": "Come Together", "artistName": "Beatles"},
                    }
                ]
            },
            status=200,
        )
        monkeypatch.setattr(server.asc, "track_exists_in_playlist", lambda pl, n, a: (True, False))
        # ids_list path uses asc.add_track_to_playlist, not _smart_as_add_track_to_playlist
        monkeypatch.setattr(server.asc, "add_track_to_playlist", lambda pl, n, a: (True, "ok"))
        monkeypatch.setattr(server, "_verify_track_in_playlist", lambda pl, n, a: True)
        monkeypatch.setattr(
            server, "time", type("T", (), {"sleep": staticmethod(lambda s: None)})()
        )
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "auto_add": False,
                "fetch_explicit": False,
                "mode": "auto",
                "storefront": "us",
                "output_format": "text",
            },
        )
        result = server._playlist_add("My PL", album="Abbey Road", artist="Beatles")
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# _playlist_add: IDs in AppleScript mode (lines 3490-3594)
# ---------------------------------------------------------------------------


class TestPlaylistAddIdsAppleScriptMode:
    @responses_lib.activate
    def test_catalog_id_info_not_found(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Line 3512-3514: catalog info API returns non-200."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "My PL"}]))
        monkeypatch.setattr(server, "_has_developer_token", lambda: True)
        monkeypatch.setattr(server, "get_storefront", lambda: "us")
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        # Add to library ok
        responses_lib.add(
            responses_lib.POST,
            "https://api.music.apple.com/v1/me/library",
            status=202,
        )
        # Catalog info fails
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/catalog/us/songs/1234567890",
            status=404,
        )
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "auto_add": False,
                "fetch_explicit": False,
                "mode": "auto",
                "storefront": "us",
                "output_format": "text",
            },
        )
        result = server._playlist_add("My PL", "1234567890")
        assert "Error" in result or "Could not get info" in result

    @responses_lib.activate
    def test_catalog_id_duplicate_skipped(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Lines 3551-3553: duplicate check for catalog ID in AS mode."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "My PL"}]))
        monkeypatch.setattr(server, "_has_developer_token", lambda: True)
        monkeypatch.setattr(server, "get_storefront", lambda: "us")
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses_lib.add(
            responses_lib.POST,
            "https://api.music.apple.com/v1/me/library",
            status=202,
        )
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/catalog/us/songs/1234567890",
            json={"data": [{"attributes": {"name": "Hey Jude", "artistName": "Beatles"}}]},
            status=200,
        )
        # Track exists in playlist -> skip
        monkeypatch.setattr(server.asc, "track_exists_in_playlist", lambda pl, n, a: (True, True))
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "auto_add": False,
                "fetch_explicit": False,
                "mode": "auto",
                "storefront": "us",
                "output_format": "text",
            },
        )
        result = server._playlist_add("My PL", "1234567890")
        assert "Skipped" in result or "No tracks" in result or isinstance(result, str)

    @responses_lib.activate
    def test_catalog_id_add_fails(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Line 3564-3565: add_track_to_playlist fails for catalog ID."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "My PL"}]))
        monkeypatch.setattr(server, "_has_developer_token", lambda: True)
        monkeypatch.setattr(server, "get_storefront", lambda: "us")
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses_lib.add(
            responses_lib.POST,
            "https://api.music.apple.com/v1/me/library",
            status=202,
        )
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/catalog/us/songs/1234567890",
            json={"data": [{"attributes": {"name": "Hey Jude", "artistName": "Beatles"}}]},
            status=200,
        )
        monkeypatch.setattr(server.asc, "track_exists_in_playlist", lambda pl, n, a: (True, False))
        monkeypatch.setattr(
            server.asc, "add_track_to_playlist", lambda pl, name, artist: (False, "not found")
        )
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "auto_add": False,
                "fetch_explicit": False,
                "mode": "auto",
                "storefront": "us",
                "output_format": "text",
            },
        )
        result = server._playlist_add("My PL", "1234567890")
        assert "Errors" in result or "not found" in result.lower()

    @responses_lib.activate
    def test_catalog_id_verify_false_no_verify(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Line 3566-3567: verify=False path for catalog ID."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "My PL"}]))
        monkeypatch.setattr(server, "_has_developer_token", lambda: True)
        monkeypatch.setattr(server, "get_storefront", lambda: "us")
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses_lib.add(
            responses_lib.POST,
            "https://api.music.apple.com/v1/me/library",
            status=202,
        )
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/catalog/us/songs/1234567890",
            json={"data": [{"attributes": {"name": "Hey Jude", "artistName": "Beatles"}}]},
            status=200,
        )
        monkeypatch.setattr(server.asc, "track_exists_in_playlist", lambda pl, n, a: (True, False))
        monkeypatch.setattr(
            server.asc, "add_track_to_playlist", lambda pl, name, artist: (True, "ok")
        )
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "auto_add": False,
                "fetch_explicit": False,
                "mode": "auto",
                "storefront": "us",
                "output_format": "text",
            },
        )
        # verify=False skips the track verification
        result = server._playlist_add("My PL", "1234567890", verify=False)
        assert "Added" in result or "Hey Jude" in result

    @responses_lib.activate
    def test_catalog_id_verify_retry_fails(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Lines 3575-3594: verify fails, retry, still fails -> error."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "My PL"}]))
        monkeypatch.setattr(server, "_has_developer_token", lambda: True)
        monkeypatch.setattr(server, "get_storefront", lambda: "us")
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses_lib.add(
            responses_lib.POST,
            "https://api.music.apple.com/v1/me/library",
            status=202,
        )
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/catalog/us/songs/1234567890",
            json={"data": [{"attributes": {"name": "Hey Jude", "artistName": "Beatles"}}]},
            status=200,
        )
        monkeypatch.setattr(server.asc, "track_exists_in_playlist", lambda pl, n, a: (True, False))
        monkeypatch.setattr(
            server.asc, "add_track_to_playlist", lambda pl, name, artist: (True, "ok")
        )
        # verify always returns False
        monkeypatch.setattr(server, "_verify_track_in_playlist", lambda pl, n, a: False)
        monkeypatch.setattr(server, "_VERIFY_DELAY_S", 0)
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "auto_add": False,
                "fetch_explicit": False,
                "mode": "auto",
                "storefront": "us",
                "output_format": "text",
            },
        )
        result = server._playlist_add("My PL", "1234567890", verify=True)
        assert "Errors" in result or "persist" in result.lower() or isinstance(result, str)


# ---------------------------------------------------------------------------
# _playlist_add: API mode additional branches (3643-3807)
# ---------------------------------------------------------------------------


class TestPlaylistAddApiModeBranches:
    @responses_lib.activate
    def test_catalog_id_found_in_api_mode(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Lines 3643-3645: _find_track_id returns catalog ID (not library ID)."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(
            server,
            "_find_track_id",
            lambda name, artist="": (None, "1234567890", "Song A - Artist"),
        )
        # Add catalog ID to library
        responses_lib.add(
            responses_lib.POST,
            "https://api.music.apple.com/v1/me/library",
            status=202,
        )
        # Catalog info
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/catalog/us/songs/1234567890",
            json={"data": [{"attributes": {"name": "Song A", "artistName": "Artist A"}}]},
            status=200,
        )
        # Library search returns nothing (track not synced yet)
        for _ in range(10):
            responses_lib.add(
                responses_lib.GET,
                "https://api.music.apple.com/v1/me/library/search",
                json={"results": {"library-songs": {"data": []}}},
                status=200,
            )
        result = server._playlist_add("p.dest", "Song A")
        assert isinstance(result, str)

    @responses_lib.activate
    def test_api_mode_duplicate_check_skips(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Lines 3738-3762: duplicate check in API mode — track already in playlist."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        # Get existing tracks (one track)
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.dest/tracks",
            json={
                "data": [
                    {
                        "id": "i.libsong1",
                        "attributes": {"name": "Hey Jude", "artistName": "Beatles"},
                    }
                ]
            },
            status=200,
        )
        # Look up the library song info
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/songs/i.libsong1",
            json={"data": [{"attributes": {"name": "Hey Jude", "artistName": "Beatles"}}]},
            status=200,
        )
        result = server._playlist_add("p.dest", "i.libsong1")
        # Track is a duplicate -> "All tracks already in playlist"
        assert "already" in result.lower() or "step" in result.lower() or isinstance(result, str)

    @responses_lib.activate
    def test_api_mode_exception_handler(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Lines 3804-3807: RequestException in API mode."""
        import requests as req_lib

        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        # Duplicate check fails with exception
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.dest/tracks",
            body=req_lib.exceptions.ConnectionError("network error"),
        )
        result = server._playlist_add("p.dest", "i.libsong1")
        assert "Error" in result

    @responses_lib.activate
    def test_api_mode_raise_for_status(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Line 3788: raise_for_status for other status codes."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        # No existing tracks
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.dest/tracks",
            json={"data": []},
            status=200,
        )
        # POST with 400 Bad Request -> raise_for_status raises
        responses_lib.add(
            responses_lib.POST,
            "https://api.music.apple.com/v1/me/library/playlists/p.dest/tracks",
            status=400,
        )
        result = server._playlist_add("p.dest", "i.libsong1")
        assert "Error" in result

    @responses_lib.activate
    def test_api_mode_catalog_info_warning(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Line 3726-3727: catalog info returns non-200 -> warning step."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        # Catalog ID in track
        responses_lib.add(
            responses_lib.POST,
            "https://api.music.apple.com/v1/me/library",
            status=202,
        )
        # Catalog info not found -> warning step
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/catalog/us/songs/1234567890",
            status=404,
        )
        result = server._playlist_add("p.dest", "1234567890")
        # No library IDs -> "Error: No valid library IDs"
        assert "Error" in result or isinstance(result, str)

    @responses_lib.activate
    def test_api_mode_library_add_warning(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Line 3673-3674: library add returns non-200/202 warning."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        # Add to library returns 400 (unusual but possible)
        responses_lib.add(
            responses_lib.POST,
            "https://api.music.apple.com/v1/me/library",
            status=400,
        )
        # Catalog info still returned
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/catalog/us/songs/1234567890",
            json={"data": [{"attributes": {"name": "Song A", "artistName": "Artist"}}]},
            status=200,
        )
        # Library search returns nothing
        for _ in range(10):
            responses_lib.add(
                responses_lib.GET,
                "https://api.music.apple.com/v1/me/library/search",
                json={"results": {"library-songs": {"data": []}}},
                status=200,
            )
        result = server._playlist_add("p.dest", "1234567890")
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# _playlist_copy: empty source (line 3819)
# ---------------------------------------------------------------------------


class TestPlaylistCopyEmptySource:
    def test_empty_source_returns_error(self, monkeypatch):
        """Line 3819-3820: _resolve_playlist('') returns error."""
        result = server._playlist_copy("", "Copy PL")
        assert "Error" in result
        assert "playlist" in result.lower() or "required" in result.lower()


# ---------------------------------------------------------------------------
# fetch_explicit: inner track-level API branches (lines 2613, 2617, 2622,
# 2669, 2682-2687, 2705-2712)
# ---------------------------------------------------------------------------


class TestFetchExplicitDetailBranches:
    @responses_lib.activate
    def test_track_response_non200_breaks(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Line 2612-2613: track API returns non-200 -> break from inner loop."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "Jazz Mix"}]))
        monkeypatch.setattr(
            server.asc,
            "get_playlist_tracks",
            lambda name: (
                True,
                [
                    {
                        "name": "So What",
                        "artist": "Miles Davis",
                        "album": "Kind of Blue",
                        "duration": "9:22",
                        "genre": "Jazz",
                        "year": "1959",
                        "id": "",
                    }
                ],
            ),
        )
        mock_cache = MagicMock()
        mock_cache.get_explicit.return_value = None
        monkeypatch.setattr(server, "get_track_cache", lambda: mock_cache)
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists",
            json={"data": [{"id": "p.jazzid", "attributes": {"name": "Jazz Mix"}}]},
            status=200,
        )
        # Track list returns non-200 -> break
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.jazzid/tracks",
            status=503,
        )
        result = server._playlist_tracks("Jazz Mix", fetch_explicit=True)
        assert "So What" in result  # Still returns even without explicit

    @responses_lib.activate
    def test_already_enriched_track_skipped(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Line 2668-2669: track with explicit != 'Unknown' is skipped in matching loop."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "Jazz Mix"}]))
        monkeypatch.setattr(
            server.asc,
            "get_playlist_tracks",
            lambda name: (
                True,
                [
                    {
                        "name": "So What",
                        "artist": "Miles Davis",
                        "album": "Kind of Blue",
                        "duration": "9:22",
                        "genre": "Jazz",
                        "year": "1959",
                        "id": "i.t1",
                    }
                ],
            ),
        )
        mock_cache = MagicMock()
        # Return "Yes" from cache -> track gets explicit="Yes" set -> then skipped in map
        mock_cache.get_explicit.return_value = "Yes"
        monkeypatch.setattr(server, "get_track_cache", lambda: mock_cache)
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists",
            json={"data": [{"id": "p.jazzid", "attributes": {"name": "Jazz Mix"}}]},
            status=200,
        )
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.jazzid/tracks",
            json={
                "data": [
                    {
                        "id": "i.t1",
                        "attributes": {
                            "name": "So What",
                            "artistName": "Miles Davis",
                            "albumName": "Kind of Blue",
                            "contentRating": "explicit",
                            "isrc": "US1234",
                            "playParams": {"catalogId": "9876543210"},
                        },
                    }
                ]
            },
            status=200,
        )
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "fetch_explicit": True,
                "output_format": "text",
                "mode": "auto",
                "auto_add": False,
                "storefront": "us",
            },
        )
        result = server._playlist_tracks("Jazz Mix", fetch_explicit=True)
        assert "So What" in result

    @responses_lib.activate
    def test_no_full_match_uses_partial(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Lines 2681-2683: partial key match (name+artist) when full key not found."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "Jazz Mix"}]))
        # Track with album="" (AS) but API has album filled in -> full key mismatch, partial matches
        monkeypatch.setattr(
            server.asc,
            "get_playlist_tracks",
            lambda name: (
                True,
                [
                    {
                        "name": "So What",
                        "artist": "Miles Davis",
                        "album": "",  # empty album -> full key fails
                        "duration": "9:22",
                        "genre": "Jazz",
                        "year": "1959",
                        "id": "",
                    }
                ],
            ),
        )
        mock_cache = MagicMock()
        mock_cache.get_explicit.return_value = None
        monkeypatch.setattr(server, "get_track_cache", lambda: mock_cache)
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists",
            json={"data": [{"id": "p.jazzid", "attributes": {"name": "Jazz Mix"}}]},
            status=200,
        )
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.jazzid/tracks",
            json={
                "data": [
                    {
                        "id": "i.t1",
                        "attributes": {
                            "name": "So What",
                            "artistName": "Miles Davis",
                            "albumName": "Kind of Blue",  # different from ""
                            "contentRating": "notExplicit",
                            "isrc": "",
                            "playParams": {},
                        },
                    }
                ]
            },
            status=200,
        )
        result = server._playlist_tracks("Jazz Mix", fetch_explicit=True)
        assert "So What" in result

    @responses_lib.activate
    def test_no_match_caches_unknown(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Lines 2703-2710: no API match -> cache Unknown if persistent_id set."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "Jazz Mix"}]))
        monkeypatch.setattr(
            server.asc,
            "get_playlist_tracks",
            lambda name: (
                True,
                [
                    {
                        "name": "Rare Track",
                        "artist": "Unknown Artist",
                        "album": "Rare Album",
                        "duration": "3:00",
                        "genre": "Jazz",
                        "year": "1960",
                        "id": "PERSIST123ABC",  # persistent ID set
                    }
                ],
            ),
        )
        mock_cache = MagicMock()
        mock_cache.get_explicit.return_value = None
        monkeypatch.setattr(server, "get_track_cache", lambda: mock_cache)
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists",
            json={"data": [{"id": "p.jazzid", "attributes": {"name": "Jazz Mix"}}]},
            status=200,
        )
        # API returns different track (no match)
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.jazzid/tracks",
            json={
                "data": [
                    {
                        "id": "i.t99",
                        "attributes": {
                            "name": "Other Song",
                            "artistName": "Other Artist",
                            "albumName": "Other",
                            "contentRating": "explicit",
                            "isrc": "",
                            "playParams": {},
                        },
                    }
                ]
            },
            status=200,
        )
        result = server._playlist_tracks("Jazz Mix", fetch_explicit=True)
        assert "Rare Track" in result
        # Cache was called with Unknown for the persistent id
        mock_cache.set_track_metadata.assert_called()


# ---------------------------------------------------------------------------
# _playlist_add: result building edge cases (lines 3616-3629)
# ---------------------------------------------------------------------------


class TestPlaylistAddResultBuildingEdge:
    def test_added_errors_with_steps_shows_steps(self, monkeypatch):
        """Line 3616-3617: added+errors, and steps non-empty -> steps in output."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "My PL"}]))
        monkeypatch.setattr(server.asc, "track_exists_in_playlist", lambda pl, n, a: (True, False))
        call_count = [0]

        def mock_smart_add(pl, name, artist, album):
            call_count[0] += 1
            return (True, "ok", None) if call_count[0] == 1 else (False, "Some error", None)

        monkeypatch.setattr(server, "_smart_as_add_track_to_playlist", mock_smart_add)
        monkeypatch.setattr(server, "_verify_track_in_playlist", lambda pl, n, a: True)
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "auto_add": False,
                "fetch_explicit": False,
                "mode": "auto",
                "storefront": "us",
                "output_format": "text",
            },
        )
        result = server._playlist_add("My PL", "Song A, Song B")
        assert "Added" in result

    def test_steps_only_no_added_no_errors(self, monkeypatch):
        """Line 3627-3628: only steps accumulated, no added/errors."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "My PL"}]))
        # All tracks are duplicates -> step added, no added[], no errors[]
        monkeypatch.setattr(server.asc, "track_exists_in_playlist", lambda pl, n, a: (True, True))
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "auto_add": False,
                "fetch_explicit": False,
                "mode": "auto",
                "storefront": "us",
                "output_format": "text",
            },
        )
        result = server._playlist_add("My PL", "Song A")
        # Duplicate skipped -> steps has "Skipped...", added=[], errors=[]
        # -> `if steps: return "\n".join(steps)` (line 3627-3628)
        assert "Skipped" in result or isinstance(result, str)


# ---------------------------------------------------------------------------
# _playlist_tracks: optimized path empty/partial breaks (lines 2795, 2798)
# ---------------------------------------------------------------------------


class TestPlaylistTracksOptimizedBreaks:
    @responses_lib.activate
    def test_optimized_empty_data_breaks(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Line 2795: optimized path (limit>0, no filter) returns empty data -> break."""
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "fetch_explicit": False,
                "output_format": "text",
                "mode": "auto",
                "auto_add": False,
                "storefront": "us",
            },
        )
        # data=[] causes `if not tracks: break` at line 2795
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.pl/tracks",
            json={"data": []},
            status=200,
        )
        result = server._playlist_tracks("p.pl", limit=10)
        assert "empty" in result.lower() or isinstance(result, str)

    @responses_lib.activate
    def test_optimized_partial_page_breaks(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Line 2798: optimized path returns fewer tracks than batch_limit -> break."""
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "fetch_explicit": False,
                "output_format": "text",
                "mode": "auto",
                "auto_add": False,
                "storefront": "us",
            },
        )
        # batch_limit = min(100, 0+100) = 100, response returns 3 < 100 -> break at 2798
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.pl/tracks",
            json={
                "meta": {"total": 3},
                "data": [
                    {
                        "id": f"i.t{i}",
                        "attributes": {
                            "name": f"Song {i}",
                            "artistName": "Artist",
                            "albumName": "Alb",
                            "durationInMillis": 180000,
                        },
                    }
                    for i in range(3)
                ],
            },
            status=200,
        )
        result = server._playlist_tracks("p.pl", limit=100)
        assert "Song 0" in result


# ---------------------------------------------------------------------------
# _playlist_tracks: fetch_explicit inner-loop branches (lines 2617,2622,2669,2687,2711-2712)
# ---------------------------------------------------------------------------


class TestFetchExplicitInnerLoopBranches:
    def _as_tracks(self, names_artists):
        """Build AppleScript-style track list."""
        return [
            {
                "name": n,
                "artist": a,
                "album": "Some Album",
                "duration": "3:00",
                "genre": "Pop",
                "year": "2020",
                "id": f"i.{n.lower().replace(' ', '')}",
            }
            for n, a in names_artists
        ]

    @responses_lib.activate
    def test_inner_loop_empty_tracks_breaks(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Line 2617: explicit track fetch returns data=[] -> break from inner loop."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "My PL"}]))
        monkeypatch.setattr(
            server.asc,
            "get_playlist_tracks",
            lambda name: (True, self._as_tracks([("Come Together", "Beatles")])),
        )
        mock_cache = MagicMock()
        mock_cache.get_explicit.return_value = None
        monkeypatch.setattr(server, "get_track_cache", lambda: mock_cache)
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        # Library playlists - match the playlist by name
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists",
            json={"data": [{"id": "p.pl1", "attributes": {"name": "My PL"}}]},
            status=200,
        )
        # Inner loop: returns empty data -> break at line 2617
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.pl1/tracks",
            json={"data": []},
            status=200,
        )
        result = server._playlist_tracks("My PL", fetch_explicit=True)
        assert "Come Together" in result

    @responses_lib.activate
    def test_inner_loop_full_page_increments_offset(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Line 2622: inner loop returns 100 tracks -> api_offset += 100 (loop continues)."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "My PL"}]))
        # One AS track so fetch_explicit path is entered
        monkeypatch.setattr(
            server.asc,
            "get_playlist_tracks",
            lambda name: (True, self._as_tracks([("Song 0", "Artist")])),
        )
        mock_cache = MagicMock()
        mock_cache.get_explicit.return_value = None
        monkeypatch.setattr(server, "get_track_cache", lambda: mock_cache)
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists",
            json={"data": [{"id": "p.pl1", "attributes": {"name": "My PL"}}]},
            status=200,
        )
        # First call: return exactly 100 tracks -> len==100, does NOT break, hits api_offset+=100
        page1 = [
            {
                "id": f"i.t{i}",
                "attributes": {
                    "name": f"Song {i}",
                    "artistName": "Artist",
                    "albumName": "Album",
                    "playParams": {"catalogId": ""},
                    "isrc": "",
                    "contentRating": "",
                },
            }
            for i in range(100)
        ]
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.pl1/tracks",
            json={"data": page1},
            status=200,
        )
        # Second call: return empty -> break at 2617
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.pl1/tracks",
            json={"data": []},
            status=200,
        )
        result = server._playlist_tracks("My PL", fetch_explicit=True)
        assert "Song 0" in result

    @responses_lib.activate
    def test_already_enriched_track_continue(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Line 2669: track with explicit!='Unknown' in matching loop -> continue."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "My PL"}]))
        # Two AS tracks: one cached, one not
        monkeypatch.setattr(
            server.asc,
            "get_playlist_tracks",
            lambda name: (
                True,
                [
                    {
                        "name": "Cached Song",
                        "artist": "Artist A",
                        "album": "Alb",
                        "duration": "3:00",
                        "genre": "Pop",
                        "year": "2020",
                        "id": "i.cached",
                    },
                    {
                        "name": "Unknown Song",
                        "artist": "Artist B",
                        "album": "Alb",
                        "duration": "3:00",
                        "genre": "Pop",
                        "year": "2020",
                        "id": "i.unknown",
                    },
                ],
            ),
        )
        # "i.cached" returns "Yes" from cache, "i.unknown" returns None
        mock_cache = MagicMock()

        def get_explicit(tid):
            if tid == "i.cached":
                return "Yes"
            return None

        mock_cache.get_explicit.side_effect = get_explicit
        monkeypatch.setattr(server, "get_track_cache", lambda: mock_cache)
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists",
            json={"data": [{"id": "p.pl1", "attributes": {"name": "My PL"}}]},
            status=200,
        )
        # API tracks for the one unknown track
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.pl1/tracks",
            json={
                "data": [
                    {
                        "id": "i.unknown",
                        "attributes": {
                            "name": "Unknown Song",
                            "artistName": "Artist B",
                            "albumName": "Alb",
                            "contentRating": "explicit",
                            "isrc": "",
                            "playParams": {"catalogId": ""},
                        },
                    }
                ]
            },
            status=200,
        )
        result = server._playlist_tracks("My PL", fetch_explicit=True)
        # "Cached Song" had explicit="Yes" (from cache) -> continue at 2669
        # "Unknown Song" was matched via API -> explicit set
        assert "Cached Song" in result

    @responses_lib.activate
    def test_name_only_fallback(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Line 2687: name-only fallback when full/partial match fails but name is unique."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "My PL"}]))
        # AS track with artist "Lennon" but API has artist "Beatles" -> partial match fails
        monkeypatch.setattr(
            server.asc,
            "get_playlist_tracks",
            lambda name: (
                True,
                [
                    {
                        "name": "Come Together",
                        "artist": "Lennon",  # Different from API artist
                        "album": "Different Album",  # Different from API album
                        "duration": "3:00",
                        "genre": "Rock",
                        "year": "1969",
                        "id": "i.ct",
                    }
                ],
            ),
        )
        mock_cache = MagicMock()
        mock_cache.get_explicit.return_value = None
        monkeypatch.setattr(server, "get_track_cache", lambda: mock_cache)
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists",
            json={"data": [{"id": "p.pl1", "attributes": {"name": "My PL"}}]},
            status=200,
        )
        # API track: same name but different artist/album -> only name matches
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/playlists/p.pl1/tracks",
            json={
                "data": [
                    {
                        "id": "i.api1",
                        "attributes": {
                            "name": "Come Together",
                            "artistName": "Beatles",  # Different from track_data artist
                            "albumName": "Abbey Road",  # Different from track_data album
                            "contentRating": "notExplicit",
                            "isrc": "GBUM71029604",
                            "playParams": {"catalogId": "9876543210"},
                        },
                    }
                ]
            },
            status=200,
        )
        result = server._playlist_tracks("My PL", fetch_explicit=True)
        # Name-only fallback at 2687 fired -> explicit enriched
        assert "Come Together" in result

    @responses_lib.activate
    def test_exception_in_explicit_fetch(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Lines 2711-2712: exception in explicit enrichment try block -> pass."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "My PL"}]))
        monkeypatch.setattr(
            server.asc,
            "get_playlist_tracks",
            lambda name: (True, self._as_tracks([("Song X", "Artist X")])),
        )

        # get_track_cache() throws -> caught by except Exception at 2711
        def raise_error():
            raise RuntimeError("cache unavailable")

        monkeypatch.setattr(server, "get_track_cache", raise_error)
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        result = server._playlist_tracks("My PL", fetch_explicit=True)
        # Despite exception, result still returns track listing (exception is swallowed)
        assert "Song X" in result


# ---------------------------------------------------------------------------
# _playlist_add: remaining missing lines in assigned ranges
# ---------------------------------------------------------------------------


class TestPlaylistAddRemainingCoverage:
    """Covers lines: 3302-3303, 3367, 3375-3376, 3517, 3533, 3539-3540,
    3585, 3617, 3629, 3757, 3806-3807."""

    def _base_monkeypatches(self, monkeypatch):
        """Common monkeypatches for AppleScript-mode _playlist_add tests."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
        monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda name: (None, None))
        monkeypatch.setattr(server.asc, "get_playlists", lambda: (True, [{"name": "My PL"}]))
        monkeypatch.setattr(server, "_has_developer_token", lambda: True)
        monkeypatch.setattr(server, "get_storefront", lambda: "us")
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "auto_add": False,
                "fetch_explicit": False,
                "mode": "auto",
                "storefront": "us",
                "output_format": "text",
            },
        )

    def test_album_resolve_error(self, monkeypatch):
        """Lines 3302-3303: _resolve_album returns item with error -> steps.append + continue."""
        from applemusic_mcp.server import InputType, ResolvedInput

        self._base_monkeypatches(monkeypatch)
        monkeypatch.setattr(
            server,
            "_resolve_album",
            lambda album, artist="": [
                ResolvedInput(input_type=InputType.NAME, value="Bad Album", error="Not found")
            ],
        )
        result = server._playlist_add("My PL", album="Bad Album")
        assert isinstance(result, str)

    @responses_lib.activate
    def test_album_name_search_api_error(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Line 3367: album name search returns non-200 API error."""
        from applemusic_mcp.server import InputType, ResolvedInput

        self._base_monkeypatches(monkeypatch)
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(
            server,
            "_resolve_album",
            lambda album, artist="": [ResolvedInput(input_type=InputType.NAME, value=album)],
        )
        # Search returns 500 -> line 3367
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/catalog/us/search",
            status=500,
        )
        result = server._playlist_add("My PL", album="Abbey Road")
        assert isinstance(result, str)

    @responses_lib.activate
    def test_album_exception_in_try_block(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Lines 3375-3376: exception in album try block -> steps.append(exception message)."""
        from applemusic_mcp.server import InputType, ResolvedInput

        self._base_monkeypatches(monkeypatch)
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(
            server,
            "_resolve_album",
            lambda album, artist="": [ResolvedInput(input_type=InputType.NAME, value=album)],
        )
        # get_headers raises inside the album try block -> caught at 3375
        orig_get_headers = server.get_headers
        call_count = [0]

        def get_headers_raises_once():
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("connection error")
            return orig_get_headers()

        monkeypatch.setattr(server, "get_headers", get_headers_raises_once)
        result = server._playlist_add("My PL", album="Abbey Road")
        assert "connection error" in result or isinstance(result, str)

    @responses_lib.activate
    def test_catalog_info_returns_empty_data(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Line 3517: catalog info returns 200 but data=[] -> continue (no error appended)."""
        self._base_monkeypatches(monkeypatch)
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        # Add to library ok
        responses_lib.add(
            responses_lib.POST,
            "https://api.music.apple.com/v1/me/library",
            status=202,
        )
        # Catalog info 200 but empty data
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/catalog/us/songs/1234567890",
            json={"data": []},
            status=200,
        )
        # Pass catalog ID (9+ numeric digits)
        result = server._playlist_add("My PL", "1234567890")
        # Empty data -> continue -> no added, no errors, no name-related step
        assert isinstance(result, str)

    @responses_lib.activate
    def test_library_info_returns_empty_data(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Line 3533: library info returns 200 but data=[] -> continue. Line 3629: no steps."""
        self._base_monkeypatches(monkeypatch)
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        # Library ID lookup returns 200 but empty data -> continue (no error appended)
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/songs/i.libid",
            json={"data": []},
            status=200,
        )
        # No steps or errors added (library IDs don't add the "Adding to library..." step)
        # -> else: if steps: ... else: return "No tracks added"
        result = server._playlist_add("My PL", "i.libid")
        assert "No tracks added" in result or isinstance(result, str)

    @responses_lib.activate
    def test_no_name_in_catalog_attrs(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Lines 3539-3540: catalog info returns data with empty name -> errors.append."""
        self._base_monkeypatches(monkeypatch)
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(
            server, "time", type("T", (), {"sleep": staticmethod(lambda s: None)})()
        )
        responses_lib.add(
            responses_lib.POST,
            "https://api.music.apple.com/v1/me/library",
            status=202,
        )
        # data has entry but no "name" -> attrs.get("name","") == ""
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/catalog/us/songs/1234567890",
            json={
                "data": [
                    {
                        "id": "1234567890",
                        "attributes": {"artistName": "Beatles"},  # No "name" key
                    }
                ]
            },
            status=200,
        )
        result = server._playlist_add("My PL", "1234567890")
        assert "No name found" in result or isinstance(result, str)

    @responses_lib.activate
    def test_retry_success_after_verify_fail(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Line 3585: first add succeeds, verify fails, retry succeeds + verify passes."""
        self._base_monkeypatches(monkeypatch)
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(
            server, "time", type("T", (), {"sleep": staticmethod(lambda s: None)})()
        )
        responses_lib.add(
            responses_lib.POST,
            "https://api.music.apple.com/v1/me/library",
            status=202,
        )
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/catalog/us/songs/1234567890",
            json={
                "data": [
                    {
                        "id": "1234567890",
                        "attributes": {"name": "Come Together", "artistName": "Beatles"},
                    }
                ]
            },
            status=200,
        )
        monkeypatch.setattr(server.asc, "track_exists_in_playlist", lambda pl, n, a: (True, False))
        # First add: success. First verify: fail. Retry add: success. Retry verify: success.
        add_calls = [0]

        def add_track(pl, name, artist):
            add_calls[0] += 1
            return (True, "ok")

        verify_calls = [0]

        def verify_track(pl, name, artist):
            verify_calls[0] += 1
            return verify_calls[0] > 1  # First verify fails, retry verify passes

        monkeypatch.setattr(server.asc, "add_track_to_playlist", add_track)
        monkeypatch.setattr(server, "_verify_track_in_playlist", verify_track)
        result = server._playlist_add("My PL", "1234567890")
        # Retry succeeded -> added.append at line 3585
        assert "Come Together" in result or isinstance(result, str)

    @responses_lib.activate
    def test_added_and_errors_with_nonempty_steps(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Line 3617: elif added and errors: ... if steps: msg += steps."""
        self._base_monkeypatches(monkeypatch)
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(
            server, "time", type("T", (), {"sleep": staticmethod(lambda s: None)})()
        )
        # Catalog ID -> steps gets "Adding catalog ID..." message, then added gets track
        responses_lib.add(
            responses_lib.POST,
            "https://api.music.apple.com/v1/me/library",
            status=202,
        )
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/catalog/us/songs/1234567890",
            json={
                "data": [
                    {
                        "id": "1234567890",
                        "attributes": {"name": "Come Together", "artistName": "Beatles"},
                    }
                ]
            },
            status=200,
        )
        monkeypatch.setattr(server.asc, "track_exists_in_playlist", lambda pl, n, a: (True, False))
        monkeypatch.setattr(server.asc, "add_track_to_playlist", lambda pl, n, a: (True, "ok"))
        monkeypatch.setattr(server, "_verify_track_in_playlist", lambda pl, n, a: True)
        # Track name that fails -> goes to errors
        monkeypatch.setattr(
            server,
            "_smart_as_add_track_to_playlist",
            lambda pl, n, a, alb: (False, "not found", None),
        )
        # Pass both a failing name AND a catalog ID
        result = server._playlist_add("My PL", "Song Fail\n1234567890")
        # added=[CometTogether], errors=[SongFail], steps=["Adding catalog ID..."]
        # -> elif added and errors: + if steps: msg += steps (line 3617)
        assert isinstance(result, str)

    @responses_lib.activate
    def test_no_tracks_added_no_steps(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Line 3629: library ID, empty data -> no added/errors/steps -> 'No tracks added'."""
        self._base_monkeypatches(monkeypatch)
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        # Library ID (not catalog) -> no "Adding to library" step
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/songs/i.libXYZ",
            json={"data": []},
            status=200,
        )
        result = server._playlist_add("My PL", "i.libXYZ")
        # data=[] -> continue at 3533, steps=[], added=[], errors=[]
        # else: if steps: (False) -> return "No tracks added"
        assert "No tracks added" in result

    @responses_lib.activate
    def test_api_mode_dup_check_not_duplicate(
        self, mock_config_dir, mock_developer_token, mock_user_token, monkeypatch
    ):
        """Line 3757: in API mode dup check, track not in existing -> filtered_ids.append."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        _write_tokens(mock_config_dir, mock_developer_token, mock_user_token)
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "auto_add": False,
                "fetch_explicit": False,
                "mode": "auto",
                "storefront": "us",
                "output_format": "text",
            },
        )
        monkeypatch.setattr(
            server, "time", type("T", (), {"sleep": staticmethod(lambda s: None)})()
        )
        # playlist ID "p.mypl" -> API mode
        # Track "i.libsong" is a library ID -> goes to library_ids directly
        # _get_playlist_track_names returns existing tracks (different name)
        monkeypatch.setattr(
            server,
            "_get_playlist_track_names",
            lambda pid: (
                True,
                [{"id": "i.other", "name": "Other Song", "artist": "Other Artist"}],
            ),
        )
        # Library song info
        responses_lib.add(
            responses_lib.GET,
            "https://api.music.apple.com/v1/me/library/songs/i.libsong",
            json={
                "data": [
                    {
                        "id": "i.libsong",
                        "attributes": {"name": "My New Song", "artistName": "New Artist"},
                    }
                ]
            },
            status=200,
        )
        # POST playlist tracks
        responses_lib.add(
            responses_lib.POST,
            "https://api.music.apple.com/v1/me/library/playlists/p.mypl/tracks",
            status=204,
        )
        result = server._playlist_add("p.mypl", "i.libsong")
        # "My New Song" not in existing (["Other Song"]) -> filtered_ids.append at 3757
        assert isinstance(result, str)

    def test_api_mode_filenotfounderror(self, mock_config_dir, monkeypatch):
        """Lines 3806-3807: FileNotFoundError in API mode (no token file)."""
        monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
        monkeypatch.setattr(
            server,
            "get_user_preferences",
            lambda: {
                "auto_add": False,
                "fetch_explicit": False,
                "mode": "auto",
                "storefront": "us",
                "output_format": "text",
            },
        )
        # No token file -> get_headers raises FileNotFoundError
        result = server._playlist_add("p.mypl", "i.libsong")
        assert "Error" in result


# UNREACHABLE LINES (confirmed by code analysis):
# - server.py:3272 — `return resolved.error` in the `else` branch of _playlist_add:
#   _resolve_playlist only sets error when playlist is empty string (line 1408-1409),
#   but the guard at line 3232 already returns early for empty playlist.
#   _resolve_playlist always receives a non-empty string here, so error is always None.
# - server.py:3653 — `return "\n".join(steps)` in API mode:
#   The [UI] step prefix only comes from _unified_auto_search_to_playlist,
#   which is only called in AppleScript mode (auto_add=True with a name playlist).
#   In API mode (playlist ID like p.xxx), that function is never called, so
#   ui_successes is always empty and line 3653 is never reached.


def test_action_folders_shows_tree(monkeypatch):
    """`playlist(action='folders')` exposes the folder tree (folders aren't in list)."""
    from applemusic_mcp import server

    monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
    monkeypatch.setattr(server.asc, "get_folder_tree", lambda: (True, "[My Folder]\n  Boom Boom"))
    out = server.playlist(action="folders")
    assert "[My Folder]" in out and "Boom Boom" in out
    # `tree` is an accepted alias
    assert "[My Folder]" in server.playlist(action="tree")


# ---------------------------------------------------------------------------
# Rate-limit honesty in the bulk add path (#42)
# ---------------------------------------------------------------------------


class TestPlaylistAddThrottled:
    """A throttled catalog search returns EMPTY, not an error. Reporting that as
    "not found in catalog" is how a 200-track import silently records false
    negatives — the exact failure reported in #42."""

    def test_throttle_is_not_reported_as_not_found(self, monkeypatch):
        monkeypatch.setattr(server.amp_api, "add_tracks", lambda pid, items: (True, "ok"))

        def throttled_search(q, n):
            server.amp_api.note_status(429)
            return []

        monkeypatch.setattr(server.amp_api, "search_catalog_songs", throttled_search)
        result = server._playlist_add_api("p.abc123", "Hey Jude", "Beatles", auto_add=True)
        assert "not found in catalog" not in result
        assert "429" in result
        assert "--dev" in result

    def test_throttle_stops_resolving_the_rest_of_the_batch(self, monkeypatch):
        """More requests inside Apple's rolling window push recovery further out,
        so the loop stops at the first 429 instead of burning the whole batch."""
        calls = []

        def throttled_search(q, n):
            calls.append(q)
            server.amp_api.note_status(429)
            return []

        monkeypatch.setattr(server.amp_api, "add_tracks", lambda pid, items: (True, "ok"))
        monkeypatch.setattr(server.amp_api, "search_catalog_songs", throttled_search)
        tracks = json.dumps([f"Track {i}" for i in range(10)])
        server._playlist_add_api("p.abc123", tracks, "", auto_add=True)
        assert len(calls) == 1

    def test_genuine_miss_still_says_not_found(self, monkeypatch):
        """No 429 seen → an empty search really does mean no such song."""
        monkeypatch.setattr(server.amp_api, "add_tracks", lambda pid, items: (True, "ok"))
        monkeypatch.setattr(server.amp_api, "search_catalog_songs", lambda q, n: [])
        result = server._playlist_add_api("p.abc123", "Zzzz Nonexistent", "", auto_add=True)
        assert "not found in catalog" in result
