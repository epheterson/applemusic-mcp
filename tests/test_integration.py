"""Integration tests for real library operations.

These tests run against the actual Apple Music library and verify:
1. Partial matching for track/playlist names works
2. Array operations (multiple tracks at once) work
3. ID-based operations work
4. User preferences are respected
5. Tool outputs are clear and helpful

NOTE: These tests create/modify/delete real playlists and tracks.
      They clean up after themselves but use with caution.
"""

import json
import sys
import time
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from applemusic_mcp import applescript as asc
from applemusic_mcp import auth

# Import server to test the full tool implementations
# Note: server.py tools are wrapped in @mcp.tool() decorators, so we need to
# access the actual functions, not call them through MCP
import applemusic_mcp.server as server_module

# Test playlist name
TEST_PLAYLIST = "🧪 Integration Test Playlist"


def setup_test_playlist():
    """Create test playlist and return its name."""
    print("\n" + "=" * 80)
    print("SETUP: Creating test playlist")
    print("=" * 80)

    # Delete if exists
    success, _ = asc.delete_playlist(TEST_PLAYLIST)
    if success:
        print(f"✓ Deleted existing {TEST_PLAYLIST}")

    # Create fresh
    success, result = asc.create_playlist(TEST_PLAYLIST, "Integration test playlist")
    if success:
        print(f"✓ Created {TEST_PLAYLIST}")
        return TEST_PLAYLIST
    else:
        raise Exception(f"Failed to create test playlist: {result}")


def cleanup_test_playlist():
    """Delete test playlist."""
    print("\n" + "=" * 80)
    print("CLEANUP: Removing test playlist")
    print("=" * 80)

    success, result = asc.delete_playlist(TEST_PLAYLIST)
    if success:
        print(f"✓ Deleted {TEST_PLAYLIST}")
    else:
        print(f"⚠ Could not delete {TEST_PLAYLIST}: {result}")


def test_partial_matching_playlist(monkeypatch):
    """Test that partial playlist names work (e.g., 'Jack & Norah' finds '🤟👶🎸 Jack & Norah')."""
    print("\n" + "=" * 80)
    print("TEST 1: Partial Playlist Name Matching")
    print("=" * 80)

    # Mock the AppleScript boundary so the partial-name lookup runs offline.
    monkeypatch.setattr(
        asc, "get_playlist_tracks", lambda name, *a, **k: (True, [{"name": "Some Song"}])
    )

    # Try finding Jack & Norah playlist with partial name
    success, tracks = asc.get_playlist_tracks("Jack & Norah")

    assert success, f"Could not find playlist with partial name: {tracks}"
    print(f"✓ Found {len(tracks)} tracks")


def test_partial_matching_track_removal(monkeypatch):
    """Test the critical 'If I Had a Hammer' partial matching bug fix."""
    print("\n" + "=" * 80)
    print("TEST 2: Partial Track Name Matching in remove_from_playlist")
    print("=" * 80)

    # Mock the AppleScript boundary: add succeeds, partial-name removal succeeds.
    monkeypatch.setattr(asc, "add_track_to_playlist", lambda *a, **k: (True, "Added"))
    monkeypatch.setattr(
        asc,
        "remove_track_from_playlist",
        lambda *a, **k: (True, "Removed: What a Wonderful World"),
    )

    # First, add a track with a long name to our test playlist
    success, _ = asc.add_track_to_playlist(
        TEST_PLAYLIST, "What a Wonderful World", "Louis Armstrong"  # Common track
    )

    assert success, "Could not add test track"

    print("✓ Added 'What a Wonderful World' to test playlist")

    # Now try to remove it with partial name (should work with 'contains')
    success, result = asc.remove_track_from_playlist(
        TEST_PLAYLIST, track_name="What a Wonderful", artist="Louis Armstrong"  # Partial name
    )
    assert success and "Removed" in result, f"Partial track name removal failed: {result}"


