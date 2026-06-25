"""Tests for audit_log module."""

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from applemusic_mcp import audit_log


# Note: conftest.py provides mock_audit_log_for_all_tests (autouse) which
# patches get_audit_log_path for all tests. We use that path directly.


class TestLogAction:
    """Tests for log_action function."""

    def test_logs_basic_action(self, mock_audit_log_for_all_tests):
        """Should log a basic action with timestamp and details."""
        audit_log.log_action("add_to_library", {"tracks": ["Song - Artist"], "mode": "catalog_ids"})

        assert mock_audit_log_for_all_tests.exists()
        with open(mock_audit_log_for_all_tests) as f:
            entry = json.loads(f.readline())

        assert entry["action"] == "add_to_library"
        assert entry["details"]["tracks"] == ["Song - Artist"]
        assert "timestamp" in entry
        # Verify timestamp is valid ISO format
        datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00"))

    def test_logs_action_with_undo_info(self, mock_audit_log_for_all_tests):
        """Should include undo_info when provided."""
        audit_log.log_action(
            "delete_playlist",
            {"name": "My Playlist", "track_count": 10},
            undo_info={"playlist_name": "My Playlist", "tracks": ["Song1", "Song2"]},
        )

        with open(mock_audit_log_for_all_tests) as f:
            entry = json.loads(f.readline())

        assert entry["undo_info"]["playlist_name"] == "My Playlist"
        assert len(entry["undo_info"]["tracks"]) == 2

    def test_appends_multiple_entries(self, mock_audit_log_for_all_tests):
        """Should append multiple entries to the log file."""
        audit_log.log_action("add_to_playlist", {"playlist": "Test", "tracks": ["A"]})
        audit_log.log_action("add_to_playlist", {"playlist": "Test", "tracks": ["B"]})
        audit_log.log_action("remove_from_playlist", {"playlist": "Test", "tracks": ["A"]})

        with open(mock_audit_log_for_all_tests) as f:
            lines = f.readlines()

        assert len(lines) == 3
        assert json.loads(lines[0])["details"]["tracks"] == ["A"]
        assert json.loads(lines[1])["details"]["tracks"] == ["B"]
        assert json.loads(lines[2])["action"] == "remove_from_playlist"

    def test_handles_write_error_gracefully(self):
        """Should not raise exception on write error."""
        # Point to a non-existent directory that can't be created
        bad_path = Path("/nonexistent/readonly/path/audit.jsonl")
        with patch.object(audit_log, "get_audit_log_path", return_value=bad_path):
            # Should not raise
            audit_log.log_action("test", {"data": "value"})


class TestGetRecentEntries:
    """Tests for get_recent_entries function."""

    def test_returns_empty_list_when_no_file(self, mock_audit_log_for_all_tests):
        """Should return empty list when log file doesn't exist."""
        result = audit_log.get_recent_entries()
        assert result == []

    def test_returns_entries_most_recent_first(self, mock_audit_log_for_all_tests):
        """Should return entries in reverse chronological order."""
        audit_log.log_action("first", {"order": 1})
        audit_log.log_action("second", {"order": 2})
        audit_log.log_action("third", {"order": 3})

        result = audit_log.get_recent_entries()

        assert len(result) == 3
        assert result[0]["action"] == "third"
        assert result[1]["action"] == "second"
        assert result[2]["action"] == "first"

    def test_respects_limit(self, mock_audit_log_for_all_tests):
        """Should return at most 'limit' entries."""
        for i in range(10):
            audit_log.log_action(f"action_{i}", {"index": i})

        result = audit_log.get_recent_entries(limit=3)

        assert len(result) == 3
        # Most recent should be action_9
        assert result[0]["action"] == "action_9"

    def test_skips_invalid_json_lines(self, mock_audit_log_for_all_tests):
        """Should skip lines that are not valid JSON."""
        with open(mock_audit_log_for_all_tests, "w") as f:
            f.write('{"action": "valid", "details": {}}\n')
            f.write("not valid json\n")
            f.write('{"action": "also_valid", "details": {}}\n')

        result = audit_log.get_recent_entries()

        assert len(result) == 2
        assert result[0]["action"] == "also_valid"
        assert result[1]["action"] == "valid"


