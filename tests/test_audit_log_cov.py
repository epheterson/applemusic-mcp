"""Full line-coverage tests for applemusic_mcp.audit_log.

Filesystem is redirected via Path.home monkeypatch (or by patching
get_audit_log_path per-test) so nothing touches the real cache dir.
"""

import json

import pytest

from applemusic_mcp import audit_log


@pytest.fixture(autouse=True)
def mock_audit_log_for_all_tests(tmp_path, monkeypatch):
    """Override the conftest autouse fixture.

    Instead of patching get_audit_log_path to a constant, redirect
    Path.home() so the *real* get_audit_log_path body runs against tmp_path.
    Individual tests may re-patch get_audit_log_path for error injection.
    """
    monkeypatch.setattr(audit_log.Path, "home", lambda: tmp_path)
    return tmp_path / ".cache" / "applemusic-mcp" / "audit_log.jsonl"


def test_get_audit_log_path_creates_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLEMUSIC_MCP_HOME", str(tmp_path))
    path = audit_log.get_audit_log_path()
    assert path == tmp_path / ".cache" / "applemusic-mcp" / "audit_log.jsonl"
    assert path.parent.is_dir()


def test_log_action_writes_entry_with_undo_info():
    audit_log.log_action(
        "create_playlist",
        {"name": "My List", "playlist_id": "p.1"},
        undo_info={"reverse": "delete_playlist"},
    )
    entries = audit_log.get_recent_entries()
    assert len(entries) == 1
    assert entries[0]["action"] == "create_playlist"
    assert entries[0]["details"]["name"] == "My List"
    assert entries[0]["undo_info"] == {"reverse": "delete_playlist"}
    assert "timestamp" in entries[0]


def test_log_action_without_undo_info():
    audit_log.log_action("delete_folder", {"name": "Old"})
    entries = audit_log.get_recent_entries()
    assert "undo_info" not in entries[0]


def test_log_action_write_failure_swallowed(monkeypatch, tmp_path, caplog):
    # Point at a path whose parent dir does not exist -> open() raises,
    # caught and logged (lines 82-83).
    bad = tmp_path / "missing_dir" / "log.jsonl"
    monkeypatch.setattr(audit_log, "get_audit_log_path", lambda: bad)
    audit_log.log_action("rating", {"track": "x"})  # must not raise
    assert "Failed to write audit log" in caplog.text


def test_get_recent_entries_no_file(monkeypatch, tmp_path):
    monkeypatch.setattr(audit_log, "get_audit_log_path", lambda: tmp_path / "nope.jsonl")
    assert audit_log.get_recent_entries() == []


def test_get_recent_entries_skips_bad_lines_and_orders(tmp_path, monkeypatch):
    log_path = tmp_path / "log.jsonl"
    monkeypatch.setattr(audit_log, "get_audit_log_path", lambda: log_path)
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"action": "a", "details": {}}) + "\n")
        f.write("\n")  # blank line, skipped
        f.write("not json\n")  # JSONDecodeError, skipped (lines 107-108)
        f.write(json.dumps({"action": "b", "details": {}}) + "\n")
    entries = audit_log.get_recent_entries()
    # most recent first
    assert [e["action"] for e in entries] == ["b", "a"]


def test_get_recent_entries_honors_limit(tmp_path, monkeypatch):
    log_path = tmp_path / "log.jsonl"
    monkeypatch.setattr(audit_log, "get_audit_log_path", lambda: log_path)
    with open(log_path, "w", encoding="utf-8") as f:
        for i in range(10):
            f.write(json.dumps({"action": str(i), "details": {}}) + "\n")
    entries = audit_log.get_recent_entries(limit=3)
    assert [e["action"] for e in entries] == ["9", "8", "7"]


def test_get_recent_entries_read_failure(tmp_path, monkeypatch, caplog):
    # Make the log path a *directory*: exists() is True but open() raises
    # IsADirectoryError -> caught, returns [] (lines 109-111).
    log_dir = tmp_path / "log.jsonl"
    log_dir.mkdir()
    monkeypatch.setattr(audit_log, "get_audit_log_path", lambda: log_dir)
    assert audit_log.get_recent_entries() == []
    assert "Failed to read audit log" in caplog.text


def test_rotation_no_backup(tmp_path, monkeypatch):
    log_path = tmp_path / "log.jsonl"
    monkeypatch.setattr(audit_log, "get_audit_log_path", lambda: log_path)
    monkeypatch.setattr(audit_log, "MAX_LOG_SIZE", 10)
    log_path.write_text("x" * 50, encoding="utf-8")
    audit_log.log_action("rating", {"track": "t"})
    backup = log_path.with_suffix(".jsonl.1")
    assert backup.exists()
    assert backup.read_text(encoding="utf-8") == "x" * 50
    # New log has only the fresh entry
    assert len(audit_log.get_recent_entries()) == 1


def test_rotation_replaces_existing_backup(tmp_path, monkeypatch):
    log_path = tmp_path / "log.jsonl"
    backup = log_path.with_suffix(".jsonl.1")
    monkeypatch.setattr(audit_log, "get_audit_log_path", lambda: log_path)
    monkeypatch.setattr(audit_log, "MAX_LOG_SIZE", 10)
    log_path.write_text("y" * 50, encoding="utf-8")
    backup.write_text("OLD BACKUP", encoding="utf-8")  # exists -> unlinked
    audit_log.log_action("rating", {"track": "t"})
    assert backup.read_text(encoding="utf-8") == "y" * 50


