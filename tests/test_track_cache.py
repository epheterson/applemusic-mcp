"""Tests for track metadata cache module.

Tests the TrackCache class that stores stable track metadata
(explicit status, ISRC) indexed by multiple ID types.
"""

import pytest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from applemusic_mcp.track_cache import TrackCache, get_track_cache


class TestTrackCacheBasics:
    """Test basic cache operations."""

    def test_cache_initialization(self, tmp_path):
        """Should initialize with empty sections."""
        with patch("applemusic_mcp.track_cache.get_cache_dir", return_value=tmp_path):
            cache = TrackCache()
            assert cache._cache == {"tracks": {}, "albums": {}, "name_index": {}}
            assert cache.cache_file == tmp_path / "cache.json"

    def test_cache_file_created(self, tmp_path):
        """Should create cache file on save."""
        with patch("applemusic_mcp.track_cache.get_cache_dir", return_value=tmp_path):
            cache = TrackCache()
            cache.set_track_metadata(explicit="No", persistent_id="ABC123")
            assert cache.cache_file.exists()


class TestGetExplicit:
    """Test explicit status retrieval."""

    def test_returns_none_for_uncached_track(self, tmp_path):
        """Should return None when track not in cache."""
        with patch("applemusic_mcp.track_cache.get_cache_dir", return_value=tmp_path):
            cache = TrackCache()
            assert cache.get_explicit("unknown_id") is None

    def test_returns_explicit_yes(self, tmp_path):
        """Should return 'Yes' for explicit tracks."""
        with patch("applemusic_mcp.track_cache.get_cache_dir", return_value=tmp_path):
            cache = TrackCache()
            cache.set_track_metadata(explicit="Yes", persistent_id="EXPLICIT123")
            assert cache.get_explicit("EXPLICIT123") == "Yes"

    def test_returns_explicit_no(self, tmp_path):
        """Should return 'No' for clean tracks."""
        with patch("applemusic_mcp.track_cache.get_cache_dir", return_value=tmp_path):
            cache = TrackCache()
            cache.set_track_metadata(explicit="No", persistent_id="CLEAN123")
            assert cache.get_explicit("CLEAN123") == "No"


class TestMultiIDIndexing:
    """Test that tracks are indexed by all three ID types."""

    def test_cache_by_persistent_id(self, tmp_path):
        """Should cache by persistent ID."""
        with patch("applemusic_mcp.track_cache.get_cache_dir", return_value=tmp_path):
            cache = TrackCache()
            cache.set_track_metadata(explicit="No", persistent_id="PERSIST123")
            assert cache.get_explicit("PERSIST123") == "No"

    def test_cache_by_library_id(self, tmp_path):
        """Should cache by library ID."""
        with patch("applemusic_mcp.track_cache.get_cache_dir", return_value=tmp_path):
            cache = TrackCache()
            cache.set_track_metadata(explicit="No", library_id="i.LIB123")
            assert cache.get_explicit("i.LIB123") == "No"

    def test_cache_by_catalog_id(self, tmp_path):
        """Should cache by catalog ID."""
        with patch("applemusic_mcp.track_cache.get_cache_dir", return_value=tmp_path):
            cache = TrackCache()
            cache.set_track_metadata(explicit="No", catalog_id="1440783617")
            assert cache.get_explicit("1440783617") == "No"

    def test_cache_by_all_three_ids(self, tmp_path):
        """Should cache by all three IDs simultaneously."""
        with patch("applemusic_mcp.track_cache.get_cache_dir", return_value=tmp_path):
            cache = TrackCache()
            cache.set_track_metadata(
                explicit="Yes",
                persistent_id="PERSIST123",
                library_id="i.LIB123",
                catalog_id="1440783617",
            )
            # All three IDs should return same explicit status
            assert cache.get_explicit("PERSIST123") == "Yes"
            assert cache.get_explicit("i.LIB123") == "Yes"
            assert cache.get_explicit("1440783617") == "Yes"

    def test_only_caches_provided_ids(self, tmp_path):
        """Should only cache by IDs that are provided."""
        with patch("applemusic_mcp.track_cache.get_cache_dir", return_value=tmp_path):
            cache = TrackCache()
            cache.set_track_metadata(
                explicit="No",
                persistent_id="PERSIST123",
                # library_id and catalog_id not provided
            )
            assert cache.get_explicit("PERSIST123") == "No"
            # Should have exactly one track entry
            assert len(cache._cache["tracks"]) == 1