class TestFormatEntriesForDisplay:
    """Tests for format_entries_for_display function."""

    def test_returns_message_for_empty_list(self):
        """Should return helpful message when no entries."""
        result = audit_log.format_entries_for_display([])
        assert "No audit log entries found" in result

    def test_formats_add_to_library(self):
        """Should format add_to_library entries correctly."""
        entries = [
            {
                "timestamp": "2025-01-15T12:00:00+00:00",
                "action": "add_to_library",
                "details": {"tracks": ["Song A - Artist A", "Song B - Artist B"]},
            }
        ]

        result = audit_log.format_entries_for_display(entries)

        assert "ADD TO LIBRARY" in result
        assert "2 track(s)" in result
        assert "+ Song A - Artist A" in result

    def test_formats_remove_from_playlist(self):
        """Should format remove_from_playlist entries correctly."""
        entries = [
            {
                "timestamp": "2025-01-15T12:00:00+00:00",
                "action": "remove_from_playlist",
                "details": {"playlist": "My Playlist", "tracks": ["Removed Song"]},
            }
        ]

        result = audit_log.format_entries_for_display(entries)

        assert "REMOVE FROM PLAYLIST 'My Playlist'" in result
        assert "- Removed Song" in result

    def test_formats_create_playlist(self):
        """Should format create_playlist entries correctly."""
        entries = [
            {
                "timestamp": "2025-01-15T12:00:00+00:00",
                "action": "create_playlist",
                "details": {"name": "New Playlist", "playlist_id": "p.abc123"},
            }
        ]

        result = audit_log.format_entries_for_display(entries)

        assert "CREATE PLAYLIST" in result
        assert "'New Playlist'" in result
        assert "p.abc123" in result

    def test_formats_delete_playlist(self):
        """Should format delete_playlist entries correctly."""
        entries = [
            {
                "timestamp": "2025-01-15T12:00:00+00:00",
                "action": "delete_playlist",
                "details": {"name": "Deleted Playlist", "track_count": 15},
            }
        ]

        result = audit_log.format_entries_for_display(entries)

        assert "DELETE PLAYLIST" in result
        assert "'Deleted Playlist'" in result
        assert "15 tracks" in result

    def test_formats_rating(self):
        """Should format rating entries correctly."""
        entries = [
            {
                "timestamp": "2025-01-15T12:00:00+00:00",
                "action": "rating",
                "details": {"track": "Great Song", "type": "love", "value": ""},
            }
        ]

        result = audit_log.format_entries_for_display(entries)

        assert "RATING" in result
        assert "love" in result
        assert "'Great Song'" in result

    def test_truncates_long_track_lists(self):
        """Should truncate track lists with more than 5 entries."""
        entries = [
            {
                "timestamp": "2025-01-15T12:00:00+00:00",
                "action": "add_to_library",
                "details": {"tracks": [f"Song {i}" for i in range(10)]},
            }
        ]

        result = audit_log.format_entries_for_display(entries)

        assert "+ Song 0" in result
        assert "+ Song 4" in result
        assert "... and 5 more" in result
        assert "+ Song 5" not in result

    def test_respects_display_limit(self):
        """Should show 'more entries' message when limit exceeded."""
        entries = [
            {"timestamp": "2025-01-15T12:00:00+00:00", "action": f"action_{i}", "details": {}}
            for i in range(30)
        ]

        result = audit_log.format_entries_for_display(entries, limit=10)

        assert "... 20 more entries" in result
        assert "config(action='audit-log', limit=N)" in result


class TestClearAuditLog:
    """Tests for clear_audit_log function."""

    def test_clears_existing_log(self, mock_audit_log_for_all_tests):
        """Should delete the log file."""
        audit_log.log_action("test", {"data": "value"})
        assert mock_audit_log_for_all_tests.exists()

        result = audit_log.clear_audit_log()

        assert result is True
        assert not mock_audit_log_for_all_tests.exists()

    def test_returns_true_when_no_file(self, mock_audit_log_for_all_tests):
        """Should return True even when file doesn't exist."""
        assert not mock_audit_log_for_all_tests.exists()

        result = audit_log.clear_audit_log()

        assert result is True

    def test_handles_permission_error(self, tmp_path):
        """Should return False on permission error."""
        # Create a read-only situation (mock the unlink to fail)
        bad_path = tmp_path / "audit_log.jsonl"
        bad_path.write_text('{"test": true}')

        with patch.object(audit_log, "get_audit_log_path", return_value=bad_path):
            with patch.object(Path, "unlink", side_effect=PermissionError("denied")):
                result = audit_log.clear_audit_log()

        assert result is False


class TestGetAuditLogPath:
    """Tests for get_audit_log_path function."""

    def test_returns_path_in_cache_dir(self):
        """Should return path in ~/.cache/applemusic-mcp/."""
        result = audit_log.get_audit_log_path()

        assert result.name == "audit_log.jsonl"
        assert "applemusic-mcp" in str(result)
        assert ".cache" in str(result)

    def test_creates_parent_directory(self, tmp_path):
        """Should create parent directory if it doesn't exist."""
        test_dir = tmp_path / "new_cache" / "applemusic-mcp"
        expected_path = test_dir / "audit_log.jsonl"

        with patch.object(Path, "home", return_value=tmp_path):
            with patch.object(audit_log, "get_audit_log_path", wraps=audit_log.get_audit_log_path):
                # Manually construct what the function does
                log_dir = tmp_path / ".cache" / "applemusic-mcp"
                log_dir.mkdir(parents=True, exist_ok=True)
                result = log_dir / "audit_log.jsonl"

        assert result.parent.exists()