def test_rotation_failure_swallowed(tmp_path, monkeypatch, caplog):
    # backup path is a non-empty directory -> backup.unlink() raises (52-53).
    log_path = tmp_path / "log.jsonl"
    backup = log_path.with_suffix(".jsonl.1")
    backup.mkdir()
    (backup / "child").write_text("blocker", encoding="utf-8")
    monkeypatch.setattr(audit_log, "get_audit_log_path", lambda: log_path)
    monkeypatch.setattr(audit_log, "MAX_LOG_SIZE", 10)
    log_path.write_text("z" * 50, encoding="utf-8")
    audit_log.log_action("rating", {"track": "t"})  # must not raise
    assert "Failed to rotate audit log" in caplog.text
    # original still appended to (rotation failed, no rename happened)
    assert log_path.exists()


def test_rotation_not_needed_when_small(tmp_path, monkeypatch):
    log_path = tmp_path / "log.jsonl"
    monkeypatch.setattr(audit_log, "get_audit_log_path", lambda: log_path)
    log_path.write_text("small", encoding="utf-8")
    audit_log._rotate_if_needed(log_path)
    assert not log_path.with_suffix(".jsonl.1").exists()


def test_rotation_skipped_when_missing(tmp_path):
    # exists() False -> no-op branch
    audit_log._rotate_if_needed(tmp_path / "absent.jsonl")


def test_clear_audit_log_removes_file(tmp_path, monkeypatch):
    log_path = tmp_path / "log.jsonl"
    monkeypatch.setattr(audit_log, "get_audit_log_path", lambda: log_path)
    log_path.write_text("data", encoding="utf-8")
    assert audit_log.clear_audit_log() is True
    assert not log_path.exists()


def test_clear_audit_log_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(audit_log, "get_audit_log_path", lambda: tmp_path / "absent.jsonl")
    assert audit_log.clear_audit_log() is True


def test_clear_audit_log_failure(tmp_path, monkeypatch, caplog):
    # Path is a non-empty directory -> unlink() raises (lines 262-264).
    log_dir = tmp_path / "log.jsonl"
    log_dir.mkdir()
    (log_dir / "child").write_text("x", encoding="utf-8")
    monkeypatch.setattr(audit_log, "get_audit_log_path", lambda: log_dir)
    assert audit_log.clear_audit_log() is False
    assert "Failed to clear audit log" in caplog.text


# =====================================================================
# format_entries_for_display
# =====================================================================


def test_format_empty():
    assert audit_log.format_entries_for_display([]) == "No audit log entries found."


def test_format_timestamp_parse_failure_uses_raw():
    # int timestamp -> ts.replace raises AttributeError -> except branch
    out = audit_log.format_entries_for_display(
        [{"timestamp": 12345, "action": "create_folder", "details": {"name": "F"}}]
    )
    assert "12345" in out


def test_format_all_action_types_and_truncation():
    many = [f"track{i}" for i in range(7)]  # >5 to hit the "... and N more"
    entries = [
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "action": "add_to_library",
            "details": {"tracks": many},
        },
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "action": "remove_from_library",
            "details": {"tracks": many},
        },
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "action": "add_to_playlist",
            "details": {"playlist": "P", "tracks": many},
        },
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "action": "remove_from_playlist",
            "details": {"playlist": "P", "tracks": many},
        },
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "action": "create_playlist",
            "details": {"name": "N", "playlist_id": "p.1"},
        },
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "action": "delete_playlist",
            "details": {"name": "N", "track_count": 3},
        },
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "action": "copy_playlist",
            "details": {"source": "A", "destination": "B", "track_count": 2},
        },
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "action": "rating",
            "details": {"track": "T", "type": "love", "value": ""},
        },
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "action": "create_folder",
            "details": {"name": "F"},
        },
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "action": "delete_folder",
            "details": {"name": "F"},
        },
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "action": "rename_folder",
            "details": {"old_name": "O", "new_name": "X"},
        },
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "action": "move_to_root",
            "details": {"playlist": "P"},
        },
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "action": "move_to_folder",
            "details": {"playlist": "P", "folder": "F"},
        },
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "action": "set_preference",
            "details": {"preference": "x", "old_value": 1, "new_value": 2},
        },
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "action": "playlist_query",
            "details": {
                "playlist": "P",
                "track_count": 4,
                "duration_sec": 1,
                "cache_hits": 2,
                "cache_misses": 1,
                "api_calls": 3,
            },
        },
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "action": "unknown_action",
            "details": {"foo": "bar"},
        },
    ]
    out = audit_log.format_entries_for_display(entries, limit=100)
    assert "ADD TO LIBRARY: 7 track(s)" in out
    assert "REMOVE FROM LIBRARY: 7 track(s)" in out
    assert "... and 2 more" in out
    assert "ADD TO PLAYLIST 'P'" in out
    assert "REMOVE FROM PLAYLIST 'P'" in out
    assert "CREATE PLAYLIST: 'N' (ID: p.1)" in out
    assert "DELETE PLAYLIST: 'N' (3 tracks)" in out
    assert "COPY PLAYLIST: 'A' -> 'B' (2 tracks)" in out
    assert "RATING: love 'T'" in out
    assert "CREATE FOLDER: 'F'" in out
    assert "DELETE FOLDER: 'F'" in out
    assert "RENAME FOLDER: 'O' -> 'X'" in out
    assert "MOVE TO ROOT: 'P'" in out
    assert "MOVE TO FOLDER: 'P' -> 'F'" in out
    assert "SET PREFERENCE: x = 2 (was: 1)" in out
    assert "PLAYLIST QUERY: 'P' (4 tracks)" in out
    assert "UNKNOWN_ACTION:" in out
    assert "2026-01-01 00:00:00 UTC" in out


def test_format_more_entries_than_limit():
    entries = [
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "action": "create_folder",
            "details": {"name": str(i)},
        }
        for i in range(5)
    ]
    out = audit_log.format_entries_for_display(entries, limit=2)
    assert "... 3 more entries" in out