class TestISRCStorage:
    """Test ISRC (International Standard Recording Code) storage."""

    def test_stores_isrc(self, tmp_path):
        """Should store ISRC when provided."""
        with patch("applemusic_mcp.track_cache.get_cache_dir", return_value=tmp_path):
            cache = TrackCache()
            cache.set_track_metadata(explicit="No", persistent_id="TRACK123", isrc="USRC19300278")
            assert cache._cache["tracks"]["TRACK123"]["isrc"] == "USRC19300278"

    def test_omits_isrc_when_not_provided(self, tmp_path):
        """Should not include ISRC key when not provided."""
        with patch("applemusic_mcp.track_cache.get_cache_dir", return_value=tmp_path):
            cache = TrackCache()
            cache.set_track_metadata(explicit="No", persistent_id="TRACK123")
            assert "isrc" not in cache._cache["tracks"]["TRACK123"]

    def test_stores_isrc_with_multiple_ids(self, tmp_path):
        """Should store ISRC accessible via all IDs."""
        with patch("applemusic_mcp.track_cache.get_cache_dir", return_value=tmp_path):
            cache = TrackCache()
            cache.set_track_metadata(
                explicit="No",
                persistent_id="PERSIST123",
                library_id="i.LIB123",
                catalog_id="1440783617",
                isrc="USRC19300278",
            )
            # ISRC should be accessible via any ID
            assert cache._cache["tracks"]["PERSIST123"]["isrc"] == "USRC19300278"
            assert cache._cache["tracks"]["i.LIB123"]["isrc"] == "USRC19300278"
            assert cache._cache["tracks"]["1440783617"]["isrc"] == "USRC19300278"


class TestCachePersistence:
    """Test cache save/load from disk."""

    def test_saves_to_disk(self, tmp_path):
        """Should save cache to JSON file."""
        with patch("applemusic_mcp.track_cache.get_cache_dir", return_value=tmp_path):
            cache = TrackCache()
            cache.set_track_metadata(explicit="No", persistent_id="TRACK123", isrc="USRC19300278")

            # Verify file exists and contains data
            assert cache.cache_file.exists()
            with open(cache.cache_file, "r") as f:
                data = json.load(f)
                assert "TRACK123" in data["tracks"]
                assert data["tracks"]["TRACK123"]["explicit"] == "No"
                assert data["tracks"]["TRACK123"]["isrc"] == "USRC19300278"

    def test_loads_from_disk(self, tmp_path):
        """Should load existing cache from disk."""
        with patch("applemusic_mcp.track_cache.get_cache_dir", return_value=tmp_path):
            # Create cache file manually (new format)
            cache_file = tmp_path / "cache.json"
            cache_data = {
                "tracks": {"TRACK123": {"explicit": "Yes", "isrc": "USRC19300278"}},
                "albums": {},
                "name_index": {},
            }
            with open(cache_file, "w") as f:
                json.dump(cache_data, f)

            # Load cache
            cache = TrackCache()
            assert cache.get_explicit("TRACK123") == "Yes"
            assert cache._cache["tracks"]["TRACK123"]["isrc"] == "USRC19300278"

    def test_handles_missing_cache_file(self, tmp_path):
        """Should initialize empty cache when file doesn't exist."""
        with patch("applemusic_mcp.track_cache.get_cache_dir", return_value=tmp_path):
            cache = TrackCache()
            assert cache._cache == {"tracks": {}, "albums": {}, "name_index": {}}

    def test_handles_corrupted_cache_file(self, tmp_path):
        """Should initialize empty cache when file is corrupted."""
        with patch("applemusic_mcp.track_cache.get_cache_dir", return_value=tmp_path):
            # Create corrupted cache file
            cache_file = tmp_path / "cache.json"
            with open(cache_file, "w") as f:
                f.write("{ this is not valid json")

            # Should handle gracefully
            cache = TrackCache()
            assert cache._cache == {"tracks": {}, "albums": {}, "name_index": {}}

    def test_persists_across_instances(self, tmp_path):
        """Should persist data across cache instances."""
        with patch("applemusic_mcp.track_cache.get_cache_dir", return_value=tmp_path):
            # First instance
            cache1 = TrackCache()
            cache1.set_track_metadata(explicit="No", persistent_id="TRACK123")

            # Second instance (simulates restart)
            cache2 = TrackCache()
            assert cache2.get_explicit("TRACK123") == "No"