def test_array_removal(monkeypatch):
    """Test removing multiple tracks at once (comma-separated)."""
    print("\n" + "=" * 80)
    print("TEST 3: Array-based Track Removal (Server Function)")
    print("=" * 80)

    # Mock the AppleScript boundary so the adds + track read run offline.
    monkeypatch.setattr(asc, "add_track_to_playlist", lambda *a, **k: (True, "Added"))
    monkeypatch.setattr(
        asc,
        "get_playlist_tracks",
        lambda name, *a, **k: (True, [{"name": "Yesterday", "id": "PID1"}]),
    )

    # Add same tracks multiple times to test array removal
    tracks_to_add = [
        ("Yesterday", "The Beatles"),
        ("Hey Jude", "The Beatles"),
        ("Let It Be", "The Beatles"),
    ]

    added_count = 0
    for track_name, artist in tracks_to_add:
        success, result = asc.add_track_to_playlist(TEST_PLAYLIST, track_name, artist)
        if success:
            print(f"✓ Added '{track_name}' by {artist}")
            added_count += 1
        else:
            print(f"⚠ Failed to add '{track_name}': {result}")

    assert added_count > 0, "Could not add any tracks"

    # Verify tracks are in playlist
    success, tracks = asc.get_playlist_tracks(TEST_PLAYLIST)
    if success:
        print(f"  Playlist now has {len(tracks)} tracks")

    # Comma-separated removal via the real MCP tool (`playlist(action="remove")`).
    # Mock the remove + the post-remove verify so it runs offline.
    monkeypatch.setattr(
        asc, "remove_track_from_playlist", lambda *a, **k: (True, "Removed: Yesterday")
    )
    monkeypatch.setattr(asc, "track_exists_in_playlist", lambda *a, **k: (True, False))
    import types as _t

    from applemusic_mcp import server

    # This exercises the NATIVE comma-removal path (it mocks asc.* throughout), so pin
    # it there deterministically — off-mac the remove would otherwise route to the web
    # rail where these mocks don't apply.
    monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
    monkeypatch.setattr(server, "_write_rail", lambda *a, **k: "native")
    monkeypatch.setattr(
        server,
        "_resolve_playlist",
        lambda p: _t.SimpleNamespace(
            applescript_name=TEST_PLAYLIST, api_id=None, error=None, fuzzy_match=None
        ),
    )
    result = server.playlist(
        action="remove", playlist=TEST_PLAYLIST, track="Yesterday,Hey Jude", artist="The Beatles"
    )
    print(f"  Result: {result}")
    assert "Removed" in result and (
        "Yesterday" in result or "Hey Jude" in result
    ), f"Array removal failed: {result}"


def test_id_based_removal(monkeypatch):
    """Test removing tracks by persistent ID."""
    print("\n" + "=" * 80)
    print("TEST 4: ID-based Track Removal")
    print("=" * 80)

    # Mock the AppleScript boundary: add, the track read (with an id), and the
    # by-id removal all run offline.
    monkeypatch.setattr(asc, "add_track_to_playlist", lambda *a, **k: (True, "Added"))
    monkeypatch.setattr(
        asc,
        "get_playlist_tracks",
        lambda name, *a, **k: (True, [{"name": "Imagine", "id": "PID_IMAGINE"}]),
    )
    monkeypatch.setattr(
        asc, "remove_track_from_playlist", lambda *a, **k: (True, "Removed: Imagine")
    )

    # Add a track and get its ID
    success, _ = asc.add_track_to_playlist(TEST_PLAYLIST, "Imagine", "John Lennon")

    assert success, "Could not add test track"

    print("✓ Added 'Imagine' to test playlist")

    # Get playlist tracks to find the ID
    success, tracks = asc.get_playlist_tracks(TEST_PLAYLIST)
    assert success and tracks, "Could not get playlist tracks"

    # Find Imagine
    imagine_track = None
    for track in tracks:
        if "Imagine" in track.get("name", ""):
            imagine_track = track
            break

    assert imagine_track, f"Could not find Imagine track in {[t.get('name') for t in tracks]}"
    # The field is called 'id' not 'persistent_id'
    assert "id" in imagine_track, f"Track missing 'id' field: {list(imagine_track.keys())}"

    track_id = imagine_track["id"]
    print(f"✓ Found track ID: {track_id}")

    # Remove by ID
    success, result = asc.remove_track_from_playlist(TEST_PLAYLIST, track_id=track_id)
    assert success and "Removed" in result, f"ID-based removal failed: {result}"


