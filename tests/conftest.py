"""Shared test fixtures."""

import json
from unittest.mock import patch

import pytest

from applemusic_mcp import applescript as asc
from applemusic_mcp import audit_log


# Mock audit log for all tests to avoid polluting real audit log
@pytest.fixture(autouse=True)
def mock_audit_log_for_all_tests(tmp_path):
    """Ensure all tests use a temp audit log, not the real one."""
    audit_dir = tmp_path / ".cache" / "applemusic-mcp"
    audit_dir.mkdir(parents=True)
    log_path = audit_dir / "audit_log.jsonl"
    with patch.object(audit_log, "get_audit_log_path", return_value=log_path):
        yield log_path


# Test debris markers. A playlist/folder is debris if its name starts with
# one of these prefixes, or exactly matches one of the legacy single names.
_TEST_NAME_PREFIXES = ("_TEST_", "_VERIFY", "_UI_TEST_", "_SNAPSHOT_TEST_")
_TEST_NAME_EXACT = ("__TEST_PLAYLIST__", "🧪 Integration Test Playlist")


def _sweep_test_debris():
    """Delete all test playlists AND folders from the live Music library.

    Idempotent and duplicate-safe: Music.app cheerfully allows duplicate
    names, so the sweep matches by prefix and deletes every hit.

    Covers BOTH collections. `folder playlist` is a *separate* collection
    that `every user playlist` does NOT include — the earlier teardown
    looped only user playlists, so every test folder (`_TEST_RENAME_FOLDER_`
    et al.) leaked forever. Folders are swept first so deleting a folder
    also takes any test playlists nested inside it.
    """
    if not asc.is_available():
        return

    # Single-name targets to delete unconditionally if present.
    for name in _TEST_NAME_EXACT:
        try:
            asc.delete_playlist(name)
        except Exception:
            pass

    # Prefix-match sweep over folders then user playlists, in one shell-out.
    conds = " or ".join(f'(pn starts with "{p}")' for p in _TEST_NAME_PREFIXES)
    asc.run_applescript(f"""
tell application "Music"
    repeat with p in (every folder playlist)
        try
            set pn to name of p
            if {conds} then
                try
                    delete p
                end try
            end if
        end try
    end repeat
    repeat with p in (every user playlist)
        try
            set pn to name of p
            if {conds} then
                try
                    delete p
                end try
            end if
        end try
    end repeat
end tell""")


# Clean up test playlists/folders before AND after the test session.
@pytest.fixture(scope="session", autouse=True)
def cleanup_test_playlists():
    """Safety net that keeps the user's Music library free of test debris.

    Sweeps known test markers across all test classes:
    - Single names (legacy): __TEST_PLAYLIST__, 🧪 Integration Test Playlist
    - Prefixed: _TEST_*, _VERIFY*, _UI_TEST_, _SNAPSHOT_TEST_

    Runs the sweep TWICE:
    - Pre-clean (before yield) heals debris left by a previously interrupted
      run (Ctrl-C, timeout, crash) so this run starts from a clean library.
    - Teardown (after yield) removes this run's debris.

    Each test should still clean up its own debris inline, but an assertion
    failure aborts a test before its inline cleanup runs — so without this
    net the library slowly fills with `_TEST_*` junk.
    """
    _sweep_test_debris()  # pre-clean
    yield
    _sweep_test_debris()  # teardown


@pytest.fixture
def temp_config_dir(tmp_path):
    """Create a temporary config directory."""
    config_dir = tmp_path / ".config" / "applemusic-mcp"
    config_dir.mkdir(parents=True)
    return config_dir


@pytest.fixture
def mock_config_dir(temp_config_dir, monkeypatch):
    """Patch get_config_dir to use temp directory."""
    from applemusic_mcp import auth

    monkeypatch.setattr(auth, "DEFAULT_CONFIG_DIR", temp_config_dir)
    return temp_config_dir


@pytest.fixture
def sample_config():
    """Sample configuration data."""
    return {
        "team_id": "TEST_TEAM_ID",
        "key_id": "TEST_KEY_ID",
        "private_key_path": "~/.config/applemusic-mcp/AuthKey_TEST.p8",
    }


@pytest.fixture
def sample_private_key():
    """Sample EC private key for testing (not a real key)."""
    # This is a test-only key, generated for testing purposes
    return """-----BEGIN PRIVATE KEY-----
MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgtest1234567890
abcdefghijklmnopqrstuvwxyzABCDEFGHhRANCAARtest1234567890abcdefgh
ijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890abcdefghi
-----END PRIVATE KEY-----"""


@pytest.fixture
def configured_config_dir(mock_config_dir, sample_config, sample_private_key):
    """Config directory with config.json and private key."""
    # Write config
    config_file = mock_config_dir / "config.json"
    with open(config_file, "w") as f:
        json.dump(sample_config, f)

    # Write fake private key
    key_file = mock_config_dir / "AuthKey_TEST.p8"
    with open(key_file, "w") as f:
        f.write(sample_private_key)

    # Update config to use actual path
    sample_config["private_key_path"] = str(key_file)
    with open(config_file, "w") as f:
        json.dump(sample_config, f)

    return mock_config_dir


@pytest.fixture
def mock_developer_token():
    """A mock developer token."""
    return "eyJhbGciOiJFUzI1NiIsInR5cCI6IkpXVCIsImtpZCI6IlRFU1RfS0VZX0lEIn0.eyJpc3MiOiJURVNUX1RFQU1fSUQiLCJpYXQiOjE3MDAwMDAwMDAsImV4cCI6MTcxNTAwMDAwMH0.test_signature"


@pytest.fixture
def mock_user_token():
    """A mock music user token."""
    return "Atest1234567890abcdefghijklmnopqrstuvwxyz"


@pytest.fixture
def mock_api_headers(mock_developer_token, mock_user_token):
    """Mock API headers."""
    return {
        "Authorization": f"Bearer {mock_developer_token}",
        "Music-User-Token": mock_user_token,
        "Content-Type": "application/json",
    }