class TestClearCache:
    """Test cache clearing functionality."""

    def test_clear_removes_all_entries(self, tmp_path):
        """Should remove all cache entries."""
        with patch("applemusic_mcp.track_cache.get_cache_dir", return_value=tmp_path):
            cache = TrackCache()
            cache.set_track_metadata(explicit="No", persistent_id="TRACK1")
            cache.set_track_metadata(explicit="Yes", persistent_id="TRACK2")

            assert len(cache._cache["tracks"]) == 2
            cache.clear()
            assert len(cache._cache["tracks"]) == 0
            assert len(cache._cache["albums"]) == 0
            assert len(cache._cache["name_index"]) == 0

    def test_clear_persists_to_disk(self, tmp_path):
        """Should save empty cache to disk."""
        with patch("applemusic_mcp.track_cache.get_cache_dir", return_value=tmp_path):
            cache = TrackCache()
            cache.set_track_metadata(explicit="No", persistent_id="TRACK1")
            cache.clear()

            # Reload and verify empty
            cache2 = TrackCache()
            assert cache2._cache == {"tracks": {}, "albums": {}, "name_index": {}}


class TestGlobalCacheInstance:
    """Test global cache singleton pattern."""

    def test_get_track_cache_returns_instance(self):
        """Should return TrackCache instance."""
        cache = get_track_cache()
        assert isinstance(cache, TrackCache)

    def test_get_track_cache_returns_same_instance(self):
        """Should return same instance on multiple calls (singleton)."""
        cache1 = get_track_cache()
        cache2 = get_track_cache()
        assert cache1 is cache2


class TestEdgeCases:
    """Test edge cases and error handling."""

    def test_handles_none_ids(self, tmp_path):
        """Should handle None IDs gracefully."""
        with patch("applemusic_mcp.track_cache.get_cache_dir", return_value=tmp_path):
            cache = TrackCache()
            cache.set_track_metadata(
                explicit="No", persistent_id=None, library_id=None, catalog_id="1440783617"
            )
            # Should only cache by catalog ID
            assert len(cache._cache["tracks"]) == 1
            assert cache.get_explicit("1440783617") == "No"

    def test_handles_empty_string_ids(self, tmp_path):
        """Should not cache empty string IDs."""
        with patch("applemusic_mcp.track_cache.get_cache_dir", return_value=tmp_path):
            cache = TrackCache()
            cache.set_track_metadata(
                explicit="No", persistent_id="", library_id="", catalog_id="1440783617"
            )
            # Empty strings are falsy, should only cache catalog ID
            assert len(cache._cache["tracks"]) == 1
            assert cache.get_explicit("1440783617") == "No"

    def test_does_not_overwrite_existing_entries(self, tmp_path):
        """Should not overwrite existing cache entries."""
        with patch("applemusic_mcp.track_cache.get_cache_dir", return_value=tmp_path):
            cache = TrackCache()
            # First set
            cache.set_track_metadata(explicit="No", persistent_id="TRACK123", isrc="USRC19300278")
            # Second set with same ID (shouldn't overwrite)
            cache.set_track_metadata(explicit="Yes", persistent_id="TRACK123", isrc="USRC99999999")
            # Should keep original
            assert cache.get_explicit("TRACK123") == "No"
            assert cache._cache["tracks"]["TRACK123"]["isrc"] == "USRC19300278"

    def test_handles_save_errors_gracefully(self, tmp_path):
        """Should handle save errors without crashing."""
        with patch("applemusic_mcp.track_cache.get_cache_dir", return_value=tmp_path):
            cache = TrackCache()
            # Make cache file read-only to cause save error
            cache.cache_file.touch()
            cache.cache_file.chmod(0o444)

            # Should not raise exception
            try:
                cache.set_track_metadata(explicit="No", persistent_id="TRACK123")
            except Exception as e:
                pytest.fail(f"set_track_metadata raised exception: {e}")
            finally:
                # Cleanup
                cache.cache_file.chmod(0o644)
