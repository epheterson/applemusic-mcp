"""Entity cache for Apple Music MCP.

Caches stable metadata for tracks and albums, keyed by various ID types:
- Tracks: Persistent IDs, Library IDs, Catalog IDs
- Albums: Library IDs (l.XXX), Catalog IDs

Also maintains a name index for reverse lookups (name+artist → primary ID).

All IDs for the same entity point to shared metadata for maximum hit rate.
"""

import json
import logging
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def get_cache_dir() -> Path:
    """Get cache directory (env-overridable; see paths.cache_dir)."""
    from . import paths

    cache_dir = paths.cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _normalize_name_key(name: str, artist: str = "") -> str:
    """Create a normalized key for name+artist lookup.

    Args:
        name: Entity name (track or album)
        artist: Artist name (optional)

    Returns:
        Lowercase key like "abbey road|the beatles"
    """
    parts = [name.lower().strip()]
    if artist:
        parts.append(artist.lower().strip())
    return "|".join(parts)


class TrackCache:
    """Cache for stable track and album metadata.

    Stores:
    - tracks: explicit status, ISRC, keyed by any ID type
    - albums: metadata keyed by library/catalog ID
    - name_index: name+artist → primary ID for reverse lookups

    Maintains backward compatibility with existing track_cache.json format.
    """

    def __init__(self):
        self.cache_file = get_cache_dir() / "cache.json"
        self._legacy_file = get_cache_dir() / "track_cache.json"
        self._cache = self._load()

    def _load(self) -> dict:
        """Load cache from disk, migrating from legacy format if needed."""
        # Try new cache file first
        if self.cache_file.exists():
            try:
                with open(self.cache_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    # Ensure required sections exist
                    if "tracks" not in data:
                        data["tracks"] = {}
                    if "albums" not in data:
                        data["albums"] = {}
                    if "name_index" not in data:
                        data["name_index"] = {}
                    return data
            except Exception as e:
                logger.warning(f"Failed to load cache from {self.cache_file}: {e}")

        # Try legacy file and migrate
        if self._legacy_file.exists():
            try:
                with open(self._legacy_file, "r", encoding="utf-8") as f:
                    legacy_data = json.load(f)
                logger.info(f"Migrating legacy track cache to {self.cache_file}")
                # Migrate: old format was flat dict, new format has sections
                migrated = {
                    "tracks": legacy_data,  # Old data goes into tracks section
                    "albums": {},
                    "name_index": {},
                }
                return migrated
            except Exception as e:
                logger.warning(f"Failed to load legacy cache: {e}")

        return {"tracks": {}, "albums": {}, "name_index": {}}

    def _save(self) -> None:
        """Save cache to disk."""
        try:
            with open(self.cache_file, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save cache to {self.cache_file}: {e}")

    # =========================================================================
    # Track methods (backward compatible)
    # =========================================================================

    def get_explicit(self, track_id: str) -> Optional[str]:
        """Get cached explicit status by any ID type.

        Args:
            track_id: Persistent ID, Library ID, or Catalog ID

        Returns:
            "Yes", "No", or None if not cached
        """
        tracks = self._cache.get("tracks", {})
        if track_id in tracks:
            return tracks[track_id].get("explicit")
        return None

    def get_track_info(self, track_id: str) -> Optional[dict]:
        """Get cached track info (name, artist, album) by any ID type.

        Args:
            track_id: Persistent ID, Library ID, or Catalog ID

        Returns:
            Dict with name/artist/album if cached, None otherwise
        """
        tracks = self._cache.get("tracks", {})
        if track_id in tracks:
            entry = tracks[track_id]
            if "name" in entry:
                return {
                    "name": entry.get("name"),
                    "artist": entry.get("artist", ""),
                    "album": entry.get("album", ""),
                }
        return None

    def set_track_metadata(
        self,
        explicit: str,
        persistent_id: Optional[str] = None,
        library_id: Optional[str] = None,
        catalog_id: Optional[str] = None,
        isrc: Optional[str] = None,
        name: Optional[str] = None,
        artist: Optional[str] = None,
        album: Optional[str] = None,
    ) -> None:
        """Cache track metadata by all known IDs.

        Stores metadata once and indexes by all provided IDs for maximum hit rate.

        Args:
            explicit: "Yes" or "No" (content rating)
            persistent_id: AppleScript persistent ID (optional)
            library_id: API library ID (optional)
            catalog_id: Universal catalog ID (optional)
            isrc: International Standard Recording Code (optional)
            name: Track name for name index (optional)
            artist: Artist name for name index (optional)
            album: Album name for disambiguation (optional)
        """
        tracks = self._cache.setdefault("tracks", {})

        # Build metadata dict
        metadata = {"explicit": explicit}
        if isrc:
            metadata["isrc"] = isrc
        if name:
            metadata["name"] = name
        if artist:
            metadata["artist"] = artist
        if album:
            metadata["album"] = album

        # Cache by all provided IDs
        ids_to_cache = [id for id in [persistent_id, library_id, catalog_id] if id]

        primary_id = ids_to_cache[0] if ids_to_cache else None

        for track_id in ids_to_cache:
            if track_id not in tracks:
                tracks[track_id] = metadata

        # Add to name index if name provided
        if name and primary_id:
            name_index = self._cache.setdefault("name_index", {})
            key = _normalize_name_key(name, artist or "")
            if key not in name_index:
                name_index[key] = {"type": "track", "id": primary_id}

        # Save to disk
        self._save()

    def get_track_by_name(self, name: str, artist: str = "") -> Optional[str]:
        """Look up track ID by name and optional artist.

        Args:
            name: Track name
            artist: Artist name (optional)

        Returns:
            Primary ID if found, None otherwise
        """
        name_index = self._cache.get("name_index", {})
        key = _normalize_name_key(name, artist)
        entry = name_index.get(key)
        if entry and entry.get("type") == "track":
            return entry.get("id")
        return None

    # =========================================================================
    # Album methods (new)
    # =========================================================================

    def get_album(self, album_id: str) -> Optional[dict]:
        """Get cached album metadata by ID.

        Args:
            album_id: Library ID (l.XXX) or Catalog ID

        Returns:
            Album metadata dict or None if not cached
        """
        albums = self._cache.get("albums", {})
        return albums.get(album_id)

    def set_album_metadata(
        self,
        library_id: Optional[str] = None,
        catalog_id: Optional[str] = None,
        name: Optional[str] = None,
        artist: Optional[str] = None,
        track_count: Optional[int] = None,
        year: Optional[str] = None,
    ) -> None:
        """Cache album metadata by all known IDs.

        Args:
            library_id: API library ID (l.XXX) (optional)
            catalog_id: Universal catalog ID (optional)
            name: Album name (optional, for name index)
            artist: Artist name (optional, for name index)
            track_count: Number of tracks (optional)
            year: Release year (optional)
        """
        albums = self._cache.setdefault("albums", {})

        # Build metadata dict
        metadata = {}
        if name:
            metadata["name"] = name
        if artist:
            metadata["artist"] = artist
        if track_count is not None:
            metadata["track_count"] = track_count
        if year:
            metadata["year"] = year

        # Cache by all provided IDs
        ids_to_cache = [id for id in [library_id, catalog_id] if id]
        primary_id = ids_to_cache[0] if ids_to_cache else None

        for album_id in ids_to_cache:
            if album_id not in albums:
                albums[album_id] = metadata

        # Add to name index if name provided
        if name and primary_id:
            name_index = self._cache.setdefault("name_index", {})
            key = _normalize_name_key(name, artist or "")
            if key not in name_index:
                name_index[key] = {"type": "album", "id": primary_id}

        self._save()

    def get_album_by_name(self, name: str, artist: str = "") -> Optional[str]:
        """Look up album ID by name and optional artist.

        Args:
            name: Album name
            artist: Artist name (optional)

        Returns:
            Primary ID if found, None otherwise
        """
        name_index = self._cache.get("name_index", {})
        key = _normalize_name_key(name, artist)
        entry = name_index.get(key)
        if entry and entry.get("type") == "album":
            return entry.get("id")
        return None

    # =========================================================================
    # General methods
    # =========================================================================

    def clear(self) -> None:
        """Clear entire cache (for testing/maintenance)."""
        self._cache = {"tracks": {}, "albums": {}, "name_index": {}}
        self._save()

    def clear_tracks(self) -> None:
        """Clear only track cache."""
        self._cache["tracks"] = {}
        self._save()

    def clear_albums(self) -> None:
        """Clear only album cache."""
        self._cache["albums"] = {}
        self._save()

    def get_stats(self) -> dict:
        """Get cache statistics.

        Returns:
            Dict with track_count, album_count, name_index_count
        """
        return {
            "track_count": len(self._cache.get("tracks", {})),
            "album_count": len(self._cache.get("albums", {})),
            "name_index_count": len(self._cache.get("name_index", {})),
        }


# Global cache instance with thread-safe initialization
_track_cache: Optional[TrackCache] = None
_track_cache_lock = threading.Lock()


def get_track_cache() -> TrackCache:
    """Get the global track cache instance (thread-safe)."""
    global _track_cache
    if _track_cache is None:
        with _track_cache_lock:
            # Double-check locking pattern
            if _track_cache is None:
                _track_cache = TrackCache()
    return _track_cache
