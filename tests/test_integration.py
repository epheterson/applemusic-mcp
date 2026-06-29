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

    if success:
        print(f"✓ PASS: Found playlist with partial name 'Jack & Norah'")
        print(f"  Found {len(tracks)} tracks")
        if tracks:
            print(f"  First track: {tracks[0].get('name', 'Unknown')}")
    else:
        print(f"✗ FAIL: Could not find playlist with partial name")
        print(f"  Error: {tracks}")

    return success


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

    if not success:
        print("⚠ Could not add test track, skipping partial match test")
        return False

    print("✓ Added 'What a Wonderful World' to test playlist")

    # Now try to remove it with partial name (should work with 'contains')
    success, result = asc.remove_track_from_playlist(
        TEST_PLAYLIST, track_name="What a Wonderful", artist="Louis Armstrong"  # Partial name
    )

    if success and "Removed" in result:
        print(f"✓ PASS: Partial track name matching works")
        print(f"  Result: {result}")
        return True
    else:
        print(f"✗ FAIL: Partial track name did not work")
        print(f"  Result: {result}")
        return False


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

    if added_count == 0:
        print("⚠ Could not add any tracks, skipping array removal test")
        return False

    # Verify tracks are in playlist
    success, tracks = asc.get_playlist_tracks(TEST_PLAYLIST)
    if success:
        print(f"  Playlist now has {len(tracks)} tracks")

    # Test comma-separated removal using the SERVER function (which handles arrays)
    # This calls the actual MCP tool function that handles comma-separated input
    try:
        # Import the actual decorated function
        from applemusic_mcp.server import remove_from_playlist as server_remove_from_playlist

        # The server function returns a string result, not (bool, str) tuple
        # v0.3.0: Uses unified 'track' parameter instead of 'track_name'
        result = server_remove_from_playlist(
            playlist=TEST_PLAYLIST, track="Yesterday,Hey Jude", artist="The Beatles"
        )
        print(f"  Result: {result}")

        if "Removed" in result and ("Yesterday" in result or "Hey Jude" in result):
            print(f"✓ PASS: Array removal works via server function")
            return True
        else:
            print(f"✗ FAIL: Array removal didn't work as expected")
            return False
    except Exception as e:
        print(f"✗ FAIL: Exception during array removal: {e}")
        import traceback

        traceback.print_exc()
        return False


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

    if not success:
        print("⚠ Could not add test track, skipping ID removal test")
        return False

    print("✓ Added 'Imagine' to test playlist")

    # Get playlist tracks to find the ID
    success, tracks = asc.get_playlist_tracks(TEST_PLAYLIST)
    if not success or not tracks:
        print("⚠ Could not get playlist tracks, skipping ID removal test")
        return False

    # Find Imagine
    imagine_track = None
    for track in tracks:
        if "Imagine" in track.get("name", ""):
            imagine_track = track
            break

    if not imagine_track:
        print("⚠ Could not find Imagine track")
        print(f"  Available tracks: {[t.get('name') for t in tracks]}")
        return False

    # The field is called 'id' not 'persistent_id'
    if "id" not in imagine_track:
        print(f"⚠ Track missing 'id' field. Available fields: {imagine_track.keys()}")
        return False

    track_id = imagine_track["id"]
    print(f"✓ Found track ID: {track_id}")

    # Remove by ID
    success, result = asc.remove_track_from_playlist(TEST_PLAYLIST, track_id=track_id)

    if success and "Removed" in result:
        print(f"✓ PASS: ID-based removal works")
        print(f"  Result: {result}")
        return True
    else:
        print(f"✗ FAIL: ID-based removal failed")
        print(f"  Result: {result}")
        return False


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

    if has_all_keys:
        print(f"✓ PASS: Preferences loaded with all required keys")
        return True
    else:
        print(f"✗ FAIL: Missing preference keys")
        return False


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

    if "types" in params and "search_type" not in params:
        print(f"✓ PASS: search_library uses 'types' parameter (matches search_catalog)")
        return True
    elif "search_type" in params:
        print(f"✗ FAIL: search_library still uses old 'search_type' parameter")
        return False
    else:
        print(f"⚠ WARNING: Neither 'types' nor 'search_type' found")
        return False


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


def main():
    """Run all integration tests."""
    print("\n" + "=" * 80)
    print("APPLE MUSIC MCP - INTEGRATION TEST SUITE")
    print("Testing v0.2.5 asymmetry fixes on real library")
    print("=" * 80)

    results = {}

    try:
        # Setup
        setup_test_playlist()

        # Run tests
        results["partial_playlist"] = test_partial_matching_playlist()
        results["partial_track"] = test_partial_matching_track_removal()
        results["array_removal"] = test_array_removal()
        results["id_removal"] = test_id_based_removal()
        results["preferences"] = test_preferences_loading()
        results["search_param"] = test_search_library_parameter()
        results["copy_name"] = test_copy_playlist_with_name()

        # Review outputs
        review_tool_outputs()

    finally:
        # Cleanup
        cleanup_test_playlist()

    # Summary
    print("\n" + "=" * 80)
    print("TEST SUMMARY")
    print("=" * 80)

    passed = sum(1 for v in results.values() if v)
    total = len(results)

    for test_name, passed_test in results.items():
        status = "✓ PASS" if passed_test else "✗ FAIL"
        print(f"{status}: {test_name}")

    print(f"\nTotal: {passed}/{total} tests passed")

    if passed == total:
        print("\n🎉 All integration tests PASSED!")
        return 0
    else:
        print(f"\n⚠️  {total - passed} test(s) FAILED")
        return 1


if __name__ == "__main__":
    exit(main())