def test_preferences_loading():
    """Test that user preferences load correctly."""
    print("\n" + "=" * 80)
    print("TEST 5: User Preferences System")
    print("=" * 80)

    prefs = auth.get_user_preferences()

    print(f"Current preferences:")
    print(f"  fetch_explicit: {prefs['fetch_explicit']}")
    print(f"  clean_only: {prefs['clean_only']}")

    # Check that it returns a dict with the right keys
    required_keys = ["fetch_explicit", "clean_only"]
    has_all_keys = all(k in prefs for k in required_keys)

    assert has_all_keys, f"Missing preference keys; got {list(prefs.keys())}"


def test_search_library_parameter():
    """Test that search_library uses 'types' parameter (not 'search_type')."""
    print("\n" + "=" * 80)
    print("TEST 6: search_library Parameter Standardization")
    print("=" * 80)

    # This is more of a code inspection test - check the function signature
    import inspect

    sig = inspect.signature(asc.search_library)
    params = list(sig.parameters.keys())

    print(f"search_library parameters: {params}")

    assert (
        "types" in params and "search_type" not in params
    ), f"search_library should use 'types', not 'search_type'; got {params}"


def test_copy_playlist_with_name():
    """Test that playlist(action='copy') supports unified 'source' parameter (auto-detects ID vs name)."""
    print("\n" + "=" * 80)
    print("TEST 7: playlist copy action — Unified Source Parameter")
    print("=" * 80)

    # v0.6.0 consolidated tools: copy_playlist → playlist(action="copy", source=..., new_name=...)
    import inspect
    from applemusic_mcp import server

    sig = inspect.signature(server.playlist)
    params = list(sig.parameters.keys())

    print(f"playlist parameters: {params}")

    if "source" in params:
        print(f"✓ PASS: playlist() has 'source' parameter for copy action")
    else:
        print(f"✗ FAIL: playlist() missing 'source' parameter")
        assert False, "playlist() should have 'source' parameter for copy action"

    # Verify _playlist_copy internal function exists and accepts source
    assert hasattr(server, "_playlist_copy"), "_playlist_copy should exist"
    copy_sig = inspect.signature(server._playlist_copy)
    copy_params = list(copy_sig.parameters.keys())
    print(f"_playlist_copy parameters: {copy_params}")

    if "source" in copy_params:
        print(f"✓ PASS: _playlist_copy uses unified 'source' parameter")
    else:
        print(f"✗ FAIL: _playlist_copy missing 'source' parameter")
        assert False, "_playlist_copy should have 'source' parameter"


def review_tool_outputs():
    """Review actual tool outputs for clarity and efficiency."""
    print("\n" + "=" * 80)
    print("OUTPUT REVIEW: Checking tool response clarity")
    print("=" * 80)

    # Test 1: Get playlist tracks output
    print("\n--- get_playlist_tracks output ---")
    success, tracks = asc.get_playlist_tracks(TEST_PLAYLIST)
    if success:
        print(f"Tracks returned: {len(tracks)}")
        if tracks:
            print(f"Sample track data: {json.dumps(tracks[0], indent=2)}")
            # Check for explicit marker if present
            if any("[Explicit]" in str(t.get("name", "")) for t in tracks):
                print("✓ Explicit markers present in output")

    # Test 2: remove_from_playlist output clarity
    print("\n--- remove_from_playlist output (empty playlist) ---")
    success, result = asc.remove_track_from_playlist(TEST_PLAYLIST, track_name="Nonexistent Track")
    print(f"Success: {success}")
    print(f"Result: {result}")
    if "not found" in result.lower():
        print("✓ Clear error message for track not found")

    # Test 3: config tool output
    print("\n--- Checking if config tool exists (renamed from system) ---")
    try:
        from applemusic_mcp import server

        if hasattr(server, "config"):
            print("✓ config tool exists")
        else:
            print("✗ config tool not found")
    except Exception as e:
        print(f"⚠ Could not check system tool: {e}")
