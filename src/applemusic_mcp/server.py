"""MCP server for Apple Music - Cross-platform playlist and library management.

On macOS, additional AppleScript-powered tools are available for playback control,
deleting tracks from playlists, and other operations not supported by the REST API.
"""

import csv
import io
import json
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

import requests
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .auth import (
    get_developer_token,
    get_user_token,
    get_config_dir,
    get_user_preferences,
    resolve_developer_token,
    has_any_developer_token,
    can_generate_developer_token,
    developer_token_info,
    has_user_token,
    secret_get,
    secret_delete,
)
from . import applescript as asc
from . import amp_api
from .track_cache import get_track_cache, get_cache_dir
from . import audit_log
from . import paths

# Check if AppleScript is available (macOS only)
APPLESCRIPT_AVAILABLE = asc.is_available()

# Max characters for track listing output
MAX_OUTPUT_CHARS = 50000

# Minimum digits for a string to be considered a catalog ID (Apple IDs are 10 digits)
MIN_CATALOG_ID_LENGTH = 9


class EntityType(Enum):
    """Types of Apple Music entities."""

    TRACK = "track"
    ALBUM = "album"
    ARTIST = "artist"
    PLAYLIST = "playlist"
    GENRE = "genre"


class InputType(Enum):
    """How the input was interpreted."""

    CATALOG_ID = "catalog_id"  # All digits, 9+ chars: "1440783617"
    LIBRARY_ID = "library_id"  # Starts with "i.": "i.ABC123"
    PLAYLIST_ID = "playlist_id"  # Starts with "p.": "p.ABC123"
    ALBUM_ID = "album_id"  # Starts with "l.": "l.ABC123"
    PERSISTENT_ID = "persistent_id"  # 12+ hex chars: "ABC123DEF456"
    NAME = "name"  # Plain name to search for
    JSON_OBJECT = "json_object"  # From JSON array


@dataclass
class ResolvedInput:
    """Result of resolving a user input to an entity reference."""

    input_type: InputType
    value: str  # The ID or name
    artist: str = ""  # Artist hint for disambiguation
    raw: str = ""  # Original input string
    error: str | None = None  # Error message if resolution failed


@dataclass
class FuzzyMatchResult:
    """Result of a fuzzy match operation."""

    matched_name: str  # The actual name that was matched
    query: str  # The original query
    normalized_query: str  # The normalized query used for matching
    normalized_match: str  # The normalized matched name
    transformations: list[str]  # List of transformations applied
    match_type: str  # "exact", "fuzzy", or "partial"


@dataclass
class ResolvedPlaylist:
    """Result of resolving a playlist parameter.

    Contains all available identifiers for a playlist. Different functions
    need different identifiers:
    - API operations prefer api_id for performance
    - AppleScript operations require applescript_name
    - Some operations need persistent_id

    Resolution should populate as many as possible so callers can choose.
    """

    api_id: str | None = None  # API playlist ID (p.XXX) for REST calls
    applescript_name: str | None = None  # Playlist name for AppleScript operations
    persistent_id: str | None = None  # Hex ID from AppleScript (e.g., 583528883966122E)
    raw_input: str = ""  # Original input from user
    error: str | None = None  # Error message if resolution failed
    fuzzy_match: FuzzyMatchResult | None = None  # Fuzzy match details if applicable


def truncate(s: str, max_len: int) -> str:
    """Truncate string with ellipsis if longer than max_len."""
    return s[:max_len] + "..." if len(s) > max_len else s


def _deduplicate_by_id(
    items: list[dict], id_key: str = "id", keep_no_id: bool = False
) -> list[dict]:
    """Remove duplicate items based on ID field.

    Args:
        items: List of dicts to deduplicate
        id_key: Key to use for ID lookup (default "id")
        keep_no_id: If True, keep items without an ID (default False)

    Returns:
        List with duplicates removed, preserving order
    """
    seen_ids: set[str] = set()
    unique = []
    for item in items:
        item_id = item.get(id_key, "")
        if item_id:
            if item_id not in seen_ids:
                seen_ids.add(item_id)
                unique.append(item)
        elif keep_no_id:
            unique.append(item)
    return unique


def _format_fuzzy_match(fuzzy: FuzzyMatchResult | None) -> str:
    """Format fuzzy match information for display.

    Args:
        fuzzy: Fuzzy match result or None

    Returns:
        Formatted string describing the fuzzy match, or empty string if None
    """
    if not fuzzy:
        return ""

    parts = [f"\n🔍 Fuzzy match: '{fuzzy.query}' → '{fuzzy.matched_name}'"]

    if fuzzy.match_type == "exact":
        return ""  # Don't show anything for exact matches

    if fuzzy.match_type == "partial":
        parts.append(f"   Match type: Partial substring match")
    elif fuzzy.match_type == "fuzzy":
        parts.append(f"   Match type: Fuzzy match")
        if fuzzy.transformations:
            trans_str = ", ".join(fuzzy.transformations)
            parts.append(f"   Transformations: {trans_str}")
    elif fuzzy.match_type == "fuzzy_partial":
        parts.append(f"   Match type: Fuzzy partial match")
        if fuzzy.transformations:
            trans_str = ", ".join(fuzzy.transformations)
            parts.append(f"   Transformations: {trans_str}")

    return "\n".join(parts)


def _normalize_for_match(s: Optional[str]) -> str:
    """Canonical normalizer for matching a user string against a candidate name.

    This is the single source of truth for "do these two strings match" across
    every comparison point (track/artist/album/playlist lookups). Both the user
    query and the candidate are run through it before comparison, so cosmetic
    differences never cause a miss. See :func:`_loose_contains`.

    Folds, in order:
      1. case (lowercase) and surrounding whitespace
      2. **diacritics** via NFD — ``café → cafe`` (must precede step 4, or the
         accented codepoint is stripped to nothing and ``café → caf`` instead)
      3. ``&`` → ``and`` (before step 4 strips it)
      4. all remaining punctuation — smart/straight quotes (U+2019 ≈ U+0027),
         ellipses, hyphens, emoji — to nothing (issue #26)
      5. collapsed internal whitespace
    """
    # None-safe: callers pass values straight from .get()/optional fields.
    if not s:
        return ""
    # 1. Lowercase and strip
    s = s.lower().strip()
    # 2. Fold diacritics (café → cafe) BEFORE stripping non-ASCII below, so the
    #    accented letter collapses to its base form instead of vanishing.
    s = "".join(
        c for c in unicodedata.normalize("NFD", s) if not unicodedata.category(c).startswith("M")
    )
    # 3. Normalize ampersand to its word form before punctuation is stripped.
    s = s.replace("&", "and")
    # 4. Word-joiners (hyphen, en/em dash, slash, underscore) become spaces so
    #    tokens stay distinct: "Peek-A-Boo" → "peek a boo", NOT "peekaboo" —
    #    the latter would let a query like "kabo" cross the original hyphen
    #    boundary and false-match. Matches _normalize_with_tracking's hyphen
    #    handling.
    s = re.sub(r"[-–—/_]+", " ", s)
    # 5. Strip the remaining punctuation (quote variants, ellipsis, emoji) to
    #    nothing, so intra-word marks fold away ("don't" → "dont", not "don t")
    #    and smart vs straight punctuation cannot cause a mismatch.
    s = re.sub(r"[^a-z0-9\s]", "", s)
    # 6. Collapse multiple spaces
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _loose_contains(needle: Optional[str], haystack: Optional[str]) -> bool:
    """True if `needle` matches inside `haystack` after canonical normalization.

    The single comparison primitive that replaces ad-hoc
    ``needle.lower() in haystack.lower()`` checks. Normalizing both sides folds
    away smart quotes, accents, ``&``/``and`` and other cosmetic differences
    that plain substring matching misses (issue #26). An empty/normalizes-empty
    needle imposes no constraint (mirrors ``"" in x`` being True), so it is a
    drop-in for the existing ``if query and query.lower() not in name`` guards.
    """
    n = _normalize_for_match(needle)
    if not n:
        return True
    return n in _normalize_for_match(haystack)


def _loose_equals(a: Optional[str], b: Optional[str]) -> bool:
    """True if `a` and `b` are equal after canonical normalization."""
    return _normalize_for_match(a) == _normalize_for_match(b)


def _normalize_with_tracking(name: str) -> tuple[list[str], list[str]]:
    """Normalize a name for fuzzy matching and track transformations applied.

    Applies various normalization rules and returns normalized variations
    plus a list of which transformations were applied.

    Args:
        name: The name to normalize

    Returns:
        Tuple of (normalized_variations, transformations_applied)
    """
    transformations = []

    # Step 1: Lowercase and strip
    name = name.lower().strip()

    # Step 2: Remove diacritics (café → cafe)
    if any(unicodedata.category(c).startswith("M") for c in unicodedata.normalize("NFD", name)):
        name = "".join(
            c
            for c in unicodedata.normalize("NFD", name)
            if not unicodedata.category(c).startswith("M")
        )
        transformations.append("removed diacritics")

    # Step 3: Strip leading articles (The Beatles → Beatles)
    for article in [r"\bthe\s+", r"\ban\s+", r"\ba\s+"]:
        if re.match(article, name):
            name = re.sub(f"^{article}", "", name)
            clean_article = article.replace(r"\b", "").replace(r"\s+", "").strip()
            transformations.append(f"removed article '{clean_article}'")
            break

    # Step 4: Normalize "and" / "&"
    if " and " in name:
        variations = [name, name.replace(" and ", " & ")]
        transformations.append("'and' ↔ '&'")
    elif " & " in name:
        variations = [name, name.replace(" & ", " and ")]
        transformations.append("'and' ↔ '&'")
    else:
        variations = [name]

    # Step 5: Normalize music-specific abbreviations
    abbrev_map = {
        r"\bfeat\.?\s": "ft ",
        r"\bfeaturing\s": "ft ",
        r"\bft\.?\s": "ft ",
        r"\bw/\s": "with ",
    }
    for pattern, replacement in abbrev_map.items():
        if re.search(pattern, name):
            name = re.sub(pattern, replacement, name)
            transformations.append(f"normalized '{pattern}' to '{replacement.strip()}'")

    # Step 6: Normalize apostrophes and quotes
    if any(char in name for char in ["'", "'", "`", '"', '"', '"']):
        name = name.replace("'", "").replace("'", "").replace("`", "")
        name = name.replace('"', "").replace('"', "").replace('"', "")
        transformations.append("removed quotes/apostrophes")

    # Step 7: Normalize hyphens to spaces
    if "-" in name:
        name = name.replace("-", " ")
        transformations.append("hyphens → spaces")

    # Step 8: Remove emojis and special characters (keep only alphanumeric and spaces)
    cleaned = re.sub(r"[^a-z0-9\s]", "", name)
    if cleaned != name:
        transformations.append("removed special characters/emojis")
        name = cleaned

    # Step 9: Collapse multiple spaces
    if re.search(r"\s{2,}", name):
        name = re.sub(r"\s+", " ", name).strip()
        transformations.append("normalized whitespace")

    # Also generate variations for "and" / "&" substitution
    all_variations = []
    for variant in variations:
        # Apply all transformations to each variant
        v = variant
        v = "".join(
            c
            for c in unicodedata.normalize("NFD", v)
            if not unicodedata.category(c).startswith("M")
        )
        for article in [r"\bthe\s+", r"\ban\s+", r"\ba\s+"]:
            v = re.sub(f"^{article}", "", v)
        for pattern, replacement in abbrev_map.items():
            v = re.sub(pattern, replacement, v)
        v = v.replace("'", "").replace("'", "").replace("`", "")
        v = v.replace('"', "").replace('"', "").replace('"', "")
        v = v.replace("-", " ")
        v = re.sub(r"[^a-z0-9\s]", "", v)
        v = re.sub(r"\s+", " ", v).strip()
        all_variations.append(v)

    return all_variations, transformations


def _fuzzy_match_entity(
    query: str,
    candidates: list[dict],
    name_extractor: Callable[[dict], str],
) -> tuple[dict | None, FuzzyMatchResult | None]:
    """Generic 3-pass fuzzy matching for any entity type.

    Matching priority:
    1. Exact match (case-insensitive) - fastest, no normalization
    2. Partial match (query contained in name) - fast, just substring
    3. Fuzzy match (normalized with transformations) - slowest, only if needed

    Args:
        query: The search query from the user
        candidates: List of candidate entities to match against
        name_extractor: Function to extract name string from a candidate dict

    Returns:
        Tuple of (matched_entity, fuzzy_match_result)
        - matched_entity: The dict from candidates that matched, or None
        - fuzzy_match_result: Details about the match if fuzzy/partial, None if exact
    """
    if not candidates:
        return None, None

    query_lower = query.lower()

    # PASS 1: Exact match (fastest - no normalization)
    for candidate in candidates:
        candidate_name = name_extractor(candidate)
        if query_lower == candidate_name.lower():
            return candidate, None  # Exact match, no fuzzy result

    # PASS 2: Partial match (fast - just substring check)
    partial_match = None
    partial_match_name = None
    for candidate in candidates:
        candidate_name = name_extractor(candidate)
        if query_lower in candidate_name.lower():
            partial_match = candidate
            partial_match_name = candidate_name
            break  # Take first partial match

    # PASS 3: Fuzzy match (slowest - only if no exact/partial)
    if partial_match is None:
        normalized_variations, transformations = _normalize_with_tracking(query)

        for candidate in candidates:
            candidate_name = name_extractor(candidate)
            candidate_variations, _ = _normalize_with_tracking(candidate_name)

            for query_variant in normalized_variations:
                for candidate_variant in candidate_variations:
                    # Check exact match after normalization
                    if query_variant == candidate_variant:
                        fuzzy_result = FuzzyMatchResult(
                            matched_name=candidate_name,
                            query=query,
                            normalized_query=query_variant,
                            normalized_match=candidate_variant,
                            transformations=transformations,
                            match_type="fuzzy",
                        )
                        return candidate, fuzzy_result
                    # Check partial match after normalization (query contained in candidate)
                    if query_variant in candidate_variant:
                        fuzzy_result = FuzzyMatchResult(
                            matched_name=candidate_name,
                            query=query,
                            normalized_query=query_variant,
                            normalized_match=candidate_variant,
                            transformations=transformations + ["partial normalized match"],
                            match_type="fuzzy_partial",
                        )
                        return candidate, fuzzy_result

    # Return partial match if found (after checking fuzzy didn't find better)
    if partial_match:
        fuzzy_result = FuzzyMatchResult(
            matched_name=partial_match_name,
            query=query,
            normalized_query=query_lower,
            normalized_match=partial_match_name.lower(),
            transformations=["partial substring match"],
            match_type="partial",
        )
        return partial_match, fuzzy_result

    return None, None


def get_timestamp() -> str:
    """Get timestamp for unique filenames (YYYYMMDD_HHMMSS)."""
    return time.strftime("%Y%m%d_%H%M%S")


_EXPORT_TTL_S = 7 * 24 * 3600  # exports older than this are GC'd on the next write
_EXPORT_MAX_FILES = 50  # hard cap on retained exports regardless of age


def _gc_exports(cache_dir: Path) -> None:
    """Keep the export dir bounded so artifacts never accumulate forever (release
    contract: "cleans up after itself"). Drops timestamped export files older than
    the TTL and caps the total count. Matches only ``prefix_timestamp`` exports
    (the ``*_*`` glob) so it never touches ``cache.json``, the audit log, or the
    snapshots subdir."""
    try:
        files = sorted(
            list(cache_dir.glob("*_*.csv")) + list(cache_dir.glob("*_*.json")),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return
    now = time.time()
    for i, f in enumerate(files):
        try:
            if i >= _EXPORT_MAX_FILES or (now - f.stat().st_mtime) > _EXPORT_TTL_S:
                f.unlink()
        except OSError:
            pass


def format_duration(ms: int | None) -> str:
    """Format milliseconds as m:ss (e.g., 3:45).

    Args:
        ms: Duration in milliseconds. Returns empty string for None, 0, or negative values.

    Returns:
        Formatted duration string like "3:45" or empty string for invalid input.
    """
    if not ms or ms <= 0:
        return ""
    total_seconds = ms // 1000
    minutes = total_seconds // 60
    seconds = total_seconds % 60
    return f"{minutes}:{seconds:02d}"


def extract_track_data(track: dict, include_extras: bool = False) -> dict:
    """Extract track data from API response into standardized dict.

    Args:
        track: Raw track dict from Apple Music API response.
        include_extras: If True, include additional metadata (track_number, artwork, etc.)

    Returns:
        Dict with standardized keys: name, duration, artist, album, year, genre, id.
        If include_extras=True, also includes: track_number, disc_number, has_lyrics,
        catalog_id, composer, isrc, is_explicit, preview_url, artwork_url.
    """
    attrs = track.get("attributes", {})
    play_params = attrs.get("playParams", {})
    genres = attrs.get("genreNames", [])
    release_date = attrs.get("releaseDate", "") or ""

    track_id = track.get("id", "")
    name = attrs.get("name", "")
    artist = attrs.get("artistName", "")
    album = attrs.get("albumName", "")
    explicit = "Yes" if attrs.get("contentRating") == "explicit" else "No"
    isrc = attrs.get("isrc", "")

    data = {
        "name": name,
        "duration": format_duration(attrs.get("durationInMillis", 0)),
        "artist": artist,
        "album": album,
        "year": release_date[:4] if release_date else "",
        "genre": genres[0] if genres else "",
        "explicit": explicit,
        "id": track_id,
    }

    if include_extras:
        previews = attrs.get("previews", [])
        data.update(
            {
                "track_number": attrs.get("trackNumber", ""),
                "disc_number": attrs.get("discNumber", ""),
                "has_lyrics": attrs.get("hasLyrics", False),
                "catalog_id": play_params.get("catalogId", ""),
                "composer": attrs.get("composerName", ""),
                "isrc": isrc,
                "is_explicit": attrs.get("contentRating") == "explicit",
                "preview_url": previews[0].get("url", "") if previews else "",
                "artwork_url": attrs.get("artwork", {})
                .get("url", "")
                .replace("{w}x{h}", "500x500"),
            }
        )

    # Cache track metadata for later ID lookups (e.g., removal by catalog ID)
    if track_id and name:
        cache = get_track_cache()
        # Determine if this is a catalog or library ID
        catalog_id = track_id if track_id.isdigit() else play_params.get("catalogId", "")
        library_id = track_id if track_id.startswith("i.") else None
        cache.set_track_metadata(
            explicit=explicit,
            catalog_id=catalog_id or None,
            library_id=library_id,
            isrc=isrc or None,
            name=name,
            artist=artist,
            album=album,
        )

    return data


def write_tracks_csv(track_data: list[dict], csv_path: Path, include_extras: bool = False) -> None:
    """Write track data to CSV file.

    Args:
        track_data: List of track dicts from extract_track_data().
        csv_path: Path to write CSV file.
        include_extras: If True, include additional metadata columns.
    """
    csv_fields = ["name", "duration", "artist", "album", "year", "genre", "explicit", "id"]
    if include_extras:
        csv_fields += [
            "track_number",
            "disc_number",
            "has_lyrics",
            "catalog_id",
            "composer",
            "isrc",
            "is_explicit",
            "preview_url",
            "artwork_url",
        ]

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=csv_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(track_data)


def _format_full(t: dict) -> str:
    """Full format: Name - Artist (duration) Album [Year] Genre [Explicit] id"""
    year_str = f" [{t['year']}]" if t["year"] else ""
    genre_str = f" {t['genre']}" if t["genre"] else ""
    explicit_str = " [Explicit]" if t.get("explicit") == "Yes" else ""
    return f"{t['name']} - {t['artist']} ({t['duration']}) {t['album']}{year_str}{genre_str}{explicit_str} {t['id']}"


def _format_clipped(t: dict) -> str:
    """Clipped format: Truncated Name - Artist (duration) Album [Year] Genre [Explicit] id"""
    year_str = f" [{t['year']}]" if t["year"] else ""
    genre_str = f" {t['genre']}" if t["genre"] else ""
    explicit_str = " [Explicit]" if t.get("explicit") == "Yes" else ""
    return f"{truncate(t['name'], 35)} - {truncate(t['artist'], 22)} ({t['duration']}) {truncate(t['album'], 30)}{year_str}{genre_str}{explicit_str} {t['id']}"


def _format_compact(t: dict) -> str:
    """Compact format: Name - Artist (duration) id"""
    return f"{truncate(t['name'], 40)} - {truncate(t['artist'], 25)} ({t['duration']}) {t['id']}"


def _format_minimal(t: dict) -> str:
    """Minimal format: Name - Artist id"""
    return f"{truncate(t['name'], 30)} - {truncate(t['artist'], 20)} {t['id']}"


def format_track_list(track_data: list[dict]) -> tuple[list[str], str]:
    """Format track list with tiered display based on output size.

    Automatically selects the most detailed format that fits within MAX_OUTPUT_CHARS:
    - Full: Name - Artist (duration) Album [Year] Genre id
    - Clipped: Same as Full but with truncated Name/Artist/Album
    - Compact: Truncated Name - Artist (duration) id
    - Minimal: Truncated Name - Artist id

    Args:
        track_data: List of track dicts from extract_track_data().

    Returns:
        Tuple of (list of formatted strings, tier_name) where tier_name is
        "Full", "Clipped", "Compact", or "Minimal".
    """
    if not track_data:
        return [], "Full"

    def char_count(lines: list[str]) -> int:
        return sum(len(line) for line in lines) + max(0, len(lines) - 1)

    # Try full format first
    full_output = [_format_full(t) for t in track_data]
    if char_count(full_output) <= MAX_OUTPUT_CHARS:
        return full_output, "Full"

    # Try clipped (truncated but keeps all fields)
    clipped_output = [_format_clipped(t) for t in track_data]
    if char_count(clipped_output) <= MAX_OUTPUT_CHARS:
        return clipped_output, "Clipped"

    # Fall back to compact (drops album/year/genre)
    compact_output = [_format_compact(t) for t in track_data]
    if char_count(compact_output) <= MAX_OUTPUT_CHARS:
        return compact_output, "Compact"

    # Fall back to minimal
    return [_format_minimal(t) for t in track_data], "Minimal"


def format_output(
    items: list[dict],
    format: str = "text",
    export: str = "none",
    full: bool = False,
    file_prefix: str = "export",
    total_count: int = 0,
    offset: int = 0,
) -> str:
    """Format output with optional file export.

    Args:
        items: List of item dicts (tracks, albums, etc.)
        format: "text" for human-readable, "json", "csv", or "none" (export only)
        export: "none" (default), "csv", or "json" to write file
        full: Include all metadata in exports (extras like artwork, track numbers)
        file_prefix: Prefix for export filename
        total_count: Total items before pagination (0 = no pagination info)
        offset: Starting offset for pagination display

    Returns:
        Formatted string (text or JSON) with optional file path info
    """
    if not items:
        return "No results" if format != "json" else "[]"

    result_parts = []

    # Build response content (skip if format="none")
    if format == "json":
        # JSON response - include standard fields, optionally extras
        if full:
            result_parts.append(json.dumps(items, indent=2))
        else:
            # Filter to standard fields only
            standard_keys = {
                "name",
                "duration",
                "artist",
                "album",
                "year",
                "genre",
                "id",
                "track_count",
                "release_date",
            }
            filtered = [{k: v for k, v in item.items() if k in standard_keys} for item in items]
            result_parts.append(json.dumps(filtered, indent=2))
    elif format == "csv":
        # CSV response inline
        output = io.StringIO()
        if items and "duration" in items[0]:
            csv_fields = ["name", "duration", "artist", "album", "year", "genre", "id"]
            if full:
                csv_fields += [
                    "track_number",
                    "disc_number",
                    "has_lyrics",
                    "catalog_id",
                    "composer",
                    "isrc",
                    "is_explicit",
                    "preview_url",
                    "artwork_url",
                ]
        else:
            csv_fields = list(items[0].keys()) if items else []
        writer = csv.DictWriter(output, fieldnames=csv_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(items)
        result_parts.append(output.getvalue())
    elif format == "text":
        # Text response - use tiered formatting for tracks
        if items and "duration" in items[0]:
            # Track data - use tiered format
            formatted_lines, _tier = format_track_list(items)
            # Build header with pagination info if provided
            if total_count > 0 and total_count > len(items):
                start = offset + 1
                end = offset + len(items)
                result_parts.append(f"=== {start}-{end} of {total_count} tracks ===\n")
            else:
                result_parts.append(f"=== {len(items)} tracks ===\n")
            result_parts.append("\n".join(formatted_lines))
        else:
            # Non-track data (albums, artists) - simple format
            result_parts.append(f"=== {len(items)} items ===\n")
            for item in items[:200]:
                if "artist" in item and "name" in item:
                    result_parts.append(
                        f"{item['name']} - {item.get('artist', '')} {item.get('id', '')}"
                    )
                elif "name" in item:
                    result_parts.append(f"{item['name']} {item.get('id', '')}")
    # format="none" - skip response body, only show export info

    # Handle file export
    if export in ("csv", "json"):
        cache_dir = get_cache_dir()
        _gc_exports(cache_dir)  # bound the export dir before writing another file
        timestamp = get_timestamp()

        if export == "csv":
            file_path = cache_dir / f"{file_prefix}_{timestamp}.csv"
            # Determine fields based on full flag
            if items and "duration" in items[0]:
                csv_fields = ["name", "duration", "artist", "album", "year", "genre", "id"]
                if full:
                    csv_fields += [
                        "track_number",
                        "disc_number",
                        "has_lyrics",
                        "catalog_id",
                        "composer",
                        "isrc",
                        "is_explicit",
                        "preview_url",
                        "artwork_url",
                    ]
            else:
                csv_fields = list(items[0].keys()) if items else []

            with open(file_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=csv_fields, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(items)
        else:  # json
            file_path = cache_dir / f"{file_prefix}_{timestamp}.json"
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(
                    (
                        items
                        if full
                        else [
                            {
                                k: v
                                for k, v in item.items()
                                if k
                                in {
                                    "name",
                                    "duration",
                                    "artist",
                                    "album",
                                    "year",
                                    "genre",
                                    "id",
                                    "track_count",
                                    "release_date",
                                }
                            }
                            for item in items
                        ]
                    ),
                    f,
                    indent=2,
                )

        result_parts.append(f"Exported {len(items)} items: {file_path}")
        result_parts.append(f"Resource: exports://{file_path.name}")

    if not result_parts:
        return f"{len(items)} items (use export='csv' or 'json' to save)"

    return "\n".join(result_parts)


BASE_URL = "https://api.music.apple.com/v1"
DEFAULT_STOREFRONT = "us"
REQUEST_TIMEOUT = 30  # seconds

# play_track retry constants for iCloud sync
PLAY_TRACK_INITIAL_DELAY = 1.0  # seconds before first retry
PLAY_TRACK_RETRY_DELAY = 0.2  # seconds between retries
PLAY_TRACK_MAX_ATTEMPTS = 45  # total retry attempts (~10 seconds)

# auto-search-to-playlist tuning (API + UI flows)
_LIBRARY_SYNC_DEADLINE_S = 18.0  # max wait for API add-to-library to show up in local Music.app
_LIBRARY_SYNC_TICK_S = 0.5  # poll interval while waiting for sync
_VERIFY_ATTEMPTS = 3  # retries for post-add playlist verification
_VERIFY_DELAY_S = 1.0  # sleep between verification retries
# Catalog→Music.app-playlist add: how long to wait for the cloud→local iCloud sync
# before deferring (cap the caller's hold), how fast to poll, and when to fire the
# last-resort Update Cloud Library nudge if the natural fast sync hasn't landed.
_SYNC_POLL_BUDGET_S = 30.0
_SYNC_POLL_INTERVAL_S = 1.5
_SYNC_NUDGE_AFTER_S = 20.0


def get_storefront() -> str:
    """Get storefront from preferences, defaulting to 'us'."""
    prefs = get_user_preferences()
    return prefs.get("storefront", DEFAULT_STOREFRONT)


mcp = FastMCP("Apple Music")


# ============ MCP RESOURCES ============


@mcp.resource("exports://list")
def list_exports() -> str:
    """List all exported files in the cache directory."""
    cache_dir = get_cache_dir()
    files = sorted(cache_dir.glob("*.*"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not files:
        return "No exports found"
    return "\n".join(f"{f.name} ({f.stat().st_size} bytes)" for f in files[:50])


@mcp.resource("exports://{filename}")
def read_export(filename: str) -> str:
    """Read an exported file from the cache directory."""
    cache_dir = get_cache_dir()
    file_path = cache_dir / filename
    if not file_path.exists():
        return f"File not found: {filename}"
    if not file_path.is_relative_to(cache_dir):
        return "Invalid path"  # pragma: no cover  # unreachable: file_path is always cache_dir/filename, never outside cache_dir
    return file_path.read_text(encoding="utf-8")


def get_token_expiration_warning() -> str | None:
    """Heads-up for the GENERATED (Apple Developer) token only — it does NOT
    auto-refresh, so the user has to renew it. The harvested web-player token
    refreshes itself (re-fetched ~1 day before expiry), so it needs no warning.

    Escalates as expiry nears and stays silent until 14 days out so it doesn't
    nag: notice at ≤14 days, urgent at ≤7, and a hard error once expired."""
    data = developer_token_info()
    if data is None:
        return None  # web/harvested path auto-refreshes — nothing to warn about
    if can_generate_developer_token():
        return None  # the signing key is present — it auto-renews, no nudge needed
    days_left = (data.get("expires", 0) - time.time()) / 86400

    # Only reaches here for a keyless generated token (no .p8 to auto-renew), so
    # give plenty of runway given sparse use: notice ≤30 days, urgent ≤7.
    renew = "renew with `applemusic-mcp login --dev`"
    days = round(days_left)
    if days_left < 0:
        return f"⚠️ Apple Developer token EXPIRED — {renew} (issues a fresh 180-day token)."
    if days_left <= 7:
        return f"⚠️ Apple Developer token expires in {days} day(s) — {renew} now."
    if days_left <= 30:
        return f"ℹ️ Apple Developer token expires in {days} days — {renew} soon."
    return None

    return None


def get_headers() -> dict:
    """Get headers for API requests.

    Uses ``resolve_developer_token`` so the unified API path works with EITHER a
    generated (preferred) or harvested (fallback) developer token, plus the
    captured media-user-token.
    """
    return {
        "Authorization": f"Bearer {resolve_developer_token()}",
        "Music-User-Token": get_user_token(),
        "Content-Type": "application/json",
        # The harvested AMPWebPlay web-player token is origin-bound: api.music.apple.com
        # returns 401 without this header. Generated (paid) tokens ignore it, so it's
        # safe to send unconditionally.
        "Origin": "https://music.apple.com",
    }


def _has_developer_token() -> bool:
    """Probe whether a usable developer token is configured.

    Returns False on any exception from get_developer_token() — that
    includes missing/expired tokens AND environmental failures (config dir
    permissions, malformed JSON, etc). Callers that have a tokenless
    fallback (e.g. AppleScript) use this for feature detection so the raw
    exception doesn't leak to users where the operation could have
    succeeded without API access.

    Set APPLEMUSIC_FORCE_TOKENLESS=1 to force the tokenless path for
    testing UI automation flows without touching your credentials.
    """
    if os.environ.get("APPLEMUSIC_FORCE_TOKENLESS") == "1":
        return False
    try:
        get_developer_token()
        return True
    except Exception:
        return False


def _has_user_token() -> bool:
    """True if a media-user-token is saved (captured via browser sign-in)."""
    try:
        return bool(get_user_token())
    except Exception:
        return False


def _forced_tokenless() -> bool:
    """True if APPLEMUSIC_FORCE_TOKENLESS=1 is set — a TEST flag that forces the
    tokenless path and so DISABLES every API write (catalog add, playlist edit,
    rating). It's easy to leave set in a host's environment and then wonder why
    adds silently fail, so status and the write-gate errors must call it out by
    name rather than blame missing auth."""
    return os.environ.get("APPLEMUSIC_FORCE_TOKENLESS") == "1"


_FORCED_TOKENLESS_MSG = (
    "APPLEMUSIC_FORCE_TOKENLESS=1 is set in this server's environment, which "
    "disables the API write path — catalog and library adds won't work. (On macOS, "
    "local Music.app playlist edits and ratings still work; off macOS, writes are "
    "blocked.) Unset it and restart the MCP server to re-enable API adds — signing "
    "in again won't help while it's set."
)


def _can_use_library_api() -> bool:
    """True if the unified API mutation path is usable: a developer token is
    obtainable (generated OR harvested) AND a media-user-token is saved.

    This is the gate that replaces UI automation for catalog-adds: when both
    tokens resolve, adds go through ``_add_to_library_api``; otherwise we fall
    back (AppleScript UI on macOS). Honors APPLEMUSIC_FORCE_TOKENLESS for testing.
    """
    if _forced_tokenless():
        return False
    return has_any_developer_token() and _has_user_token()


def _engine() -> str:
    """Resolve the active engine from the single ``mode`` preference:
    ``native`` (AppleScript / local Music.app) or ``api`` (the web API +
    Chrome web player, cross-platform).

    mode values: ``auto`` (default — native on macOS, web elsewhere), ``native``,
    or ``web``. ``api`` is accepted as a back-compat alias for ``web``.

    Playback follows the engine (see ``_use_browser_playback``); there is no
    separate playback knob. Honors APPLEMUSIC_FORCE_API_MODE for testing."""
    if os.environ.get("APPLEMUSIC_FORCE_API_MODE") == "1":
        return "api"
    mode = (get_user_preferences().get("mode") or "auto").lower()
    # safari/chrome are PLAYBACK engines — their data still comes from the REST API.
    if mode in ("web", "api", "safari", "chrome"):
        return "api"
    # native or auto: native only when AppleScript is available; otherwise fall
    # back to the web engine so a non-macOS host never hits AppleScript.
    return "native" if APPLESCRIPT_AVAILABLE else "api"


def _playback_engine(engine_override: Optional[str] = None, for_queue: bool = False) -> str:
    """Resolve the playback engine: 'native' | 'safari' | 'chrome' | 'none'.

    Priority: per-call ``engine=`` override → ``mode`` pref → ``auto`` routing.
    'none' means no player is available for this selection (the caller turns that
    into an actionable error). ``for_queue`` distinguishes Up Next (web-player only:
    Safari on macOS, Chrome off-mac) from plain playback (native on macOS).
    AppleScript availability is the macOS proxy (native + Safari both need it)."""
    sel = (engine_override or "").strip().lower()
    if not sel:
        sel = (get_user_preferences().get("mode") or "auto").lower()
    if sel == "browser":  # legacy alias for the Chrome web player
        sel = "chrome"
    if sel == "api":
        return "none"  # api mode has no player
    if sel == "native":
        return "native" if APPLESCRIPT_AVAILABLE else "none"
    if sel == "safari":
        return "safari" if APPLESCRIPT_AVAILABLE else "none"  # macOS-only
    if sel == "chrome":
        return "chrome"
    if sel == "web":  # "the web engine" — Safari on macOS, Chrome off-mac
        return "safari" if APPLESCRIPT_AVAILABLE else "chrome"
    # auto: Up Next is web-player only (Safari on macOS, else Chrome); plain
    # playback prefers the real Music.app on macOS.
    if for_queue:
        return "safari" if APPLESCRIPT_AVAILABLE else "chrome"
    return "native" if APPLESCRIPT_AVAILABLE else "chrome"


def _queue_engine(engine_override: Optional[str] = None) -> str:
    """Resolve the engine for Up Next ops. Native Music.app has no exposed queue,
    so a 'native' resolution collapses to 'none' (caller guides to safari/chrome)."""
    eng = _playback_engine(engine_override, for_queue=True)
    return "none" if eng == "native" else eng


def _web_player(engine: str):
    """Return the module that drives the given web engine ('safari' | 'chrome')."""
    if engine == "safari":
        from . import safari_player

        return safari_player
    from . import browser

    return browser


def _platform_players() -> str:
    """The playback engines that actually exist on THIS machine — so guidance names
    only options the user can use (native/safari are macOS-only; chrome is anywhere)."""
    return "native, safari, or chrome" if APPLESCRIPT_AVAILABLE else "chrome"


def _no_player_msg(engine_override: Optional[str] = None, for_queue: bool = False) -> str:
    """Actionable error when no playback/queue engine is available. System-aware: it
    names only the engines available on this platform."""
    mode = (engine_override or get_user_preferences().get("mode") or "auto").lower()
    if for_queue and mode == "native":
        # Only reachable on macOS (native needs AppleScript), so the macOS advice fits.
        return (
            "Up Next isn't available in native (Music.app) mode — it's a web-player "
            "feature. Set mode to safari or chrome (or pass engine='safari')."
        )
    if mode == "api":
        return (
            f"API mode has no player. Set mode to {_platform_players()} (or pass "
            "engine=) to play or queue."
        )
    if APPLESCRIPT_AVAILABLE:
        return (
            "No playback engine is available. Use native (Music.app) or safari — or "
            "install Google Chrome for the cross-platform web player."
        )
    return (
        "No playback engine is available. On Windows/Linux playback uses Google Chrome "
        "(the web player) — install Chrome and run `applemusic-mcp login` once to set "
        "it up."
    )


# Active-engine tracking — the engine that last played or queued. control /
# now_playing target it so a session that started native (Music.app) but then used
# Up Next (Safari) stays coherent: the queue pulls transport control into Safari.
_active_playback_engine = ""  # '' | 'native' | 'safari' | 'chrome'


def _set_active_playback(engine: str) -> None:
    global _active_playback_engine
    if engine in ("native", "safari", "chrome"):
        _active_playback_engine = engine


def _get_active_playback() -> str:
    """Engine for control / now_playing: the last-used one, else the auto default."""
    return _active_playback_engine or _playback_engine()


def _mode_pinned_native() -> bool:
    """True when the user explicitly pinned ``mode=native`` (so playback must
    stay in Music.app and never fall back to the browser). ``auto`` is NOT
    pinned, so it may use the browser as a playback safety net."""
    return (get_user_preferences().get("mode") or "auto").lower() == "native"


def _use_browser_playback() -> bool:
    """Playback follows the engine: the web engine plays through the local Chrome
    web player, native plays through Music.app. There is no separate playback
    preference. Browser playback needs a signed-in session (a media-user-token)."""
    if os.environ.get("APPLEMUSIC_FORCE_BROWSER_PLAYBACK") == "1":
        return True
    return _engine() == "api"


# Writes choose their rail by CREDENTIAL + capability, independent of the playback
# `mode`. Choosing web *playback* must not force writes onto the grey rail when the
# user holds a developer token.
#
# Ops whose only non-web (legit) implementation is macOS AppleScript — off macOS
# the public API can't do them (delete 401s, move/rename have no public endpoint),
# so they fall to the web (amp-api) rail there.
_WEB_ONLY_OFF_MAC_OPS = {
    "delete",
    "remove",
    "rename",
    "move",
    "create_folder",
    "delete_folder",
    "rename_folder",
}

# Human labels for the suffix that tells the user which rail a write took.
_RAIL_LABELS = {
    "native": "via Music.app",
    "sanctioned": "via Apple Music API",
    "web": "via web player",
}


def _write_rail(op: str, *, catalog: bool = False) -> str:
    """Pick the write rail for ``op`` — INDEPENDENT of the playback mode.

    Returns one of:
      ``native``     – AppleScript on macOS (tokenless, local, fully legit)
      ``sanctioned`` – Apple Music API with a generated developer token (official)
      ``web``        – amp-api with the harvested web token (community path; used
                       only for ops the public API can't do, or with no dev token)

    Prefers the most official rail available. ``catalog=True`` marks an add of a
    song not already in the library (it needs the API even on macOS)."""
    if _forced_tokenless() and APPLESCRIPT_AVAILABLE:
        return "native"
    if APPLESCRIPT_AVAILABLE:
        # macOS: local Music.app handles tokenless writes. Only a catalog add (a
        # song not yet in the library) needs the API.
        if catalog and op in ("add", "library_add"):
            return "sanctioned" if _has_developer_token() else "web"
        return "native"
    # off macOS — no AppleScript:
    if op in _WEB_ONLY_OFF_MAC_OPS:
        return "web"
    return "sanctioned" if _has_developer_token() else "web"


def _label_write(result: str, rail: str) -> str:
    """Append a 'via <rail>' suffix to a successful write result so the user can
    see which path it took. Left untouched on error strings."""
    label = _RAIL_LABELS.get(rail)
    if not label or not result or result.lstrip().startswith("Error"):
        return result
    return f"{result} ({label})"


# --- playlist mutations over the API engine (cross-platform, no AppleScript) ---


def _playlist_create_api(name: str, description: str = "") -> str:
    ok, res = amp_api.create_playlist(name, description)
    if ok:
        audit_log.log_action("create_playlist", {"name": name, "via": "api"})
        return f"Created playlist '{name}' (ID: {res})"
    return f"Error: {res}"


def _resolve_playlist_for_write(name: str):
    """Resolve a playlist for a DESTRUCTIVE web op (delete/rename). Exact
    (case-insensitive) match wins; a SINGLE substring match is allowed; MULTIPLE
    substring matches are refused — never silently destroy the wrong playlist on a
    short/common name. Returns ``(pl_or_None, error_or_None)``; callers must echo
    ``pl['name']`` (the resolved name), not the requested one."""
    try:
        pls = amp_api.list_playlists()
    except Exception:
        pls = []
    if pls:
        tl = name.strip().lower()
        exact = [p for p in pls if p.get("name", "").strip().lower() == tl]
        if exact:
            return exact[0], None
        loose = [p for p in pls if tl in p.get("name", "").strip().lower()]
        if len(loose) == 1:
            return loose[0], None
        if len(loose) > 1:
            names = ", ".join(repr(p.get("name", "")) for p in loose[:6])
            return None, (
                f"Error: '{name}' matches multiple playlists ({names}) — "
                "use the exact name so the right one is affected."
            )
        return None, None  # genuinely not found — caller renders _resolve_failure_msg
    # No library listing available (offline / empty) — fall back to id resolution.
    pid = amp_api.resolve_playlist_id(name, api_created_only=False)
    return ({"id": pid, "name": name} if pid else None), None


def _playlist_delete_api(name: str) -> str:
    pl, err = _resolve_playlist_for_write(name)
    if err:
        return err
    if not pl:
        return _resolve_failure_msg(f"playlist {name!r} not found in your library")
    ok, msg = amp_api.delete_playlist(pl["id"])
    if ok:
        audit_log.log_action("delete_playlist", {"name": pl["name"], "via": "api"})
        return f"Deleted playlist: {pl['name']}"
    return f"Error: {msg}"


def _playlist_rename_api(name: str, new_name: str) -> str:
    pl, err = _resolve_playlist_for_write(name)
    if err:
        return err
    if not pl:
        return _resolve_failure_msg(f"playlist {name!r} not found in your library")
    ok, msg = amp_api.rename_playlist(pl["id"], new_name)
    return f"Renamed '{pl['name']}' to '{new_name}'" if ok else f"Error: {msg}"


_SESSION_EXPIRED_MSG = (
    "Error: your Apple Music session has expired — re-run `applemusic-mcp login` "
    "(browser) or `applemusic-mcp login --dev` (developer-token path)."
)
# Apple's throttle is a ROLLING ~60-minute window with no Retry-After header, so
# "wait a moment" was wrong advice: a short cooldown still 429s, and each retry
# adds to the very count keeping you throttled (#42).
_THROTTLED_REASON = (
    "Apple Music is rate-limiting requests (HTTP 429). Apple's window is rolling and "
    "up to ~60 minutes long — a short wait won't clear it, and retrying extends it. "
    "For bulk work (playlist imports, library migrations), `applemusic-mcp login --dev` "
    "uses your own Apple Developer token, which gets its own much larger quota instead "
    "of sharing Apple's public web-player one."
)
_SESSION_THROTTLED_MSG = f"Error: {_THROTTLED_REASON}"


def _api_error(e: Exception) -> str:
    """Render a failed ``requests`` call for the user, and record its status.

    A raw ``429 Client Error: Too Many Requests`` tells the user nothing useful
    and implies an immediate retry will work — it won't, and it makes things
    worse. Swap it for the real explanation, and note the 429 so the reads that
    swallow errors into empty lists can be attributed correctly (#42)."""
    resp = getattr(e, "response", None)
    code = getattr(resp, "status_code", None)
    if code is not None:
        amp_api.note_status(code, amp_api.API)
        if code == 429:
            return _SESSION_THROTTLED_MSG
    return f"API Error: {e}"


def _catalog_miss_reason(not_found_msg: str) -> str:
    """An empty catalog search normally means "no such song" — unless a 429 just
    came back, in which case the search never really ran and "not found" is a
    false negative (#42). Unlike ``_resolve_failure_msg`` this costs no request:
    on the miss path of a bulk loop, a probe per miss is exactly what you can't
    afford while throttled."""
    return _THROTTLED_REASON if amp_api.throttled_recently() else not_found_msg


def _resolve_failure_msg(not_found_msg: str) -> str:
    """A resolve/read came back empty. Disambiguate genuinely-not-found from an
    expired session or a 429 (which the swallow-and-return-empty reads hide) so
    the user sees the real cause and the right fix, not a misleading 'not found'."""
    st = amp_api.session_status()
    if st == "expired":
        return _SESSION_EXPIRED_MSG
    if st == "throttled":
        return _SESSION_THROTTLED_MSG
    return f"Error: {not_found_msg}"


def _safe_single_match(matches: list[dict], term: str) -> tuple[dict, int]:
    """Pick exactly ONE match for a destructive op — an exact (case-insensitive)
    title match if present, else the first candidate. Returns (chosen, others).

    This is the guardrail that stops a short/common ``term`` (e.g. "Love") from
    fanning a delete across every substring match. It mirrors the native
    AppleScript path, which removes a single track, not all loose matches."""
    tl = term.strip().lower()
    exact = [m for m in matches if m.get("name", "").strip().lower() == tl]
    chosen = exact[0] if exact else matches[0]
    return chosen, len(matches) - 1


def _playlist_remove_api(playlist: str, track: str, artist: str = "") -> str:
    pid = amp_api.resolve_playlist_id(playlist, api_created_only=False)
    if not pid:
        return _resolve_failure_msg(f"playlist {playlist!r} not found in your library")
    tracks = amp_api.get_tracks(pid)
    tl = track.lower()
    al = artist.lower()
    matches = [
        t for t in tracks if tl in t["name"].lower() and (not artist or al in t["artist"].lower())
    ]
    if not matches:
        # An EMPTY read can mean an expired/throttled session, not a genuinely
        # absent track — disambiguate instead of lying "not found".
        if not tracks:
            return _resolve_failure_msg(f"{track!r} not found in {playlist!r}")
        return f"Error: {track!r} not found in {playlist!r}"
    chosen, others = _safe_single_match(matches, track)
    ok, msg = amp_api.remove_track(pid, chosen["relationship_id"])
    if not ok:
        # Same 401/403 ambiguity as add: expired session vs a Music.app-made
        # playlist the web API can't modify. Surface the real cause, not a flat
        # "remove failed."
        if msg.startswith("status 401") or msg.startswith("status 403"):
            st = amp_api.session_status()
            if st == "throttled":
                return (
                    f"Error: {msg}. Can't tell whether your session expired or this "
                    "playlist just isn't writable over the web API — you're rate-limited "
                    "right now, so the check that would distinguish them can't run. "
                    "Retry once the window clears."
                )
            if st == "expired":
                return f"Error: your web session expired — re-run `applemusic-mcp login`. ({msg})"
            return (
                f"Error: couldn't remove from '{playlist}' over the web API ({msg}). This "
                "playlist was likely created in Music.app, which the web API can't modify "
                "(on macOS it's edited locally instead)."
            )
        return f"Error: remove failed for {track!r}: {msg}"
    audit_log.log_action("remove_track", {"playlist": playlist, "track": track, "via": "api"})
    out = f"Removed {chosen['name']} - {chosen['artist']} from {playlist!r}"
    if others:
        out += (
            f"\n({others} other track(s) also matched {track!r} — only that one was "
            f"removed; pass artist=… or a more exact title to target another.)"
        )
    return out


def _folder_create_api(name: str) -> str:
    ok, res = amp_api.create_folder(name)
    if ok:
        audit_log.log_action("create_folder", {"name": name, "via": "api"})
        return f"Created folder '{name}' (ID: {res})"
    return f"Error: {res}"


def _folder_delete_api(name: str) -> str:
    fid = amp_api.resolve_folder_id(name)
    if not fid:
        return f"Error: folder {name!r} not found"
    ok, msg = amp_api.delete_folder(fid)
    if ok:
        audit_log.log_action("delete_folder", {"name": name, "via": "api"})
        return f"Deleted folder: {name}"
    return f"Error: {msg}"


def _playlist_move_api(playlist: str, folder: str) -> str:
    pid = amp_api.resolve_playlist_id(playlist, api_created_only=False)
    if not pid:
        return _resolve_failure_msg(f"playlist {playlist!r} not found in your library")
    # Move to root when folder is empty/"root"; else into the folder (create it
    # if it doesn't exist yet, mirroring the native move-into-folder behavior).
    if not folder or folder.strip().lower() in ("root", ""):
        ok, msg = amp_api.move_playlist_to_folder(pid, amp_api.ROOT_FOLDER)
        return f"Moved '{playlist}' to top level" if ok else f"Error: {msg}"
    fid = amp_api.resolve_folder_id(folder)
    if not fid:
        cok, cres = amp_api.create_folder(folder)
        if not cok:
            return f"Error: could not create folder {folder!r}: {cres}"
        fid = cres
    ok, msg = amp_api.move_playlist_to_folder(pid, fid)
    if ok:
        audit_log.log_action(
            "move_playlist", {"playlist": playlist, "folder": folder, "via": "api"}
        )
        return f"Moved '{playlist}' into folder '{folder}'"
    return f"Error: {msg}"


def _playlist_add_api(
    playlist: str,
    track: str,
    artist: str = "",
    allow_duplicates: bool = False,
    auto_add: Optional[bool] = None,
) -> str:
    """Add track(s) to a playlist entirely over the API (cross-platform): resolve
    the playlist's library id, resolve each track to a catalog id (direct id, or
    catalog search by name), and POST them. Adds to the library implicitly."""
    # A bare playlist id (p.XXXXX) is used directly; a name goes through the rich
    # 3-pass fuzzy matcher (handles &/and, emoji, accents).
    fuzzy = None
    if playlist.startswith("p.") and len(playlist) > 2 and playlist[2:].isalnum():
        pid = playlist
    else:
        pid, fuzzy = _find_api_playlist_by_name(playlist)
        if not pid:
            pid = amp_api.resolve_playlist_id(playlist, api_created_only=False)
    if not pid:
        return _resolve_failure_msg(f"playlist {playlist!r} not found in your library")
    # Honor the auto_add preference (parity with the native path): when off, a bare
    # NAME isn't catalog-searched + added — the user opted out of that.
    if auto_add is None:
        auto_add = bool(get_user_preferences().get("auto_add"))
    # De-dup against what's already in the playlist (unless allow_duplicates), so a
    # repeated add doesn't silently stack copies — the native path does this too.
    existing_ids: set = set()
    existing_names: set = set()
    if not allow_duplicates:
        for t in amp_api.get_tracks(pid):
            if t.get("catalog_id"):
                existing_ids.add(str(t["catalog_id"]))
            if t.get("name"):
                existing_names.add(t["name"].strip().lower())

    items: list = []  # str catalog id, or (id, "library-songs") for a library song
    added_names: list[str] = []
    errors: list[str] = []
    skipped: list[str] = []

    def _dup(cid: str = "", nm: str = "") -> bool:
        return bool(
            (cid and str(cid) in existing_ids) or (nm and nm.strip().lower() in existing_names)
        )

    for r in _resolve_track(track, artist):
        if r.error:
            errors.append(r.error)
        elif r.input_type == InputType.CATALOG_ID:
            if _dup(cid=r.value):
                skipped.append(f"track {r.value}")
                continue
            items.append(r.value)
            added_names.append(f"track {r.value}")
            existing_ids.add(str(r.value))
        elif r.input_type == InputType.LIBRARY_ID:
            items.append((r.value, "library-songs"))
            added_names.append(f"library track {r.value}")
        elif r.input_type in (InputType.NAME, InputType.JSON_OBJECT):
            q = f"{r.value} {r.artist or artist}".strip()
            # auto_add controls whether a name NOT already in your library is pulled
            # from the catalog (parity with native): off → only add it if it's already
            # in your library; on → search the catalog. Either way, de-dup.
            hit = None
            if auto_add:
                songs = amp_api.search_catalog_songs(q, 1)
                hit = {"id": songs[0]["id"], **songs[0]} if songs else None
            else:
                libs = amp_api.search_library_songs(q, 1)
                if libs:
                    hit = {"id": libs[0].get("catalog_id"), **libs[0]}
                else:
                    skipped.append(
                        f"{r.value} (not in your library — set auto_add=True to add it from the catalog)"
                    )
            if hit is None:
                if auto_add:
                    # A rate-limited search returns EMPTY, not an error — reporting
                    # "not found in catalog" here is how a throttle silently becomes
                    # a run full of false negatives (#42). Name the real cause, and
                    # stop resolving: more requests inside Apple's rolling window
                    # only push the recovery further out.
                    if amp_api.throttled_recently():
                        errors.append(_THROTTLED_REASON)
                        break
                    errors.append(f"{r.value}: not found in catalog")
            elif _dup(cid=hit.get("id"), nm=hit.get("name")):
                skipped.append(f"{hit.get('name')} - {hit.get('artist')}")
            else:
                cid = hit.get("id")
                items.append(cid if cid else (hit["id"], "library-songs"))
                added_names.append(f"{hit.get('name')} - {hit.get('artist')}")
                if cid:
                    existing_ids.add(str(cid))
                existing_names.add((hit.get("name") or "").strip().lower())
        else:
            errors.append(f"{r.value}: unsupported id type for add")
    if not items:
        if skipped and not errors:
            return "Nothing added — all already in the playlist or skipped:\n  - " + "\n  - ".join(
                skipped
            )
        msg = "Error: nothing to add"
        if skipped:
            msg += "\nSkipped: " + ", ".join(skipped)
        if errors:
            msg += "\n" + "\n".join(errors)
        return msg
    ok, msg = amp_api.add_tracks(pid, items)
    if not ok:
        # A 401/403 is ambiguous: either the web session expired, OR the playlist
        # was created in Music.app (the web API can't modify those). Disambiguate
        # with a cheap session probe before blaming origin — otherwise an expired
        # session gets the wrong cause and the wrong fix.
        if msg.startswith("status 401") or msg.startswith("status 403"):
            st = amp_api.session_status()
            if st == "throttled":
                return (
                    f"Error: {msg}. Can't tell whether your session expired or this "
                    "playlist just isn't writable over the web API — you're rate-limited "
                    "right now, so the check that would distinguish them can't run. "
                    "Retry once the window clears."
                )
            if st == "expired":
                return f"Error: your web session expired — re-run `applemusic-mcp login`. ({msg})"
            return (
                f"Error: couldn't add to '{playlist}' over the web API ({msg}). This "
                "playlist was likely created in Music.app, which the web API can't "
                "modify. On macOS it's edited locally instead; off macOS, add to an "
                "API-created playlist."
            )
        # The amp-api tracks endpoint reliably 500s ("Unable to update tracks",
        # code 50001) for Music.app-made and Apple-curated playlists — the web API
        # genuinely can't write them (verified in-page and external, both id forms).
        # Say so honestly rather than surface a raw 500.
        if "Unable to update tracks" in msg or msg.startswith("status 500"):
            return (
                f"Error: '{playlist}' can't be modified over the Apple Music web API "
                "— it was created in Music.app (or is Apple-curated). On macOS the "
                "tool edits it locally through Music.app; off macOS, the API can't "
                "add to these playlists."
            )
        return f"Error: {msg}"
    # Surface the playlist fuzzy match (parity with the native path) so the user
    # knows if "Rock" landed in "Rock & Roll Classics".
    dest = f"'{fuzzy.matched_name}'" if fuzzy and fuzzy.matched_name else f"'{playlist}'"
    out = f"Added {len(items)} track(s) to {dest}:\n" + "\n".join(
        f"  + {n} (added to library + playlist via the Apple Music web API)" for n in added_names
    )
    if fuzzy:
        out += f"\n{_format_fuzzy_match(fuzzy)}"
    if skipped:
        out += "\n" + "\n".join(f"  ~ skipped (already in playlist): {s}" for s in skipped)
    if errors:
        out += "\n" + "\n".join(f"  - {e}" for e in errors)
    return out


def _library_remove_api(track: str, artist: str = "") -> str:
    """Remove track(s) from the user's library over the API (cross-platform):
    find matching library songs, then DELETE each by its library-song id."""
    if not track:
        return "Error: Provide track parameter"
    term = f"{track} {artist}".strip()
    songs = amp_api.search_library_songs(term)
    tl = track.lower()
    al = artist.lower()
    matches = [
        s for s in songs if tl in s["name"].lower() and (not artist or al in s["artist"].lower())
    ]
    if not matches:
        return _resolve_failure_msg(f"{track!r} not found in your library")
    # Library removal is permanent and not cheaply reversible, so NEVER fan a
    # fuzzy term across many songs — remove exactly one (exact title preferred),
    # matching the native path, and tell the user what else matched.
    chosen, others = _safe_single_match(matches, track)
    ok, msg = amp_api.remove_from_library(chosen["id"])
    if not ok:
        return f"Error: {chosen['name']}: {msg}"
    audit_log.log_action("remove_from_library", {"track": track, "via": "api"})
    out = f"Removed from your library: {chosen['name']} - {chosen['artist']}"
    if others:
        out += (
            f"\n({others} other title(s) also matched {track!r} — nothing else was "
            f"removed; pass artist=… or a more exact title to target another.)"
        )
    return out


def _browser_play(
    wp,
    track: str = "",
    artist: str = "",
    url: str = "",
    playlist: str = "",
    album: str = "",
    shuffle: bool = False,
) -> str:
    """Play a track, playlist, album, or Apple Music URL in a web player. ``wp`` is
    the resolved engine module (safari_player or browser) — cross-platform parity
    with native macOS playback."""
    if url:
        ok, msg = wp.play_url(url, shuffle)
        return msg if ok else f"Error: {msg}"
    if playlist:
        pid, _ = _find_api_playlist_by_name(playlist)
        if not pid:
            pid = amp_api.resolve_playlist_id(playlist, api_created_only=False)
        if not pid:
            return _resolve_failure_msg(f"playlist {playlist!r} not found in your library")
        ok, msg = wp.play_descriptor({"playlist": pid}, shuffle)
        return msg if ok else f"Error: {msg}"
    if album:
        alb, err, _ = _find_matching_catalog_album(album, artist)
        if not alb:
            return f"Error: {err or f'album {album!r} not found in catalog'}"
        ok, msg = wp.play_descriptor({"album": alb["id"]}, shuffle)
        return msg if ok else f"Error: {msg}"
    if track:
        resolved = _resolve_catalog_track_itunes(track, artist)
        if not resolved:
            return f"Error: '{track}' not found in catalog"
        ok, msg = wp.play_url(resolved["url"], shuffle)
        return msg if ok else f"Error: {msg}"
    return "Error: provide track, playlist, album, or url"


def _format_applescript_error(raw: str, operation: str = "") -> str:
    """Translate a raw AppleScript error into an actionable user message.

    The MCP previously fell through to the API path when AppleScript
    failed, surfacing "Developer token not found" — even when the real
    cause was Music.app being closed or Automation permissions not granted.
    This helper picks the right user-facing message based on
    ``asc.classify_error`` so callers can stop misdirecting users to the
    token-setup CLI when that's not what's broken.

    Args:
        raw: stderr / message returned by ``run_applescript`` on failure
        operation: human-readable description of what was being attempted
            (e.g. "create playlist", "list playlists"). Embedded in the
            message so the user knows which call surfaced the problem.

    Returns:
        Single-line user-facing string. Includes the raw error in
        parentheses for the unknown category so users can still report it.
    """
    op = f" ({operation})" if operation else ""
    category = asc.classify_error(raw)

    if category == asc.ERROR_MUSIC_NOT_RUNNING:
        return (
            f"Music.app isn't running{op}. Open Music.app, then retry. "
            "(macOS playlist/library operations talk to Music.app via AppleScript "
            "— no developer token required, but the app needs to be running.)"
        )
    if category == asc.ERROR_AUTOMATION_DENIED:
        return (
            f"Automation permission denied{op}. Your MCP host (Claude Desktop, "
            "your terminal, etc.) hasn't been granted permission to control "
            "Music.app. Open System Settings → Privacy & Security → Automation, "
            "find the app running this MCP, and enable the 'Music' toggle."
        )
    if category == asc.ERROR_TIMEOUT:
        return (
            f"AppleScript timed out{op}. Music.app may be unresponsive — try "
            "quitting and reopening it, then retry."
        )
    if category == asc.ERROR_SYNTAX:
        return f"AppleScript syntax error{op} — please report this. Raw: {raw}"
    return f"AppleScript failed{op}: {raw}"


def _attach_error(name: str, raw: str) -> str:
    """Friendly per-track message for a playlist-attach failure. Catches the
    track-specific -10006 ("Can't set user playlist … to shared track …"), which
    means that library copy is a cloud/shared reference Music.app refuses to add to
    a playlist — a different version of the same song usually attaches fine."""
    if "-10006" in raw or ("shared track" in raw and "user playlist" in raw):
        return (
            f"{name}: this library copy is a cloud/shared reference that Music.app "
            "can't add to a playlist — try a different version of the track."
        )
    return f"{name}: {raw}"


def _play_after_add(label: str, last_err: str) -> str:
    """After adding a catalog track to the library, a failed play attempt is either
    transient (the track hasn't synced locally yet → honest "sync pending") OR a
    real break (Automation denied, Music not running, timeout). Surface the real
    cause instead of always blaming sync — otherwise the user waits forever on a
    "sync pending" that will never resolve."""
    cat = asc.classify_error(last_err or "")
    if cat in (asc.ERROR_AUTOMATION_DENIED, asc.ERROR_MUSIC_NOT_RUNNING, asc.ERROR_TIMEOUT):
        return _format_applescript_error(last_err, "play the just-added track")
    return f"[Catalog→Library] Added but sync pending: {label}"


# ============ INTERNAL HELPERS ============


def _apply_pagination(
    items: list,
    limit: int = 0,
    offset: int = 0,
) -> tuple[list, int, str | None]:
    """Apply offset/limit pagination to a list.

    Args:
        items: List of items to paginate
        limit: Max items to return (0 = all)
        offset: Skip first N items

    Returns:
        Tuple of (paginated_items, total_count, error_message)
        - On success: (items, total, None)
        - On error: ([], total, error message)
    """
    total_count = len(items)

    # offset == total_count means the caller has paged through everything and is
    # requesting the next (empty) page. Only error when offset exceeds the total.
    if offset > total_count and total_count > 0:
        return [], total_count, f"Offset {offset} exceeds {total_count} items"

    if offset > 0:
        items = items[offset:]
    if limit > 0:
        items = items[:limit]

    return items, total_count, None


def _detect_id_type(id_str: str) -> str:
    """Detect the type of an Apple Music ID.

    ID patterns:
    - Catalog: 9+ digits (e.g., "1440783617")
    - Library: starts with "i." (e.g., "i.ABC123XYZ")
    - Playlist: starts with "p." (e.g., "p.XYZ789ABC")
    - Persistent: 12+ hex chars (e.g., "ABC123DEF456")

    Args:
        id_str: The ID string to classify

    Returns:
        One of: "catalog", "library", "playlist", "persistent", "unknown"
    """
    id_str = id_str.strip()
    if id_str.startswith("i."):
        return "library"
    elif id_str.startswith("p."):
        return "playlist"
    elif id_str.isdigit() and len(id_str) >= MIN_CATALOG_ID_LENGTH:
        return "catalog"
    elif (
        len(id_str) >= 12 and re.match(r"^[A-Fa-f0-9]+$", id_str) and re.search(r"[A-Fa-f]", id_str)
    ):
        return "persistent"
    else:
        return "unknown"


def _find_api_playlist_by_name(name: str) -> tuple[str | None, FuzzyMatchResult | None]:
    """Find API playlist ID by name with fuzzy matching.

    Uses generic _fuzzy_match_entity for 3-pass matching:
    1. Exact match (case-insensitive)
    2. Partial match (query contained in playlist name)
    3. Fuzzy match (normalized with transformations)

    Args:
        name: Playlist name to search for

    Returns:
        Tuple of (playlist_id, fuzzy_match_result)
        - playlist_id: API playlist ID (p.XXX) if found, None otherwise
        - fuzzy_match_result: Details about the match if fuzzy/partial, None if exact
    """
    try:
        headers = get_headers()
        api_offset = 0

        # Collect all playlists first (for multi-pass matching)
        all_playlists = []
        while True:
            response = requests.get(
                f"{BASE_URL}/me/library/playlists",
                headers=headers,
                params={"limit": 100, "offset": api_offset},
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code != 200:
                break

            playlists = response.json().get("data", [])
            if not playlists:
                break

            all_playlists.extend(playlists)

            if len(playlists) < 100:
                break
            api_offset += 100

        # Use generic fuzzy matching
        def playlist_name_extractor(pl: dict) -> str:
            return pl.get("attributes", {}).get("name", "")

        matched, fuzzy_result = _fuzzy_match_entity(name, all_playlists, playlist_name_extractor)

        if matched:
            return matched.get("id"), fuzzy_result

    except Exception:
        pass  # Fall back to AppleScript

    return None, None


def _resolve_playlist(playlist: str) -> ResolvedPlaylist:
    """Resolve a playlist parameter to all available identifiers.

    Populates as many identifiers as possible (API ID, name, persistent ID) so
    callers can use what they need. Different operations require different IDs:
    - API operations prefer api_id for performance
    - AppleScript operations require applescript_name

    Auto-detects based on pattern:
    - Matches "p." + alphanumeric only → playlist ID (e.g., p.ABC123xyz)
    - Otherwise → playlist name, tries to find API ID first for better performance

    Args:
        playlist: Either a playlist ID (p.XXX) or name

    Returns:
        ResolvedPlaylist with populated fields
    """
    playlist = playlist.strip()

    if not playlist:
        return ResolvedPlaylist(raw_input=playlist, error="Error: playlist parameter required")

    # Real playlist IDs are "p." followed by alphanumeric chars only (no spaces/punctuation)
    # This correctly treats "p.s. I love you" as a name, not an ID
    if playlist.startswith("p.") and len(playlist) > 2 and playlist[2:].isalnum():
        # User provided explicit ID
        # TODO: Could look up the name from API for completeness
        return ResolvedPlaylist(
            raw_input=playlist,
            api_id=playlist,
            applescript_name=None,  # Not available without lookup
        )

    # User provided a name - try to find API ID first (faster than AppleScript)
    api_id, fuzzy_match = _find_api_playlist_by_name(playlist)

    if api_id:
        # Found via API - we have both ID and name
        # The matched name comes from fuzzy_match if it was fuzzy, otherwise it's exact
        matched_name = fuzzy_match.matched_name if fuzzy_match else playlist
        return ResolvedPlaylist(
            raw_input=playlist,
            api_id=api_id,
            applescript_name=matched_name,  # Use the actual matched name
            fuzzy_match=fuzzy_match,
        )

    # Not found via API - try AppleScript-based fuzzy matching if available
    if APPLESCRIPT_AVAILABLE:
        success, playlists = asc.get_playlists()
        if success and playlists:
            # Use fuzzy matching on AppleScript playlist names
            def playlist_name_extractor(pl: dict) -> str:
                return pl.get("name", "")

            matched, fuzzy_result = _fuzzy_match_entity(
                playlist, playlists, playlist_name_extractor
            )
            if matched:
                matched_name = matched.get("name", playlist)
                return ResolvedPlaylist(
                    raw_input=playlist,
                    api_id=None,
                    applescript_name=matched_name,  # Use actual matched name
                    fuzzy_match=fuzzy_result,
                )

    # Fall back to raw input for AppleScript
    return ResolvedPlaylist(
        raw_input=playlist, api_id=None, applescript_name=playlist  # Use as-is for AppleScript
    )


def _detect_input_type(value: str) -> InputType:
    """Detect what type of input a string represents.

    Detection order:
    1. Prefixed IDs (i., p., l.) - explicit type markers
    2. All digits AND length >= 9 - catalog ID
    3. 12+ hex chars, no spaces - persistent ID (AppleScript)
    4. Everything else - name

    Args:
        value: The input string to classify

    Returns:
        InputType enum value
    """
    value = value.strip()

    # Check prefix-based IDs first
    if value.startswith("i."):
        return InputType.LIBRARY_ID
    if value.startswith("p.") and len(value) > 2 and value[2:].isalnum():
        return InputType.PLAYLIST_ID
    if value.startswith("l."):
        return InputType.ALBUM_ID

    # Catalog IDs are 9+ digits (Apple uses 10-digit IDs)
    if value.isdigit() and len(value) >= MIN_CATALOG_ID_LENGTH:
        return InputType.CATALOG_ID

    # Persistent IDs from AppleScript are 12+ hex chars with no spaces, must contain at least one letter
    if (
        len(value) >= 12
        and " " not in value
        and re.match(r"^[A-Fa-f0-9]+$", value)
        and re.search(r"[A-Fa-f]", value)
    ):
        return InputType.PERSISTENT_ID

    # Default to name
    return InputType.NAME


def _resolve_input(
    value: str,
    entity_type: EntityType,
    artist: str = "",
) -> list[ResolvedInput]:
    """Universal input resolution for any entity type.

    Accepts multiple formats and returns a list of resolved inputs:
    - JSON array: '[{"name":"Hey Jude","artist":"Beatles"}]'
    - CSV names: "Hey Jude, Let It Be"
    - Single ID: "1440783617" or "i.ABC123"
    - Single name: "Hey Jude"

    Detection order for single values:
    1. Starts with '[' → JSON array of objects
    2. Contains newline → one entry per line (safe for titles with commas)
    3. Contains comma → CSV of names
    4. Otherwise → single value (ID or name auto-detected)

    Args:
        value: Raw input - ID, name, CSV, or JSON
        entity_type: What kind of entity we're resolving (for context)
        artist: Artist name for disambiguation (used with names)

    Returns:
        List of ResolvedInput objects (single item for ID/name, multiple for CSV/JSON)
    """
    value = value.strip()
    if not value:
        return [ResolvedInput(input_type=InputType.NAME, value="", raw=value, error="Empty input")]

    results = []

    # 1. JSON array detection
    if value.startswith("["):
        try:
            items = json.loads(value)
            if not isinstance(items, list):
                return [  # pragma: no cover  # unreachable: json.loads of a '['-prefixed value always yields a list
                    ResolvedInput(
                        input_type=InputType.NAME,
                        value=value,
                        raw=value,
                        error="JSON must be an array",
                    )
                ]

            for item in items:
                if isinstance(item, dict):
                    name = item.get("name", "")
                    item_artist = item.get("artist", "") or artist
                    if not name:
                        results.append(
                            ResolvedInput(
                                input_type=InputType.JSON_OBJECT,
                                value="",
                                artist=item_artist,
                                raw=str(item),
                                error="Object missing 'name' field",
                            )
                        )
                    else:
                        results.append(
                            ResolvedInput(
                                input_type=InputType.JSON_OBJECT,
                                value=name,
                                artist=item_artist,
                                raw=str(item),
                            )
                        )
                elif isinstance(item, str):
                    # JSON array of strings treated as names
                    input_type = _detect_input_type(item)
                    results.append(
                        ResolvedInput(
                            input_type=input_type, value=item.strip(), artist=artist, raw=item
                        )
                    )
                else:
                    results.append(
                        ResolvedInput(
                            input_type=InputType.NAME,
                            value=str(item),
                            raw=str(item),
                            error="Invalid item type in array",
                        )
                    )

            return (
                results
                if results
                else [
                    ResolvedInput(
                        input_type=InputType.NAME, value="", raw=value, error="Empty JSON array"
                    )
                ]
            )

        except json.JSONDecodeError as e:
            return [
                ResolvedInput(
                    input_type=InputType.NAME, value=value, raw=value, error=f"Invalid JSON: {e}"
                )
            ]

    # 2. Newline-delimited detection (one entry per line).
    # Newlines are an unambiguous separator: unlike commas they never appear
    # inside a track/album title, so this is the safe way to batch names that
    # themselves contain commas (e.g. "Take Me Home, Country Roads"). A single
    # `artist` still applies to every line for disambiguation.
    if "\n" in value:
        for item in value.splitlines():
            item = item.strip()
            if item:
                input_type = _detect_input_type(item)
                results.append(
                    ResolvedInput(input_type=input_type, value=item, artist=artist, raw=item)
                )
        return (
            results
            if results
            else [
                ResolvedInput(
                    input_type=InputType.NAME, value="", raw=value, error="Empty newline list"
                )
            ]
        )

    # 3. CSV detection (contains comma, not JSON, and no artist specified)
    # When artist is provided the input is a single track/album name — commas
    # are part of the title (e.g. "Take Me Home, Country Roads"), not separators.
    if "," in value and not artist:
        for item in value.split(","):
            item = item.strip()
            if item:
                input_type = _detect_input_type(item)
                results.append(
                    ResolvedInput(input_type=input_type, value=item, artist=artist, raw=item)
                )
        return (
            results
            if results
            else [ResolvedInput(input_type=InputType.NAME, value="", raw=value, error="Empty CSV")]
        )

    # 4. Single value - detect type
    input_type = _detect_input_type(value)
    return [ResolvedInput(input_type=input_type, value=value, artist=artist, raw=value)]


def _resolve_track(track: str, artist: str = "") -> list[ResolvedInput]:
    """Convenience wrapper for track resolution."""
    return _resolve_input(track, EntityType.TRACK, artist)


def _resolve_album(album: str, artist: str = "") -> list[ResolvedInput]:
    """Convenience wrapper for album resolution."""
    return _resolve_input(album, EntityType.ALBUM, artist)


def _build_track_results(
    results: list[str],
    errors: list[str],
    success_prefix: str = "✓",
    error_prefix: str = "✗",
    success_verb: str = "processed",
    error_verb: str = "failed",
) -> str:
    """Build formatted results message from success/error lists.

    Args:
        results: List of success messages
        errors: List of error messages
        success_prefix: Prefix for success section (default: ✓)
        error_prefix: Prefix for error section (default: ✗)
        success_verb: Verb for success count (default: processed)
        error_verb: Verb for error count (default: failed)

    Returns:
        Formatted multi-line message, or "No tracks were processed" if empty
    """
    output = []

    if results:
        output.append(f"{success_prefix} {success_verb.capitalize()} {len(results)} track(s):")
        for r in results:
            output.append(f"  {r}")

    if errors:
        if output:
            output.append("")  # Blank line between sections
        output.append(f"{error_prefix} {error_verb.capitalize()} {len(errors)} track(s):")
        for e in errors:
            output.append(f"  {e}")

    if not output:
        return f"No tracks were {success_verb}"

    return "\n".join(output)


def _find_matching_catalog_song(
    name: str, artist: str = ""
) -> tuple[dict | None, str | None, FuzzyMatchResult | None]:
    """Search catalog and find a song matching name and optional artist.

    Matching priority:
    1. Exact match (case-insensitive) on name, with artist filter
    2. Partial match (name in song_name), with artist filter
    3. Fuzzy match on name only (relaxes artist constraint)

    Args:
        name: Track name to search for
        artist: Artist name (optional, for filtering)

    Returns:
        Tuple of (song_dict, error_message, fuzzy_match_result)
        - On success: (song dict, None, fuzzy_result or None)
        - On not found: (None, "Not found in catalog", None)
    """
    search_term = f"{name} {artist}".strip() if artist else name
    songs = _search_catalog_songs(search_term, limit=5)  # Get more results for fuzzy

    if not songs:
        return None, _catalog_miss_reason("Not found in catalog"), None

    # Filter by artist first if provided
    def artist_matches(song: dict) -> bool:
        if not artist:
            return True
        song_artist = song.get("attributes", {}).get("artistName", "")
        return _loose_contains(artist, song_artist)

    # Candidates that match artist filter
    artist_filtered = [s for s in songs if artist_matches(s)]

    # Use generic fuzzy matching on name
    def song_name_extractor(song: dict) -> str:
        return song.get("attributes", {}).get("name", "")

    # Try fuzzy match on artist-filtered songs first
    matched = None
    fuzzy_result = None
    if artist_filtered:
        matched, fuzzy_result = _fuzzy_match_entity(name, artist_filtered, song_name_extractor)
        if matched:
            _cache_song_metadata(matched)
            return matched, None, fuzzy_result

    # If no match with artist filter, try all songs (relaxed matching)
    if artist and not matched:
        matched, fuzzy_result = _fuzzy_match_entity(name, songs, song_name_extractor)
        if matched:
            _cache_song_metadata(matched)
            return matched, None, fuzzy_result

    return None, "Not found in catalog", None


def _cache_song_metadata(song: dict) -> None:
    """Cache song metadata for later ID lookups."""
    attrs = song.get("attributes", {})
    catalog_id = song.get("id", "")
    song_name = attrs.get("name", "")

    if catalog_id and song_name:
        cache = get_track_cache()
        cache.set_track_metadata(
            explicit="Yes" if attrs.get("contentRating") == "explicit" else "No",
            catalog_id=catalog_id,
            isrc=attrs.get("isrc") or None,
            name=song_name,
            artist=attrs.get("artistName", ""),
            album=attrs.get("albumName", ""),
        )


def _search_catalog_songs(query: str, limit: int = 5) -> list[dict]:
    """Search catalog for songs and return raw song data.

    Args:
        query: Search term
        limit: Max results (default 5)

    Returns:
        List of song dicts with 'id', 'attributes' (name, artistName, etc.)
        Empty list on error — including a 429, which is recorded via
        ``amp_api.note_status`` so callers can say "rate limited" instead of
        the false "not found" an empty list would otherwise imply (#42).
    """
    try:
        headers = get_headers()
        response = requests.get(
            f"{BASE_URL}/catalog/{get_storefront()}/search",
            headers=headers,
            params={"term": query, "types": "songs", "limit": min(limit, 25)},
            timeout=REQUEST_TIMEOUT,
        )
        amp_api.note_status(response.status_code, amp_api.API)
        if response.status_code == 200:
            data = response.json()
            return data.get("results", {}).get("songs", {}).get("data", [])
    except Exception:
        pass
    return []


def _search_catalog_albums(query: str, limit: int = 5) -> list[dict]:
    """Search catalog for albums and return raw album data.

    Args:
        query: Search term
        limit: Max results (default 5)

    Returns:
        List of album dicts with 'id', 'attributes' (name, artistName, etc.)
        Empty list on error.
    """
    try:
        headers = get_headers()
        response = requests.get(
            f"{BASE_URL}/catalog/{get_storefront()}/search",
            headers=headers,
            params={"term": query, "types": "albums", "limit": min(limit, 25)},
            timeout=REQUEST_TIMEOUT,
        )
        amp_api.note_status(response.status_code, amp_api.API)
        if response.status_code == 200:
            data = response.json()
            return data.get("results", {}).get("albums", {}).get("data", [])
    except Exception:
        pass
    return []


def _find_matching_catalog_album(
    name: str, artist: str = ""
) -> tuple[dict | None, str | None, FuzzyMatchResult | None]:
    """Search catalog and find an album matching name and optional artist.

    Uses _fuzzy_match_entity for 3-pass matching:
    1. Exact match (case-insensitive) on name, with artist filter
    2. Partial match (name in album_name), with artist filter
    3. Fuzzy match on name only (relaxes artist constraint)

    Args:
        name: Album name to search for
        artist: Artist name (optional, for filtering)

    Returns:
        Tuple of (album_dict, error_message, fuzzy_match_result)
        - On success: (album dict, None, fuzzy_result or None)
        - On not found: (None, "Not found in catalog", None)
    """
    search_term = f"{name} {artist}".strip() if artist else name
    albums = _search_catalog_albums(search_term, limit=5)

    if not albums:
        return None, "Not found in catalog", None

    # Filter by artist first if provided
    def artist_matches(album: dict) -> bool:
        if not artist:
            return True
        album_artist = album.get("attributes", {}).get("artistName", "")
        return _loose_contains(artist, album_artist)

    # Candidates that match artist filter
    artist_filtered = [a for a in albums if artist_matches(a)]

    # Use generic fuzzy matching on name
    def album_name_extractor(album: dict) -> str:
        return album.get("attributes", {}).get("name", "")

    # Try fuzzy match on artist-filtered albums first
    matched = None
    fuzzy_result = None
    if artist_filtered:
        matched, fuzzy_result = _fuzzy_match_entity(name, artist_filtered, album_name_extractor)
        if matched:
            return matched, None, fuzzy_result

    # If no match with artist filter, try all albums (relaxed matching)
    if artist and not matched:
        matched, fuzzy_result = _fuzzy_match_entity(name, albums, album_name_extractor)
        if matched:
            return matched, None, fuzzy_result

    return None, "Not found in catalog", None


def _search_library_songs(query: str, limit: int = 5) -> list[dict]:
    """Search library for songs and return raw song data.

    Args:
        query: Search term
        limit: Max results (default 5)

    Returns:
        List of song dicts with 'id', 'attributes' (name, artistName, etc.)
        Empty list on error.
    """
    try:
        headers = get_headers()
        response = requests.get(
            f"{BASE_URL}/me/library/search",
            headers=headers,
            params={"term": query, "types": "library-songs", "limit": min(limit, 25)},
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code == 200:
            data = response.json()
            return data.get("results", {}).get("library-songs", {}).get("data", [])
    except Exception:
        pass
    return []


def _find_track_id(name: str, artist: str = "") -> tuple[str | None, str | None, str]:
    """Find a track by name, searching library first then catalog.

    This is the canonical way to find a track - always prefers library
    (track is already added) over catalog (would need to add first).

    Args:
        name: Track name to search for (partial match)
        artist: Artist name (optional, improves matching)

    Returns:
        Tuple of (library_id, catalog_id, display_name)
        - If in library: (library_id, None, "Name - Artist")
        - If in catalog only: (None, catalog_id, "Name - Artist")
        - If not found: (None, None, "")
    """
    search_term = f"{name} {artist}".strip() if artist else name

    # 1. Search library first
    library_songs = _search_library_songs(search_term, limit=5)
    for song in library_songs:
        attrs = song.get("attributes", {})
        song_name = attrs.get("name", "")
        song_artist = attrs.get("artistName", "")

        # Partial match on name
        if not _loose_contains(name, song_name):
            continue
        # Partial match on artist if provided
        if artist and not _loose_contains(artist, song_artist):
            continue

        library_id = song.get("id", "")
        display = f"{song_name} - {song_artist}"
        return library_id, None, display

    # 2. Fall back to catalog
    catalog_songs = _search_catalog_songs(search_term, limit=5)
    for song in catalog_songs:
        attrs = song.get("attributes", {})
        song_name = attrs.get("name", "")
        song_artist = attrs.get("artistName", "")

        # Partial match on name
        if not _loose_contains(name, song_name):
            continue
        # Partial match on artist if provided
        if artist and not _loose_contains(artist, song_artist):
            continue

        catalog_id = song.get("id", "")
        display = f"{song_name} - {song_artist}"

        # Cache for later lookups
        cache = get_track_cache()
        cache.set_track_metadata(
            explicit="Yes" if attrs.get("contentRating") == "explicit" else "No",
            catalog_id=catalog_id,
            isrc=attrs.get("isrc") or None,
            name=song_name,
            artist=song_artist,
            album=attrs.get("albumName", ""),
        )

        return None, catalog_id, display

    return None, None, ""


def _add_to_library_api(catalog_ids: list[str], content_type: str = "songs") -> tuple[bool, str]:
    """Add content to library by catalog ID.

    Args:
        catalog_ids: List of catalog IDs
        content_type: Type of content - "songs" (default) or "albums"

    Returns:
        Tuple of (success, message)
    """
    if not catalog_ids:
        return False, "No catalog IDs provided"

    # Map type to API parameter
    type_param = {
        "songs": "ids[songs]",
        "albums": "ids[albums]",
    }.get(content_type, "ids[songs]")

    type_label = "song" if content_type == "songs" else "album"

    try:
        headers = get_headers()
        response = requests.post(
            f"{BASE_URL}/me/library",
            headers=headers,
            params={type_param: ",".join(catalog_ids)},
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code in (200, 201, 202, 204):
            return True, f"Added {len(catalog_ids)} {type_label}(s) to library"
        if response.status_code in (401, 403):
            return False, (
                f"Not authorized (status {response.status_code}) — your session may have "
                "expired. Re-run `applemusic-mcp login` or `applemusic-mcp login --dev`."
            )
        return False, f"API returned status {response.status_code}"
    except Exception as e:
        return False, str(e)


def _add_songs_to_library(catalog_ids: list[str]) -> tuple[bool, str]:
    """Add songs to library by catalog ID. (Legacy wrapper)"""
    return _add_to_library_api(catalog_ids, "songs")


def _add_album_to_library(album_id: str) -> tuple[bool, str]:
    """Add album to library by catalog ID."""
    return _add_to_library_api([album_id], "albums")


def _split_track_artist_candidates(combined: str) -> list[tuple[str, str]]:
    """Split a free-form 'Track - Artist' string into candidate (name, artist) pairs.

    Users often paste tracks as "Song - Artist" in a single field. This splits
    on ' - ' and returns ordered candidates to try. Returns empty list if no
    plausible split exists (no ' - ' separator).

    Ordering rationale: forward form (Song - Artist) first since that's the
    conventional write order. Reverse form (Artist - Song) as a safety net for
    users who wrote it backwards. Last-dash variants cover multi-dash names
    like "Sgt. Pepper's Lonely Hearts Club Band - Reprise - The Beatles".
    Callers should only feed this function inputs gated on "Track not found"
    errors — don't second-guess a first-attempt success.
    """
    if " - " not in combined:
        return []
    candidates: list[tuple[str, str]] = []
    # Forward form (Song - Artist) — the common convention
    first_idx = combined.find(" - ")
    left = combined[:first_idx].strip()
    right = combined[first_idx + 3 :].strip()
    if left and right:
        candidates.append((left, right))
        # Reverse form (Artist - Song) in case user wrote it backwards
        candidates.append((right, left))
    # Last-dash split differs from first-dash when multiple ' - ' present
    last_idx = combined.rfind(" - ")
    if last_idx != first_idx:
        left_l = combined[:last_idx].strip()
        right_l = combined[last_idx + 3 :].strip()
        if left_l and right_l and (left_l, right_l) not in candidates:
            candidates.append((left_l, right_l))
    return candidates


def _smart_as_add_track_to_playlist(
    playlist_name: str,
    track_name: str,
    artist: Optional[str],
    album: Optional[str],
) -> tuple[bool, str, Optional[tuple[str, str]]]:
    """AppleScript add-to-playlist with open-ended-input recovery.

    Tries the inputs as given first. If that fails and the caller supplied only
    a combined ``track_name`` (no artist, no album), splits on ' - ' and retries
    each candidate. Returns the matched (name, artist) pair on split success so
    callers can report what actually resolved.
    """
    ok, result = asc.add_track_to_playlist(playlist_name, track_name, artist, album)
    if ok:
        return True, result, None

    # Only try splits for genuinely open-ended inputs — don't second-guess the
    # caller when they gave us an explicit artist or album filter.
    if artist or album or " - " not in track_name:
        return False, result, None
    if "Track not found" not in result:
        return False, result, None

    for cand_name, cand_artist in _split_track_artist_candidates(track_name):
        ok2, result2 = asc.add_track_to_playlist(playlist_name, cand_name, cand_artist, None)
        if ok2:
            # Verify the split-resolved track actually landed. We've already
            # second-guessed the caller's input by splitting it; if the wrong
            # track silently got added (UI raciness, propagation lag), we'd
            # rather fail loudly than claim a false success on a guess.
            if _verify_track_in_playlist(playlist_name, cand_name, cand_artist):
                return True, result2, (cand_name, cand_artist)
            # Verify failed after a successful add. Don't try the next
            # candidate — that would risk a second wrong-track add if the
            # first one really did land but verify is just slow (iCloud
            # propagation lag). Better one suspect add than two.
            return (
                False,
                f"Added '{cand_name}' but could not verify in '{playlist_name}' "
                f"(may have landed; check manually)",
                None,
            )
    return False, result, None


def _verify_track_not_in_playlist(
    playlist_name: str,
    track_name: str,
    artist: str,
) -> bool:
    """Confirm a track is NOT in a playlist, with retries.

    Inverse of _verify_track_in_playlist — used after remove operations to
    catch the symmetric false-positive: AppleScript reports successful
    removal but the track is still there (server-side state didn't accept
    the local edit). Retries to absorb propagation lag in the success case
    where removal is genuine but the local query is stale.
    """
    if not APPLESCRIPT_AVAILABLE or not track_name:
        return False
    for i in range(_VERIFY_ATTEMPTS):
        if i > 0:
            time.sleep(_VERIFY_DELAY_S)
        ok, exists = asc.track_exists_in_playlist(playlist_name, track_name, artist or None)
        if ok and not exists:
            return True
    return False


_ROLLBACK_SETTLE_S = 2.0  # wait for Music.app's local reconciliation (~1s observed)


def _confirm_swap_track(
    track_name: str,
    artist: str = "",
    *,
    applescript_name: Optional[str] = None,
    api_id: Optional[str] = None,
) -> bool:
    """Strict, artist-aware confirmation that the SPECIFIC new track is in a playlist,
    used by swaps BEFORE the destructive remove.

    Unlike the general verify (substring `contains`, to tolerate punctuation), this
    requires a normalized-EXACT name match plus the artist when one is given — so a
    reverted add can't hide behind a similarly-named track already in the playlist
    (e.g. adding "One" while "One More Time" is present) and cost the old track. Reads
    native truth on macOS (AppleScript; the API lags local writes) and the API off-mac.
    Retries for read-after-write lag. Fails safe: any doubt → False → swap keeps the
    old track."""
    if not track_name:
        return False
    for i in range(_VERIFY_ATTEMPTS):
        if i > 0:
            time.sleep(_VERIFY_DELAY_S)
        rows = None
        if applescript_name and APPLESCRIPT_AVAILABLE:
            ok, res = asc.search_playlist(applescript_name, track_name)
            rows = res if ok and isinstance(res, list) else None
        elif api_id:
            ok, res = _get_playlist_track_names(api_id)
            rows = res if ok and isinstance(res, list) else None
        if rows:
            for r in rows:
                if _loose_equals(track_name, r.get("name", "")) and (
                    not artist or _loose_contains(artist, r.get("artist", ""))
                ):
                    return True
    return False


def _verify_track_in_playlist(
    playlist_name: str,
    track_name: str,
    artist: str,
) -> bool:
    """Confirm a track matching (name, artist) is STABLY in a playlist.

    Catches three failure modes:

    - AppleScript add-to-playlist reports success but local state hasn't
      propagated yet (re-query after a short sleep).
    - UI automation adds the wrong track silently (e.g. clicked a non-Song
      result with stale search state). If what ends up in the playlist
      doesn't match what the caller asked for, verification fails.
    - **Music.app server-side rollback** (the J&N class of bug): some
      user-created playlists (e.g. those with `canEdit:false` in the
      Apple Music REST API metadata) accept AppleScript `duplicate`
      LOCALLY for ~1 second — track visible in the playlist UI — then
      the change reverts. A naive "is the track there now?" check
      returns True during the transient window and misses the rollback
      entirely. To catch this, sleep ~2s first (covers the observed
      ~1s revert window with headroom), THEN check. Mechanism unknown
      (Apple's local de-dup / auto-curation / canEdit enforcement —
      not iCloud sync, the timing is too fast); the symptom is what
      we're protecting against.

    Uses ``track_exists_in_playlist``'s ``name contains`` + ``artist contains``
    match, which tolerates mild name/artist punctuation differences.
    """
    if not APPLESCRIPT_AVAILABLE or not track_name:
        return False
    # Settle delay first — covers the rollback window. Then a short retry
    # chain to absorb local-state propagation lag.
    time.sleep(_ROLLBACK_SETTLE_S)
    for i in range(_VERIFY_ATTEMPTS):
        if i > 0:
            time.sleep(_VERIFY_DELAY_S)
        ok, result = asc.track_exists_in_playlist(playlist_name, track_name, artist or None)
        if ok and result:
            return True
    return False


def _unified_auto_search_to_playlist(
    track_name: str,
    artist: str,
    playlist_name: str,
) -> tuple[bool, str, list[str]]:
    """Find-and-add an out-of-library track to a playlist.

    API path is preferred when a developer token exists (fast, accurate catalog
    search). Otherwise falls back to UI automation, which uses Music.app's
    search field + hover-click add buttons — no API credentials needed.

    Preprocesses open-ended "Song - Artist" input by splitting on ' - ' so both
    search paths get a clean query. Post-validates each path via
    ``_verify_track_in_playlist`` so a claimed success that didn't actually
    land the expected track is caught and reported (or retried via the next
    path).

    Same return signature as ``_auto_search_and_add_to_playlist`` for drop-in
    use at call sites.
    """
    steps: list[str] = []

    # Preprocess free-form input: split "Song - Artist" into (name, artist) so
    # both catalog searches run against a clean query. Only split when no
    # artist was supplied — respect explicit caller intent.
    search_name = track_name
    search_artist = artist
    if not artist and " - " in track_name:
        candidates = _split_track_artist_candidates(track_name)
        if candidates:
            search_name, search_artist = candidates[0]
            steps.append(f"Split '{track_name}' → name='{search_name}' artist='{search_artist}'")

    # Catalog add-to-playlist runs over the unified API (dev token generated OR
    # harvested, plus a captured media-user-token). The fragile UI automation
    # that broke across macOS/Music.app versions (#37) has been removed — there is
    # no UI fallback. If the API path isn't available, tell the user how to enable
    # it rather than degrading to something unreliable.
    if not _can_use_library_api():
        if _forced_tokenless():
            return (False, f"Catalog add disabled: {_FORCED_TOKENLESS_MSG}", steps)
        return (
            False,
            "Catalog add needs the API. Run `applemusic-mcp login` (browser sign-in, "
            "no Apple Developer account) or `applemusic-mcp login --dev`.",
            steps,
        )

    ok, result, api_steps = _auto_search_and_add_to_playlist(
        search_name, search_artist, playlist_name
    )
    steps.extend(api_steps)
    if ok and _verify_track_in_playlist(playlist_name, search_name, search_artist):
        return True, result, steps
    if ok:
        # API reported success but the track isn't visible in the playlist yet —
        # treat the API result as authoritative (cloud propagation can lag the
        # read), but surface the nuance.
        steps.append("API add succeeded; playlist read not yet reflecting it (propagation lag)")
        return True, result, steps
    return False, result, steps


def _auto_search_and_add_to_playlist(
    track_name: str,
    artist: str,
    playlist_name: str,
    playlist_id: str | None = None,
) -> tuple[bool, str, list[str]]:
    """Search catalog for track, add to library, add to playlist.

    Args:
        track_name: Track name to search for
        artist: Artist name (optional but helps matching)
        playlist_name: Playlist name for messaging
        playlist_id: Playlist ID for API add (optional, will look up if not provided)

    Returns:
        Tuple of (success, result_message, steps_log)
    """
    steps = []
    catalog_search = f"{track_name} {artist}" if artist else track_name

    try:
        headers = get_headers()

        # Search catalog
        response = requests.get(
            f"{BASE_URL}/catalog/{get_storefront()}/search",
            headers=headers,
            params={"term": catalog_search, "types": "songs", "limit": 3},
            timeout=REQUEST_TIMEOUT,
        )

        if response.status_code != 200:
            return False, f"Catalog search failed (status {response.status_code})", steps

        data = response.json()
        songs = data.get("results", {}).get("songs", {}).get("data", [])

        if not songs:
            return False, f"Not found in library or catalog", steps

        # Take the first match
        song = songs[0]
        catalog_id = song["id"]
        attrs = song.get("attributes", {})
        found_name = attrs.get("name", "")
        found_artist = attrs.get("artistName", "")

        steps.append(f"Found in catalog: {found_name} - {found_artist}")

        # Add to library via API
        add_response = requests.post(
            f"{BASE_URL}/me/library",
            headers=headers,
            params={"ids[songs]": catalog_id},
            timeout=REQUEST_TIMEOUT,
        )

        if add_response.status_code not in (200, 202):
            return False, f"Failed to add to library (status {add_response.status_code})", steps

        # Attach the now-in-library track to the playlist via the rail that can
        # ACTUALLY write that KIND of playlist (classified by origin):
        #   api    → sanctioned dev-token POST (works, no sync race).
        #   apple  → Apple-curated; NO rail can add — reject honestly.
        #   user   → the user's own Music.app playlist. The amp-api REST add 500s on
        #            these (verified), so on macOS (this path) attach via AppleScript;
        #            the track was just library-added to the cloud, so poll for it to
        #            sync into the local Music.app first.
        if playlist_id:
            pl = {"id": playlist_id, "canEdit": True}  # explicit id from caller
        else:
            pl = amp_api.resolve_playlist(playlist_name, api_created_only=False)
        if not pl:
            return (
                False,
                f"Could not find playlist '{playlist_name}' in your library "
                "(a just-created playlist can take a moment to sync before it's addable)",
                steps,
            )
        playlist_id = pl["id"]
        kind = amp_api.playlist_kind(pl)

        if kind == "apple":
            return (
                False,
                f"'{playlist_name}' is an Apple-curated playlist — you can't add your "
                "own tracks to it (it's Apple's content, not editable on any path).",
                steps,
            )

        if kind == "api":
            pl_add_response = requests.post(
                f"{BASE_URL}/me/library/playlists/{playlist_id}/tracks",
                headers=headers,
                json={"data": [{"id": catalog_id, "type": "songs"}]},
                timeout=REQUEST_TIMEOUT,
            )
            if pl_add_response.status_code in (200, 201, 204):
                return (
                    True,
                    f"{found_name} - {found_artist} "
                    "(added to library + playlist via the Apple Music API)",
                    steps,
                )
            return (
                False,
                f"Failed to add to playlist (status {pl_add_response.status_code})",
                steps,
            )

        # kind == "user": macOS-only (off-mac never reaches here). The catalog track
        # was just library-added in the cloud; wait for it to sync down to the LOCAL
        # Music.app, then attach via AppleScript.
        if not APPLESCRIPT_AVAILABLE:  # pragma: no cover  # off-mac uses _playlist_add_api
            return (
                False,
                f"'{playlist_name}' was created in Music.app; the web API can't modify "
                "it. Adding to it currently requires macOS.",
                steps,
            )
        # Don't nudge "Update Cloud Library" UP FRONT: measured A/B, nudging at t=0
        # didn't speed the cloud→local sync — a sample synced ~11s WITHOUT it vs ~90s
        # WITH (activating Music likely interrupts the in-flight sync), and it steals
        # focus + needs a menu item that's absent when Sync Library is off. So let the
        # natural (usually fast) sync run, polling the LOCAL library; only as a LAST
        # RESORT — if it still hasn't landed after _SYNC_NUDGE_AFTER_S — nudge once to
        # kick a stuck sync. Capped so we never hold the caller past ~30s.
        synced = False
        nudged = False
        start = time.monotonic()
        while time.monotonic() - start < _SYNC_POLL_BUDGET_S:
            if asc.find_library_track(found_name, found_artist or "")[0]:
                synced = True
                break
            if not nudged and time.monotonic() - start >= _SYNC_NUDGE_AFTER_S:
                nudged = True
                if asc.update_cloud_library()[0]:
                    steps.append(
                        "Sync was slow, so I triggered Music's Update Cloud Library as a "
                        "last resort (brief Music.app flash) to kick the iCloud sync"
                    )
            time.sleep(_SYNC_POLL_INTERVAL_S)
        if synced:
            # Synced locally — attach via AppleScript, then verify. CRITICAL: the
            # `duplicate` add must happen AT MOST ONCE. A successful add whose verify
            # lags (iCloud propagation) must NOT trigger a re-add — that's how the old
            # loop stacked up to 4 duplicate copies. So: retry the ADD only while it
            # keeps failing with "Track not found" (nothing landed yet); once an add
            # succeeds, stop adding and only re-poll the verify.
            added = False
            for _ in range(4):
                if not added:
                    ok2, res2, _split = _smart_as_add_track_to_playlist(
                        playlist_name, found_name, found_artist or None, None
                    )
                    if ok2:
                        added = True
                    elif "Track not found" not in res2:
                        return False, _attach_error(found_name, res2), steps
                    # else: not findable yet — safe to retry the add next loop
                if added and _verify_track_in_playlist(
                    playlist_name, found_name, found_artist or ""
                ):
                    steps.append("Attached via Music.app (native)")
                    return (
                        True,
                        f"{found_name} - {found_artist} "
                        "(added to library via the Apple Music API; "
                        "attached to playlist via Music.app)",
                        steps,
                    )
                time.sleep(_VERIFY_DELAY_S)
            if added:
                # The `duplicate` ran but native verify never confirmed it — treat as
                # NOT landed (no optimistic "likely landed"; that's exactly what misled
                # us when Music.app silently reverted the edit). This is the server-side
                # rollback state — the real fix is to relaunch Music.app.
                steps.append("Attach did not persist on native verify (likely a Music.app revert)")
                return (
                    False,
                    f"Added '{found_name}' to your library, but attaching it to "
                    f"'{playlist_name}' did not persist — Music.app silently reverted the "
                    "edit (an Apple bug; even a manual add fails in this state). Quit and "
                    "reopen Music.app, then re-run this add. (Re-adding without relaunching "
                    "won't stick.)",
                    steps,
                )
        # Not synced within the budget (Apple's iCloud sync is variable — usually
        # seconds, occasionally a minute+). The library-add succeeded; tell the model
        # exactly that and the one-step fix so the experience stays smooth.
        return (
            False,
            f"Added '{found_name}' to your library — but it hasn't finished syncing to "
            f"this Mac yet, so it's not in '{playlist_name}' yet. This is Apple's iCloud "
            f"sync (usually seconds, sometimes up to a minute). The track is safely in "
            f"your library; re-run this exact add in a moment and it'll attach instantly.",
            steps,
        )

    except Exception as e:
        return False, f"Error: {str(e)}", steps


def _rate_song_api(song_id: str, rating: str) -> tuple[bool, str]:
    """Rate a song via API.

    Args:
        song_id: Catalog song ID
        rating: 'love' or 'dislike'

    Returns:
        Tuple of (success, message)
    """
    rating_value = {"love": 1, "dislike": -1}.get(rating.lower())
    if rating_value is None:
        return False, "rating must be 'love' or 'dislike'"

    try:
        headers = get_headers()
        body = {"type": "rating", "attributes": {"value": rating_value}}
        response = requests.put(
            f"{BASE_URL}/me/ratings/songs/{song_id}",
            headers=headers,
            json=body,
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code in (200, 201, 204):
            return True, f"Marked as {rating}"
        if response.status_code in (401, 403):
            return False, (
                f"Not authorized (status {response.status_code}) — your session may have "
                "expired. Re-run `applemusic-mcp login` or `applemusic-mcp login --dev`."
            )
        return False, f"API returned status {response.status_code}"
    except Exception as e:
        return False, str(e)


# ============ PLAYLIST MANAGEMENT ============


def _playlist_list(
    format: str = "text",
    export: str = "none",
    full: bool = False,
    filter: str = "",
) -> str:
    """Internal: Get all playlists, optionally narrowed by name (loose match)."""
    playlist_data = []

    # Try AppleScript first (local, instant, no auth required)
    if APPLESCRIPT_AVAILABLE:
        success, as_playlists = asc.get_playlists()
        if success:
            if not as_playlists:
                return "No playlists in library"
            for p in as_playlists:
                playlist_data.append(
                    {
                        "id": p.get("id", ""),
                        "name": p.get("name", "Unknown"),
                        "track_count": p.get("track_count", 0),
                        "smart": p.get("smart", False),
                        "can_edit": True,  # AS can edit any playlist
                    }
                )
            if filter:
                playlist_data = [p for p in playlist_data if _loose_contains(filter, p["name"])]
                if not playlist_data:
                    return f"No playlists matching '{filter}'"
            return format_output(playlist_data, format, export, full, "playlists")
        # AppleScript failed on macOS — surface the actionable error
        # instead of cascading to API and leaking "Developer token not
        # found" when the real cause is Music.app not running or
        # Automation permissions denied.
        as_error = str(as_playlists) if as_playlists else "AppleScript get_playlists failed"
        return f"Error listing playlists: {_format_applescript_error(as_error, 'list playlists')}"

    # Fall back to API (non-macOS only)
    try:
        headers = get_headers()
        all_playlists = []
        offset = 0

        # Paginate to get all playlists
        while True:
            response = requests.get(
                f"{BASE_URL}/me/library/playlists",
                headers=headers,
                params={"limit": 100, "offset": offset},
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code == 404:
                break
            response.raise_for_status()
            playlists = response.json().get("data", [])
            if not playlists:
                break
            all_playlists.extend(playlists)
            if len(playlists) < 100:
                break
            offset += 100

        # Extract playlist data
        for playlist in all_playlists:
            attrs = playlist.get("attributes", {})
            desc = attrs.get("description", {})

            playlist_data.append(
                {
                    "id": playlist.get("id", ""),
                    "name": attrs.get("name", "Unknown"),
                    "can_edit": attrs.get("canEdit", False),
                    "is_public": attrs.get("isPublic", False),
                    "date_added": attrs.get("dateAdded", ""),
                    "last_modified": attrs.get("lastModifiedDate", ""),
                    "description": (
                        desc.get("standard", "") if isinstance(desc, dict) else str(desc)
                    ),
                    "has_catalog": attrs.get("hasCatalog", False),
                }
            )

        if filter:
            playlist_data = [p for p in playlist_data if _loose_contains(filter, p["name"])]
            if not playlist_data:
                return f"No playlists matching '{filter}'"

        # Add token warning if text format
        warning = get_token_expiration_warning()
        prefix = f"{warning}\n\n" if warning and format == "text" else ""

        return prefix + format_output(playlist_data, format, export, full, "playlists")

    except requests.exceptions.RequestException as e:
        return _api_error(e)
    except (FileNotFoundError, ValueError) as e:
        return str(e)


def _playlist_tracks(
    playlist: str = "",
    filter: str = "",
    limit: int = 0,
    offset: int = 0,
    format: str = "text",
    export: str = "none",
    full: bool = False,
    fetch_explicit: Optional[bool] = None,
) -> str:
    """Internal: Get playlist tracks."""
    start_time = time.time()
    query_stats = {"cache_hits": 0, "cache_misses": 0, "api_calls": 0}

    # Resolve playlist parameter
    resolved: ResolvedPlaylist = _resolve_playlist(playlist)
    if resolved.error:
        return resolved.error

    # Apply user preferences
    if fetch_explicit is None:
        prefs = get_user_preferences()
        fetch_explicit = prefs["fetch_explicit"]

    use_api = bool(resolved.api_id)
    use_applescript = bool(resolved.applescript_name)

    # Use AppleScript with name (only if we don't have API ID)
    if use_applescript and not use_api:
        if not APPLESCRIPT_AVAILABLE:
            return "Error: AppleScript (playlist_name) requires macOS"
        success, result = asc.get_playlist_tracks(resolved.applescript_name)
        if not success:
            return f"Error: {_format_applescript_error(str(result), 'list playlist tracks')}"
        if not result:
            return "Playlist is empty"

        # Format AppleScript results
        track_data = []
        for t in result:
            track_data.append(
                {
                    "name": t.get("name", "Unknown"),
                    "artist": t.get("artist", "Unknown"),
                    "album": t.get("album", ""),
                    "duration": t.get("duration", "0:00"),
                    "genre": t.get("genre", ""),
                    "year": t.get("year", ""),
                    "explicit": "Unknown",  # Will be enriched below if fetch_explicit=True
                    "id": t.get("id", ""),
                }
            )

        # Enrich with explicit status via API if requested
        # Uses TrackCache for ID-based caching (persistent, library, catalog IDs)
        if fetch_explicit and track_data:
            try:
                cache = get_track_cache()

                # First pass: fill in what we know from cache (ID-based lookup only)
                unknown_tracks = []
                for track in track_data:
                    track_id = track.get("id", "")
                    if track_id:
                        cached_explicit = cache.get_explicit(track_id)
                        if cached_explicit:
                            track["explicit"] = cached_explicit
                            query_stats["cache_hits"] += 1
                            continue
                    query_stats["cache_misses"] += 1
                    unknown_tracks.append(track)

                # If we have unknown tracks, fetch from API
                if unknown_tracks:
                    headers = get_headers()

                    # Find the playlist in the API library by matching name
                    query_stats["api_calls"] += 1
                    response = requests.get(
                        f"{BASE_URL}/me/library/playlists",
                        headers=headers,
                        params={"limit": 100},
                        timeout=REQUEST_TIMEOUT,
                    )

                    if response.status_code == 200:
                        playlists = response.json().get("data", [])
                        api_playlist_id = None

                        # Find matching playlist by name
                        for pl in playlists:
                            pl_name = pl.get("attributes", {}).get("name", "")
                            if _loose_equals(pl_name, resolved.applescript_name) or _loose_contains(
                                resolved.applescript_name, pl_name
                            ):
                                api_playlist_id = pl.get("id")
                                break

                        # If found, fetch all tracks from API with explicit info
                        if api_playlist_id:
                            all_api_tracks = []
                            api_offset = 0

                            while True:
                                query_stats["api_calls"] += 1
                                track_response = requests.get(
                                    f"{BASE_URL}/me/library/playlists/{api_playlist_id}/tracks",
                                    headers=headers,
                                    params={"limit": 100, "offset": api_offset},
                                    timeout=REQUEST_TIMEOUT,
                                )
                                if track_response.status_code != 200:
                                    break

                                tracks = track_response.json().get("data", [])
                                if not tracks:
                                    break

                                all_api_tracks.extend(tracks)
                                if len(tracks) < 100:
                                    break
                                api_offset += 100

                            # Build temporary maps for matching (normalized keys -> API data)
                            # Multiple keys for fallback matching: name+artist+album, name+artist, name
                            api_track_map_full = {}  # name+artist+album
                            api_track_map_partial = {}  # name+artist (for fallback)
                            api_track_map_name = {}  # name only (for last resort, only if unique)
                            api_track_name_counts = {}  # count occurrences of each name

                            for api_track in all_api_tracks:
                                attrs = api_track.get("attributes", {})
                                play_params = attrs.get("playParams", {})
                                library_id = api_track.get("id", "")
                                catalog_id = play_params.get("catalogId", "")
                                isrc = attrs.get("isrc", "")
                                track_name = _normalize_for_match(attrs.get("name", ""))
                                track_artist = _normalize_for_match(attrs.get("artistName", ""))
                                track_album = _normalize_for_match(attrs.get("albumName", ""))
                                explicit = (
                                    "Yes" if attrs.get("contentRating") == "explicit" else "No"
                                )

                                api_data = {
                                    "library_id": library_id,
                                    "catalog_id": catalog_id,
                                    "isrc": isrc,
                                    "explicit": explicit,
                                }

                                # Full match key (name+artist+album)
                                full_key = f"{track_name}|||{track_artist}|||{track_album}"
                                api_track_map_full[full_key] = api_data

                                # Partial match key (name+artist) for fallback
                                partial_key = f"{track_name}|||{track_artist}"
                                if partial_key not in api_track_map_partial:
                                    api_track_map_partial[partial_key] = api_data

                                # Name-only map (only use if name is unique)
                                api_track_name_counts[track_name] = (
                                    api_track_name_counts.get(track_name, 0) + 1
                                )
                                api_track_map_name[track_name] = api_data

                            # Match AppleScript tracks to API tracks and cache
                            for track in track_data:
                                if track["explicit"] != "Unknown":
                                    continue

                                persistent_id = track.get("id", "")
                                norm_name = _normalize_for_match(track["name"])
                                norm_artist = _normalize_for_match(track["artist"])
                                norm_album = _normalize_for_match(track["album"])

                                # Try full match first
                                full_key = f"{norm_name}|||{norm_artist}|||{norm_album}"
                                api_data = api_track_map_full.get(full_key)

                                # Fallback to partial match (name+artist)
                                if not api_data:
                                    partial_key = f"{norm_name}|||{norm_artist}"
                                    api_data = api_track_map_partial.get(partial_key)

                                # Last resort: name only (if unique in playlist)
                                if not api_data and api_track_name_counts.get(norm_name, 0) == 1:
                                    api_data = api_track_map_name.get(norm_name)

                                if api_data:
                                    track["explicit"] = api_data["explicit"]

                                    # Cache by all IDs for this track
                                    cache.set_track_metadata(
                                        explicit=api_data["explicit"],
                                        persistent_id=persistent_id,
                                        library_id=api_data["library_id"],
                                        catalog_id=api_data["catalog_id"],
                                        isrc=api_data["isrc"] or None,
                                        name=track["name"],
                                        artist=track["artist"],
                                        album=track.get("album", ""),
                                    )
                                else:
                                    # Cache unmatched track as Unknown to avoid re-fetching
                                    if persistent_id:
                                        cache.set_track_metadata(
                                            explicit="Unknown",
                                            persistent_id=persistent_id,
                                        )

            except Exception:
                pass  # API not available - explicit stays "Unknown"

        # Apply filter
        if filter:
            track_data = [
                t
                for t in track_data
                if _loose_contains(filter, t["name"]) or _loose_contains(filter, t["artist"])
            ]

        # Apply pagination
        track_data, total_count, error = _apply_pagination(track_data, limit, offset)
        if error:
            return error

        safe_name = "".join(c if c.isalnum() else "_" for c in resolved.applescript_name)
        result = format_output(
            track_data,
            format,
            export,
            full,
            f"playlist_{safe_name}",
            total_count=total_count,
            offset=offset,
        )

        # Add timing and stats
        elapsed = time.time() - start_time
        stats_line = f"\n\n⏱️ {elapsed:.2f}s | Cache: {query_stats['cache_hits']} hits, {query_stats['cache_misses']} misses | API calls: {query_stats['api_calls']}"

        # Log to audit
        if fetch_explicit:
            audit_log.log_action(
                "playlist_query",
                {
                    "playlist": resolved.applescript_name,
                    "track_count": total_count,
                    "duration_sec": round(elapsed, 2),
                    "cache_hits": query_stats["cache_hits"],
                    "cache_misses": query_stats["cache_misses"],
                    "api_calls": query_stats["api_calls"],
                },
            )

        fuzzy_info = _format_fuzzy_match(resolved.fuzzy_match)
        return result + fuzzy_info + stats_line

    # Use API with ID
    try:
        headers = get_headers()
        all_tracks = []

        # Optimization: if no filter and limit specified, only fetch what we need
        # Use playlist_track_count for total if available
        can_optimize = not filter and limit > 0
        if can_optimize:
            # Fetch only offset+limit tracks. Capture meta.total from the
            # first response so the header can show "1-200 of 436" rather
            # than masking truncation when limit is set. The library-playlists
            # endpoint omits trackCount, but the /tracks endpoint returns
            # meta.total on every page — no extra API call needed.
            true_total = None
            needed = offset + limit
            api_offset = 0
            while len(all_tracks) < needed:
                batch_limit = min(100, needed - len(all_tracks))
                query_stats["api_calls"] += 1
                response = requests.get(
                    f"{BASE_URL}/me/library/playlists/{resolved.api_id}/tracks",
                    headers=headers,
                    params={"limit": batch_limit, "offset": api_offset},
                    timeout=REQUEST_TIMEOUT,
                )
                if response.status_code == 404:
                    break
                response.raise_for_status()
                payload = response.json()
                if true_total is None:
                    meta_total = payload.get("meta", {}).get("total")
                    if isinstance(meta_total, int) and meta_total >= 0:
                        true_total = meta_total
                tracks = payload.get("data", [])
                if not tracks:
                    break
                all_tracks.extend(tracks)
                if len(tracks) < batch_limit:
                    break
                api_offset += batch_limit

            if not all_tracks:
                return "Playlist is empty"

            track_data = [extract_track_data(t, full) for t in all_tracks]

            # Apply pagination locally (skip offset, take limit)
            if offset > 0:
                track_data = track_data[offset:]
            track_data = track_data[:limit]

            total_count = true_total if true_total is not None else len(all_tracks)

            safe_id = resolved.api_id.replace(".", "_")
            result = format_output(
                track_data,
                format,
                export,
                full,
                f"playlist_{safe_id}",
                total_count=total_count,
                offset=offset,
            )

            # Add stats line
            elapsed = time.time() - start_time
            stats_line = f"\n\n⏱️ {elapsed:.2f}s | API calls: {query_stats['api_calls']}"
            return result + stats_line

        # Full fetch path (filter specified or no limit)
        api_offset = 0
        while True:
            query_stats["api_calls"] += 1
            response = requests.get(
                f"{BASE_URL}/me/library/playlists/{resolved.api_id}/tracks",
                headers=headers,
                params={"limit": 100, "offset": api_offset},
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code == 404:
                break
            response.raise_for_status()
            tracks = response.json().get("data", [])
            if not tracks:
                break
            all_tracks.extend(tracks)
            if len(tracks) < 100:
                break
            api_offset += 100

        if not all_tracks:
            return "Playlist is empty"

        track_data = [extract_track_data(t, full) for t in all_tracks]

        # Apply filter
        if filter:
            track_data = [
                t
                for t in track_data
                if _loose_contains(filter, t["name"]) or _loose_contains(filter, t["artist"])
            ]

        # Apply pagination
        track_data, total_count, error = _apply_pagination(track_data, limit, offset)
        if error:
            return error

        safe_id = resolved.api_id.replace(".", "_")
        result = format_output(
            track_data,
            format,
            export,
            full,
            f"playlist_{safe_id}",
            total_count=total_count,
            offset=offset,
        )

        # Add stats line
        elapsed = time.time() - start_time
        stats_line = f"\n\n⏱️ {elapsed:.2f}s | API calls: {query_stats['api_calls']}"
        fuzzy_info = _format_fuzzy_match(resolved.fuzzy_match)
        return result + fuzzy_info + stats_line

    except requests.exceptions.RequestException as e:
        return _api_error(e)
    except (FileNotFoundError, ValueError) as e:
        return str(e)


def _playlist_search(
    query: str,
    playlist: str = "",
) -> str:
    """Internal: Search playlist tracks."""
    # Resolve playlist parameter
    resolved: ResolvedPlaylist = _resolve_playlist(playlist)
    if resolved.error:
        return resolved.error

    use_api = bool(resolved.api_id)
    use_applescript = bool(resolved.applescript_name)

    matches = []

    if use_applescript and not use_api and not APPLESCRIPT_AVAILABLE:
        return "Error: playlist_name requires macOS"

    # Prefer native AppleScript on macOS — it's the GROUND TRUTH. The API/cache path
    # lags native writes by minutes (showed just-removed tracks as present and new
    # ones as absent right after edits), so on a Mac read Music.app directly.
    if use_applescript and APPLESCRIPT_AVAILABLE:
        # Use native AppleScript search (fast, same as Music app search field)
        success, result = asc.search_playlist(resolved.applescript_name, query)
        if not success:
            return f"Error: {_format_applescript_error(str(result), 'search playlist')}"
        for t in result:
            track_id = t.get("id", "")
            matches.append({"name": t["name"], "artist": t["artist"], "id": track_id})
    elif use_api:
        # API path: manually filter tracks (cross-platform; off-mac, or no native).
        success, tracks = _get_playlist_track_names(resolved.api_id)
        if not success:
            return f"Error: {tracks}"
        for t in tracks:
            name = t.get("name", "")
            artist = t.get("artist", "")
            album = t.get("album", "")
            track_id = t.get("id", "")
            if (
                _loose_contains(query, name)
                or _loose_contains(query, artist)
                or _loose_contains(query, album)
            ):
                matches.append({"name": name, "artist": artist, "id": track_id})

    fuzzy_info = _format_fuzzy_match(resolved.fuzzy_match)

    if not matches:
        return f"No matches for '{query}'{fuzzy_info}"

    def format_match(m: dict) -> str:
        return f"{m['name']} by {m['artist']} {m['id']}"

    if len(matches) == 1:
        return f"Found: {format_match(matches[0])}{fuzzy_info}"

    output = f"Found {len(matches)} matches:\n"
    output += "\n".join(f"  - {format_match(m)}" for m in matches[:10])
    if len(matches) > 10:
        output += f"\n  ...and {len(matches) - 10} more"
    return output + fuzzy_info


def _is_catalog_id(track_id: str) -> bool:
    """Check if an ID is a catalog ID (numeric) vs library ID (prefixed or hex).

    Catalog IDs are purely numeric (e.g., "1440783617").
    Library IDs are either prefixed (i.XXX, l.XXX, p.XXX) or hexadecimal strings.

    Uses _detect_id_type() internally for consistent ID classification.
    """
    return _detect_id_type(track_id) == "catalog"


def _get_playlist_track_names(playlist_id: str) -> tuple[bool, list[dict] | str]:
    """Get track names from a playlist for duplicate checking."""
    try:
        headers = get_headers()
        all_tracks = []
        offset = 0

        while True:
            response = requests.get(
                f"{BASE_URL}/me/library/playlists/{playlist_id}/tracks",
                headers=headers,
                params={"limit": 100, "offset": offset},
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code == 404:
                break
            response.raise_for_status()
            tracks = response.json().get("data", [])
            if not tracks:
                break
            all_tracks.extend(tracks)
            if len(tracks) < 100:
                break
            offset += 100

        return True, [
            {
                "id": t.get("id", ""),
                "name": t.get("attributes", {}).get("name", ""),
                "artist": t.get("attributes", {}).get("artistName", ""),
            }
            for t in all_tracks
        ]
    except Exception as e:
        return False, str(e)


def _find_track_in_list(tracks: list[dict], track_name: str, artist: str = "") -> list[str]:
    """Find matching tracks in a list by name/artist."""
    matches = []

    for t in tracks:
        if _loose_contains(track_name, t["name"]):
            if artist:
                if _loose_contains(artist, t["artist"]):
                    matches.append(f"{t['name']} - {t['artist']}")
            else:
                matches.append(f"{t['name']} - {t['artist']}")

    return matches


def _playlist_create(name: str, description: str = "") -> str:
    """Internal: Create playlist."""
    # Try AppleScript first (local, instant, no auth required)
    if APPLESCRIPT_AVAILABLE:
        success, result = asc.create_playlist(name, description)
        if success:
            audit_log.log_action(
                "create_playlist",
                {"name": name, "playlist_id": result, "method": "applescript"},
                undo_info={"playlist_name": name, "playlist_id": result},
            )
            return f"Created playlist '{name}' (ID: {result})"
        # AppleScript failed on macOS. Don't cascade to the API path —
        # that leaks "Developer token not found" when the real cause is
        # Music.app not running or Automation permissions denied. Surface
        # the actionable AS error directly. Falling back to API would only
        # help if the user has a token configured AND wants to bypass AS;
        # they can opt in by running on a non-darwin host.
        return f"Error creating playlist: {_format_applescript_error(result, 'create playlist')}"

    # Fall back to API (non-macOS only)
    try:
        headers = get_headers()

        body = {"attributes": {"name": name, "description": description}}

        response = requests.post(
            f"{BASE_URL}/me/library/playlists",
            headers=headers,
            json=body,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()

        playlist_id = data.get("data", [{}])[0].get("id")
        audit_log.log_action(
            "create_playlist",
            {"name": name, "playlist_id": playlist_id, "method": "api"},
            undo_info={"playlist_name": name, "playlist_id": playlist_id},
        )
        return f"Created playlist '{name}' (ID: {playlist_id})"

    except requests.exceptions.RequestException as e:
        return _api_error(e)
    except (FileNotFoundError, ValueError) as e:
        return str(e)


def _macos_only(action_name: str) -> Optional[str]:
    """Return the standard "requires macOS" error if AppleScript isn't available.

    Used by playlist() / library() dispatcher branches as a one-line gate:
    ``if (err := _macos_only("remove")): return err``.
    """
    if not APPLESCRIPT_AVAILABLE:
        return f"Error: {action_name} action requires macOS"
    return None


def _playlist_create_folder(path: str) -> str:
    """Internal: Create a folder or folder path. Supports slash-separated paths.

    e.g. "Summer/Chill/Deep" creates all three levels, nesting each.
    """
    if APPLESCRIPT_AVAILABLE:
        if "/" in path:
            # Multi-level path
            success, result = asc.create_folder_path(path)
        else:
            success, result = asc.create_folder(path)
        if success:
            audit_log.log_action(
                "create_folder",
                {"path": path, "folder_id": result, "method": "applescript"},
            )
            return f"Created folder path '{path}' (ID: {result})"
        return f"Error creating folder: {result}"

    # API fallback: POST /v1/me/library/playlist-folders (single level only)
    if "/" in path:
        return "Error: Nested folder paths require macOS. API only supports single-level folders."
    try:
        headers = get_headers()
        response = requests.post(
            f"{BASE_URL}/me/library/playlist-folders",
            headers={**headers, "Content-Type": "application/json"},
            json={"attributes": {"name": path}},
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code in (200, 201):
            data = response.json().get("data", [{}])
            folder_id = data[0].get("id", "") if data else ""
            audit_log.log_action(
                "create_folder",
                {"path": path, "folder_id": folder_id, "method": "api"},
            )
            return f"Created folder '{path}' (ID: {folder_id})"
        return f"Error creating folder via API: HTTP {response.status_code}"
    except FileNotFoundError:
        return "Error: API credentials required for folder creation on non-macOS"
    except requests.exceptions.RequestException as e:
        return _api_error(e)


def _playlist_tree() -> str:
    """Internal: Show folder hierarchy."""
    if not APPLESCRIPT_AVAILABLE:
        return "Error: tree view requires macOS"
    success, result = asc.get_folder_tree()
    if success:
        return result if result.strip() else "No folders found."
    return f"Error: {result}"


def _playlist_path(name: str) -> str:
    """Internal: Get the full folder path of a playlist or folder."""
    if not APPLESCRIPT_AVAILABLE:
        return "Error: path lookup requires macOS"
    if not name:
        return "Error: playlist or folder name required"
    success, result = asc.get_playlist_path(name)
    if success:
        return result
    return f"Error: {result}"


def _playlist_move(playlist_name: str, folder_name: str) -> str:
    """Internal: Move a playlist into a folder via AppleScript."""
    success, result = asc.move_to_folder(playlist_name, folder_name)
    if success:
        audit_log.log_action(
            "move_to_folder",
            {"playlist": playlist_name, "folder": folder_name, "method": "applescript"},
        )
        return result
    return f"Error moving playlist: {result}"


def _playlist_move_to_root(playlist_name: str) -> str:
    """Internal: Move a playlist out of its folder to the top level."""
    success, result = asc.move_to_root(playlist_name)
    if success:
        audit_log.log_action(
            "move_to_root",
            {"playlist": playlist_name, "method": "applescript"},
        )
        return result
    return f"Error: {result}"


def _playlist_create_in_folder(name: str, folder: str, description: str = "") -> str:
    """Internal: Create a playlist inside a folder. Creates the folder if it doesn't exist."""
    # Off-mac there's no AppleScript — route through the web API (folder create +
    # playlist create + move). The native branch below would silently fail every
    # osascript call and then falsely claim the playlist was removed.
    if not APPLESCRIPT_AVAILABLE:
        _folder_create_api(folder)  # ok if it already exists
        create_result = _playlist_create_api(name, description)
        if "Error" in create_result:
            return create_result
        move_result = _playlist_move_api(name, folder)
        if "Error" in move_result:
            return (
                f"Created playlist '{name}', but couldn't move it into '{folder}': "
                f"{move_result}. It's in your library at the top level (NOT removed) — "
                f"move it manually or retry."
            )
        return f"Created playlist '{name}' in folder '{folder}'"

    # Ensure folder exists (ignore errors — folder may already exist)
    if "/" in folder:
        asc.create_folder_path(folder)
    else:
        asc.create_folder(folder)

    # Create the playlist
    create_result = _playlist_create(name, description)
    if "Error" in create_result:
        return create_result

    # Move it into the folder
    move_result = _playlist_move(name, folder)
    if "Error" in move_result:
        # Rollback: delete the orphaned playlist
        asc.delete_playlist(name)
        return f"Error: Created playlist '{name}' but failed to move to folder '{folder}': {move_result}. Playlist was removed."

    return f"Created playlist '{name}' in folder '{folder}'"


def _playlist_delete_folder(folder_name: str) -> str:
    """Internal: Delete a folder via AppleScript."""
    if not folder_name:
        return "Error: folder name required"
    success, result = asc.delete_folder(folder_name)
    if success:
        audit_log.log_action(
            "delete_folder",
            {"name": folder_name, "method": "applescript"},
        )
        return result
    return f"Error: {result}"


def _playlist_rename_folder(folder_name: str, new_name: str) -> str:
    """Internal: Rename a folder via AppleScript."""
    if not folder_name:
        return "Error: folder name required"
    if not new_name:
        return "Error: new_name required"
    # Use the generic rename which now falls back to folder playlists
    success, result = asc.rename_playlist(folder_name, new_name)
    if success:
        audit_log.log_action(
            "rename_folder",
            {"old_name": folder_name, "new_name": new_name, "method": "applescript"},
        )
        return result
    return f"Error: {result}"


def _playlist_add(
    playlist: str = "",
    track: str = "",
    album: str = "",
    artist: str = "",
    allow_duplicates: bool = False,
    verify: bool = True,
    auto_add: Optional[bool] = None,
) -> str:
    """Internal: Add to playlist."""
    steps = []  # Track what we did for verbose output

    if not playlist.strip():
        return "Error: playlist parameter required"

    if not track and not album:
        return "Error: Provide track or album parameter"

    # Convert resolved inputs to internal format
    ids_list = []
    names_list = []

    # Resolve track input - handles ID, name, CSV, JSON
    if track:
        resolved_tracks = _resolve_track(track, artist)
        for r in resolved_tracks:
            if r.error:
                return f"Error parsing track input: {r.error}"
            if r.input_type in (
                InputType.CATALOG_ID,
                InputType.LIBRARY_ID,
                InputType.PERSISTENT_ID,
            ):
                ids_list.append(r.value)
            elif r.input_type in (InputType.NAME, InputType.JSON_OBJECT):
                names_list.append({"name": r.value, "artist": r.artist})

    # Resolve playlist with fuzzy matching
    # Always resolve first to get fuzzy-matched name, then decide API vs AppleScript mode
    playlist_str = playlist.strip()
    resolved: ResolvedPlaylist
    if playlist_str.startswith("p.") and len(playlist_str) > 2 and playlist_str[2:].isalnum():
        # Explicit playlist ID - use API mode only (no fuzzy matching needed)
        resolved = ResolvedPlaylist(
            raw_input=playlist_str,
            api_id=playlist_str,
            applescript_name=None,  # Not available for ID-only input
        )
    else:
        # Resolve playlist with fuzzy matching
        resolved = _resolve_playlist(playlist_str)
        if resolved.error:
            return (
                resolved.error
            )  # pragma: no cover  # unreachable: empty playlist is rejected by the guard above, so .error is always None here

        # If we have tracks (names or IDs) and AppleScript is available, prefer AppleScript mode
        # But use the fuzzy-matched applescript_name, not the raw input!
        if APPLESCRIPT_AVAILABLE and (names_list or ids_list) and resolved.applescript_name:
            # Clear api_id to force AppleScript mode (searches library directly)
            resolved = ResolvedPlaylist(
                raw_input=resolved.raw_input,
                api_id=None,
                applescript_name=resolved.applescript_name,
                fuzzy_match=resolved.fuzzy_match,
            )

    # Resolve album input - get all tracks from album(s)
    # When track is also provided, album acts as disambiguation filter (not "add whole album")
    if album and not track:
        # Album resolution requires the catalog API — there's no AppleScript
        # equivalent for fetching an album's tracklist by name. Without a
        # token we'd leak "Developer token not found" (same class as the
        # ID-guard below). Tell the user clearly what's needed.
        if not _has_developer_token():
            return (
                "Error: Adding by album requires an API token (the album's "
                "tracklist is fetched from the catalog). To add tracks "
                "without a token, pass them by name. To configure an API "
                "token, run: applemusic-mcp login --dev"
            )
        resolved_albums = _resolve_album(album, artist)
        for r in resolved_albums:
            if r.error:
                steps.append(f"Album error: {r.error}")
                continue

            album_tracks = []
            try:
                headers = get_headers()
                if r.input_type == InputType.CATALOG_ID:
                    # Direct album ID - fetch tracks
                    response = requests.get(
                        f"{BASE_URL}/catalog/{get_storefront()}/albums/{r.value}/tracks",
                        headers=headers,
                        params={"limit": 100},
                        timeout=REQUEST_TIMEOUT,
                    )
                    if response.status_code == 200:
                        album_tracks = response.json().get("data", [])
                        steps.append(f"Album {r.value}: found {len(album_tracks)} tracks")
                    else:
                        steps.append(f"Album {r.value}: API error {response.status_code}")
                elif r.input_type in (InputType.NAME, InputType.JSON_OBJECT):
                    # Search for album by name
                    query = f"{r.value} {r.artist}" if r.artist else r.value
                    response = requests.get(
                        f"{BASE_URL}/catalog/{get_storefront()}/search",
                        headers=headers,
                        params={"term": query, "types": "albums", "limit": 5},
                        timeout=REQUEST_TIMEOUT,
                    )
                    if response.status_code == 200:
                        albums = (
                            response.json().get("results", {}).get("albums", {}).get("data", [])
                        )
                        # Find best match
                        found_album = None
                        for alb in albums:
                            attrs = alb.get("attributes", {})
                            if _loose_contains(r.value, attrs.get("name", "")):
                                if r.artist:
                                    if _loose_contains(r.artist, attrs.get("artistName", "")):
                                        found_album = alb
                                        break
                                else:
                                    found_album = alb
                                    break
                        if not found_album and albums:
                            found_album = albums[0]

                        if found_album:
                            album_id = found_album.get("id")
                            album_name = found_album.get("attributes", {}).get("name", r.value)
                            # Fetch tracks
                            track_response = requests.get(
                                f"{BASE_URL}/catalog/{get_storefront()}/albums/{album_id}/tracks",
                                headers=headers,
                                params={"limit": 100},
                                timeout=REQUEST_TIMEOUT,
                            )
                            if track_response.status_code == 200:
                                album_tracks = track_response.json().get("data", [])
                                steps.append(
                                    f"Album '{album_name}': found {len(album_tracks)} tracks"
                                )
                        else:
                            steps.append(f"Album '{r.value}': not found in catalog")
                    else:
                        steps.append(f"Album '{r.value}': API error {response.status_code}")

                # Add album tracks to ids_list
                for t in album_tracks:
                    catalog_id = t.get("id")
                    if catalog_id:
                        ids_list.append(catalog_id)

            except Exception as e:
                steps.append(f"Album '{r.value}': {e}")
    elif album and track:
        # Album is used as disambiguation filter — pass album name through to names_list
        for item in names_list:
            item["album"] = album

    # === AppleScript mode (playlist by name, only if no API ID) ===
    if resolved.applescript_name and not resolved.api_id:
        if not APPLESCRIPT_AVAILABLE:
            return (
                "Error: couldn't find that playlist over the Apple Music API "
                "(on Windows/Linux playlists are resolved via the API, not Music.app). "
                "Check the exact name with playlist(action='list', filter='...'), or pass "
                "its id (p.XXX). Note: adding a whole ALBUM to a playlist isn't supported "
                "off macOS — add by track instead."
            )

        # Apply auto_add preference once
        if auto_add is None:
            prefs = get_user_preferences()
            auto_add = prefs["auto_add"]

        added = []
        errors = []

        # Process names first (from track names or JSON objects)
        for track_obj in names_list:
            name = track_obj["name"]
            track_artist = track_obj["artist"]
            track_album = track_obj.get("album")

            # Check for duplicates
            if not allow_duplicates:
                success, exists = asc.track_exists_in_playlist(
                    resolved.applescript_name, name, track_artist or None
                )
                if success and exists:
                    steps.append(f"Skipped duplicate: {name}")
                    continue

            # Add track — smart wrapper retries split forms for open-ended input
            success, result, split_match = _smart_as_add_track_to_playlist(
                resolved.applescript_name, name, track_artist or None, track_album or None
            )
            # Verify the add actually persisted. AppleScript `duplicate` can
            # return success while iCloud Library silently rolls back the
            # local edit on user-created (non-API) playlists. Without this
            # check the user gets a false-positive "Added 1 track(s)" while
            # the playlist is unchanged. _smart_as_add_track_to_playlist's
            # split-recovery path already verifies; the primary attempt does
            # not (deliberate latency choice — see test_first_attempt_success_does_not_verify).
            if success and split_match is None and verify:
                v_name, v_artist = name, track_artist or ""
                if not _verify_track_in_playlist(resolved.applescript_name, v_name, v_artist):
                    time.sleep(_VERIFY_DELAY_S)
                    success, result, split_match = _smart_as_add_track_to_playlist(
                        resolved.applescript_name,
                        name,
                        track_artist or None,
                        track_album or None,
                    )
                    if success and not _verify_track_in_playlist(
                        resolved.applescript_name, v_name, v_artist
                    ):
                        success = False
                        result = (
                            f"AppleScript reported success but the track did not "
                            f"persist in '{resolved.applescript_name}' after retry. "
                            f"Music.app silently reverted the edit server-side (an Apple "
                            f"bug — a manual right-click add fails the same way). Quit and "
                            f"reopen Music.app, then retry; re-adding without relaunching "
                            f"won't stick."
                        )
            if success:
                if split_match:
                    s_name, s_artist = split_match
                    steps.append(f"Resolved '{name}' → '{s_name}' by '{s_artist}'")
                    added.append(f"{s_name} - {s_artist} (via Music.app)")
                else:
                    base = f"{name} - {track_artist}" if track_artist else name
                    added.append(f"{base} (via Music.app)")
            elif "Track not found" in result and auto_add:
                # Out-of-library auto-search: prefer API, fall back to UI automation
                search_success, search_result, search_steps = _unified_auto_search_to_playlist(
                    name, track_artist or "", resolved.applescript_name
                )
                if search_steps:
                    steps.extend(search_steps)
                if search_success:
                    added.append(search_result)
                else:
                    errors.append(f"{name}: {search_result}")
            elif "Track not found" in result:
                # Library miss without auto_add — common new-user surprise.
                # Hint at the right next step rather than just stopping at
                # "not found" with no path forward.
                errors.append(
                    f"{name}: not in your library. Set auto_add=True to "
                    "find it via the Apple Music catalog (uses API if a "
                    "token is configured, UI automation on macOS otherwise)."
                )
            else:
                errors.append(_attach_error(name, result))

        # Process IDs (catalog or library IDs)
        # NOTE: track IDs require the API to resolve catalog/library metadata
        # before handing off to AppleScript. If the user has no developer
        # token, fail with a specific message rather than letting the
        # FileNotFoundError leak — same defensive class as _playlist_create
        # et al, just with a different fix shape (we can't avoid the API
        # entirely here, but we can tell the user exactly why their input
        # type isn't workable on this configuration).
        if ids_list:
            if not _has_developer_token():
                errors.append(
                    "Track IDs require an API token on macOS (resolving the ID's "
                    "catalog metadata uses the REST API). To add by ID without a "
                    "token, pass the track by name instead. To configure an API "
                    "token, run: applemusic-mcp login --dev"
                )
                ids_list = []  # Skip the ID loop below

        if ids_list:
            headers = get_headers()

            for track_id in ids_list:
                # Get track info from catalog or library
                if _is_catalog_id(track_id):
                    # Add to library first
                    steps.append(f"Adding catalog ID {track_id} to library...")
                    params = {"ids[songs]": track_id}
                    requests.post(
                        f"{BASE_URL}/me/library",
                        headers=headers,
                        params=params,
                        timeout=REQUEST_TIMEOUT,
                    )

                    # Get catalog info
                    response = requests.get(
                        f"{BASE_URL}/catalog/{get_storefront()}/songs/{track_id}",
                        headers=headers,
                        timeout=REQUEST_TIMEOUT,
                    )
                    if response.status_code != 200:
                        errors.append(f"Could not get info for {track_id}")
                        continue
                    data = response.json().get("data", [])
                    if not data:
                        continue
                    attrs = data[0].get("attributes", {})
                    name = attrs.get("name", "")
                    artist_name = attrs.get("artistName", "")
                else:
                    # Library ID - look up info
                    response = requests.get(
                        f"{BASE_URL}/me/library/songs/{track_id}",
                        headers=headers,
                        timeout=REQUEST_TIMEOUT,
                    )
                    if response.status_code != 200:
                        errors.append(f"Could not get info for {track_id}")
                        continue
                    data = response.json().get("data", [])
                    if not data:
                        continue
                    attrs = data[0].get("attributes", {})
                    name = attrs.get("name", "")
                    artist_name = attrs.get("artistName", "")

                if not name:
                    errors.append(f"No name found for {track_id}")
                    continue

                # Wait a moment for library sync if it was a catalog ID
                if _is_catalog_id(track_id):
                    time.sleep(0.5)

                # Check duplicates for IDs
                if not allow_duplicates:
                    success, exists = asc.track_exists_in_playlist(
                        resolved.applescript_name, name, artist_name or None
                    )
                    if success and exists:
                        steps.append(f"Skipped duplicate: {name}")
                        continue

                # Add via AppleScript, then verify the track actually landed
                # in the playlist. The AppleScript `duplicate` operation can
                # silently no-op when the catalog→library sync hasn't fully
                # propagated, leaving us with a false-positive success. Other
                # paths (_smart_as_add_track_to_playlist, _unified_auto_search_to_playlist)
                # already verify; this API-by-name path was an audit miss.
                success, result = asc.add_track_to_playlist(
                    resolved.applescript_name, name, artist_name if artist_name else None
                )
                if not success:
                    errors.append(_attach_error(name, result))
                elif not verify:
                    added.append(
                        (f"{name} - {artist_name}" if artist_name else name) + " (via Music.app)"
                    )
                elif _verify_track_in_playlist(
                    resolved.applescript_name, name, artist_name or None
                ):
                    added.append(
                        (f"{name} - {artist_name}" if artist_name else name) + " (via Music.app)"
                    )
                else:
                    # AppleScript reported success but verify says the track
                    # isn't in the playlist. Retry once to absorb iCloud
                    # library sync lag, then surface the failure clearly.
                    time.sleep(_VERIFY_DELAY_S)
                    success2, result2 = asc.add_track_to_playlist(
                        resolved.applescript_name,
                        name,
                        artist_name if artist_name else None,
                    )
                    if success2 and _verify_track_in_playlist(
                        resolved.applescript_name, name, artist_name or None
                    ):
                        added.append(
                            (f"{name} - {artist_name}" if artist_name else name)
                            + " (via Music.app)"
                        )
                    else:
                        errors.append(
                            f"{name}: AppleScript reported success but the track did not "
                            f"persist after retry — Music.app silently reverted the edit "
                            f"server-side (an Apple bug; a manual add fails the same way). "
                            f"Quit and reopen Music.app, then retry. "
                            f"Detail: {result2 if not success2 else 'verify failed'}"
                        )

        # Log successful adds
        if added:
            audit_log.log_action(
                "add_to_playlist",
                {"playlist": resolved.applescript_name, "tracks": added, "method": "applescript"},
                undo_info={"playlist_name": resolved.applescript_name, "tracks": added},
            )

        # Build result
        fuzzy_info = _format_fuzzy_match(resolved.fuzzy_match)
        if added and not errors:
            return (
                f"Added {len(added)} track(s) to '{resolved.applescript_name}':\n"
                + "\n".join(f"  + {t}" for t in added)
                + fuzzy_info
            )
        elif added and errors:
            msg = f"Added {len(added)} track(s), {len(errors)} failed:\n"
            msg += "\n".join(f"  + {t}" for t in added)
            msg += "\nErrors:\n" + "\n".join(f"  - {e}" for e in errors)
            if steps:
                msg += "\n\n" + "\n".join(steps)
            return msg + fuzzy_info
        elif errors:
            msg = "Errors:\n" + "\n".join(f"  - {e}" for e in errors)
            if auto_add is False or (
                auto_add is None and not get_user_preferences().get("auto_add")
            ):
                msg += (
                    "\n\n💡 Tip: Enable auto_add to automatically find and add tracks from catalog"
                )
            return msg + fuzzy_info
        else:
            if steps:
                return "\n".join(steps) + fuzzy_info
            return "No tracks added" + fuzzy_info

    # === API mode (playlist by ID) ===
    try:
        headers = get_headers()
        if not ids_list:
            # In API mode with names, search library first, then catalog
            for track_obj in names_list:
                name = track_obj["name"]
                track_artist = track_obj["artist"]
                library_id, catalog_id, display = _find_track_id(name, track_artist)
                if library_id:
                    ids_list.append(library_id)
                    steps.append(f"Found in library: {display}")
                elif catalog_id:
                    ids_list.append(catalog_id)
                    steps.append(f"Found in catalog: {display}")
                else:
                    steps.append(f"Could not find '{name}' in library or catalog")

        if not ids_list:
            # Check if UI fallback handled all tracks successfully
            ui_successes = [s for s in steps if s.startswith("[UI]")]
            if ui_successes:
                return "\n".join(
                    steps
                )  # pragma: no cover  # unreachable: [UI] steps only come from AppleScript auto_add; never present in this API-mode branch
            return "Error: No tracks to add\n" + "\n".join(steps)

        library_ids = []
        track_info = {}  # For verbose output

        # Process each ID - add to library if catalog ID
        for track_id in ids_list:
            if _is_catalog_id(track_id):
                # It's a catalog ID - need to add to library first
                steps.append(f"Adding catalog ID {track_id} to library...")

                # Add to library
                params = {"ids[songs]": track_id}
                response = requests.post(
                    f"{BASE_URL}/me/library",
                    headers=headers,
                    params=params,
                    timeout=REQUEST_TIMEOUT,
                )
                if response.status_code not in (200, 202):
                    steps.append(f"  Warning: library add returned {response.status_code}")

                # Get catalog info for the track name
                cat_response = requests.get(
                    f"{BASE_URL}/catalog/{get_storefront()}/songs/{track_id}",
                    headers=headers,
                    timeout=REQUEST_TIMEOUT,
                )
                if cat_response.status_code == 200:
                    cat_data = cat_response.json().get("data", [])
                    if cat_data:
                        attrs = cat_data[0].get("attributes", {})
                        name = attrs.get("name", "")
                        artist_name = attrs.get("artistName", "")
                        track_info[track_id] = f"{name} - {artist_name}"

                        # Poll library until track appears (up to 1s)
                        found_id = None
                        for attempt in range(10):
                            if attempt > 0:
                                time.sleep(0.1)
                            lib_response = requests.get(
                                f"{BASE_URL}/me/library/search",
                                headers=headers,
                                params={"term": name, "types": "library-songs", "limit": 25},
                                timeout=REQUEST_TIMEOUT,
                            )
                            if lib_response.status_code == 200:
                                lib_data = lib_response.json()
                                songs = (
                                    lib_data.get("results", {})
                                    .get("library-songs", {})
                                    .get("data", [])
                                )
                                for song in songs:
                                    song_attrs = song.get("attributes", {})
                                    if (
                                        song_attrs.get("name", "").lower() == name.lower()
                                        and artist_name.lower()
                                        in song_attrs.get("artistName", "").lower()
                                    ):
                                        found_id = song["id"]
                                        break
                                if found_id:
                                    break
                        if found_id:
                            library_ids.append(found_id)
                            steps.append(f"  Found in library: {name} (ID: {found_id})")
                        else:
                            steps.append(
                                f"  Warning: could not find '{name}' in library after adding"
                            )
                else:
                    steps.append(f"  Warning: could not get catalog info for {track_id}")
            else:
                # Already a library ID
                library_ids.append(track_id)

        if not library_ids:
            return "Error: No valid library IDs to add\n" + "\n".join(steps)

        # Check for duplicates
        if not allow_duplicates:
            success, existing = _get_playlist_track_names(resolved.api_id)
            if success and existing:
                filtered_ids = []
                for lib_id in library_ids:
                    # Get track name for this library ID
                    response = requests.get(
                        f"{BASE_URL}/me/library/songs/{lib_id}",
                        headers=headers,
                        timeout=REQUEST_TIMEOUT,
                    )
                    if response.status_code == 200:
                        data = response.json().get("data", [])
                        if data:
                            attrs = data[0].get("attributes", {})
                            name = attrs.get("name", "")
                            artist_name = attrs.get("artistName", "")
                            matches = _find_track_in_list(existing, name, artist_name)
                            if matches:
                                steps.append(f"Skipped duplicate: {name} - {artist_name}")
                                continue
                    filtered_ids.append(lib_id)
                library_ids = filtered_ids

        if not library_ids:
            steps.append("All tracks already in playlist")
            return "\n".join(steps)

        # Add to playlist
        track_data = [{"id": lid, "type": "library-songs"} for lid in library_ids]
        body = {"data": track_data}

        response = requests.post(
            f"{BASE_URL}/me/library/playlists/{resolved.api_id}/tracks",
            headers=headers,
            json=body,
            timeout=REQUEST_TIMEOUT,
        )

        if response.status_code == 204:
            steps.append(f"Added {len(library_ids)} track(s) to playlist")
        elif response.status_code == 403:
            return (
                "Error: Cannot edit this playlist (not API-created). Use playlist_name on macOS.\n"
                + "\n".join(steps)
            )
        elif response.status_code == 500:
            return (
                "Error: Cannot edit this playlist (not API-created). Use playlist_name on macOS.\n"
                + "\n".join(steps)
            )
        else:
            response.raise_for_status()

        # Verify
        success, updated = _get_playlist_track_names(resolved.api_id)
        if success:
            steps.append(f"Verified: playlist now has {len(updated)} tracks")

        # Log successful add (API mode)
        added_tracks = [track_info.get(tid, tid) for tid in library_ids]
        audit_log.log_action(
            "add_to_playlist",
            {"playlist": resolved.api_id, "tracks": added_tracks, "method": "api"},
            undo_info={"playlist_id": resolved.api_id, "library_ids": library_ids},
        )
        return "\n".join(steps)

    except requests.exceptions.RequestException as e:
        return f"API Error: {str(e)}\n" + "\n".join(steps)
    except (FileNotFoundError, ValueError) as e:
        return f"Error: {str(e)}\n" + "\n".join(steps)


def _playlist_copy(source: str = "", new_name: str = "") -> str:
    """Internal: Copy playlist."""
    # Validate inputs
    if not new_name:
        return "Error: new_name is required"

    # Resolve source playlist parameter
    resolved = _resolve_playlist(source)
    if resolved.error:
        return resolved.error

    has_id = bool(resolved.api_id)
    has_name = bool(resolved.applescript_name)

    # === AppleScript mode (by name, only if we don't have API ID) ===
    # Run before any API-token fetch so the AS-only path works without a
    # developer token — that's what leaked "Developer token not found"
    # for tokenless macOS users on prior versions.
    if has_name and not has_id:
        if not APPLESCRIPT_AVAILABLE:
            return (
                "Error: couldn't find that playlist over the Apple Music API "
                "(on Windows/Linux playlists are resolved via the API, not Music.app). "
                "Check the exact name with playlist(action='list', filter='...'), or pass "
                "its id (p.XXX). Note: adding a whole ALBUM to a playlist isn't supported "
                "off macOS — add by track instead."
            )

        # Get tracks from source playlist via AppleScript
        success, source_tracks = asc.get_playlist_tracks(resolved.applescript_name)
        if not success:
            return f"Error: {_format_applescript_error(str(source_tracks), 'read source playlist')}"
        if not source_tracks:
            return f"Error: Playlist '{resolved.applescript_name}' is empty"

        # Create new playlist via AppleScript
        success, new_playlist_id = asc.create_playlist(new_name, "")
        if not success:
            return f"Error creating playlist: {_format_applescript_error(str(new_playlist_id), 'create destination playlist')}"

        # Add tracks to new playlist via AppleScript
        added = 0
        failed = []
        for track in source_tracks:
            track_name = track.get("name", "")
            artist = track.get("artist", "")
            if track_name:
                success, _ = asc.add_track_to_playlist(
                    new_name, track_name, artist if artist else None
                )
                if success:
                    added += 1
                else:
                    failed.append(track_name)

        if failed:
            failed_list = ", ".join(failed[:5])
            if len(failed) > 5:
                failed_list += f", ... (+{len(failed) - 5} more)"
            audit_log.log_action(
                "copy_playlist",
                {
                    "source": resolved.applescript_name,
                    "destination": new_name,
                    "track_count": added,
                    "failed_count": len(failed),
                    "method": "applescript",
                },
                undo_info={"playlist_name": new_name, "playlist_id": new_playlist_id},
            )
            fuzzy_info = _format_fuzzy_match(resolved.fuzzy_match)
            return f"Created '{new_name}' (ID: {new_playlist_id}) with {added}/{len(source_tracks)} tracks. Failed: {failed_list}{fuzzy_info}"
        audit_log.log_action(
            "copy_playlist",
            {
                "source": resolved.applescript_name,
                "destination": new_name,
                "track_count": added,
                "method": "applescript",
            },
            undo_info={"playlist_name": new_name, "playlist_id": new_playlist_id},
        )
        fuzzy_info = _format_fuzzy_match(resolved.fuzzy_match)
        return (
            f"Created '{new_name}' (ID: {new_playlist_id}) with {added} tracks (macOS){fuzzy_info}"
        )

    # === API mode (by ID) ===
    # Token is only required in this branch — AS-mode above doesn't need it.
    try:
        headers = get_headers()

        # Get source playlist tracks
        all_tracks = []
        offset = 0
        while True:
            response = requests.get(
                f"{BASE_URL}/me/library/playlists/{resolved.api_id}/tracks",
                headers=headers,
                params={"limit": 100, "offset": offset},
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code == 404:
                break  # End of pagination or empty
            response.raise_for_status()
            tracks = response.json().get("data", [])
            if not tracks:
                break
            all_tracks.extend(tracks)
            if len(tracks) < 100:
                break  # Last page
            offset += 100

        # Create new playlist
        body = {"attributes": {"name": new_name}}
        response = requests.post(
            f"{BASE_URL}/me/library/playlists",
            headers=headers,
            json=body,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        new_id = response.json()["data"][0]["id"]

        # Add tracks in batches
        batch_size = 25
        for i in range(0, len(all_tracks), batch_size):
            batch = all_tracks[i : i + batch_size]
            track_data = [{"id": t["id"], "type": "library-songs"} for t in batch]
            requests.post(
                f"{BASE_URL}/me/library/playlists/{new_id}/tracks",
                headers=headers,
                json={"data": track_data},
                timeout=REQUEST_TIMEOUT,
            )

        audit_log.log_action(
            "copy_playlist",
            {
                "source": resolved.api_id,
                "destination": new_name,
                "track_count": len(all_tracks),
                "method": "api",
            },
            undo_info={"playlist_name": new_name, "playlist_id": new_id},
        )
        fuzzy_info = _format_fuzzy_match(resolved.fuzzy_match)
        return f"Created '{new_name}' (ID: {new_id}) with {len(all_tracks)} tracks{fuzzy_info}"

    except requests.exceptions.RequestException as e:
        return _api_error(e)
    except (FileNotFoundError, ValueError) as e:
        return str(e)


def _add_landed(result: str) -> bool:
    """Conservatively decide whether a playlist-add RESULT means the track is now
    really in the playlist. Used to gate a transactional swap's destructive remove:
    any doubt → False (keep the old track). The add helpers already verify against
    native AppleScript truth and emit honest failure strings; we read them strictly."""
    low = result.lower()
    fail_markers = (
        "error",
        "did not persist",
        "couldn't confirm",
        "could not confirm",
        "re-run",
        "syncing",
        "nothing added",
        "not found",
        "relaunch",
        "revert",
        "skipped",
    )
    if any(m in low for m in fail_markers):
        return False
    return result.startswith("Added") or ("added" in low and "playlist" in low)


def _playlist_swap(
    playlist: str,
    track: str,
    album: str,
    artist: str,
    replace: str,
    allow_duplicates: bool,
    auto_add: Optional[bool],
) -> str:
    """Transactional swap: add ``track``, CONFIRM it persisted, and only THEN remove
    ``replace``. If the add can't be confirmed — e.g. Music.app's silent server-side
    revert, or an amp-api write that didn't stick — the old track is KEPT, so a swap
    never silently loses an artist (the Coltrane bug). On macOS the add helpers verify
    against native truth; off-mac we re-read the playlist over the API."""
    if not track or not replace:
        return "Error: swap needs both `track` (new) and `replace` (old to remove)"
    if APPLESCRIPT_AVAILABLE:
        add_result = _playlist_add(playlist, track, album, artist, allow_duplicates, True, auto_add)
    elif track and not album:
        add_result = _playlist_add_api(playlist, track, artist, allow_duplicates, auto_add)
    else:
        add_result = _playlist_add(playlist, track, album, artist, allow_duplicates, True, auto_add)
    if not _add_landed(add_result):
        return (
            f"⚠️ Swap aborted — couldn't confirm '{track}' landed in '{playlist}', so "
            f"'{replace}' was NOT removed (nothing lost). Fix the add first, then retry.\n\n"
            f"{add_result}"
        )
    # STRICT confirmation before the destructive remove: require the SPECIFIC new track
    # (normalized-exact name + artist), not just a substring the add path's looser verify
    # would accept — so a reverted add can't hide behind a similarly-named track and cost
    # the old one. For a catalog id the add already pinned the exact edition, so trust
    # _add_landed there.
    if track and not album and not str(track).strip().isdigit():
        resolved = _resolve_playlist(playlist)
        if not _confirm_swap_track(
            track, artist, applescript_name=resolved.applescript_name, api_id=resolved.api_id
        ):
            return (
                f"⚠️ Swap aborted — added '{track}' but couldn't confirm that exact track is "
                f"now in '{playlist}', so '{replace}' was NOT removed (nothing lost). "
                f"Re-check the playlist, then retry.\n\n{add_result}"
            )
    if APPLESCRIPT_AVAILABLE:
        rm = _playlist_remove(playlist, replace, "")
    else:
        rm = _playlist_remove_api(playlist, replace, "")
    # Be honest if the remove didn't take — don't claim "removed" when the old track
    # may still be there (the add succeeded, so this is not data loss).
    if rm.lower().startswith("error") or "did not" in rm.lower() or "still" in rm.lower():
        return (
            f"Added '{track}' to '{playlist}', but removing '{replace}' didn't take — it "
            f"may still be there; check and remove it manually.\n\n{add_result}\n\n{rm}"
        )
    return f"Swapped in '{playlist}': added '{track}', removed '{replace}'.\n\n{add_result}\n\n{rm}"


@mcp.tool(
    annotations=ToolAnnotations(
        title="Playlists & folders", readOnlyHint=False, destructiveHint=True, openWorldHint=True
    )
)
def playlist(
    action: str = "list",
    name: str = "",
    playlist: str = "",
    folder: str = "",
    query: str = "",
    track: str = "",
    album: str = "",
    artist: str = "",
    source: str = "",
    new_name: str = "",
    description: str = "",
    filter: str = "",
    limit: int = 0,
    offset: int = 0,
    format: str = "text",
    export: str = "none",
    full: bool = False,
    fetch_explicit: Optional[bool] = None,
    allow_duplicates: bool = False,
    verify: bool = True,
    auto_add: Optional[bool] = None,
    replace: str = "",
) -> str:
    """Playlist and folder operations. Actions: list, folders (macOS — show the folder tree; folders are NOT in `list`), tracks, search, create, add, copy, move, path (macOS), remove, delete, rename. (move/remove/delete/rename work on every OS — via Music.app on macOS, via the web API on Windows/Linux. Only folders/path are macOS-only.) To find a playlist by name, use action='list' with filter='jack' (loose name match) rather than action='search', which searches the TRACKS inside a given playlist and needs a playlist param. Folders support slash-separated paths (e.g. 'Summer/Chill/Deep'). For action='add', `track` accepts a song NAME, a catalog song id (a numeric id like '1440857781' — pins the EXACT edition, avoiding name/album version mismatches), or a library id; set auto_add=True to find tracks not already in the user's library — this is required to add catalog songs the user doesn't own. Note: adding a not-yet-owned catalog track to a Music.app-made playlist is two-step — it's added to the library over the API, then attached locally once iCloud syncs it down (usually seconds). If the sync is slow it may return "added to your library — re-run to attach"; just re-run the same add. Rarely, if the sync stalls past ~20s, Music.app briefly flashes to the foreground as a last-resort sync nudge — expected, not a glitch. To SWAP one track for another, use action='add' with `replace`=<the old track to remove>: it adds the new track, confirms it actually persisted, and only THEN removes the old one — so if the add silently reverts (a Music.app bug), the old track is kept rather than lost."""
    action = action.lower().strip().replace("-", "_")

    if action == "list":
        return _playlist_list(format, export, full, filter)
    elif action == "tracks":
        return _playlist_tracks(
            playlist, filter, limit, offset, format, export, full, fetch_explicit
        )
    elif action == "search":
        if not query:
            return "Error: query required for search"
        return _playlist_search(query, playlist)
    elif action == "create":
        if folder and not name:
            rail = _write_rail("create_folder")
            if rail == "web":
                return _label_write(_folder_create_api(folder), rail)
            return _label_write(_playlist_create_folder(folder), rail)
        elif folder and name:
            return _playlist_create_in_folder(name, folder, description)
        elif name:
            rail = _write_rail("create")
            if rail == "web":
                return _label_write(_playlist_create_api(name, description), rail)
            return _label_write(_playlist_create(name, description), rail)
        else:
            return "Error: name and/or folder required for create"
    elif action == "add":
        # Transactional swap: add the new track, confirm it persisted, and only then
        # remove the old one — so a silently-reverted add never costs you the old track.
        if replace:
            return _playlist_swap(
                playlist, track, album, artist, replace, allow_duplicates, auto_add
            )
        # macOS: the native path attaches via AppleScript, which edits ANY playlist
        # — including Music.app-made ones the dev-token API can't (it library-adds
        # catalog tracks over the API first, then attaches). So prefer it on a Mac.
        # No coarse rail label here: an add can touch two rails (library vs
        # playlist) and a batch can mix per-track methods, so each sub-path
        # attributes its own method in the result instead.
        if APPLESCRIPT_AVAILABLE:
            return _playlist_add(playlist, track, album, artist, allow_duplicates, verify, auto_add)
        # Off macOS: the web rail (amp-api). It now resolves Music.app-made
        # playlists too and attempts the write; if the web token can't edit one it
        # surfaces the real error instead of a bogus "not found."
        if track and not album:
            return _playlist_add_api(playlist, track, artist, allow_duplicates, auto_add)
        return _playlist_add(playlist, track, album, artist, allow_duplicates, verify, auto_add)
    elif action == "copy":
        return _playlist_copy(source, new_name)
    elif action == "remove":
        rail = _write_rail("remove")
        if rail == "web":
            return _label_write(_playlist_remove_api(playlist, track, artist), rail)
        if err := _macos_only("remove"):
            return err
        return _label_write(_playlist_remove(playlist, track, artist), rail)
    elif action == "delete":
        if folder:
            rail = _write_rail("delete_folder")
            if rail == "web":
                return _label_write(_folder_delete_api(folder), rail)
            if err := _macos_only("delete folder"):
                return err
            return _label_write(_playlist_delete_folder(folder), rail)
        playlist_name = name or playlist
        if not playlist_name:
            return "Error: name, playlist, or folder required for delete"
        rail = _write_rail("delete")
        if rail == "web":
            return _label_write(_playlist_delete_api(playlist_name), rail)
        if err := _macos_only("delete"):
            return err
        return _label_write(_playlist_delete(playlist_name), rail)
    elif action == "rename":
        if not new_name:
            return "Error: new_name required for rename"
        if folder:
            # Folder rename has no API implementation — AppleScript only.
            if err := _macos_only("rename folder"):
                return err
            return _label_write(_playlist_rename_folder(folder, new_name), "native")
        playlist_name = name or playlist
        if not playlist_name:
            return "Error: playlist, name, or folder required for rename"
        rail = _write_rail("rename")
        if rail == "web":
            return _label_write(_playlist_rename_api(playlist_name, new_name), rail)
        if err := _macos_only("rename"):
            return err
        return _label_write(_playlist_rename(playlist_name, new_name), rail)
    elif action == "create_folder":
        # Backward compat — redirect to create(folder=...)
        if not name:
            return "Error: name required for create_folder"
        rail = _write_rail("create_folder")
        if rail == "web":
            return _label_write(_folder_create_api(name), rail)
        if err := _macos_only("create_folder"):
            return err
        return _label_write(_playlist_create_folder(name), rail)
    elif action == "move":
        if not playlist:
            return "Error: playlist required for move"
        folder_target = folder or name
        rail = _write_rail("move")
        if rail == "web":
            # The API moves a playlist into a folder OR back to the top level
            # directly (the AppleScript path can't move out of folders at all).
            return _label_write(_playlist_move_api(playlist, folder_target or "root"), rail)
        if err := _macos_only("move"):
            return err
        if not folder_target:
            if allow_duplicates:
                # User explicitly confirmed — recreate at root
                return _playlist_move_to_root(playlist)
            return (
                "Music.app cannot move playlists out of folders via AppleScript. "
                "Drag the playlist out of its folder in the Music app sidebar instead.\n\n"
                "If you need to do this programmatically, call again with "
                "allow_duplicates=True — this recreates the playlist at root "
                "with the same tracks, but the playlist ID will change."
            )
        return _playlist_move(playlist, folder_target)
    elif action in ("folders", "tree"):
        # Explicit, discoverable folder view (macOS). Folders are otherwise invisible
        # to `list` (playlists only) — this is how you find folder clutter.
        return _playlist_tree()
    elif action == "path":
        target = playlist or folder
        if target:
            return _playlist_path(target)
        else:
            return _playlist_tree()
    else:
        return f"Unknown action: {action}. Use: list, folders (macOS), tracks, search, create, add, copy, move, path (macOS), remove, delete, rename"


# ============ LIBRARY MANAGEMENT ============


@mcp.tool(
    annotations=ToolAnnotations(
        title="Library", readOnlyHint=False, destructiveHint=True, openWorldHint=True
    )
)
def library(
    action: str = "search",
    query: str = "",
    types: str = "songs",
    item_type: str = "songs",
    track: str = "",
    album: str = "",
    artist: str = "",
    limit: int = 25,
    offset: int = 0,
    format: str = "text",
    export: str = "none",
    full: bool = False,
    fetch_explicit: Optional[bool] = None,
    clean_only: Optional[bool] = None,
    # rate params
    rate_action: str = "",
    stars: int = 0,
) -> str:
    """Your library. Actions: search, add, recently_played, recently_added, browse, favorites (macOS), rate, remove, snapshot (macOS). action='search' searches the user's local library only — for catalog (Apple Music's full library) use catalog(action='search'). For search, types can be 'songs' (default), 'artists', 'albums', 'all', or 'genre' — types='genre' lists the user's own tracks whose genre matches query (e.g. query='Rock'); genre filtering is macOS-only (local Music app). Search returns one page: limit (default 25) caps results and offset pages through larger result sets — the text header shows 'start-end of total' so you know when more remain. action='favorites' lists songs marked Favorite (loved) in Music.app. action='add' adds catalog tracks/albums to your library over the API (developer token — generated or harvested — plus a media-user-token from `signin`); there is no UI-automation fallback. In api mode, search/browse read via the API and love/dislike rate via the API; star ratings (rate get/set) need native mode (local Music.app)."""
    action = action.lower().strip().replace("-", "_")

    if action == "search":
        if not query:
            return "Error: query is required for search action"
        return _library_search(
            query, types, limit, offset, format, export, full, fetch_explicit, clean_only
        )
    elif action == "add":
        return _library_add(track, album, artist)
    elif action == "recently_played":
        return _library_recently_played(limit, format, export, full)
    elif action == "recently_added":
        return _library_recently_added(limit, format, export, full)
    elif action == "browse":
        return _library_browse(
            item_type, limit, offset, format, export, full, fetch_explicit, clean_only
        )
    elif action in ("favorites", "loved"):
        if err := _macos_only("favorites"):
            return err
        return _library_favorites(limit, offset, format, export, full, fetch_explicit, clean_only)
    elif action == "rate":
        if not rate_action:
            return "Error: rate_action required (love, dislike, get, set)"
        return _library_rate(rate_action, track, artist, stars)
    elif action == "remove":
        rail = _write_rail("remove")
        if rail == "web":
            return _label_write(_library_remove_api(track, artist), rail)
        if err := _macos_only("remove"):
            return err
        return _label_write(_library_remove(track, artist), rail)
    elif action == "snapshot":
        if err := _macos_only("snapshot"):
            return err
        sub = query.strip() if query else ""
        sub_lower = sub.lower()
        if sub_lower == "new":
            return _library_snapshot_new()
        elif sub_lower == "history":
            return _library_history()
        elif sub_lower == "list":
            return _library_snapshot_list()
        elif sub_lower.startswith("delete "):
            filename = sub[7:].strip()
            return _library_snapshot_delete(filename)
        else:
            return _library_snapshot_default()
    else:
        return f"Unknown action: {action}. Use: search, add, recently_played, recently_added, browse, favorites, rate, remove, snapshot"


def _library_search(
    query: str,
    types: str = "songs",
    limit: int = 25,
    offset: int = 0,
    format: str = "text",
    export: str = "none",
    full: bool = False,
    fetch_explicit: Optional[bool] = None,
    clean_only: Optional[bool] = None,
) -> str:
    """Search your personal Apple Music library. Returns library IDs for playlist operations."""
    # Apply user preferences
    prefs = get_user_preferences()
    if fetch_explicit is None:
        fetch_explicit = prefs["fetch_explicit"]
    if clean_only is None:
        clean_only = prefs["clean_only"]

    # Search returns one offset/limit page; "all" isn't supported because a broad
    # query can match thousands of tracks. Normalize non-positive limits so they
    # page instead of erroring out in the AppleScript range access.
    if limit <= 0:
        limit = 25

    # Try AppleScript on macOS (faster for local searches) — only when the
    # active engine is native; api mode goes straight to the HTTP API below.
    asc_error: Optional[str] = None
    if _engine() == "native" and APPLESCRIPT_AVAILABLE:
        success, results, total, as_err = asc.search_library_page(
            query, types, offset=offset, limit=limit
        )
        if success and results:
            # Enrich with explicit status if requested
            if fetch_explicit or clean_only:
                cache = get_track_cache()
                for track in results:
                    track_id = track.get("id", "")
                    if track_id:
                        cached_explicit = cache.get_explicit(track_id)
                        if cached_explicit:
                            track["explicit"] = cached_explicit
                        else:
                            track["explicit"] = "Unknown"
                    else:
                        track["explicit"] = "Unknown"

            # Deduplicate by track ID (AppleScript can return duplicates)
            results = _deduplicate_by_id(results, keep_no_id=True)

            # Filter explicit content if clean_only
            if clean_only:
                results = [t for t in results if t.get("explicit") != "Yes"]

            return format_output(
                results,
                format,
                export,
                full,
                f"search_{query[:20]}",
                total_count=total,
                offset=offset,
            )
        if not success:
            # Capture so the API-fallback error path can surface what really
            # broke. Without this, an AS failure followed by missing-token
            # would only show "Developer token not found" — hiding the cause.
            asc_error = as_err or "AppleScript search failed"
        else:
            # AS succeeded with zero results — the song is genuinely not in
            # the user's library. On a tokenless macOS host, cascading to
            # the API path here would raise "Developer token not found" and
            # a Claude session reading that error would (correctly) tell
            # the user to run generate-token+authorize — which is the EXACT
            # misleading-message bug v0.9.3 and v0.9.4 fought against. Only
            # cascade when a token is actually configured (the API may see
            # cloud-synced tracks AS hasn't seen yet).
            #
            # Genre is local-only and never cascades to the API, so a zero
            # result means zero — say so plainly. Without this, a configured
            # token would fall through to the genre-unavailable message below
            # and wrongly tell a macOS user genre search "isn't available"
            # when it ran fine and simply matched nothing.
            if types == "genre":
                return f"No tracks found in library with genre matching " f"'{query}'."
            if not _has_developer_token():
                return (
                    f"No {types} found in library. "
                    f"To search the Apple Music catalog instead, use "
                    f"catalog(action='search', query='...')."
                )
        # AppleScript failed or returned empty (and we have a token) - fall
        # through to API.

    # Genre filtering is a local-library capability. The /me/library/search API
    # has no genre filter, so a full-text fallback on the genre name would
    # false-match (e.g. "Rock" hitting a song titled "Rock Your Body"). Stop with
    # a clear message instead of silently degrading to wrong results.
    if types == "genre":
        hint = f" (AppleScript error: {asc_error})" if asc_error else ""
        return (
            "Searching the library by genre needs the local Music app on macOS; "
            f"it isn't available through the Apple Music API.{hint}"
        )

    # API fallback (or primary on non-macOS)
    try:
        headers = get_headers()
        response = requests.get(
            f"{BASE_URL}/me/library/search",
            headers=headers,
            params={
                "term": query,
                "types": "library-songs",
                "limit": min(limit, 25),
                "offset": offset,
            },
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()

        songs = data.get("results", {}).get("library-songs", {}).get("data", [])
        if not songs:
            msg = "No songs found"
            if asc_error:
                # AS failure + zero API hits is the exact case the legacy
                # error swallowed silently — surface what AS reported.
                msg += f"\n\n(AppleScript also failed: {asc_error})"
            return msg

        song_data = [extract_track_data(s, full) for s in songs]

        # Deduplicate by track ID (API can return duplicates)
        song_data = _deduplicate_by_id(song_data)

        # Filter explicit content if clean_only
        if clean_only:
            song_data = [s for s in song_data if s.get("explicit") != "Yes"]

        return format_output(song_data, format, export, full, f"search_{query[:20]}")

    except requests.exceptions.RequestException as e:
        msg = f"API Error: {str(e)}"
        if asc_error:
            msg += f"\n\n(AppleScript also failed: {asc_error})"
        return msg
    except (FileNotFoundError, ValueError) as e:
        msg = str(e)
        if asc_error:
            msg += f"\n\n(AppleScript also failed: {asc_error})"
        return msg


def _resolve_catalog_track_itunes(name: str, artist: str = "") -> Optional[dict]:
    """Resolve a track to a catalog entry via the FREE iTunes Search API.

    ``itunes.apple.com/search`` is public — no developer token, no $99/yr
    Apple Developer account (issue #24/#28). It returns catalog metadata plus an
    ``music.apple.com`` URL, which the macOS-15 add path deep-links to. This is
    the tokenless resolver for "find a song and add it": it replaces scraping
    Music's search UI to *find* the track (the part that breaks on macOS 15.x
    where the autocomplete pop-over isn't in the accessibility tree).

    Returns ``{"name", "artist", "url"}`` of the best canonical match, or None.
    """
    term = f"{name} {artist}".strip()
    try:
        resp = requests.get(
            "https://itunes.apple.com/search",
            params={"term": term, "entity": "song", "limit": 8, "country": get_storefront()},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            return None
        results = resp.json().get("results", [])
    except Exception:
        return None

    def score(r: dict) -> Optional[int]:
        rn = r.get("trackName", "")
        ra = r.get("artistName", "")
        if not _loose_contains(name, rn):  # the requested name must be present
            return None
        if artist:
            # The user named an artist — that's the STRONG signal. A different
            # artist's same-titled song is the wrong track, so reject it rather
            # than risk a silent wrong-add. (Measured: "Lemons"/"Brye" must NOT
            # resolve to "Lemons" by Hairitage just because the title is exact.)
            if not _loose_contains(artist, ra):
                return None
            s = 10  # artist match dominates title exactness
            if _loose_equals(name, rn):
                s += 3  # exact canonical title
            elif _normalize_for_match(rn).startswith(_normalize_for_match(name)):
                s += 1  # title carries a suffix (feat./remaster/version)
            return s
        # No artist given: only trust an exact title match — a bare substring
        # with an unknown artist is too weak to deep-link to confidently.
        return 2 if _loose_equals(name, rn) else None

    # Keep iTunes' relevance order as the tie-breaker (lower index = more
    # relevant), so equal-scoring variants (e.g. "(feat. X)" vs "(Demo)") pick
    # the one Apple ranks first.
    scored = [(s, i, r) for i, r in enumerate(results) if (s := score(r)) is not None]
    if not scored:
        return None
    scored.sort(key=lambda x: (-x[0], x[1]))
    best = scored[0][2]
    if not best.get("trackViewUrl"):
        return None
    return {
        "name": best.get("trackName", name),
        "artist": best.get("artistName", artist),
        "url": best["trackViewUrl"],
    }


def _verify_in_library(name: str, artist: str) -> Optional[tuple[str, str]]:
    """Poll the local library for a track matching ``name`` (+ optional
    ``artist``); return (matched_name, matched_artist) or None.

    A short retry covers the brief iCloud-sync lag after a UI library-add. Used
    as the source of truth for "is the track in the library now" — whether it
    was just added or was already there.

    The retry window must outlast the library-index lag: measured on macOS 26.5,
    a freshly UI-added track first appeared in ``library playlist 1`` at ~+3s, so
    a 3-attempt (0/1/2s) window false-negatived a real success. Six attempts
    (~5s of coverage) clears it with margin.
    """
    for attempt in range(6):
        if attempt > 0:
            time.sleep(1.0)
        # Direct object lookup (not the native search index, which lags after a
        # fresh UI add) — finds the track the instant it lands in the library.
        ok, info = asc.find_library_track(name, artist)
        if ok and "|||" in info:
            n, a = info.split("|||", 1)
            return n, a
    return None


def _library_add(
    track: str = "",
    album: str = "",
    artist: str = "",
) -> str:
    """Add tracks or albums from the Apple Music catalog to your library."""
    added = []
    errors = []

    if not track and not album:
        return "Error: Provide track or album parameter"

    # Catalog add-to-library runs over the unified API (dev token generated OR
    # harvested, plus a captured media-user-token). The fragile UI automation
    # that broke across macOS/Music.app versions (#37) has been removed — there
    # is no UI fallback. If the API path isn't available, tell the user how to
    # enable it.
    if not _can_use_library_api():
        if _forced_tokenless():
            return f"Error: Adding to your library is disabled: {_FORCED_TOKENLESS_MSG}"
        return (
            "Error: Adding to your library needs the API. Run "
            "`applemusic-mcp login` (browser sign-in, no Apple Developer account) "
            "or `applemusic-mcp login --dev`."
        )

    # Helper to add a song by catalog search
    def _add_track_by_search(name: str, search_artist: str) -> None:
        song, error, fuzzy_result = _find_matching_catalog_song(name, search_artist)
        if error:
            errors.append(f"{name}: {error}")
            return
        attrs = song.get("attributes", {})
        catalog_id = song.get("id")
        success, msg = _add_to_library_api([catalog_id], "songs")
        if success:
            result_name = attrs.get("name", name)
            result_artist = attrs.get("artistName", "Unknown")
            added_msg = f"{result_name} by {result_artist}"
            if fuzzy_result:
                added_msg += f" (fuzzy: '{fuzzy_result.query}' → '{fuzzy_result.matched_name}')"
            added.append(added_msg)
        else:
            errors.append(f"{name}: {msg}")

    # Helper to add an album by catalog search
    def _add_album_by_search(name: str, search_artist: str) -> None:
        album, error, fuzzy_result = _find_matching_catalog_album(name, search_artist)
        if error:
            errors.append(f"Album '{name}': {error}")
            return
        attrs = album.get("attributes", {})
        catalog_id = album.get("id")
        success, msg = _add_to_library_api([catalog_id], "albums")
        if success:
            result_name = attrs.get("name", name)
            result_artist = attrs.get("artistName", "Unknown")
            added_msg = f"Album: {result_name} by {result_artist}"
            if fuzzy_result:
                added_msg += f" (fuzzy: '{fuzzy_result.query}' → '{fuzzy_result.matched_name}')"
            added.append(added_msg)
        else:
            errors.append(f"Album '{name}': {msg}")

    # Process tracks
    if track:
        resolved_tracks = _resolve_track(track, artist)
        for r in resolved_tracks:
            if r.error:
                errors.append(f"Track parse error: {r.error}")
                continue

            if r.input_type == InputType.CATALOG_ID:
                success, msg = _add_to_library_api([r.value], "songs")
                if success:
                    added.append(f"Track ID {r.value}")
                else:
                    errors.append(f"Track {r.value}: {msg}")
            elif r.input_type in (InputType.NAME, InputType.JSON_OBJECT):
                _add_track_by_search(r.value, r.artist)
            else:
                # Library ID or persistent ID - already in library
                errors.append(f"Track {r.value}: Already a library ID, not a catalog ID")

    # Process albums
    if album:
        resolved_albums = _resolve_album(album, artist)
        for r in resolved_albums:
            if r.error:
                errors.append(f"Album parse error: {r.error}")
                continue

            if r.input_type == InputType.CATALOG_ID:
                # Direct catalog ID — add over the API (same as the track
                # catalog-ID path). The old tokenless-UI fallback was removed,
                # so there's no UI branch here anymore.
                success, msg = _add_to_library_api([r.value], "albums")
                if success:
                    added.append(f"Album ID {r.value}")
                else:
                    errors.append(f"Album {r.value}: {msg}")
            elif r.input_type in (InputType.NAME, InputType.JSON_OBJECT):
                # Search by name
                _add_album_by_search(r.value, r.artist)
            else:
                # Library ID - already in library
                errors.append(f"Album {r.value}: Already a library ID")

    # Log successful additions
    if added:
        audit_log.log_action(
            "add_to_library",
            {"items": added, "mode": "unified"},
        )

    # Build result message
    if added and not errors:
        return f"Added {len(added)} item(s) to library:\n" + "\n".join(f"  + {a}" for a in added)
    elif added and errors:
        msg = f"Added {len(added)} item(s), {len(errors)} failed:\n"
        msg += "\n".join(f"  + {a}" for a in added)
        msg += "\nErrors:\n" + "\n".join(f"  - {e}" for e in errors)
        return msg
    elif errors:
        return "Errors:\n" + "\n".join(f"  - {e}" for e in errors)
    else:
        return "No items added"  # pragma: no cover  # unreachable: every path past the input guard appends to added or errors


def _library_recently_played(
    limit: int = 30,
    format: str = "text",
    export: str = "none",
    full: bool = False,
) -> str:
    """Get recently played tracks from your Apple Music history."""
    try:
        headers = get_headers()
        all_tracks = []
        max_limit = min(limit, 50)

        # API limits to 10 per request, paginate up to max
        for offset in range(0, max_limit, 10):
            batch_limit = min(10, max_limit - offset)
            response = requests.get(
                f"{BASE_URL}/me/recent/played/tracks",
                headers=headers,
                params={"limit": batch_limit, "offset": offset},
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code != 200:
                break
            tracks = response.json().get("data", [])
            if not tracks:
                break
            all_tracks.extend(tracks)

        if not all_tracks:
            return "No recently played tracks"

        track_data = [extract_track_data(t, full) for t in all_tracks]
        return format_output(track_data, format, export, full, "recently_played")

    except requests.exceptions.RequestException as e:
        return _api_error(e)
    except (FileNotFoundError, ValueError) as e:
        return str(e)


# ============ CATALOG SEARCH ============


def _catalog_search(
    query: str = "",
    types: str = "songs",
    limit: int = 15,
    format: str = "text",
    export: str = "none",
    full: bool = False,
    clean_only: Optional[bool] = None,
) -> str:
    """Internal: Search catalog."""
    # Apply user preferences
    if clean_only is None:
        prefs = get_user_preferences()
        clean_only = prefs["clean_only"]

    # Require query for non-music-videos types
    if not query and types != "music-videos":
        return "Error: query required (except for types='music-videos' which shows featured)"

    try:
        headers = get_headers()

        # Handle music-videos with empty query (get featured/charts)
        if types == "music-videos" and not query:
            response = requests.get(
                f"{BASE_URL}/catalog/{get_storefront()}/charts",
                headers=headers,
                params={"types": "music-videos", "limit": min(limit, 25)},
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            data = response.json()
            charts = data.get("results", {}).get("music-videos", [])
            videos = charts[0].get("data", []) if charts else []
            results = {"music-videos": {"data": videos}}
        else:
            response = requests.get(
                f"{BASE_URL}/catalog/{get_storefront()}/search",
                headers=headers,
                params={"term": query, "types": types, "limit": min(limit, 25)},
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            data = response.json()
            results = data.get("results", {})

        # Collect all data for JSON format
        all_data = {"songs": [], "albums": [], "artists": [], "playlists": [], "music-videos": []}

        if "songs" in results:
            all_data["songs"] = [
                extract_track_data(s, full) for s in results["songs"].get("data", [])
            ]
            # Deduplicate by track ID (API can return duplicates)
            all_data["songs"] = _deduplicate_by_id(all_data["songs"])
            # Filter out explicit content if clean_only is True
            if clean_only:
                all_data["songs"] = [s for s in all_data["songs"] if s.get("explicit") == "No"]

        if "albums" in results:
            for album in results["albums"].get("data", []):
                attrs = album.get("attributes", {})
                all_data["albums"].append(
                    {
                        "id": album.get("id"),
                        "name": attrs.get("name"),
                        "artist": attrs.get("artistName"),
                        "track_count": attrs.get("trackCount", 0),
                        "year": attrs.get("releaseDate", "")[:4],
                    }
                )

        if "artists" in results:
            for artist in results["artists"].get("data", []):
                attrs = artist.get("attributes", {})
                all_data["artists"].append(
                    {
                        "id": artist.get("id"),
                        "name": attrs.get("name"),
                        "genres": attrs.get("genreNames", []),
                    }
                )

        if "playlists" in results:
            for pl in results["playlists"].get("data", []):
                attrs = pl.get("attributes", {})
                all_data["playlists"].append(
                    {
                        "id": pl.get("id"),
                        "name": attrs.get("name"),
                        "curator": attrs.get("curatorName", ""),
                    }
                )

        if "music-videos" in results:
            for video in results["music-videos"].get("data", []):
                attrs = video.get("attributes", {})
                all_data["music-videos"].append(
                    {
                        "id": video.get("id"),
                        "name": attrs.get("name", ""),
                        "artist": attrs.get("artistName", ""),
                        "duration": format_duration(attrs.get("durationInMillis", 0)),
                    }
                )

        # Handle export (songs only)
        export_msg = ""
        if export not in ("", "none") and all_data["songs"]:
            export_msg = (
                "\n"
                + format_output(
                    all_data["songs"], "text", export, full, f"catalog_{query[:20]}"
                ).split("\n")[-1]
            )

        # JSON format - return all data
        if format == "json":
            return json.dumps(all_data, indent=2) + export_msg

        # Text format
        output = []
        if all_data["songs"]:
            output.append(f"=== {len(all_data['songs'])} Songs ===")
            for s in all_data["songs"]:
                explicit_marker = " [Explicit]" if s.get("explicit") == "Yes" else ""
                output.append(
                    f"{s['name']} - {s['artist']} ({s['duration']}) {s['album']} [{s['year']}]{explicit_marker} {s['id']}"
                )

        if all_data["albums"]:
            output.append(f"\n=== {len(all_data['albums'])} Albums ===")
            for a in all_data["albums"]:
                output.append(
                    f"  {a['name']} - {a['artist']} ({a['track_count']} tracks) [{a['year']}] {a['id']}"
                )

        if all_data["artists"]:
            output.append(f"\n=== {len(all_data['artists'])} Artists ===")
            for a in all_data["artists"]:
                output.append(f"  {a['name']} {a['id']}")

        if all_data["playlists"]:
            output.append(f"\n=== {len(all_data['playlists'])} Playlists ===")
            for p in all_data["playlists"]:
                output.append(f"  {p['name']} {p['id']}")

        if all_data["music-videos"]:
            output.append(f"\n=== {len(all_data['music-videos'])} Music Videos ===")
            for v in all_data["music-videos"]:
                output.append(f"  {v['name']} - {v['artist']} ({v['duration']}) {v['id']}")

        return ("\n".join(output) + export_msg) if output else "No results found"

    except requests.exceptions.RequestException as e:
        return _api_error(e)
    except (FileNotFoundError, ValueError) as e:
        return str(e)


# ============ ISRC BATCH RESOLVE ============
#
# The import shape — "for each track, one catalog search to turn title+artist into
# a catalog id" — costs one request per track and matches fuzzily. Where the caller
# has ISRCs (Spotify/Rekordbox/Plex exports all carry them), Apple's
# ``filter[isrc]`` resolves 25 at a time and matches EXACTLY: a ~25x cut in requests
# (which is what keeps you under the rate limit, #42) and no fuzzy-match errors.

_ISRC_BATCH_SIZE = 25  # Apple caps filter[isrc] at 25 values per request
# CC (country) + 3-char registrant + 2-digit year + 5-digit designation.
_ISRC_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}[0-9]{7}$")


def _normalize_isrc(raw: str) -> str:
    """ISRCs are printed with separators (``US-ABC-12-34567``) but sent without."""
    return re.sub(r"[^A-Z0-9]", "", raw.strip().upper())


def _parse_isrc_list(value: str) -> tuple[list[str], list[str]]:
    """Parse a JSON array or a comma/whitespace-separated list of ISRCs.

    Returns (valid, invalid). Malformed entries are reported rather than sent —
    a batch that includes garbage still costs a request, and the caller needs to
    know which of its inputs never got asked about."""
    value = value.strip()
    if value.startswith("["):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return [], [value]
        items = [str(i) for i in parsed]
    else:
        items = re.split(r"[,\s]+", value)

    valid: list[str] = []
    invalid: list[str] = []
    for item in items:
        if not item.strip():
            continue
        norm = _normalize_isrc(item)
        if not _ISRC_RE.match(norm):
            invalid.append(item.strip())
        elif norm not in valid:  # de-dup: no point paying for the same one twice
            valid.append(norm)
    return valid, invalid


def _catalog_resolve_isrc(isrcs: str, format: str = "text", full: bool = False) -> str:
    """Resolve ISRCs to catalog songs in batches of 25 — exact, not fuzzy.

    ``filter[isrc]`` is a filter, not a search: Apple returns only what it matched
    and silently omits the rest, so the misses are computed by diffing the request
    set against what came back. One ISRC can legitimately map to several catalog
    songs (regional releases, remasters); the first is returned and the match count
    travels with it, so the caller can tell an unambiguous hit from a judgement call.
    """
    if not isrcs.strip():
        return "Error: isrcs required (comma-separated or a JSON array)"

    wanted, invalid = _parse_isrc_list(isrcs)
    if not wanted:
        # Easy to land here by passing titles to the wrong action — say so, rather
        # than leaving the caller to guess what a "valid ISRC" is.
        return (
            f"Error: no valid ISRCs in input (rejected: {', '.join(invalid[:5])}). "
            "An ISRC looks like USABC1234567. If these are titles, use "
            "catalog(action='match', tracks=...) instead."
        )

    resolved: dict[str, dict] = {}
    asked: list[str] = []  # only batches that actually came back
    requests_made = 0
    throttled = False

    try:
        headers = get_headers()
        storefront = get_storefront()
        for start in range(0, len(wanted), _ISRC_BATCH_SIZE):
            batch = wanted[start : start + _ISRC_BATCH_SIZE]
            response = requests.get(
                f"{BASE_URL}/catalog/{storefront}/songs",
                headers=headers,
                params={"filter[isrc]": ",".join(batch)},
                timeout=REQUEST_TIMEOUT,
            )
            requests_made += 1
            amp_api.note_status(response.status_code, amp_api.API)
            if response.status_code == 429:
                # Stop immediately: further batches can only extend the window.
                # Whatever resolved before this point is still good, so report it
                # rather than throwing the partial work away.
                throttled = True
                break
            response.raise_for_status()
            asked.extend(batch)

            for song in response.json().get("data", []):
                # extract_track_data also populates the track cache (name/artist/ISRC
                # → catalog id), so a later lookup for the same track is free.
                data = extract_track_data(song, full)
                isrc = _normalize_isrc(song.get("attributes", {}).get("isrc", ""))
                if not isrc:
                    continue  # pragma: no cover  # Apple always echoes the filtered ISRC
                if isrc in resolved:
                    resolved[isrc]["matches"] += 1
                else:
                    resolved[isrc] = {**data, "isrc": isrc, "matches": 1}

    except requests.exceptions.RequestException as e:
        return _api_error(e)
    except (FileNotFoundError, ValueError) as e:
        return str(e)

    # "Asked and not returned" is a real absence; "never asked" is unknown. Folding
    # the second into the first would recreate the exact false-negative this whole
    # change exists to kill (#42), so they stay separate.
    unmatched = [i for i in asked if i not in resolved]
    never_asked = [i for i in wanted if i not in asked]

    if format == "json":
        return json.dumps(
            {
                "resolved": resolved,
                "unmatched": unmatched,
                "never_asked": never_asked,
                "invalid": invalid,
                "requests": requests_made,
                "throttled": throttled,
            },
            indent=2,
        )

    lines = [f"=== Resolved {len(resolved)}/{len(wanted)} ISRCs in {requests_made} request(s) ==="]
    for isrc, song in resolved.items():
        ambiguous = f"  [{song['matches']} catalog matches]" if song["matches"] > 1 else ""
        lines.append(f"{isrc} -> {song['id']}  {song['name']} - {song['artist']}{ambiguous}")
    if unmatched:
        lines.append(f"\n=== Not in the {get_storefront()} catalog ({len(unmatched)}) ===")
        lines.append(", ".join(unmatched))
    if invalid:
        lines.append(f"\n=== Malformed, not sent ({len(invalid)}) ===")
        lines.append(", ".join(invalid))
    if throttled:
        lines.append(f"\n{_SESSION_THROTTLED_MSG}")
        lines.append(
            f"Stopped after {requests_made} request(s); {len(never_asked)} ISRC(s) were "
            "never asked about — their status is UNKNOWN, not 'not in the catalog'. "
            "Re-run with just those once the window clears."
        )
    return "\n".join(lines)


# ============ DRY-RUN TRACK MATCHING ============
#
# Adding by name resolves each title through the fuzzy matcher and writes
# immediately, so a 150-track import is 150 unreviewed judgement calls — and the
# wrong-edition case is common (one ISRC routinely maps to 2-4 catalog releases).
# This runs the SAME matcher and reports what it would pick, without touching the
# library, so the choice can be reviewed before it's committed.
#
# Unlike resolve_isrc this costs one request PER TRACK (Apple has no batch
# title+artist endpoint), which is exactly the shape that hits the rate limit —
# hence the cap and the nudge toward ISRCs.

_MATCH_DEFAULT_CAP = 25


def _catalog_match_tracks(
    tracks: str, artist: str = "", max_tracks: int = 0, format: str = "text"
) -> str:
    """Dry-run: what would these names resolve to? Adds nothing.

    Reports the proposed catalog song per input plus how confident the match is
    — ``exact`` (title matched outright), ``partial``/``fuzzy`` (the matcher had
    to work for it, so it's worth a look), or ``id`` (already a catalog id, no
    lookup needed). The ids come back ready to paste into ``playlist(action="add")``.
    """
    if not tracks.strip():
        return "Error: tracks required (comma-separated, newline-separated, or a JSON array)"

    inputs = _resolve_track(tracks, artist)
    # Deliberately NOT the tool's `limit` — that one means "how many search results",
    # and quietly reusing it here capped this at 15 instead of 25.
    cap = max(1, max_tracks or _MATCH_DEFAULT_CAP)
    considered, deferred = inputs[:cap], inputs[cap:]

    matched: list[dict] = []
    unmatched: list[dict] = []
    never_asked: list[str] = []
    requests_made = 0
    throttled = False

    for i, r in enumerate(considered):
        if r.error:
            unmatched.append({"input": r.raw or r.value, "reason": r.error})
            continue
        if r.input_type == InputType.CATALOG_ID:
            # Already an exact reference — spending a request to confirm it would
            # be the same wasted probe the 429 work just removed elsewhere.
            matched.append({"input": r.value, "id": r.value, "confidence": "id"})
            continue
        if r.input_type not in (InputType.NAME, InputType.JSON_OBJECT):
            unmatched.append(
                {"input": r.raw or r.value, "reason": f"{r.input_type.value} can't be name-matched"}
            )
            continue

        song, err, fuzzy = _find_matching_catalog_song(r.value, r.artist or artist)
        requests_made += 1
        if song is None:
            # A throttle surfaces here as the rate-limit reason, not "not found"
            # (#42) — stop, and mark the remainder UNKNOWN rather than absent.
            if amp_api.throttled_recently():
                throttled = True
                never_asked.extend(x.value or x.raw for x in considered[i:])
                break
            unmatched.append({"input": r.value, "reason": err or "Not found in catalog"})
            continue

        attrs = song.get("attributes", {})
        matched.append(
            {
                "input": r.value,
                "id": song.get("id", ""),
                "name": attrs.get("name", ""),
                "artist": attrs.get("artistName", ""),
                "album": attrs.get("albumName", ""),
                "year": (attrs.get("releaseDate", "") or "")[:4],
                "confidence": fuzzy.match_type if fuzzy else "exact",
            }
        )

    if format == "json":
        return json.dumps(
            {
                "matched": matched,
                "unmatched": unmatched,
                "never_asked": never_asked,
                "deferred": [x.value or x.raw for x in deferred],
                "requests": requests_made,
                "throttled": throttled,
                "ids": [m["id"] for m in matched],
            },
            indent=2,
        )

    total = len(considered)
    lines = [
        f"=== Matched {len(matched)}/{total} — DRY RUN, nothing was added ===",
    ]
    # Anything the matcher had to work for gets flagged; only an outright title
    # match (or an id the caller supplied) is trusted silently.
    marks = {"exact": "=", "id": "#", "partial": "~", "fuzzy": "?", "fuzzy_partial": "?"}
    for m in matched:
        mark = marks.get(m["confidence"], "?")
        if m["confidence"] == "id":
            lines.append(f"{mark} {'id':<13} {m['id']} (already a catalog id)")
            continue
        year = f", {m['year']}" if m["year"] else ""
        arrow = " -> " if m["confidence"] != "exact" else " == "
        lines.append(
            f"{mark} {m['confidence']:<13} {m['input']!r}{arrow}{m['name']} - {m['artist']} "
            f"[{m['album']}{year}]  {m['id']}"
        )
    if unmatched:
        lines.append(f"\n=== No match ({len(unmatched)}) ===")
        lines.extend(f"x {u['input']!r}: {u['reason']}" for u in unmatched)
    if never_asked:
        lines.append(f"\n=== Never asked ({len(never_asked)}) — UNKNOWN, not absent ===")
        lines.append(", ".join(never_asked))
        lines.append(_SESSION_THROTTLED_MSG)
    if deferred:
        lines.append(
            f"\n{len(deferred)} more not attempted (cap {cap}). This action costs one request "
            "PER TRACK — for a large import use catalog(action='resolve_isrc') instead "
            "(25 per request, exact), or raise `limit` deliberately."
        )
    if matched:
        ids = ",".join(m["id"] for m in matched)
        lines.append(
            f"\nReviewed and happy? Add them with:\n"
            f"  playlist(action='add', playlist='<name>', track='{ids}')"
        )
    inexact = [m for m in matched if m["confidence"] not in ("exact", "id")]
    if inexact:
        lines.append(
            f"\n!! {len(inexact)} match(es) were NOT an outright title match — check these "
            "before adding. A missing apostrophe or a title without an artist can land on a "
            "different act entirely, and one title often maps to several catalog editions:"
        )
        lines.extend(f"   {m['input']!r} -> {m['name']} - {m['artist']}" for m in inexact)
    return "\n".join(lines)


def _catalog_album_tracks(
    album: str = "",
    artist: str = "",
    limit: int = 0,
    offset: int = 0,
    format: str = "text",
    export: str = "none",
    full: bool = False,
) -> str:
    """Internal: Get album tracks."""
    if not album:
        return "Error: Provide album parameter"

    # Resolve album input
    resolved = _resolve_album(album, artist)
    if not resolved:
        return "Error: Could not resolve album"  # pragma: no cover  # unreachable: _resolve_album/_resolve_input always returns a non-empty list

    r = resolved[0]  # Only use first resolved album
    if r.error:
        return f"Error: {r.error}"

    album_id = None

    if r.input_type == InputType.CATALOG_ID:
        album_id = r.value
    elif r.input_type == InputType.ALBUM_ID:
        album_id = r.value
    elif r.input_type == InputType.NAME:
        # Search for album by name
        try:
            headers = get_headers()
            search_term = f"{r.value} {r.artist}".strip() if r.artist else r.value
            response = requests.get(
                f"{BASE_URL}/catalog/{get_storefront()}/search",
                headers=headers,
                params={"term": search_term, "types": "albums", "limit": 5},
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code == 200:
                albums = response.json().get("results", {}).get("albums", {}).get("data", [])
                for a in albums:
                    attrs = a.get("attributes", {})
                    album_name = attrs.get("name", "")
                    album_artist = attrs.get("artistName", "")
                    if _loose_contains(r.value, album_name):
                        if not r.artist or _loose_contains(r.artist, album_artist):
                            album_id = a.get("id")
                            break
        except Exception:
            pass
        if not album_id:
            return f"Album not found: {r.value}"
    else:
        return f"Unsupported input type for album lookup"

    try:
        headers = get_headers()

        # Detect if it's a library or catalog ID
        if album_id.startswith("l."):
            base_url = f"{BASE_URL}/me/library/albums/{album_id}/tracks"
        else:
            base_url = f"{BASE_URL}/catalog/{get_storefront()}/albums/{album_id}/tracks"

        # Paginate to handle box sets / compilations with 100+ tracks
        all_tracks = []
        api_offset = 0

        while True:
            response = requests.get(
                base_url,
                headers=headers,
                params={"limit": 100, "offset": api_offset},
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code == 404:
                break
            response.raise_for_status()
            tracks = response.json().get("data", [])
            if not tracks:
                break
            all_tracks.extend(tracks)
            if len(tracks) < 100:
                break
            api_offset += 100

        if not all_tracks:
            return "No tracks found"

        # Extract track data with extras for numbered display
        track_data = [extract_track_data(t, include_extras=True) for t in all_tracks]

        # Apply pagination
        track_data, total_count, error = _apply_pagination(track_data, limit, offset)
        if error:
            return error

        return format_output(
            track_data,
            format,
            export,
            full,
            f"album_{album_id.replace('.', '_')}",
            total_count=total_count,
            offset=offset,
        )

    except requests.exceptions.RequestException as e:
        return _api_error(e)
    except (FileNotFoundError, ValueError) as e:
        return str(e)


def _catalog_album_details(
    album: str = "",
    artist: str = "",
    format: str = "text",
    export: str = "none",
    full: bool = False,
) -> str:
    """Internal: Get complete album details including metadata and tracks."""
    if not album:
        return "Error: Provide album parameter"

    # Resolve album input
    resolved = _resolve_album(album, artist)
    if not resolved:
        return "Error: Could not resolve album"  # pragma: no cover  # unreachable: _resolve_album/_resolve_input always returns a non-empty list

    r = resolved[0]  # Only use first resolved album
    if r.error:
        return f"Error: {r.error}"

    album_id = None

    if r.input_type == InputType.CATALOG_ID:
        album_id = r.value
    elif r.input_type == InputType.ALBUM_ID:
        album_id = r.value
    elif r.input_type == InputType.NAME:
        # Search for album by name using fuzzy matching
        album_match, error, fuzzy_result = _find_matching_catalog_album(r.value, r.artist)
        if error:
            return f"Album not found: {r.value}"
        album_id = album_match.get("id")
    else:
        return f"Unsupported input type for album lookup"

    try:
        headers = get_headers()

        # Fetch album metadata
        album_url = f"{BASE_URL}/catalog/{get_storefront()}/albums/{album_id}"
        album_response = requests.get(
            album_url,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        album_response.raise_for_status()
        album_data = album_response.json().get("data", [])

        if not album_data:
            return "Album not found"

        album_obj = album_data[0]
        attrs = album_obj.get("attributes", {})

        # Build metadata output
        output_lines = [
            f"=== {attrs.get('name', 'Unknown Album')} ===",
            f"Artist: {attrs.get('artistName', 'Unknown')}",
            f"Release Date: {attrs.get('releaseDate', 'Unknown')}",
            f"Genre: {attrs.get('genreNames', ['Unknown'])[0] if attrs.get('genreNames') else 'Unknown'}",
            f"Label: {attrs.get('recordLabel', 'Unknown')}",
            f"Track Count: {attrs.get('trackCount', 0)}",
            f"Copyright: {attrs.get('copyright', 'Unknown')}",
            f"Album ID: {album_id}",
            "",
            "=== Tracks ===",
        ]

        # Fetch all tracks
        tracks_url = f"{BASE_URL}/catalog/{get_storefront()}/albums/{album_id}/tracks"
        all_tracks = []
        api_offset = 0

        while True:
            response = requests.get(
                tracks_url,
                headers=headers,
                params={"limit": 100, "offset": api_offset},
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code == 404:
                break
            response.raise_for_status()
            tracks = response.json().get("data", [])
            if not tracks:
                break
            all_tracks.extend(tracks)
            if len(tracks) < 100:
                break
            api_offset += 100

        # Format tracks
        for i, track in enumerate(all_tracks, 1):
            track_attrs = track.get("attributes", {})
            track_name = track_attrs.get("name", "Unknown")
            duration_ms = track_attrs.get("durationInMillis", 0)
            duration = f"{duration_ms // 60000}:{(duration_ms % 60000) // 1000:02d}"
            output_lines.append(f"{i}. {track_name} ({duration})")

        return "\n".join(output_lines)

    except requests.exceptions.RequestException as e:
        return _api_error(e)
    except (FileNotFoundError, ValueError) as e:
        return str(e)


# ============ LIBRARY BROWSING ============


def _build_library_track_data(
    songs: list[dict],
    fetch_explicit: bool,
    clean_only: bool,
) -> list[dict]:
    """Build output-ready track dicts from AppleScript library song dicts.

    Constructs core fields, enriches with explicit status from cache, and
    filters out explicit tracks when clean_only is True.
    """
    data = [
        {
            "name": s.get("name", ""),
            "artist": s.get("artist", ""),
            "album": s.get("album", ""),
            "duration": s.get("duration", ""),
            "genre": s.get("genre", ""),
            "year": s.get("year", ""),
            "id": s.get("id", ""),
            "explicit": "Unknown",
        }
        for s in songs
    ]
    if fetch_explicit or clean_only:
        cache = get_track_cache()
        for track in data:
            track_id = track.get("id", "")
            if track_id:
                cached_explicit = cache.get_explicit(track_id)
                if cached_explicit:
                    track["explicit"] = cached_explicit
    if clean_only:
        data = [t for t in data if t.get("explicit") != "Yes"]
    return data


def _library_browse(
    item_type: str = "songs",
    limit: int = 100,
    offset: int = 0,
    format: str = "text",
    export: str = "none",
    full: bool = False,
    fetch_explicit: Optional[bool] = None,
    clean_only: Optional[bool] = None,
) -> str:
    """Browse your Apple Music library by type: songs, albums, artists, or videos."""
    item_type = item_type.lower().strip()

    # Apply user preferences (only relevant for songs)
    prefs = get_user_preferences()
    if fetch_explicit is None:
        fetch_explicit = prefs["fetch_explicit"]
    if clean_only is None:
        clean_only = prefs["clean_only"]

    # Try AppleScript first for songs (local, instant, no auth required) —
    # only in native engine mode; api mode browses via the HTTP API below.
    if _engine() == "native" and APPLESCRIPT_AVAILABLE and item_type == "songs":
        if limit > 0 and not clean_only:
            # O(limit): fetch only the requested page and the true total count.
            success, as_songs, true_total, as_err = asc.get_library_songs_page(offset, limit)
            if success:
                if true_total == 0:
                    return f"No {item_type} in library"
                if offset >= true_total:
                    return f"Offset {offset} exceeds library size of {true_total} songs"
                data = _build_library_track_data(as_songs, fetch_explicit, clean_only)
                return format_output(
                    data, format, export, full, "songs", total_count=true_total, offset=offset
                )
            as_error = as_err or "AppleScript get_library_songs_page failed"
            return (
                f"Error browsing library: {_format_applescript_error(as_error, 'browse library')}"
            )
        else:
            # Full fetch: limit=0 (all songs) or clean_only=True (needs post-filter total).
            success, as_songs = asc.get_library_songs(0)
            if success:
                if not as_songs:
                    return f"No {item_type} in library"
                data = _build_library_track_data(as_songs, fetch_explicit, clean_only)
                data, total_count, error = _apply_pagination(data, limit, offset)
                if error:
                    return error
                return format_output(
                    data, format, export, full, "songs", total_count=total_count, offset=offset
                )
            # AppleScript failed on macOS — surface the actionable error
            # instead of cascading to API and leaking "Developer token not
            # found" when the real cause is Music.app not running or
            # Automation permissions denied. Same defense as _playlist_list.
            as_error = str(as_songs) if as_songs else "AppleScript get_library_songs failed"
            return (
                f"Error browsing library: {_format_applescript_error(as_error, 'browse library')}"
            )

    # Fall back to API (non-macOS, or non-songs item_type)
    try:
        headers = get_headers()

        # Map type to API endpoint
        type_map = {
            "songs": "library-songs",
            "albums": "library/albums",
            "artists": "library/artists",
            "videos": "library/music-videos",
        }
        if item_type not in type_map:
            return f"Invalid type: {item_type}. Use: songs, albums, artists, or videos"

        endpoint = type_map[item_type]
        all_items = []
        api_offset = 0
        fetch_all = limit == 0
        # Need to fetch enough for both offset and limit
        max_to_fetch = (offset + limit) if not fetch_all else float("inf")

        # Paginate
        while len(all_items) < max_to_fetch:
            batch_limit = 100 if fetch_all else min(100, int(max_to_fetch - len(all_items)))
            url = f"{BASE_URL}/me/{endpoint}" if "/" in endpoint else f"{BASE_URL}/me/library/songs"
            response = requests.get(
                url,
                headers=headers,
                params={"limit": batch_limit, "offset": api_offset},
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code == 404:
                break
            response.raise_for_status()
            items = response.json().get("data", [])
            if not items:
                break
            all_items.extend(items)
            if len(items) < 100:
                break
            api_offset += 100

        if not all_items:
            return f"No {item_type} in library"

        # Extract data based on type
        if item_type == "songs":
            data = [extract_track_data(s, full) for s in all_items]
        elif item_type == "albums":
            data = []
            for album in all_items:
                attrs = album.get("attributes", {})
                genres = attrs.get("genreNames", [])
                data.append(
                    {
                        "id": album.get("id", ""),
                        "name": attrs.get("name", ""),
                        "artist": attrs.get("artistName", ""),
                        "track_count": attrs.get("trackCount", 0),
                        "genre": genres[0] if genres else "",
                        "release_date": attrs.get("releaseDate", ""),
                    }
                )
        elif item_type == "artists":
            data = [
                {"id": a.get("id", ""), "name": a.get("attributes", {}).get("name", "")}
                for a in all_items
            ]
        else:  # videos
            data = [
                {
                    "id": v.get("id", ""),
                    "name": v.get("attributes", {}).get("name", ""),
                    "artist": v.get("attributes", {}).get("artistName", ""),
                }
                for v in all_items
            ]

        # Filter explicit content if clean_only (songs only, API already has explicit status)
        if item_type == "songs" and clean_only:
            data = [t for t in data if t.get("explicit") != "Yes"]

        # Apply pagination
        data, total_count, error = _apply_pagination(data, limit, offset)
        if error:
            return error

        return format_output(
            data,
            format,
            export,
            full,
            f"library_{item_type}",
            total_count=total_count,
            offset=offset,
        )

    except requests.exceptions.RequestException as e:
        return _api_error(e)
    except (FileNotFoundError, ValueError) as e:
        return str(e)


def _library_favorites(
    limit: int = 25,
    offset: int = 0,
    format: str = "text",
    export: str = "none",
    full: bool = False,
    fetch_explicit: Optional[bool] = None,
    clean_only: Optional[bool] = None,
) -> str:
    """List songs marked Favorite (loved) in Music.app.

    macOS-only: relies on AppleScript's ``whose loved is true`` filter, so
    Music.app does the selection natively. Loved status isn't exposed by the
    catalog REST API, hence no cross-platform fallback.
    """
    prefs = get_user_preferences()
    if fetch_explicit is None:
        fetch_explicit = prefs["fetch_explicit"]
    if clean_only is None:
        clean_only = prefs["clean_only"]

    success, as_songs = asc.get_loved_songs(0)
    if not success:
        as_error = str(as_songs) if as_songs else "AppleScript get_loved_songs failed"
        return f"Error listing favorites: {_format_applescript_error(as_error, 'list favorites')}"
    if not as_songs:
        return "No favorite songs in library"

    data = _build_library_track_data(as_songs, fetch_explicit, clean_only)
    if not data:
        return "No favorite songs in library"
    data, total_count, error = _apply_pagination(data, limit, offset)
    if error:
        return error
    return format_output(
        data, format, export, full, "songs", total_count=total_count, offset=offset
    )


# ============ DISCOVERY & PERSONALIZATION ============


def _discover_recommendations(limit: int, format: str, export: str, full: bool) -> str:
    """Internal: Get personalized recommendations."""
    try:
        headers = get_headers()
        response = requests.get(
            f"{BASE_URL}/me/recommendations",
            headers=headers,
            params={"limit": 10},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()

        all_items = []
        for rec in data.get("data", []):
            attrs = rec.get("attributes", {})
            title = attrs.get("title", {}).get("stringForDisplay", "Recommendation")
            relationships = rec.get("relationships", {})
            contents = relationships.get("contents", {}).get("data", [])

            for item in contents[:8]:
                item_attrs = item.get("attributes", {})
                all_items.append(
                    {
                        "category": title,
                        "name": item_attrs.get("name", "Unknown"),
                        "artist": item_attrs.get("artistName", ""),
                        "type": item.get("type", "").replace("library-", ""),
                        "id": item.get("id"),
                        "year": item_attrs.get("releaseDate", "")[:4],
                    }
                )

        # Apply user's limit to final results
        if limit > 0:
            all_items = all_items[:limit]

        return format_output(all_items, format, export, full, "recommendations")

    except requests.exceptions.RequestException as e:
        return _api_error(e)
    except (FileNotFoundError, ValueError) as e:
        return str(e)


def _discover_heavy_rotation(format: str, export: str, full: bool) -> str:
    """Internal: Get heavy rotation."""
    try:
        headers = get_headers()
        response = requests.get(
            f"{BASE_URL}/me/history/heavy-rotation",
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()

        items = data.get("data", [])
        if not items:
            return "No heavy rotation data"

        item_data = []
        for item in items:
            attrs = item.get("attributes", {})
            genres = attrs.get("genreNames", [])

            item_data.append(
                {
                    "id": item.get("id", ""),
                    "name": attrs.get("name", ""),
                    "artist": attrs.get("artistName", ""),
                    "type": item.get("type", "").replace("library-", "").replace("-", " "),
                    "track_count": attrs.get("trackCount", ""),
                    "genre": genres[0] if genres else "",
                    "release_date": attrs.get("releaseDate", ""),
                    "date_added": attrs.get("dateAdded", ""),
                    "artwork_url": attrs.get("artwork", {})
                    .get("url", "")
                    .replace("{w}x{h}", "500x500"),
                }
            )

        return format_output(item_data, format, export, full, "heavy_rotation")

    except requests.exceptions.RequestException as e:
        return _api_error(e)
    except (FileNotFoundError, ValueError) as e:
        return str(e)


def _library_recently_added(limit: int, format: str, export: str, full: bool) -> str:
    """Internal: Get recently added content."""
    try:
        headers = get_headers()
        all_items = []
        offset = 0
        max_to_fetch = min(limit, 100)

        while len(all_items) < max_to_fetch:
            batch_limit = min(25, max_to_fetch - len(all_items))
            response = requests.get(
                f"{BASE_URL}/me/library/recently-added",
                headers=headers,
                params={"limit": batch_limit, "offset": offset},
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code == 404:
                break
            response.raise_for_status()
            items = response.json().get("data", [])
            if not items:
                break
            all_items.extend(items)
            if len(items) < batch_limit:
                break
            offset += 25

        if not all_items:
            return "No recently added content"

        item_data = []
        for item in all_items:
            attrs = item.get("attributes", {})
            genres = attrs.get("genreNames", [])

            item_data.append(
                {
                    "id": item.get("id", ""),
                    "name": attrs.get("name", ""),
                    "artist": attrs.get("artistName", ""),
                    "type": item.get("type", "").replace("library-", ""),
                    "track_count": attrs.get("trackCount", ""),
                    "genre": genres[0] if genres else "",
                    "release_date": attrs.get("releaseDate", ""),
                    "date_added": attrs.get("dateAdded", ""),
                    "artwork_url": attrs.get("artwork", {})
                    .get("url", "")
                    .replace("{w}x{h}", "500x500"),
                }
            )

        return format_output(item_data, format, export, full, "recently_added")

    except requests.exceptions.RequestException as e:
        return _api_error(e)
    except (FileNotFoundError, ValueError) as e:
        return str(e)


def _discover_personal_station() -> str:
    """Internal: Get personal station."""
    try:
        headers = get_headers()
        response = requests.get(
            f"{BASE_URL}/catalog/{get_storefront()}/stations",
            headers=headers,
            params={"filter[identity]": "personal"},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()

        stations = data.get("data", [])
        if not stations:
            return "No personal station found (may require more listening history)"

        station = stations[0]
        attrs = station.get("attributes", {})
        name = attrs.get("name", "Your Personal Station")
        station_id = station.get("id")
        is_live = attrs.get("isLive", False)

        output = [
            f"=== {name} ===",
            f"Station ID: {station_id}",
            f"Type: {'Live' if is_live else 'On-demand'}",
            "",
            "This station plays music based on your listening history and preferences.",
        ]
        return "\n".join(output)

    except requests.exceptions.RequestException as e:
        return _api_error(e)
    except (FileNotFoundError, ValueError) as e:
        return str(e)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Discover", readOnlyHint=True, destructiveHint=False, openWorldHint=True
    )
)
def discover(
    action: str = "recommendations",
    artist: str = "",
    song_id: str = "",
    chart_type: str = "songs",
    limit: int = 50,
    format: str = "text",
    export: str = "none",
    full: bool = False,
    storefront: str = "",
) -> str:
    """Personalized discovery. Actions: recommendations, heavy_rotation, personal_station, charts, top_songs, similar_artists, song_station."""
    action = action.lower().strip().replace("-", "_")

    if action == "recommendations":
        return _discover_recommendations(limit, format, export, full)
    elif action == "heavy_rotation":
        return _discover_heavy_rotation(format, export, full)
    elif action == "personal_station":
        return _discover_personal_station()
    else:
        # Determine storefront for catalog-based actions
        sf = storefront if storefront else get_storefront()

        if action == "charts":
            return _discover_charts(chart_type, sf)
        elif action == "top_songs":
            if not artist:
                return "Error: artist required for top_songs"
            return _discover_top_songs(artist, sf)
        elif action == "similar_artists":
            if not artist:
                return "Error: artist required for similar_artists"
            return _discover_similar_artists(artist, sf)
        elif action == "song_station":
            if not song_id:
                return "Error: song_id required for song_station"
            return _discover_song_station(song_id, sf)
        else:
            return f"Unknown action: {action}. Use: recommendations, heavy_rotation, personal_station, charts, top_songs, similar_artists, song_station"


def _discover_top_songs(artist: str, storefront: str = "") -> str:
    """Internal: Get artist's top songs."""
    if not artist:
        return "Error: Provide artist parameter"

    try:
        headers = get_headers()
        sf = storefront if storefront else get_storefront()

        # Check if it's a catalog ID (all digits)
        if artist.isdigit():
            artist_id = artist
            # Look up artist name
            response = requests.get(
                f"{BASE_URL}/catalog/{sf}/artists/{artist_id}",
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code == 200:
                data = response.json().get("data", [])
                artist_actual_name = (
                    data[0].get("attributes", {}).get("name", artist) if data else artist
                )
            else:
                artist_actual_name = artist
        else:
            # Search for artist by name
            search_response = requests.get(
                f"{BASE_URL}/catalog/{sf}/search",
                headers=headers,
                params={"term": artist, "types": "artists", "limit": 1},
                timeout=REQUEST_TIMEOUT,
            )
            search_response.raise_for_status()
            artists = search_response.json().get("results", {}).get("artists", {}).get("data", [])

            if not artists:
                return f"No artist found matching '{artist}'"

            artist_data = artists[0]
            artist_id = artist_data.get("id")
            artist_actual_name = artist_data.get("attributes", {}).get("name", artist)

        # Get top songs
        response = requests.get(
            f"{BASE_URL}/catalog/{sf}/artists/{artist_id}/view/top-songs",
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        songs = response.json().get("data", [])

        output = [f"=== Top Songs by {artist_actual_name} ==="]
        for i, song in enumerate(songs, 1):
            attrs = song.get("attributes", {})
            name = attrs.get("name", "Unknown")
            album = attrs.get("albumName", "")
            song_id = song.get("id")
            output.append(
                f"{i}. {name}" + (f" ({album})" if album else "") + f" [catalog ID: {song_id}]"
            )

        return "\n".join(output) if len(output) > 1 else "No top songs found"

    except requests.exceptions.RequestException as e:
        return _api_error(e)
    except (FileNotFoundError, ValueError) as e:
        return str(e)


def _discover_similar_artists(artist: str, storefront: str = "") -> str:
    """Internal: Get similar artists."""
    if not artist:
        return "Error: Provide artist parameter"

    try:
        headers = get_headers()
        sf = storefront if storefront else get_storefront()

        # Check if it's a catalog ID (all digits)
        if artist.isdigit():
            artist_id = artist
            # Look up artist name
            response = requests.get(
                f"{BASE_URL}/catalog/{sf}/artists/{artist_id}",
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code == 200:
                data = response.json().get("data", [])
                artist_actual_name = (
                    data[0].get("attributes", {}).get("name", artist) if data else artist
                )
            else:
                artist_actual_name = artist
        else:
            # Search for artist by name
            search_response = requests.get(
                f"{BASE_URL}/catalog/{sf}/search",
                headers=headers,
                params={"term": artist, "types": "artists", "limit": 1},
                timeout=REQUEST_TIMEOUT,
            )
            search_response.raise_for_status()
            artists = search_response.json().get("results", {}).get("artists", {}).get("data", [])

            if not artists:
                return f"No artist found matching '{artist}'"

            artist_data = artists[0]
            artist_id = artist_data.get("id")
            artist_actual_name = artist_data.get("attributes", {}).get("name", artist)

        # Get similar artists
        response = requests.get(
            f"{BASE_URL}/catalog/{sf}/artists/{artist_id}/view/similar-artists",
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        similar = response.json().get("data", [])

        output = [f"=== Artists Similar to {artist_actual_name} ==="]
        for artist in similar:
            attrs = artist.get("attributes", {})
            name = attrs.get("name", "Unknown")
            genres = ", ".join(attrs.get("genreNames", [])[:2])
            artist_id = artist.get("id")
            output.append(f"{name} ({genres}) [artist ID: {artist_id}]")

        return "\n".join(output) if len(output) > 1 else "No similar artists found"

    except requests.exceptions.RequestException as e:
        return _api_error(e)
    except (FileNotFoundError, ValueError) as e:
        return str(e)


def _discover_song_station(song_id: str, storefront: str = "") -> str:
    """Internal: Get song station."""
    try:
        headers = get_headers()
        sf = storefront if storefront else get_storefront()

        response = requests.get(
            f"{BASE_URL}/catalog/{sf}/songs/{song_id}/station",
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()

        stations = data.get("data", [])
        if not stations:
            return "No station found for this song"

        station = stations[0]
        attrs = station.get("attributes", {})
        name = attrs.get("name", "Unknown Station")
        station_id = station.get("id")

        return f"Station: {name}\nStation ID: {station_id}\n\nUse this station to discover music similar to this song."

    except requests.exceptions.RequestException as e:
        return _api_error(e)
    except (FileNotFoundError, ValueError) as e:
        return str(e)


# ============ RATINGS ============


# Apple's API has no 1-5 star rating — only love/dislike. Stars live solely in
# the local Music.app, so they're a native-engine capability. In api mode we
# refuse rather than reach into a possibly-different local account.
_STAR_NATIVE_ONLY = (
    "Error: Star ratings (get/set) need native mode and the local Music.app on "
    "macOS; api mode supports love/dislike only."
)


def _library_rate(
    action: str,
    track: str = "",
    artist: str = "",
    stars: int = 0,
) -> str:
    """Rate tracks. Actions: love, dislike, get, set. get/set require macOS."""
    action = action.lower().strip()

    if not track:
        return "Error: Provide track parameter"

    if action not in ("love", "dislike", "get", "set"):
        return f"Invalid action: {action}. Use: love, dislike, get, set"

    # Resolve track input (only single track supported for rating)
    resolved = _resolve_track(track, artist)
    if not resolved:
        return "Error: Could not resolve track"  # pragma: no cover  # unreachable: _resolve_track/_resolve_input always returns a non-empty list

    r = resolved[0]  # Only use first resolved track
    if r.error:
        return f"Error: {r.error}"

    track_name = ""
    track_artist = r.artist or artist

    # Handle based on input type
    if r.input_type == InputType.CATALOG_ID:
        catalog_id = r.value

        # Direct API rating for love/dislike
        if action in ("love", "dislike"):
            success, msg = _rate_song_api(catalog_id, action)
            if success:
                audit_log.log_action(
                    "rating",
                    {"track": f"catalog_id:{catalog_id}", "type": action, "method": "api"},
                )
                return f"Set '{action}' for song {catalog_id}"
            return f"Error: {msg}"

        # For get/set, need to look up track name for AppleScript
        if not APPLESCRIPT_AVAILABLE:
            return _STAR_NATIVE_ONLY
        try:
            headers = get_headers()
            response = requests.get(
                f"{BASE_URL}/catalog/{get_storefront()}/songs/{catalog_id}",
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code == 200:
                data = response.json().get("data", [])
                if data:
                    attrs = data[0].get("attributes", {})
                    track_name = attrs.get("name", "")
                    track_artist = attrs.get("artistName", "")
        except Exception:
            pass
        if not track_name:
            return f"Error: Could not find track info for catalog ID {catalog_id}"

    elif r.input_type == InputType.PERSISTENT_ID:
        # Persistent IDs can't be used for rating - need track name or catalog ID
        return f"Error: Persistent ID {r.value} not supported for rating - use track name or catalog ID"

    elif r.input_type in (InputType.NAME, InputType.JSON_OBJECT):
        track_name = r.value
        track_artist = r.artist or artist

    elif r.input_type == InputType.LIBRARY_ID:
        return (
            f"Error: Library ID {r.value} not supported for rating - use track name or catalog ID"
        )

    # Now we have track_name, handle each action
    if action == "get":
        if not APPLESCRIPT_AVAILABLE:
            return _STAR_NATIVE_ONLY
        success, rating_val = asc.get_rating(track_name, track_artist if track_artist else None)
        if success:
            s = rating_val // 20
            return f"{track_name}: {'★' * s}{'☆' * (5 - s)} ({rating_val}/100)"
        return f"Error: {_format_applescript_error(str(rating_val), 'get star rating')}"

    if action == "set":
        if not APPLESCRIPT_AVAILABLE:
            return _STAR_NATIVE_ONLY
        rating_val = max(0, min(5, stars)) * 20
        success, result = asc.set_rating(
            track_name, rating_val, track_artist if track_artist else None
        )
        if success:
            track_desc = f"{track_name} - {track_artist}" if track_artist else track_name
            audit_log.log_action(
                "rating",
                {"track": track_desc, "type": "set_stars", "value": stars, "method": "applescript"},
            )
            return f"Set {track_name} to {'★' * stars}{'☆' * (5 - stars)}"
        return f"Error: {_format_applescript_error(str(result), 'set star rating')}"

    # Love/dislike by name - try AppleScript first (native engine only; api
    # mode goes straight to the catalog-rating API path below).
    if _engine() == "native" and APPLESCRIPT_AVAILABLE:
        func = asc.love_track if action == "love" else asc.dislike_track
        success, result = func(track_name, track_artist if track_artist else None)
        if success:
            track_desc = f"{track_name} - {track_artist}" if track_artist else track_name
            audit_log.log_action(
                "rating",
                {"track": track_desc, "type": action, "method": "applescript"},
            )
            return result
        # If AS failed for an environmental reason (Music.app not running,
        # Automation denied, timeout), don't cascade to the API — the API
        # can't fix any of those, and its own "Developer token not found"
        # error would mislead the user. Logic-level failures (track not
        # found in library) should still cascade so the catalog API can
        # rate songs the user doesn't have downloaded.
        if asc.classify_error(result) != asc.ERROR_UNKNOWN:
            return f"Error: {_format_applescript_error(result, action + ' track')}"

    # API fallback for love/dislike (catalog rating path)
    search_term = f"{track_name} {track_artist}".strip() if track_artist else track_name
    songs = _search_catalog_songs(search_term, limit=5)

    for song in songs:
        attrs = song.get("attributes", {})
        song_name = attrs.get("name", "")
        song_artist = attrs.get("artistName", "")
        if _loose_contains(track_name, song_name):
            if not track_artist or _loose_contains(track_artist, song_artist):
                success, msg = _rate_song_api(song.get("id"), action)
                if success:
                    audit_log.log_action(
                        "rating",
                        {
                            "track": f"{song_name} by {song_artist}",
                            "type": action,
                            "method": "api_fallback",
                        },
                    )
                    return f"{action.capitalize()}d: {song_name} by {song_artist}"
                return f"Error: {msg}"

    return _catalog_miss_reason(f"Track not found: {track_name}")


# ============ CATALOG DETAILS ============


def _catalog_song_details(song_id: str) -> str:
    """Internal: Get song details."""
    try:
        headers = get_headers()
        response = requests.get(
            f"{BASE_URL}/catalog/{get_storefront()}/songs/{song_id}",
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()

        songs = data.get("data", [])
        if not songs:
            return "Song not found"

        attrs = songs[0].get("attributes", {})
        duration = format_duration(attrs.get("durationInMillis", 0)) or "Unknown"
        output = [
            f"Title: {attrs.get('name', 'Unknown')}",
            f"Artist: {attrs.get('artistName', 'Unknown')}",
            f"Album: {attrs.get('albumName', 'Unknown')}",
            f"Genre: {', '.join(attrs.get('genreNames', ['Unknown']))}",
            f"Duration: {duration}",
            f"Release Date: {attrs.get('releaseDate', 'Unknown')}",
            f"Explicit: {'Yes' if attrs.get('contentRating') == 'explicit' else 'No'}",
            f"ISRC: {attrs.get('isrc', 'N/A')}",
        ]

        return "\n".join(output)

    except requests.exceptions.RequestException as e:
        return _api_error(e)
    except (FileNotFoundError, ValueError) as e:
        return str(e)


def _catalog_artist_details(artist: str) -> str:
    """Internal: Get artist details."""
    if not artist:
        return "Error: Provide artist parameter"

    try:
        headers = get_headers()

        # Check if it's a catalog ID (all digits)
        if artist.isdigit():
            artist_id = artist
            # Look up artist details directly
            response = requests.get(
                f"{BASE_URL}/catalog/{get_storefront()}/artists/{artist_id}",
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code != 200:
                return f"Artist with ID {artist_id} not found"
            data = response.json().get("data", [])
            if not data:
                return f"Artist with ID {artist_id} not found"
            artist_data = data[0]
            attrs = artist_data.get("attributes", {})
        else:
            # Search for the artist by name
            search_response = requests.get(
                f"{BASE_URL}/catalog/{get_storefront()}/search",
                headers=headers,
                params={"term": artist, "types": "artists", "limit": 1},
                timeout=REQUEST_TIMEOUT,
            )
            search_response.raise_for_status()
            search_data = search_response.json()

            artists = search_data.get("results", {}).get("artists", {}).get("data", [])
            if not artists:
                return f"No artist found matching '{artist}'"

            artist_data = artists[0]
            artist_id = artist_data.get("id")
            attrs = artist_data.get("attributes", {})

        output = [
            f"Artist: {attrs.get('name', 'Unknown')}",
            f"Artist ID: {artist_id}",
            f"Genres: {', '.join(attrs.get('genreNames', ['Unknown']))}",
        ]

        # Get artist's albums
        albums_response = requests.get(
            f"{BASE_URL}/catalog/{get_storefront()}/artists/{artist_id}/albums",
            headers=headers,
            params={"limit": 10},
            timeout=REQUEST_TIMEOUT,
        )
        if albums_response.status_code == 200:
            albums_data = albums_response.json()
            albums = albums_data.get("data", [])
            if albums:
                output.append("\nRecent Albums:")
                for album in albums[:10]:
                    album_attrs = album.get("attributes", {})
                    name = album_attrs.get("name", "Unknown")
                    year = album_attrs.get("releaseDate", "")[:4]
                    album_id = album.get("id")
                    output.append(f"  - {name} ({year}) [catalog ID: {album_id}]")

        return "\n".join(output)

    except requests.exceptions.RequestException as e:
        return _api_error(e)
    except (FileNotFoundError, ValueError) as e:
        return str(e)


def _discover_charts(chart_type: str = "songs", storefront: str = "") -> str:
    """Internal: Get charts."""
    try:
        headers = get_headers()
        sf = storefront if storefront else get_storefront()
        response = requests.get(
            f"{BASE_URL}/catalog/{sf}/charts",
            headers=headers,
            params={"types": chart_type, "limit": 20},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()

        output = []
        results = data.get("results", {})

        for chart_name, chart_data in results.items():
            for chart in chart_data:
                chart_title = chart.get("name", chart_name)
                output.append(f"=== {chart_title} ===")

                for i, item in enumerate(chart.get("data", [])[:20], 1):
                    attrs = item.get("attributes", {})
                    name = attrs.get("name", "Unknown")
                    artist = attrs.get("artistName", "")
                    if artist:
                        output.append(f"  {i}. {name} - {artist}")
                    else:
                        output.append(f"  {i}. {name}")

        return "\n".join(output) if output else "No chart data available"

    except requests.exceptions.RequestException as e:
        return _api_error(e)
    except (FileNotFoundError, ValueError) as e:
        return str(e)


def _catalog_genres() -> str:
    """Internal: Get genres."""
    try:
        headers = get_headers()
        response = requests.get(
            f"{BASE_URL}/catalog/{get_storefront()}/genres",
            headers=headers,
            params={"limit": 50},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()

        output = []
        for genre in data.get("data", []):
            attrs = genre.get("attributes", {})
            name = attrs.get("name", "Unknown")
            genre_id = genre.get("id")
            output.append(f"{name} (ID: {genre_id})")

        return "\n".join(output) if output else "No genres found"

    except requests.exceptions.RequestException as e:
        return _api_error(e)
    except (FileNotFoundError, ValueError) as e:
        return str(e)


def _catalog_suggestions(term: str) -> str:
    """Internal: Get search suggestions."""
    try:
        headers = get_headers()
        response = requests.get(
            f"{BASE_URL}/catalog/{get_storefront()}/search/suggestions",
            headers=headers,
            params={"term": term, "kinds": "terms", "limit": 10},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()

        suggestions = data.get("results", {}).get("suggestions", [])
        output = ["=== Search Suggestions ==="]
        for suggestion in suggestions:
            if suggestion.get("kind") == "terms":
                search_term = suggestion.get("searchTerm", "")
                display = suggestion.get("displayTerm", search_term)
                output.append(f"  {display}")

        return "\n".join(output) if len(output) > 1 else "No suggestions found"

    except requests.exceptions.RequestException as e:
        return _api_error(e)
    except (FileNotFoundError, ValueError) as e:
        return str(e)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Catalog search", readOnlyHint=True, destructiveHint=False, openWorldHint=True
    )
)
def catalog(
    action: str = "search",
    query: str = "",
    types: str = "songs",
    album: str = "",
    artist: str = "",
    song_id: str = "",
    chart_type: str = "songs",
    term: str = "",
    isrcs: str = "",
    tracks: str = "",
    max_tracks: int = 0,
    limit: int = 15,
    offset: int = 0,
    format: str = "text",
    export: str = "none",
    full: bool = False,
    clean_only: Optional[bool] = None,
) -> str:
    """Apple Music catalog. Actions: search, resolve_isrc, match, album_tracks, album_details, song_details, artist_details, genres, suggestions.

    For turning a list of tracks into catalog IDs, prefer these over repeated `search`:

    - `resolve_isrc` (isrcs=...) when you have ISRCs — exact, 25 per request. This is
      the one that keeps a large import under Apple's rate limit.
    - `match` (tracks=...) for titles/artists — a DRY RUN that reports what each name
      would resolve to and how confident the match is, without adding anything. Costs
      one request per track, so it's capped at 25 by default (raise with `max_tracks`,
      not `limit`). Use it to review before a bulk add, especially since one title can
      map to several catalog editions.

    Both hand back IDs ready for `playlist(action="add", track=...)`.
    """
    action = action.lower().strip().replace("-", "_")

    if action == "search":
        # Try API first, fall back to UI search if no API token
        try:
            get_headers()  # Verify API access is available
            return _catalog_search(query, types, limit, format, export, full, clean_only)
        except (FileNotFoundError, ValueError):
            if APPLESCRIPT_AVAILABLE and query:
                ok, results, why = asc.ui_search_catalog(query)
                if ok and results:
                    asc.ui_clear_search()
                    lines = [f"=== UI Search: {query} (no API — results from Music.app) ===", ""]
                    for r in results:
                        artist_str = f" by {r['artist']}" if r.get("artist") else ""
                        type_str = f" ({r['type']})" if r.get("type") else ""
                        lines.append(f"{r['index']}. {r['name']}{type_str}{artist_str}")
                    lines.append("")
                    lines.append(
                        "Note: UI search shows Top Results only. For full catalog search, set up API access."
                    )
                    return "\n".join(lines)
                asc.ui_clear_search()
                if why:
                    return f"Error: UI search failed — {why}"
            return "Error: API token required for catalog search. Set up API access or use UI search on macOS."
    elif action == "resolve_isrc" or (action == "resolve" and (isrcs or not tracks)):
        return _catalog_resolve_isrc(isrcs or query, format, full)
    elif action in ("match", "match_tracks", "resolve_tracks", "resolve"):
        return _catalog_match_tracks(tracks or query, artist, max_tracks, format)
    elif action == "album_tracks":
        return _catalog_album_tracks(album, artist, limit, offset, format, export, full)
    elif action == "album_details":
        if not album:
            return "Error: album required for album_details"
        return _catalog_album_details(album, artist, format, export, full)
    elif action == "song_details":
        if not song_id:
            return "Error: song_id required for song_details"
        return _catalog_song_details(song_id)
    elif action == "artist_details":
        if not artist:
            return "Error: artist required for artist_details"
        return _catalog_artist_details(artist)
    elif action == "genres":
        return _catalog_genres()
    elif action == "suggestions":
        if not term:
            return "Error: term required for suggestions"
        return _catalog_suggestions(term)
    else:
        return f"Unknown action: {action}. Use: search, resolve_isrc, match, album_tracks, album_details, song_details, artist_details, genres, suggestions"


# ============ SYSTEM MANAGEMENT ============


@mcp.tool(
    annotations=ToolAnnotations(
        title="Settings & auth", readOnlyHint=False, destructiveHint=True, openWorldHint=True
    )
)
def config(
    action: str = "info",
    days_old: int = 0,
    preference: str = "",
    value: Optional[bool] = None,
    string_value: str = "",
    limit: int = 20,
    confirm: bool = False,
) -> str:
    """Config, cache, and authentication.

    Settings/cache actions: info, set-pref, list-storefronts, audit-log,
    clear-tracks, clear-exports, clear-audit-log.

    Auth actions (no terminal needed — just ask):
    - auth-status (or status): which tokens are active, expiry/auto-renew, what works, next step
    - signin: open a browser to sign in (any OS) and capture your session
    - logout: sign out — clears your user token + browser session so you can switch accounts (needs confirm=True)
    - reset: wipe ALL credentials for a clean slate or to drop a dev token for the web path; keeps your .p8 (needs confirm=True)
    """
    try:
        action = action.lower()

        # === SET PREFERENCE ===
        if action == "set-pref":
            bool_prefs = ["fetch_explicit", "clean_only", "auto_add"]
            string_prefs = ["storefront", "mode"]
            # Enum string prefs: only these values are accepted. `mode` is the
            # single engine knob (playback follows it): auto (best-of mix), native
            # (Music.app), safari (drive Safari, macOS), chrome (Chrome web player),
            # api (REST only). `web` stays accepted as a back-compat alias (the web
            # engine — Safari on macOS, Chrome off-mac). (Token storage is
            # auto-decided by platform, not a user pref.)
            enum_prefs = {
                "mode": ("auto", "native", "safari", "chrome", "web", "api"),
            }
            all_prefs = bool_prefs + string_prefs

            if not preference:
                return f"Error: set-pref requires 'preference' parameter. Valid: {', '.join(all_prefs)}"

            if preference not in all_prefs:
                return f"Error: preference must be one of: {', '.join(all_prefs)}"

            # Determine the value to set
            if preference in string_prefs:
                if not string_value:
                    hint = (
                        f" (one of: {', '.join(enum_prefs[preference])})"
                        if preference in enum_prefs
                        else " (e.g., string_value='gb')"
                    )
                    return f"Error: '{preference}' requires 'string_value' parameter{hint}"
                pref_value = string_value.lower()
                if preference in enum_prefs and pref_value not in enum_prefs[preference]:
                    return (
                        f"Error: '{preference}' must be one of: "
                        f"{', '.join(enum_prefs[preference])}"
                    )
            else:
                if value is None:
                    return f"Error: '{preference}' requires 'value' parameter (true or false)"
                pref_value = value

            # Load current config. Deep-copy so we never mutate the shared cached
            # dict (load_config returns the cached object) — a failed write must
            # not poison the in-memory cache with unsaved state.
            import copy

            from .auth import load_config, get_config_dir as get_auth_config_dir

            config = copy.deepcopy(load_config())

            # Update preferences
            if "preferences" not in config:
                config["preferences"] = {}
            old_value = config.get("preferences", {}).get(preference)
            config["preferences"][preference] = pref_value

            # Save atomically (temp + os.replace) so a concurrent reader never
            # sees a half-written file.
            config_file = get_auth_config_dir() / "config.json"
            tmp = config_file.with_suffix(".json.tmp")
            with open(tmp, "w") as f:
                json.dump(config, f, indent=2)
            os.replace(tmp, config_file)

            audit_log.log_action(
                "set_preference",
                {"preference": preference, "old_value": old_value, "new_value": pref_value},
            )

            return f"✓ Updated: {preference} = {pref_value}\n\nUse config() to see current preferences."

        # === LIST STOREFRONTS ===
        if action == "list-storefronts":
            try:
                headers = get_headers()
                response = requests.get(
                    f"{BASE_URL}/storefronts",
                    headers=headers,
                    timeout=REQUEST_TIMEOUT,
                )
                response.raise_for_status()
                data = response.json()

                output = ["=== Available Storefronts ===", ""]
                for storefront in data.get("data", []):
                    sf_id = storefront.get("id", "")
                    attrs = storefront.get("attributes", {})
                    name = attrs.get("name", "Unknown")
                    output.append(f"  {sf_id}: {name}")

                output.append("")
                output.append(f"Current: {get_storefront()}")
                output.append(
                    "Set via: config(action='set-pref', preference='storefront', string_value='xx')"
                )
                return "\n".join(output)
            except Exception as e:
                return f"Error listing storefronts: {e}"

        # === CLEAR TRACK CACHE ===
        if action == "clear-tracks":
            track_cache = get_track_cache()
            num_entries = len(track_cache._cache)
            track_cache.clear()
            return f"✓ Cleared track metadata cache ({num_entries} entries removed)"

        # === CLEAR EXPORT FILES ===
        if action == "clear-exports":
            cache_dir = get_cache_dir()
            if not cache_dir.exists():
                return "Cache directory doesn't exist"

            export_files = list(cache_dir.glob("*.csv")) + list(cache_dir.glob("*.json"))
            # Don't delete track_cache.json
            export_files = [f for f in export_files if f.name != "track_cache.json"]

            if not export_files:
                return "No export files in cache"

            now = time.time()
            cutoff = now - (days_old * 86400) if days_old > 0 else now + 1
            deleted = []
            kept = []
            total_size = 0

            for f in export_files:
                file_size = f.stat().st_size
                if days_old == 0 or f.stat().st_mtime < cutoff:
                    deleted.append(f.name)
                    total_size += file_size
                    f.unlink()
                else:
                    kept.append(f.name)

            if total_size < 1024:
                size_str = f"{total_size} bytes"
            elif total_size < 1024 * 1024:
                size_str = f"{total_size / 1024:.1f} KB"
            else:
                size_str = f"{total_size / (1024 * 1024):.1f} MB"

            output = [f"✓ Deleted: {len(deleted)} export files ({size_str})"]
            if kept:
                output.append(f"Kept: {len(kept)} files (newer than {days_old} days)")
            return "\n".join(output)

        # === AUDIT LOG ===
        if action == "audit-log":
            entries = audit_log.get_recent_entries(limit=limit)
            return audit_log.format_entries_for_display(entries, limit=limit)

        # === CLEAR AUDIT LOG ===
        if action == "clear-audit-log":
            entries = audit_log.get_recent_entries(limit=1000)
            if audit_log.clear_audit_log():
                return f"✓ Cleared audit log ({len(entries)} entries removed)"
            return "Error: Failed to clear audit log"

        # === INFO (DEFAULT) ===
        if action == "info":
            output = ["=== System Info ===", ""]

            # User Preferences
            prefs = get_user_preferences()
            output.append("Preferences (set via config(action='set-pref', ...)):")
            output.append(
                f"  storefront: {prefs['storefront']} (list: config(action='list-storefronts'))"
            )
            output.append(f"  fetch_explicit: {prefs['fetch_explicit']}")
            output.append(f"  clean_only: {prefs['clean_only']}")
            output.append(f"  auto_add: {prefs['auto_add']}")
            output.append("")

            # Track Metadata Cache
            track_cache = get_track_cache()
            num_tracks = len(track_cache._cache)
            if track_cache.cache_file.exists():
                cache_size = track_cache.cache_file.stat().st_size
                if cache_size < 1024:
                    size_str = f"{cache_size}B"
                elif cache_size < 1024 * 1024:
                    size_str = f"{cache_size / 1024:.0f}KB"
                else:
                    size_str = f"{cache_size / (1024 * 1024):.1f}MB"
                output.append(f"Track Metadata Cache: {num_tracks} entries, {size_str}")
            else:
                output.append(f"Track Metadata Cache: {num_tracks} entries (not yet saved)")
            output.append(f"  Location: {track_cache.cache_file}")
            output.append(f"  Clear: config(action='clear-tracks')")
            output.append("")

            # Export Files
            cache_dir = get_cache_dir()
            if cache_dir.exists():
                export_files = list(cache_dir.glob("*.csv")) + list(cache_dir.glob("*.json"))
                # Don't count track_cache.json
                export_files = [f for f in export_files if f.name != "track_cache.json"]

                if export_files:
                    export_files = sorted(
                        export_files, key=lambda f: f.stat().st_mtime, reverse=True
                    )
                    total_size = sum(f.stat().st_size for f in export_files)
                    total_str = (
                        f"{total_size / 1024:.0f}KB"
                        if total_size < 1024 * 1024
                        else f"{total_size / (1024 * 1024):.1f}MB"
                    )
                    output.append(f"Export Files: {len(export_files)} files, {total_str}")

                    now = time.time()
                    for f in export_files[:10]:  # Show most recent 10
                        file_size = f.stat().st_size
                        age_days = (now - f.stat().st_mtime) / 86400

                        if file_size < 1024:
                            size_str = f"{file_size}B"
                        elif file_size < 1024 * 1024:
                            size_str = f"{file_size / 1024:.0f}KB"
                        else:
                            size_str = f"{file_size / (1024 * 1024):.1f}MB"

                        age_str = (
                            f"{age_days * 24:.0f}h ago" if age_days < 1 else f"{age_days:.0f}d ago"
                        )
                        output.append(f"  {f.name} ({size_str}, {age_str})")

                    if len(export_files) > 10:
                        output.append(f"  ... and {len(export_files) - 10} more")
                    output.append(f"  Clear: config(action='clear-exports')")
                else:
                    output.append("Export Files: None")
            else:
                output.append("Export Files: Cache directory doesn't exist yet")

            output.append("")

            # Audit Log
            log_path = audit_log.get_audit_log_path()
            if log_path.exists():
                log_size = log_path.stat().st_size
                if log_size < 1024:
                    log_size_str = f"{log_size}B"
                elif log_size < 1024 * 1024:
                    log_size_str = f"{log_size / 1024:.0f}KB"
                else:
                    log_size_str = f"{log_size / (1024 * 1024):.1f}MB"
                entries = audit_log.get_recent_entries(limit=5)
                output.append(f"Audit Log: {len(entries)}+ entries, {log_size_str}")
            else:
                output.append("Audit Log: Empty (no operations logged yet)")
            output.append(f"  Location: {log_path}")
            output.append(f"  View: config(action='audit-log')")
            output.append(f"  Clear: config(action='clear-audit-log')")
            output.append("")

            # Library Snapshots
            snap_dir = _get_snapshot_dir()
            baseline = sorted(snap_dir.glob("snapshot-*-baseline.json"))
            diff_files = sorted(snap_dir.glob("diff-*.json"), reverse=True)
            if baseline:
                output.append(f"Library Baseline: {baseline[-1].name}")
                output.append(f"  Diffs recorded: {len(diff_files)}")
            else:
                output.append("Library Baseline: None")
            output.append(f"  Diff/take: library(action='snapshot')")
            output.append(f"  Reset: library(action='snapshot', query='new')")
            output.append(f"  History: library(action='snapshot', query='history')")

            return "\n".join(output)

        # === AUTH (status / signin / logout / reset) ===
        if action in ("auth-status", "auth_status", "status", "signin", "login", "logout", "reset"):
            sub = "status" if action in ("auth-status", "auth_status", "status") else action
            return _auth_action(sub, confirm)

        # === UNKNOWN ACTION ===
        valid_actions = (
            "info, set-pref, list-storefronts, audit-log, clear-tracks, clear-exports, "
            "clear-audit-log, status, signin, logout, reset"
        )
        return f"Error: Unknown action '{action}'. Valid: {valid_actions}"

    except Exception as e:
        return f"Error: {str(e)}"


def _config_auth_status(mutation_status: "Optional[str]" = None) -> str:
    """Check if authentication tokens are valid and API is accessible.

    ``mutation_status`` (from ``amp_api.session_status()``) is rendered as a
    separate line when supplied. It matters because catalog adds, playlist edits,
    and ratings go through ``amp-api.music.apple.com`` with the harvested web
    token, NOT the ``api.music.apple.com`` read path tested below — so the two can
    disagree (reads fine, writes 401). The caller probes it once and passes it in.
    """
    dev_info = developer_token_info()
    user_present = has_user_token()

    status = []

    # Developer token. Two sources: a GENERATED (Apple Developer, 180-day) token
    # the user renews themselves, or the HARVESTED web-player token that the tool
    # auto-refreshes. Only the generated one needs a renewal nudge.
    if dev_info is not None and can_generate_developer_token():
        status.append(
            "Developer Token: OK (generated — auto-renews ≤30 days out, no action needed)"
        )
    elif dev_info is not None:
        try:
            days_left = (dev_info.get("expires", 0) - time.time()) / 86400
            days = round(days_left)
            if days_left < 0:
                status.append("Developer Token: EXPIRED — run `applemusic-mcp login --dev`")
            elif days_left <= 7:
                status.append(
                    f"Developer Token: ⚠️ EXPIRES IN {days} DAY(S) — "
                    "run `applemusic-mcp login --dev` now"
                )
            elif days_left <= 30:
                status.append(
                    f"Developer Token: expires in {days} days — "
                    "run `applemusic-mcp login --dev` soon"
                )
            else:
                status.append(f"Developer Token: OK ({days} days remaining, generated)")
        except Exception:
            status.append("Developer Token: ERROR reading token")
    elif has_any_developer_token():
        # No generated token, but a harvestable web-player token is available.
        status.append("Developer Token: OK (web player — auto-refreshes, no action needed)")
    else:
        status.append(
            "Developer Token: MISSING — run `applemusic-mcp login` (browser, no account) "
            "or `applemusic-mcp login --dev` (Apple Developer)"
        )

    # User token (media-user-token). Persists; re-auth = signin (browser) or authorize.
    if user_present:
        status.append(
            "Music User Token: OK (persists; re-auth with `applemusic-mcp login` if it fails)"
        )
    else:
        status.append(
            "Music User Token: MISSING — run `applemusic-mcp login` (browser) "
            "or `applemusic-mcp login --dev` (dev token)"
        )

    # Test API connection
    if has_any_developer_token() and user_present:
        try:
            headers = get_headers()
            response = requests.get(
                f"{BASE_URL}/me/library/playlists",
                headers=headers,
                params={"limit": 1},
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code == 200:
                status.append("API Connection: OK")
            elif response.status_code in (401, 403):
                status.append(
                    "API Connection: UNAUTHORIZED — your session expired. Re-run "
                    "`applemusic-mcp login` (browser) or `applemusic-mcp login --dev` (dev token)."
                )
            elif response.status_code == 429:
                amp_api.note_status(429, amp_api.API)
                status.append(f"API Connection: RATE-LIMITED (429) — {_THROTTLED_REASON}")
            else:
                status.append(f"API Connection: FAILED ({response.status_code})")
        except Exception as e:
            status.append(f"API Connection: ERROR - {str(e)}")

    # Mutation path (catalog add, playlist edit, rate): amp-api + the harvested
    # web token. Reported separately because it can fail while the read path above
    # is fine — that mismatch is what makes "add works" claims untrustworthy.
    if mutation_status is not None:
        status.append(
            {
                "ok": "Web fallback writes (amp-api): OK",
                "expired": (
                    "Web fallback writes (amp-api): UNAUTHORIZED — the web-player "
                    "session expired. Re-run `applemusic-mcp login`."
                ),
                "throttled": (
                    f"Web fallback writes (amp-api): RATE-LIMITED (429) — {_THROTTLED_REASON}"
                ),
                "error": (
                    "Web fallback writes (amp-api): ERROR reaching amp-api "
                    "(check your connection)."
                ),
            }.get(mutation_status, f"Web fallback writes (amp-api): {mutation_status}")
        )

    return "\n".join(status)


# =============================================================================
# Library Snapshot Manager
# =============================================================================
# Stores one full baseline snapshot + lightweight diffs from baseline.
# The baseline persists forever. Diffs are tiny (just the changes).
# All accessed via library(action="snapshot|diff|history").

_DIFF_MAX_KEEP = 50


def _get_snapshot_dir() -> Path:
    """Get the snapshot storage directory."""
    snap_dir = paths.cache_dir() / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    return snap_dir


def _get_baseline() -> Optional[tuple[dict, Path]]:
    """Load the baseline snapshot."""
    snap_dir = _get_snapshot_dir()
    baselines = list(snap_dir.glob("snapshot-*-baseline.json"))
    if not baselines:
        return None
    path = sorted(baselines)[-1]  # most recent baseline
    try:
        return json.loads(path.read_text()), path
    except (json.JSONDecodeError, OSError):
        return None


def _save_baseline(snapshot: dict) -> Path:
    """Save a new baseline snapshot, removing any previous baselines."""
    snap_dir = _get_snapshot_dir()

    # Remove old baselines to prevent accumulation
    for old in snap_dir.glob("snapshot-*-baseline.json"):
        old.unlink()

    ts = time.strftime("%Y%m%d-%H%M%S")
    path = snap_dir / f"snapshot-{ts}-baseline.json"
    path.write_text(json.dumps(snapshot, indent=2, default=str))
    return path


def _save_diff(diff: dict) -> Path:
    """Save a diff from baseline. Rotates old diffs."""
    snap_dir = _get_snapshot_dir()
    ts = time.strftime("%Y%m%d-%H%M%S")
    path = snap_dir / f"diff-{ts}.json"
    path.write_text(json.dumps(diff, indent=2, default=str))

    # Rotate: keep last _DIFF_MAX_KEEP diffs
    diff_files = sorted(snap_dir.glob("diff-*.json"))
    if len(diff_files) > _DIFF_MAX_KEEP:
        for f in diff_files[: len(diff_files) - _DIFF_MAX_KEEP]:
            f.unlink()

    return path


def _format_snapshot_summary(snapshot: dict) -> list[str]:
    """Format a snapshot as human-readable lines."""
    playlist_count = len(snapshot.get("playlists", {}))
    total_tracks = sum(len(t) for t in snapshot.get("playlists", {}).values())
    pb = snapshot.get("playback", {})

    lines = [
        f"Library: {snapshot.get('track_count', '?')} tracks",
        f"Playlists: {playlist_count} ({total_tracks} total playlist tracks)",
        f"Player: {pb.get('player_state', '?')}, vol {pb.get('volume', '?')}, "
        f"shuffle {'on' if pb.get('shuffle') else 'off'}, repeat {pb.get('repeat', '?')}",
    ]
    if pb.get("current_track"):
        lines.append(f"Now playing: {pb['current_track']} - {pb.get('current_artist', '?')}")
    lines.append("")
    for name, tracks in sorted(snapshot.get("playlists", {}).items()):
        lines.append(f"  {name}: {len(tracks)} tracks")
    return lines


def _format_diff(diff: dict, reference: str = "baseline") -> list[str]:
    """Format a diff as human-readable lines."""
    if diff.get("is_clean"):
        pb_note = ""
        if diff.get("playback_changes"):
            parts = [
                f"{k}: {v['before']} -> {v['after']}" for k, v in diff["playback_changes"].items()
            ]
            pb_note = f" (playback: {', '.join(parts)})"
        return [f"No library changes since {reference}.{pb_note}"]

    lines = []
    if diff.get("track_count_change"):
        sign = "+" if diff["track_count_change"] > 0 else ""
        lines.append(f"Library tracks: {sign}{diff['track_count_change']}")
    if diff.get("playback_changes"):
        for k, v in diff["playback_changes"].items():
            lines.append(f"  {k}: {v['before']} -> {v['after']}")
    if diff.get("playlists_added"):
        lines.append(f"Playlists added: {', '.join(diff['playlists_added'])}")
    if diff.get("playlists_removed"):
        lines.append(f"Playlists removed: {', '.join(diff['playlists_removed'])}")
    if diff.get("playlists_changed"):
        for name, changes in diff["playlists_changed"].items():
            if changes.get("added"):
                lines.append(f"  {name}: +{len(changes['added'])} tracks")
                for t in changes["added"][:5]:
                    lines.append(f"    + {t}")
                if len(changes["added"]) > 5:
                    lines.append(f"    ... and {len(changes['added']) - 5} more")
            if changes.get("removed"):
                lines.append(f"  {name}: -{len(changes['removed'])} tracks")
                for t in changes["removed"][:5]:
                    lines.append(f"    - {t}")
                if len(changes["removed"]) > 5:
                    lines.append(f"    ... and {len(changes['removed']) - 5} more")
    return lines


def _library_snapshot_default() -> str:
    """Default snapshot action: diff from baseline, or take baseline if none exists."""
    existing = _get_baseline()

    if not existing:
        # No baseline — take one
        return _library_snapshot_new()

    # Diff from baseline
    baseline_data, baseline_path = existing

    ok, current = asc.library_snapshot()
    if not ok:
        return f"Error: Failed to read current state: {current.get('error', 'unknown')}"

    diff = asc.library_diff(baseline_data, current)

    output = [f"=== Library Snapshot (vs {baseline_path.name}) ===", ""]
    output.extend(_format_diff(diff, baseline_path.name))
    output.append("")
    output.extend(_format_snapshot_summary(current))
    output.append("")
    output.append("Reset baseline: library(action='snapshot', query='new')")
    output.append("View history: library(action='snapshot', query='history')")
    return "\n".join(output)


def _library_snapshot_new() -> str:
    """Take a new baseline snapshot."""
    ok, snapshot = asc.library_snapshot()
    if not ok:
        return f"Error: Failed to take snapshot: {snapshot.get('error', 'unknown')}"

    existing = _get_baseline()
    if existing:
        baseline_data, baseline_path = existing
        diff = asc.library_diff(baseline_data, snapshot)
        if not diff["is_clean"]:
            diff["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            diff["from_baseline"] = baseline_path.name
            _save_diff(diff)

    path = _save_baseline(snapshot)
    output = [
        f"=== {'New' if existing else 'Initial'} Library Baseline ===",
        f"Saved: {path.name}",
        "",
    ]
    output.extend(_format_snapshot_summary(snapshot))
    return "\n".join(output)


def _library_history() -> str:
    """Show history of library changes (saved diffs from baseline)."""
    snap_dir = _get_snapshot_dir()
    baseline = _get_baseline()
    diff_files = sorted(snap_dir.glob("diff-*.json"), reverse=True)

    output = ["=== Library History ===", ""]

    if baseline:
        _, baseline_path = baseline
        size = baseline_path.stat().st_size
        size_str = f"{size / 1024:.0f}KB" if size >= 1024 else f"{size}B"
        output.append(f"Baseline: {baseline_path.name} ({size_str})")
    else:
        output.append("Baseline: None — take one with library(action='snapshot')")

    if not diff_files:
        output.append("No changes recorded yet.")
        return "\n".join(output)

    output.append(f"Changes recorded: {len(diff_files)}")
    output.append("")

    for f in diff_files[:20]:
        try:
            diff = json.loads(f.read_text())
            # Build one-line summary
            parts = []
            if diff.get("track_count_change"):
                parts.append(
                    f"tracks {'+' if diff['track_count_change'] > 0 else ''}{diff['track_count_change']}"
                )
            if diff.get("playlists_added"):
                parts.append(f"+{len(diff['playlists_added'])} playlists")
            if diff.get("playlists_removed"):
                parts.append(f"-{len(diff['playlists_removed'])} playlists")
            if diff.get("playlists_changed"):
                parts.append(f"{len(diff['playlists_changed'])} playlists modified")
            summary = ", ".join(parts) if parts else "no library changes"
            ts = diff.get("timestamp", f.stem.replace("diff-", ""))
            output.append(f"  {ts}: {summary}")
        except (json.JSONDecodeError, OSError):
            output.append(f"  {f.name}: (unreadable)")

    return "\n".join(output)


def _library_snapshot_list() -> str:
    """List all saved snapshot and diff files."""
    snap_dir = _get_snapshot_dir()
    baselines = sorted(snap_dir.glob("snapshot-*-baseline.json"), reverse=True)
    diffs = sorted(snap_dir.glob("diff-*.json"), reverse=True)

    if not baselines and not diffs:
        return "No snapshots saved yet. Take one: library(action='snapshot')"

    def _size_str(path: Path) -> str:
        size = path.stat().st_size
        if size < 1024:
            return f"{size}B"
        elif size < 1024 * 1024:
            return f"{size / 1024:.0f}KB"
        return f"{size / (1024 * 1024):.1f}MB"

    output = ["=== Snapshot Files ===", ""]
    for b in baselines:
        output.append(f"  {b.name} ({_size_str(b)}) [BASELINE]")
    for d in diffs:
        output.append(f"  {d.name} ({_size_str(d)})")

    output.append("")
    output.append(f"Location: {snap_dir}")
    output.append("Delete: library(action='snapshot', query='delete FILENAME')")
    return "\n".join(output)


def _library_snapshot_delete(filename: str) -> str:
    """Delete a specific snapshot or diff file."""
    if not filename:
        return "Error: provide a filename. See library(action='snapshot', query='list')"

    # Sanitize: strip path separators to prevent directory traversal
    safe_name = Path(filename).name
    if not safe_name:
        return f"Error: Invalid filename: {filename}"

    snap_dir = _get_snapshot_dir()
    path = snap_dir / safe_name
    if not path.exists():
        path = snap_dir / f"{safe_name}.json"
    if not path.exists():
        return f"Error: File not found: {filename}"

    if "-baseline" in path.name:
        return "Error: Cannot delete baseline. Use query='new' to replace it instead."

    path.unlink()
    return f"Deleted: {path.name}"


# =============================================================================
# Auth — conversational sign-in / status / switch-account (no terminal needed)
# =============================================================================
# Local stdio MCP servers can't use Claude Code's native /mcp OAuth chip (that's
# for remote HTTP servers), so the idiomatic "smooth auth" is a tool the
# assistant drives: check status, sign in, switch accounts, reset.


def _clear_credentials(*keys: str) -> tuple[list[str], list[str]]:
    """Forget the named SECRET keys (token names, no .json) from BOTH the keychain
    and disk. Returns (removed, failed) — ``failed`` lists secrets that were
    present but couldn't be fully cleared (e.g. a locked keychain), so logout/
    reset never report success while a credential survives."""
    removed: list[str] = []
    failed: list[str] = []
    for key in keys:
        present = secret_get(key) is not None
        cleared = secret_delete(key)
        if not cleared:
            failed.append(key)
        elif present:
            removed.append(key)
    return removed, failed


def _auth_action(action: str = "status", confirm: bool = False) -> str:
    """Auth management, exposed through ``config`` (status/signin/logout/reset)."""
    action = action.lower().strip().replace("-", "_")

    if action in ("status", "info"):
        mode = (get_user_preferences().get("mode") or "auto").lower()
        # The "add/playlist/rate work" claim must be backed by the SAME path those
        # operations use (amp-api + web token), not just token presence — otherwise
        # status can promise writes that 401. Probe it once; pass it to the body
        # renderer and use it for the verdict.
        tokens_present = has_any_developer_token() and has_user_token()
        # APPLEMUSIC_FORCE_TOKENLESS disables the API write path regardless of
        # tokens. It's a test flag that's easy to leave set, so never green-lit.
        forced = _forced_tokenless()
        # The verdict must reflect the rail writes ACTUALLY take, not just the
        # web-session probe: on macOS writes go native (Music.app), so a stale web
        # session must not be reported as "your writes are broken."
        rail = _write_rail("add")
        mut = amp_api.session_status() if (tokens_present and not forced) else None
        body = _config_auth_status(mut)
        if forced:
            body = f"⚠️ {_FORCED_TOKENLESS_MSG}\n\n{body}"

        if forced:
            nxt = (
                "⚠️ API catalog/library adds are DISABLED — APPLEMUSIC_FORCE_TOKENLESS=1 "
                "is set; unset it and restart the server. (On macOS, local Music.app "
                "playlist edits and ratings still work; reads still work.)"
            )
        elif rail == "native":
            # macOS: playlist & library edits and ratings run locally through
            # Music.app, independent of the web session. Catalog add still needs API.
            nxt = (
                "✅ Ready — on macOS, playlist & library edits and ratings run locally "
                "through Music.app."
            )
            if not _can_use_library_api():
                nxt += " Adding catalog tracks needs sign-in: config(action='signin')."
            elif mut and mut != "ok":
                nxt += " (Web player session looks degraded; playback/queue may need re-auth.)"
        elif rail == "sanctioned":
            # off-macOS with a developer token: writes go through the official API.
            nxt = "✅ Ready — writes go through the Apple Music API (developer token)."
        elif not tokens_present:
            nxt = "⚠️ Not signed in yet — run config(action='signin') to finish setup."
        elif mut == "ok":
            nxt = "✅ Ready — catalog, playlists, add, and rate all work."
        elif mut == "throttled":
            nxt = f"⚠️ Rate-limited (429) right now — auth looks fine. {_THROTTLED_REASON}"
        elif mut == "expired":
            nxt = (
                "⚠️ Reads may work, but add/playlist/rate are unauthorized — the "
                "web-player session expired. Re-run config(action='signin')."
            )
        else:
            nxt = (
                "⚠️ Couldn't confirm the add/playlist/rate path (amp-api unreachable). "
                "Check your connection, then retry."
            )
        # Show which rail writes will actually take (independent of the playback mode).
        if forced:
            write_rail = (
                f"{_RAIL_LABELS.get(rail, 'unknown')} for local edits; "
                "API catalog/library add DISABLED by APPLEMUSIC_FORCE_TOKENLESS"
            )
        else:
            write_rail = _RAIL_LABELS.get(rail, "unknown")
        # Show the engines `mode` resolves to, so the user can see what auto picks
        # (e.g. playback=native, queue=safari on macOS) and where playback will land.
        engines = f"Engines: playback={_playback_engine()}, queue={_queue_engine()}"
        return f"{body}\nMode: {mode}\n{engines}\nWrites: {write_rail}\n\n{nxt}"

    if action in ("signin", "login"):
        # macOS default: harvest the token from a signed-in Safari (zero-install) —
        # the same default as the CLI `login`. Only fall back to Chrome (with
        # guidance), and only try Chrome at all when Playwright is actually present.
        if APPLESCRIPT_AVAILABLE:
            from . import safari
            from .auth import save_user_token

            try:
                ok, res = safari.media_user_token()
            except Exception as exc:  # noqa: BLE001
                ok, res = False, str(exc)
            if ok:
                save_user_token(res)
                return (
                    "✓ Signed in via Safari — no Chrome needed. Playback uses Music.app; "
                    "for the cross-platform Chrome web player, "
                    "`pip install 'applemusic-mcp[browser]'`."
                )
            from . import browser

            if not browser.is_available():
                return (
                    f"{res}\n\nTo finish on macOS without Chrome: enable Safari → Settings → "
                    'Advanced → "Show features for web developers", then Develop → "Allow '
                    'JavaScript from Apple Events", sign into Apple Music at music.apple.com '
                    "in Safari, and ask me to sign in again. Or install the Chrome web "
                    "player: `pip install 'applemusic-mcp[browser]'`, then ask again."
                )
            # Chrome/Playwright is installed — fall through to the Chrome flow.

        from . import browser

        try:
            ok, msg = browser.signin_interactive()
        except Exception as exc:  # noqa: BLE001
            return f"Error: {exc}"
        if ok:
            return f"✓ {msg}"
        if msg == "still-waiting":
            return (
                "A Chrome window is open on music.apple.com — finish signing in "
                "(Apple ID + 2FA), then run config(action='signin') again and I'll capture "
                "your session."
            )
        return (
            f"Error: {msg}\n\nBrowser sign-in needs Google Chrome installed (for "
            "full-length playback) and a desktop session — not a headless server. "
            "The browser engine downloads itself automatically on first use."
        )

    if action == "logout":
        if not confirm:
            return (
                "This signs you out: it clears your Apple Music user token and the browser "
                "session (your library and playlists are untouched). Afterwards, run "
                "config(action='signin') to sign back in — with a different account if you "
                "like. To proceed, call config(action='logout', confirm=True)."
            )
        removed, failed = _clear_credentials("music_user_token", "harvested_token")
        from . import browser

        browser.clear_session()
        audit_log.log_action("logout", {"removed": removed, "failed": failed})
        if failed:
            return (
                f"⚠️ Partly signed out — couldn't clear {', '.join(failed)} "
                "(keychain may be locked). Unlock it and try again."
            )
        return (
            "✓ Signed out — user token and browser session cleared. Run "
            "config(action='signin') to sign in (you can switch accounts now)."
        )

    if action == "reset":
        if not confirm:
            return (
                "This wipes ALL credentials: developer token, config.json, user token, web "
                "token, and the browser session. Your downloaded .p8 key file is left in "
                "place. Use it for a clean slate, or to drop an Apple Developer token and "
                "fall back to the free web path. To proceed, call "
                "config(action='reset', confirm=True)."
            )
        removed, failed = _clear_credentials(
            "developer_token", "music_user_token", "harvested_token"
        )
        # config.json is plain config (team_id/key_id/prefs), not a keychain secret.
        cfg_file = get_config_dir() / "config.json"
        if cfg_file.exists():
            try:
                cfg_file.unlink()
                removed.append("config.json")
            except OSError:
                failed.append("config.json")
        from . import browser

        browser.clear_session()
        audit_log.log_action("reset", {"removed": removed, "failed": failed})
        if failed:
            return (
                f"⚠️ Partial reset — couldn't clear {', '.join(failed)} "
                "(keychain may be locked). Unlock it and try again."
            )
        return (
            "✓ Reset complete. Run config(action='signin') for the free web path, or set up an "
            "Apple Developer token with `applemusic-mcp login --dev`."
        )

    return f"Unknown action: {action}. Use: status, signin, logout, reset"


# =============================================================================
# Up Next / play queue (browser web player — cross-platform)
# =============================================================================
# The personal playback queue is MusicKit-instance state (no REST endpoint), so
# these route through the browser engine that drives the web player's MusicKit.
# Cross-platform; needs a signed-in browser session (`applemusic-mcp login`).


def _queue_resolve_catalog_id(track: str, artist: str = "") -> Optional[str]:
    """Resolve a track param to a catalog song id: a bare catalog id passes
    through; a name is resolved via catalog search."""
    t = (track or "").strip()
    if not t:
        return None
    if t.isdigit():
        return t
    songs = amp_api.search_catalog_songs(f"{t} {artist}".strip(), 1)
    return songs[0]["id"] if songs else None


def _format_queue(data: dict, limit: Optional[int] = None) -> str:
    items = data.get("items", [])
    autoplay = " · autoplay on" if data.get("autoplay") else ""
    if not items:
        return f"Up Next is empty{autoplay}"
    pos = data.get("position", -1)
    # When limited, center the window on the current item so the relevant part of a
    # long queue shows (the thing just played/jumped to, plus what's coming).
    shown = items
    if limit is not None and len(items) > limit:
        start = max(0, pos)
        shown = items[start : start + limit]
        more = len(items) - len(shown)
        head = f"Up Next ({len(items)} item(s){autoplay}; showing {len(shown)}, +{more} more):"
    else:
        head = f"Up Next ({len(items)} item(s){autoplay}):"
    lines = [head]
    for it in shown:
        marker = "▶ " if it["index"] == pos else "  "
        artist = f" — {it['artist']}" if it.get("artist") else ""
        lines.append(f"{marker}{it['index']}. {it['name']}{artist}")
    return "\n".join(lines)


def _queue_after(wp, header: str, top_n: int = 6) -> str:
    """After a queue mutation, append the resulting Up Next (windowed to top_n) so
    the caller sees the effect without a follow-up `list` call. ``wp`` is the
    resolved web-player module (safari_player or browser)."""
    ok, data = wp.queue_list()
    if not ok or not isinstance(data, dict):
        return header
    return f"{header}\n\n{_format_queue(data, limit=top_n)}"


def _queue_current_name(wp) -> str:
    """Name of the now-current Up Next item (the thing a jump landed on), or ''."""
    ok, data = wp.queue_list()
    if not ok or not isinstance(data, dict):
        return ""
    pos = data.get("position", -1)
    for it in data.get("items", []):
        if it.get("index") == pos:
            artist = f" — {it['artist']}" if it.get("artist") else ""
            return f"{it.get('name', '')}{artist}"
    return ""


@mcp.tool(
    annotations=ToolAnnotations(
        title="Up Next queue", readOnlyHint=False, destructiveHint=False, openWorldHint=True
    )
)
def queue(
    action: str = "list",
    track: str = "",
    artist: str = "",
    index: int = -1,
    enabled: Optional[bool] = None,
    engine: str = "",
) -> str:
    """The Up Next play queue — the web player's own MusicKit state (the same Up Next
    you see in the player). It runs on a web engine: Safari on macOS (no Chrome
    needed) or Chrome elsewhere, picked by your `mode` (auto/safari/chrome) or a
    per-call `engine=` ('safari' | 'chrome'). Using the queue makes it the active
    playback engine, so transport controls reach it. Native (Music.app) mode has no
    Up Next — set mode to safari/chrome or pass engine='safari'.

    Actions:
    - `list` — show Up Next (▶ marks the current item; indices are 0-based)
    - `set` — replace the whole queue in order, one call (`track`=comma/newline-separated ids or names)
    - `play_next` — insert a track right after the current one (`track`=name or catalog id, optional `artist`)
    - `play_last` — append a track to the end of Up Next
    - `remove` — remove the item at `index` (can't remove the currently-playing item — jump away first)
    - `clear` — empty the queue
    - `jump` — jump playback to a track: by `track` (name or catalog id — drift-proof, preferred since Up Next auto-advances) or by `index`
    - `autoplay` — set Autoplay (∞: keep playing similar music when the queue ends); pass `enabled=true` or `enabled=false` (required)
    """
    action = action.lower().strip().replace("-", "_")

    eng = _queue_engine(engine)
    if eng == "none":
        return "Error: " + _no_player_msg(engine, for_queue=True)
    wp = _web_player(eng)

    if action in ("list", "show", "up_next"):
        ok, data = wp.queue_list()
        return _format_queue(data) if ok else f"Error: {data}"
    if action == "set":
        raw = [t.strip() for t in re.split(r"[,\n]", track) if t.strip()]
        if not raw:
            return "Error: set needs track=comma/newline-separated ids or names"
        ids: list[str] = []
        misses: list[str] = []
        for t in raw:
            cid = _queue_resolve_catalog_id(t, artist)
            (ids.append(cid) if cid else misses.append(t))
        if not ids:
            return f"Error: none of those resolved to catalog tracks: {', '.join(misses)}"
        ok, msg = wp.queue_set(ids)
        if not ok:
            return f"Error: {msg}"
        if misses:
            msg += f" (skipped, not found: {', '.join(misses)})"
        _set_active_playback(eng)
        return _queue_after(wp, msg)
    if action in ("play_next", "play_last"):
        cid = _queue_resolve_catalog_id(track, artist)
        if not cid:
            return f"Error: '{track}' not found in catalog"
        ok, msg = wp.queue_play_next(cid) if action == "play_next" else wp.queue_play_later(cid)
        if not ok:
            return f"Error: {msg}"
        _set_active_playback(eng)
        return _queue_after(wp, msg)
    if action == "remove":
        if index < 0:
            return "Error: index required (0-based) for remove"
        ok, msg = wp.queue_remove(index)
        return _queue_after(wp, msg) if ok else f"Error: {msg}"
    if action == "clear":
        ok, msg = wp.queue_clear()
        return _queue_after(wp, msg) if ok else f"Error: {msg}"
    if action == "jump":
        # Prefer jump-by-track (name or catalog id): the Up Next auto-advances in
        # real time, so an index captured a moment ago can land on the wrong track.
        # Targeting by id is drift-proof.
        if track:
            cid = _queue_resolve_catalog_id(track, artist)
            if not cid:
                return f"Error: '{track}' not found to jump to"
            ok, msg = wp.queue_jump_id(cid)
        elif index >= 0:
            ok, msg = wp.queue_jump(index)
        else:
            return "Error: jump needs index (0-based) or track (name/catalog id — drift-proof)"
        if not ok:
            return f"Error: {msg}"
        _set_active_playback(eng)
        name = _queue_current_name(wp)
        header = f"Jumped to: {name}" if name else msg
        return _queue_after(wp, header)
    if action == "autoplay":
        if enabled is None:
            return "Error: autoplay needs enabled=true or enabled=false"
        # autoplayEnabled is the player's OWN state — set it directly, don't shadow it
        # in our config. `queue list` reports the current value (read), this sets it.
        ok, msg = wp.queue_autoplay(enabled)
        return _queue_after(wp, msg) if ok else f"Error: {msg}"
    return (
        f"Unknown action: {action}. Use: list, set, play_next, play_last, remove, clear, jump, "
        "autoplay"
    )


# =============================================================================
# Playback transport (cross-platform — browser web player or native Music.app)
# =============================================================================
# Registered unconditionally so non-macOS clients get it too. play/control/
# now_playing/settings run through the web player or, on macOS, the local
# Music.app — chosen by the `mode` preference (auto/native/web) or a per-call
# engine= override. reveal/airplay are macOS-only and gated at runtime.

_PLAYBACK_NEEDS_BROWSER = (
    "Native (Music.app) playback needs macOS, and this host isn't macOS. "
    'Set the web engine — config(action="set-pref", preference="mode", string_value="web") '
    "— and run `applemusic-mcp login`, or pass engine='web' for this one call."
)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Playback transport", readOnlyHint=False, destructiveHint=False, openWorldHint=True
    )
)
def playback(
    action: str = "now_playing",
    # play params
    track: str = "",
    playlist: str = "",
    album: str = "",
    artist: str = "",
    url: str = "",
    shuffle: bool = False,
    reveal: Optional[bool] = None,
    add_to_library: bool = False,
    # control params
    control: str = "",
    seconds: float = 0,
    # settings params
    volume: int = -1,
    shuffle_mode: str = "",
    repeat: str = "",
    # reveal params
    track_name: str = "",
    # airplay params
    device_name: str = "",
    # engine override (one call only)
    engine: str = "",
) -> str:
    """Playback transport. play/control/now_playing/settings run on the engine the
    `mode` preference resolves to — native Music.app (macOS), the Safari web player
    (macOS), or the Chrome web player (any OS). Override it for ONE call with
    `engine=`: 'native', 'safari', 'chrome', 'web' (the web engine — Safari on macOS,
    Chrome off-mac), or 'auto'. control/now_playing follow whichever engine is
    actively playing (so after a Safari queue, pause/next reach Safari). Safari needs
    a signed-in Safari + "Allow JavaScript from Apple Events"; Chrome needs a
    signed-in Chrome (`applemusic-mcp login`) + a desktop session. reveal and airplay
    are macOS-only. For the Up Next queue, use the separate `queue` tool.
    Actions: play, control, now_playing, settings, reveal, airplay."""
    action = action.lower().strip().replace("-", "_")

    # Resolve the engine: native | safari | chrome | none. `play` uses the play
    # resolver; control/now_playing/settings/reveal follow the ACTIVE engine (so a
    # Safari-queued session is the one you control) unless this call overrides it.
    override = engine.strip()
    if override and override.lower() not in (
        "native",
        "safari",
        "chrome",
        "web",
        "browser",
        "auto",
        "api",
    ):
        return f"Error: engine must be one of native, safari, chrome, web, auto (got {engine!r})"
    eng = _playback_engine(override) if (action == "play" or override) else _get_active_playback()

    if action == "play":
        if eng == "none":
            return "Error: " + _no_player_msg(override)
        if eng == "native":
            if not APPLESCRIPT_AVAILABLE:
                return _PLAYBACK_NEEDS_BROWSER
            res = _playback_play(
                track, playlist, album, artist, shuffle, reveal, add_to_library, url
            )
            _set_active_playback("native")
            return res
        res = _browser_play(_web_player(eng), track, artist, url, playlist, album, shuffle)
        if not res.startswith("Error"):
            _set_active_playback(eng)
        return res
    elif action == "control":
        if not control:
            return "Error: control param required. Use: play, pause, stop, next, previous, seek"
        if eng == "none":
            return "Error: " + _no_player_msg(override)
        if eng == "native":
            if not APPLESCRIPT_AVAILABLE:
                return _PLAYBACK_NEEDS_BROWSER
            msg = _playback_control(control, seconds)
            if msg.startswith("Error"):
                return msg
        else:
            ok, msg = _web_player(eng).playback_control(control, seconds)
            if not ok:
                return f"Error: {msg}"
        # Return the resulting now-playing so the caller doesn't need a follow-up
        # now_playing call after a play/pause/next/seek.
        return f"{msg}\n\n{playback(action='now_playing', engine=engine)}"
    elif action == "now_playing":
        # PRIMARY = full state of the active engine (native keeps its rich detail:
        # state / progress). Then surface any OTHER engine that's also playing, so a
        # split is visible, with a hint to drive a specific one. Peeks never launch an
        # engine — they only read one already running.
        from . import browser, safari_player

        _ENGINE_LABELS = {"native": "Music.app", "safari": "Safari", "chrome": "Chrome web player"}

        def _peek(key):  # no-launch read of a non-active engine
            if key == "native":
                return asc.now_playing_if_running() if APPLESCRIPT_AVAILABLE else None
            if key == "safari":
                return safari_player.now_playing_if_running()
            if key == "chrome":
                return browser.now_playing_if_running()
            return None

        def _compact(label, np):
            st = f" [{np.get('state')}]" if np.get("state") else ""
            artist = f" — {np.get('artist')}" if np.get("artist") else ""
            album = f" ({np.get('album')})" if np.get("album") else ""
            pos, dur = np.get("position"), np.get("duration")
            prog = ""
            if isinstance(pos, (int, float)) and isinstance(dur, (int, float)) and dur:
                prog = f" {int(pos) // 60}:{int(pos) % 60:02d}/{int(dur) // 60}:{int(dur) % 60:02d}"
            return f"{label}{st}: {np.get('name')}{artist}{album}{prog}"

        # Primary line for the active engine.
        if eng == "native":
            if not APPLESCRIPT_AVAILABLE:
                return _PLAYBACK_NEEDS_BROWSER
            primary = _playback_now_playing()  # rich: state / track / artist / album / position
        elif eng in ("safari", "chrome"):
            np = _web_player(eng).now_playing()
            primary = (
                _compact(_ENGINE_LABELS[eng], np)
                if np
                else f"Nothing playing ({_ENGINE_LABELS[eng]})"
            )
        else:  # none — no player resolved (e.g. api mode)
            primary = _no_player_msg(override)

        # Other engines also playing (peek-only), so a split is visible.
        others = []
        for key in ["native", "safari", "chrome"]:
            if key == eng:
                continue
            np = _peek(key)
            if np and np.get("name"):
                others.append(f"  also on {_compact(_ENGINE_LABELS[key], np)}")

        out = primary
        if others:
            out += "\n\nOther engines also playing:\n" + "\n".join(others)
            out += "\n(pass engine='native' | 'safari' | 'chrome' to control a specific one)"
        tabs = safari_player.music_tab_count() if APPLESCRIPT_AVAILABLE else 0
        if tabs > 1:
            out += f"\n\nℹ️ {tabs} Apple Music tabs are open in Safari — driving a consistent one."
        return out
    elif action == "settings":
        if eng in ("safari", "chrome"):
            shuffle_b = (
                {"on": True, "off": False}.get(shuffle_mode.lower()) if shuffle_mode else None
            )
            repeat_v = repeat.lower() if repeat else None
            ok, msg = _web_player(eng).browser_settings(volume, shuffle_b, repeat_v)
            return msg if ok else f"Error: {msg}"
        if not APPLESCRIPT_AVAILABLE:
            return _PLAYBACK_NEEDS_BROWSER
        return _playback_settings(volume, shuffle_mode, repeat)
    elif action == "reveal":
        name = track_name or track
        if not name and not url:
            return "Error: track_name, track, or url required for reveal action"
        if eng in ("safari", "chrome"):
            target = url
            if not target:
                resolved = _resolve_catalog_track_itunes(name, artist)
                if not resolved:
                    return f"Error: '{name}' not found in catalog"
                target = resolved["url"]
            ok, msg = _web_player(eng).reveal_url(target)
            return msg if ok else f"Error: {msg}"
        if err := _macos_only("reveal"):
            return err
        return _playback_reveal(name, artist)
    elif action == "airplay":
        if err := _macos_only("airplay"):
            return err
        return _playback_airplay(device_name)
    else:
        return (
            f"Unknown action: {action}. Use: play, control, now_playing, settings, reveal, airplay"
        )


# =============================================================================
# AppleScript-powered tools (macOS only)
# =============================================================================
# These tools provide capabilities not available through the REST API:
# - Playback control (play, pause, skip)
# - Delete tracks from playlists
# - Delete playlists
# - Volume and shuffle control
# - Get currently playing track


def _catalog_song_name(song_id: str) -> str:
    """Look up a catalog song's display name by id (for deep-link row
    matching). Returns "" if the lookup fails — callers treat that as
    'no name hint' and fall back to the highlighted-row strategy."""
    try:
        response = requests.get(
            f"{BASE_URL}/catalog/{get_storefront()}/songs/{song_id}",
            headers=get_headers(),
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code == 200:
            data = response.json().get("data", [])
            if data:
                return data[0].get("attributes", {}).get("name", "") or ""
    except Exception:
        pass
    return ""


def _convert_song_url_to_album(url: str) -> Optional[str]:
    """Convert a /song/ URL to /album/?i= format via Apple Music API.

    Extracts the song ID from the URL, looks up its album via API,
    and returns an album URL with ?i=songId. Returns None if the API
    is unavailable or the lookup fails.
    """
    match = re.search(r"/song/[^/]*/(\d+)", url)
    if not match:
        return None
    song_id = match.group(1)

    try:
        headers = get_headers()
        sf = get_storefront()
        response = requests.get(
            f"{BASE_URL}/catalog/{sf}/songs/{song_id}",
            headers=headers,
            params={"include": "albums"},
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code != 200:
            return None
        data = response.json().get("data", [])
        if not data:
            return None
        # Get album from relationships
        albums = data[0].get("relationships", {}).get("albums", {}).get("data", [])
        if not albums:
            return None
        album_id = albums[0].get("id")
        album_name = data[0].get("attributes", {}).get("albumName", "album")
        if album_id:
            # Construct album URL with ?i= for the specific song
            album_slug = re.sub(r"[^a-z0-9]+", "-", album_name.lower()).strip("-")
            return f"https://music.apple.com/{sf}/album/{album_slug}/{album_id}?i={song_id}"
    except Exception:
        pass
    return None


def _try_ui_catalog_play(
    track_name: str,
    track_artist: str,
    source_label: str = "ui_catalog",
    prefix: str = "[UI Catalog]",
) -> tuple[bool, Optional[str]]:
    """Try to play a catalog track via Music.app UI automation.

    Centralizes the APPLESCRIPT_AVAILABLE gating, audit logging, and
    success-message formatting that was previously duplicated across
    three call sites in this function.

    Returns:
        (True, formatted_message) on success.
        (False, raw_error_message) on UI failure (caller can choose
            whether to surface the failure inline or fall through).
        (False, None) when APPLESCRIPT_AVAILABLE is False — caller
            should fall through to the next path.
    """
    if not APPLESCRIPT_AVAILABLE:
        return False, None
    ui_query = f"{track_name} {track_artist}".strip()
    ok, msg = asc.ui_play_result_by_query(ui_query)
    if ok:
        audit_log.log_action(
            "play_track",
            {"track": track_name, "artist": track_artist, "source": source_label},
        )
        return True, f"{prefix} {msg}"
    return False, msg


def _catalog_miss_play(name: str, artist: str, url: str, reveal: bool) -> str:
    """A catalog item isn't in the library and the UI-search play didn't take.
    Play it natively in Music.app by deep-linking the URL and driving the UI
    (the fixed CoreGraphics click for the album/playlist Play button, or a
    name-matched double-click for a specific ?i= track). No library changes;
    reveal=True just opens the page for a manual click.

    If native UI play fails and playback isn't pinned to ``native``, fall
    back to the browser web player (now full-DRM) rather than dead-ending —
    a pinned-``native`` user explicitly opted out of the browser, so they
    get the actionable message instead."""
    if reveal and url:
        success, _ = asc.open_catalog_song(url)
        if success:
            return f"[Catalog] Opened: {name} by {artist} (click play)"
    if not url:
        return f"[Catalog] Found {name} by {artist}."

    ok, msg = asc.open_catalog_and_play(url, track_name=name)
    if ok:
        audit_log.log_action("play_track", {"track": name, "artist": artist, "source": "deep_link"})
        return f"[Catalog] {msg}"

    # Native UI play failed (commonly: Accessibility not granted, or a Music
    # layout this build doesn't match). In auto/browser playback, the browser
    # web player is a working fallback; pinned-native opted out of it.
    pinned_native = _mode_pinned_native()
    if not pinned_native and has_user_token():
        bmsg = _browser_play(_web_player(_playback_engine("web")), url=url)
        if not bmsg.startswith("Error"):
            return f"[Catalog→Browser] {bmsg}"

    return (
        f"[Catalog] Found {name} by {artist} — couldn't auto-play it in Music. "
        "Grant Accessibility (System Settings → Privacy & Security → Accessibility) "
        "for your terminal/MCP host, "
        + (
            "or play in the web player: set mode=web, or pass engine='web' for this call."
            if has_user_token()
            else "or run `login` to play in the web player (needs an Apple Music subscription)."
        )
    )


def _playback_play(
    track: str = "",
    playlist: str = "",
    album: str = "",
    artist: str = "",
    shuffle: bool = False,
    reveal: Optional[bool] = None,
    add_to_library: bool = False,
    url: str = "",
) -> str:
    """Play a track, playlist, album, or URL (macOS). Provide ONE target."""
    # === URL === (handle first, separate from other targets)
    if url:
        url = url.strip()
        if track or playlist or album or artist:
            return "Error: When using url, don't provide track, playlist, album, or artist"

        # Convert /song/ URLs to /album/?i= format via API lookup
        if "/song/" in url and "?i=" not in url:
            converted = _convert_song_url_to_album(url)
            if converted:
                url = converted

        # For a specific ?i= track, look up its name so the deep-link path can
        # match the exact row and double-click it (rather than the album Play
        # button, which would start the whole album from track 1).
        track_name_hint = ""
        i_match = re.search(r"[?&]i=(\d+)", url)
        if i_match:
            track_name_hint = _catalog_song_name(i_match.group(1))

        success, result = asc.open_catalog_and_play(
            url, shuffle=shuffle, track_name=track_name_hint
        )
        if success:
            audit_log.log_action("play_url", {"url": url, "result": result})
            return result
        # Native UI play failed — fall back to the browser web player unless
        # playback is pinned to native (same policy as _catalog_miss_play).
        pinned_native = _mode_pinned_native()
        if not pinned_native and has_user_token():
            bmsg = _browser_play(_web_player(_playback_engine("web")), url=url, shuffle=shuffle)
            if not bmsg.startswith("Error"):
                audit_log.log_action("play_url", {"url": url, "via": "browser"})
                return f"[Browser] {bmsg}"
        return f"Error: {result}"

    # Count how many targets provided
    targets = sum(1 for t in [track, playlist, album] if t)
    if targets == 0:
        return "Error: Provide track, playlist, or album parameter"
    if targets > 1:
        return "Error: Provide only ONE of track, playlist, or album"

    # === PLAYLIST ===
    if playlist:
        success, result = asc.play_playlist(playlist, shuffle)
        if success:
            audit_log.log_action("play_playlist", {"playlist": playlist, "shuffle": shuffle})
            return result
        return f"Error: {result}"

    # === ALBUM ===
    if album:
        reveal = bool(reveal)  # explicit "just show it"; default is to play

        # Search library for tracks from this album
        search_ok, lib_results = asc.search_library(album, "albums")
        if search_ok and lib_results:
            # Filter by artist if provided
            for lib_track in lib_results:
                lib_album = lib_track.get("album", "")
                lib_artist = lib_track.get("artist", "")
                if not _loose_contains(album, lib_album):
                    continue
                if artist and not _loose_contains(artist, lib_artist):
                    continue
                # Found match - play first track (Music continues with album)
                if shuffle:
                    asc.set_shuffle(True)
                success, result = asc.play_track(lib_track.get("name", ""), lib_artist)
                if success:
                    shuffle_note = " (shuffled)" if shuffle else ""
                    audit_log.log_action("play_album", {"album": lib_album, "artist": lib_artist})
                    return f"[Library] Playing: {lib_album} by {lib_artist}{shuffle_note}"
                break

        # Not in library - search catalog
        try:
            headers = get_headers()
            response = requests.get(
                f"{BASE_URL}/catalog/{get_storefront()}/search",
                headers=headers,
                params={"term": f"{album} {artist}".strip(), "types": "albums", "limit": 5},
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code == 200:
                data = response.json()
                albums_data = data.get("results", {}).get("albums", {}).get("data", [])
                for cat_album in albums_data:
                    attrs = cat_album.get("attributes", {})
                    album_name = attrs.get("name", "")
                    album_artist = attrs.get("artistName", "")
                    album_id = cat_album.get("id", "")
                    if not _loose_contains(album, album_name):
                        continue
                    if artist and not _loose_contains(artist, album_artist):
                        continue
                    album_url = attrs.get("url", "")

                    # Option 1: Add album to library and play
                    if add_to_library and album_id:
                        add_ok, add_msg = _add_album_to_library(album_id)
                        if add_ok:
                            time.sleep(PLAY_TRACK_INITIAL_DELAY)
                            # Re-search library for the album
                            result = ""  # bound even if no synced track is found yet
                            for attempt in range(PLAY_TRACK_MAX_ATTEMPTS):
                                if attempt > 0:
                                    time.sleep(PLAY_TRACK_RETRY_DELAY)
                                search_ok2, lib_results2 = asc.search_library(album_name, "albums")
                                if search_ok2 and lib_results2:
                                    for lib_track2 in lib_results2:
                                        if (
                                            album_name.lower()
                                            in lib_track2.get("album", "").lower()
                                        ):
                                            if shuffle:
                                                asc.set_shuffle(True)
                                            success, result = asc.play_track(
                                                lib_track2.get("name", ""),
                                                lib_track2.get("artist", ""),
                                            )
                                            if success:
                                                shuffle_note = " (shuffled)" if shuffle else ""
                                                audit_log.log_action(
                                                    "play_album",
                                                    {
                                                        "album": album_name,
                                                        "artist": album_artist,
                                                    },
                                                )
                                                return f"[Catalog→Library] Playing: {album_name} by {album_artist}{shuffle_note}"
                                            break
                            return _play_after_add(f"{album_name} by {album_artist}", result)
                        return f"[Catalog] Failed to add: {add_msg}"

                    # Not in library — play it via the browser web player.
                    return _catalog_miss_play(album_name, album_artist, album_url, reveal)
        except requests.exceptions.RequestException as e:
            return f"API Error searching catalog: {str(e)}"
        except (FileNotFoundError, ValueError) as e:
            return f"Error: {str(e)}"
        return f"Album not found: {album}"

    # === TRACK ===
    reveal = bool(reveal)  # explicit "just show it"; default is to play

    # Resolve track input
    resolved = _resolve_track(track, artist)
    if not resolved:
        return "Error: Could not resolve track"

    r = resolved[0]  # Only first track
    if r.error:
        return f"Error: {r.error}"

    track_name = ""
    track_artist = r.artist or artist

    # If catalog ID, look up track info and play directly
    if r.input_type == InputType.CATALOG_ID:
        catalog_id = r.value
        try:
            headers = get_headers()
            response = requests.get(
                f"{BASE_URL}/catalog/{get_storefront()}/songs/{catalog_id}",
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code == 200:
                data = response.json().get("data", [])
                if data:
                    attrs = data[0].get("attributes", {})
                    track_name = attrs.get("name", "")
                    track_artist = attrs.get("artistName", "")
                    song_url = attrs.get("url", "")

                    # For catalog ID, try to add to library and play
                    if add_to_library:
                        add_ok, add_msg = _add_songs_to_library([catalog_id])
                        if add_ok:
                            time.sleep(PLAY_TRACK_INITIAL_DELAY)
                            result = ""  # bound even if no attempt runs
                            for attempt in range(PLAY_TRACK_MAX_ATTEMPTS):
                                if attempt > 0:
                                    time.sleep(PLAY_TRACK_RETRY_DELAY)
                                success, result = asc.play_track(track_name, track_artist)
                                if success:
                                    if reveal:
                                        asc.reveal_track(track_name, track_artist)
                                    audit_log.log_action(
                                        "play_track",
                                        {"track": track_name, "artist": track_artist},
                                    )
                                    return (
                                        f"[Catalog→Library] Playing: {track_name} by {track_artist}"
                                    )
                            return _play_after_add(f"{track_name} by {track_artist}", result)
                        return f"[Catalog] Failed to add: {add_msg}"

                    # UI play first; else play via the browser web player.
                    ui_ok, ui_msg = _try_ui_catalog_play(track_name, track_artist)
                    if ui_ok:
                        return ui_msg
                    return _catalog_miss_play(track_name, track_artist, song_url, reveal)
        except requests.exceptions.RequestException as e:
            return f"Error looking up catalog ID {catalog_id}: {e}"
        except (FileNotFoundError, ValueError) as e:
            return f"Error: {e}"
        except Exception as e:  # noqa: BLE001 - surface, never swallow into "not found"
            return f"Error looking up catalog ID {catalog_id}: {e}"
        # Reached only without an exception: a non-200/404 is a real API problem,
        # not a missing track — don't disguise it as "not found".
        if response.status_code not in (200, 404):
            return f"Error looking up catalog ID {catalog_id}: HTTP {response.status_code}"
        return f"Track not found for catalog ID: {catalog_id}"

    # Name-based lookup
    track_name = r.value
    track_artist = r.artist or artist

    # Search library first (doesn't foreground Music)
    search_ok, lib_results = asc.search_library(track_name, "songs")
    if search_ok and lib_results:
        # Filter for matching artist if provided
        for lib_track in lib_results:
            lib_name = lib_track.get("name", "")
            lib_artist = lib_track.get("artist", "")
            if not _loose_contains(track_name, lib_name):
                continue
            if track_artist and not _loose_contains(track_artist, lib_artist):
                continue
            # Found match - now play it (will foreground Music)
            success, result = asc.play_track(lib_name, lib_artist)
            if success:
                if reveal:
                    asc.reveal_track(lib_name, lib_artist)
                audit_log.log_action("play_track", {"track": lib_name, "artist": lib_artist})
                return f"[Library] {result}"
            break

    # Track not in library - search catalog
    search_term = f"{track_name} {track_artist}".strip() if track_artist else track_name
    songs = _search_catalog_songs(search_term, limit=5)

    # Find best match
    for song in songs:
        attrs = song.get("attributes", {})
        song_name = attrs.get("name", "")
        song_artist = attrs.get("artistName", "")

        # Check if it's a reasonable match
        if not _loose_contains(track_name, song_name):
            continue
        # Check artist in artistName OR song name (for "feat. X" cases)
        if (
            track_artist
            and not _loose_contains(track_artist, song_artist)
            and not _loose_contains(track_artist, song_name)
        ):
            continue

        catalog_id = song.get("id")
        song_url = attrs.get("url", "")

        # Option 1: Add to library first, then play
        if add_to_library:
            add_ok, add_msg = _add_songs_to_library([catalog_id])
            if add_ok:
                # Wait for iCloud sync, then play
                time.sleep(PLAY_TRACK_INITIAL_DELAY)
                result = ""  # bound even if no attempt runs
                for attempt in range(PLAY_TRACK_MAX_ATTEMPTS):
                    if attempt > 0:
                        time.sleep(PLAY_TRACK_RETRY_DELAY)
                    success, result = asc.play_track(song_name, song_artist)
                    if success:
                        if reveal:
                            asc.reveal_track(song_name, song_artist)
                        audit_log.log_action(
                            "play_track", {"track": song_name, "artist": song_artist}
                        )
                        return f"[Catalog→Library] Playing: {song_name} by {song_artist}"
                return _play_after_add(f"{song_name} by {song_artist}", result)
            return f"[Catalog] Failed to add: {add_msg}"

        # UI play — works without adding to library; tried before the
        # browser fallback when Music.app automation is available.
        ui_ok, ui_msg = _try_ui_catalog_play(song_name, song_artist)
        if ui_ok:
            return ui_msg
        if ui_msg is not None:
            # UI was attempted and failed — surface the reason rather than
            # falling through to reveal/error. APPLESCRIPT_AVAILABLE=False
            # returns (False, None), in which case we do fall through.
            return f"[UI Catalog failed: {ui_msg}] Falling back — {song_name} by {song_artist}"

        # Not in library and UI play didn't take — play via the browser.
        return _catalog_miss_play(song_name, song_artist, song_url, reveal)

    # API catalog search found nothing — try UI search as last resort.
    # Different prefix ([UI Search] vs [UI Catalog]) signals to the user
    # that this matched only via UI search, not via API confirmation.
    ui_ok, ui_msg = _try_ui_catalog_play(
        track_name, track_artist, source_label="ui_search", prefix="[UI Search]"
    )
    if ui_ok:
        return ui_msg

    return f"Track not found in library or catalog: {track_name}"


def _playback_control(action: str, seconds: float = 0) -> str:
    """Control playback (macOS). Actions: play, pause, playpause, stop, next, previous, seek."""
    action = action.lower().strip()

    # Handle seek separately since it takes a parameter
    if action == "seek":
        success, result = asc.seek(seconds)
        if success:
            audit_log.log_action(
                "playback_control", {"control": action, "seconds": seconds if seconds else None}
            )
            return f"Seeked to {int(seconds // 60)}:{int(seconds % 60):02d}"
        return f"Error: {result}"

    action_map = {
        "play": asc.play,
        "pause": asc.pause,
        "playpause": asc.playpause,
        "stop": asc.stop,
        "next": asc.next_track,
        "previous": asc.previous_track,
    }
    if action not in action_map:
        return f"Invalid action: {action}. Use: play, pause, playpause, stop, next, previous, seek"

    success, result = action_map[action]()
    if not success:
        return f"Error: {result}"
    audit_log.log_action("playback_control", {"control": action, "seconds": None})
    # Confirm the action actually took by re-reading player state — never claim a
    # pause/stop that didn't stick. The classic "won't stay paused" symptom is
    # another engine still making sound; point the user at the all-engines view.
    ok, info = asc.get_current_track()
    state = (info.get("state") if ok else "") or ""
    if action in ("pause", "stop") and state == "playing":
        return (
            f"Asked Music.app to {action}, but its player is still playing. If you still "
            "hear audio, another engine (Safari/Chrome web player) is likely the one "
            "playing — run playback(action='now_playing') to see every engine, then pass "
            "engine= to control the right one."
        )
    return f"Playback: {action}" + (f" (Music.app: {state})" if state else "")


def _playback_now_playing() -> str:
    """Get currently playing track and player state (macOS)."""
    success, info = asc.get_current_track()
    if not success:
        return f"Error: {info}"

    if info.get("state") == "stopped":
        return "State: stopped\nNot currently playing"

    parts = []
    # Add player state first
    state = info.get("state", "unknown")
    parts.append(f"State: {state}")

    if "name" in info:
        parts.append(f"Track: {info['name']}")
    if "artist" in info:
        parts.append(f"Artist: {info['artist']}")
    if "album" in info:
        parts.append(f"Album: {info['album']}")
    if "position" in info and "duration" in info:
        try:
            pos = float(info["position"])
            dur = float(info["duration"])
            pos_min, pos_sec = int(pos) // 60, int(pos) % 60
            dur_min, dur_sec = int(dur) // 60, int(dur) % 60
            parts.append(f"Position: {pos_min}:{pos_sec:02d} / {dur_min}:{dur_sec:02d}")
        except (ValueError, TypeError):
            pass

    return "\n".join(parts) if parts else "Playing (no track info available)"


def _playback_settings(
    volume: int = -1,
    shuffle: str = "",
    repeat: str = "",
) -> str:
    """Get or set playback settings (macOS): volume, shuffle, repeat."""
    changes = []

    # Apply any changes
    if volume >= 0:
        v = max(0, min(100, volume))
        success, result = asc.set_volume(v)
        if not success:
            return f"Error setting volume: {result}"
        changes.append(f"Volume: {v}")

    if shuffle:
        enabled = shuffle.lower() in ("on", "true", "1", "yes")
        success, result = asc.set_shuffle(enabled)
        if not success:
            return f"Error setting shuffle: {result}"
        changes.append(f"Shuffle: {'on' if enabled else 'off'}")

    if repeat:
        success, result = asc.set_repeat(repeat.lower())
        if not success:
            return f"Error setting repeat: {result}"
        changes.append(f"Repeat: {repeat}")

    # If changes were made, return confirmation
    if changes:
        audit_changes = {}
        if volume >= 0:
            audit_changes["volume"] = volume
        if shuffle:
            audit_changes["shuffle"] = shuffle
        if repeat:
            audit_changes["repeat"] = repeat
        if audit_changes:
            audit_log.log_action("playback_settings", audit_changes)
        return "Updated: " + ", ".join(changes)

    # Otherwise return current settings
    success, stats = asc.get_library_stats()
    if not success:
        return f"Error: {stats}"

    return (
        f"Player: {stats['player_state']}\n"
        f"Volume: {stats['volume']}\n"
        f"Shuffle: {'on' if stats['shuffle'] else 'off'}\n"
        f"Repeat: {stats['repeat']}"
    )


def _playlist_remove(
    playlist: str = "",
    track: str = "",
    artist: str = "",
    verify: bool = True,
) -> str:
    """Remove track(s) from a playlist (macOS). Removes from playlist only, not library."""
    # Resolve playlist (name-based only for removal)
    resolved = _resolve_playlist(playlist)
    if resolved.error:
        return resolved.error

    # This function requires AppleScript name (macOS only)
    if not resolved.applescript_name:
        return "Error: Playlist not found or requires explicit playlist name (not just ID)"

    if not track:
        return "Error: Provide track parameter"

    results = []
    errors = []

    def _record(
        ok: bool, msg: str, name: Optional[str], track_artist: Optional[str], err_prefix: str
    ):
        """Record an asc.remove_track_from_playlist outcome with verify-after-remove.

        On success, confirms the track is genuinely gone from the playlist —
        same false-positive class as add (some user-created playlists
        silently revert AppleScript edits server-side). On verify miss,
        returns the action to errors with a clear caveat.
        """
        if not ok:
            errors.append(f"{err_prefix}: {msg}")
            return
        if not verify or name is None:
            results.append(msg)
            return
        if _verify_track_not_in_playlist(resolved.applescript_name, name, track_artist or ""):
            results.append(msg)
        else:
            errors.append(
                f"{err_prefix}: AppleScript reported success but the track still "
                f"appears in '{resolved.applescript_name}' — Music.app silently reverted "
                f"the edit server-side (an Apple bug; a manual remove fails the same way). "
                f"Quit and reopen Music.app, then retry."
            )

    # Resolve track input
    track_resolved = _resolve_track(track, artist)

    for r in track_resolved:
        if r.error:
            errors.append(r.error)
            continue

        if r.input_type == InputType.PERSISTENT_ID:
            # Remove by persistent ID — verify by name/artist requires a
            # name lookup we don't have here; verify is skipped.
            success, result = asc.remove_track_from_playlist(
                resolved.applescript_name, track_id=r.value
            )
            _record(success, result, name=None, track_artist=None, err_prefix=f"ID {r.value}")

        elif r.input_type == InputType.CATALOG_ID:
            cache = get_track_cache()
            info = cache.get_track_info(r.value)
            if info and info.get("name"):
                success, result = asc.remove_track_from_playlist(
                    resolved.applescript_name,
                    track_name=info["name"],
                    artist=info.get("artist") or None,
                )
                _record(
                    success,
                    result,
                    name=info["name"],
                    track_artist=info.get("artist"),
                    err_prefix=info["name"],
                )
            else:
                errors.append(f"Catalog ID {r.value}: Not in cache - use track name instead")

        elif r.input_type == InputType.LIBRARY_ID:
            cache = get_track_cache()
            info = cache.get_track_info(r.value)
            if info and info.get("name"):
                success, result = asc.remove_track_from_playlist(
                    resolved.applescript_name,
                    track_name=info["name"],
                    artist=info.get("artist") or None,
                )
                _record(
                    success,
                    result,
                    name=info["name"],
                    track_artist=info.get("artist"),
                    err_prefix=info["name"],
                )
            else:
                errors.append(f"Library ID {r.value}: Not in cache - use track name instead")

        elif r.input_type in (InputType.NAME, InputType.JSON_OBJECT):
            success, result = asc.remove_track_from_playlist(
                resolved.applescript_name, track_name=r.value, artist=r.artist or None
            )
            _record(success, result, name=r.value, track_artist=r.artist, err_prefix=r.value)

    # Log successful removes
    if results:
        audit_log.log_action(
            "remove_from_playlist",
            {"playlist": resolved.applescript_name, "tracks": results},
            undo_info={"playlist_name": resolved.applescript_name, "tracks": results},
        )

    result = _build_track_results(
        results, errors, success_verb="removed", error_verb="failed to remove"
    )
    fuzzy_info = _format_fuzzy_match(resolved.fuzzy_match)
    return result + fuzzy_info


def _library_remove(
    track: str = "",
    artist: str = "",
    verify: bool = True,
) -> str:
    """Remove track(s) from your library entirely (macOS). PERMANENT deletion."""
    if not track:
        return "Error: Provide track parameter"

    results = []
    errors = []

    def _verify_gone(name: str, track_artist: Optional[str]) -> bool:
        """Confirm a track is no longer searchable in the local library."""
        for attempt in range(_VERIFY_ATTEMPTS):
            if attempt > 0:
                time.sleep(_VERIFY_DELAY_S)
            ok, lib_results = asc.search_library(name, "songs")
            if not ok or not lib_results:
                return True
            # If we got results but none match the artist filter, treat as gone
            if track_artist:
                matches = [
                    t for t in lib_results if _loose_contains(track_artist, t.get("artist") or "")
                ]
                if not matches:
                    return True
        return False

    def _record(
        ok: bool, msg: str, name: Optional[str], track_artist: Optional[str], err_prefix: str
    ):
        """Record an asc.remove_from_library outcome with verify-after-remove."""
        if not ok:
            errors.append(f"{err_prefix}: {msg}")
            return
        if not verify or name is None:
            results.append(msg)
            return
        if _verify_gone(name, track_artist):
            results.append(msg)
        else:
            errors.append(
                f"{err_prefix}: AppleScript reported success but the track "
                f"is still in the library after retry. Some tracks resist "
                f"library removal (iCloud Music Library re-syncs them); "
                f"removing manually via Music.app may be required."
            )

    # Resolve track input
    resolved = _resolve_track(track, artist)

    for r in resolved:
        if r.error:
            errors.append(r.error)
            continue

        if r.input_type == InputType.PERSISTENT_ID:
            success, result = asc.remove_from_library(track_id=r.value)
            # Persistent ID removal — verify by name requires a lookup we
            # don't have here. Skip verify (best-effort).
            _record(success, result, name=None, track_artist=None, err_prefix=f"ID {r.value}")

        elif r.input_type == InputType.CATALOG_ID:
            cache = get_track_cache()
            info = cache.get_track_info(r.value)
            if info and info.get("name"):
                success, result = asc.remove_from_library(
                    track_name=info["name"], artist=info.get("artist") or None
                )
                _record(
                    success,
                    result,
                    name=info["name"],
                    track_artist=info.get("artist"),
                    err_prefix=info["name"],
                )
            else:
                errors.append(f"Catalog ID {r.value}: Not in cache - use track name instead")

        elif r.input_type == InputType.LIBRARY_ID:
            cache = get_track_cache()
            info = cache.get_track_info(r.value)
            if info and info.get("name"):
                success, result = asc.remove_from_library(
                    track_name=info["name"], artist=info.get("artist") or None
                )
                _record(
                    success,
                    result,
                    name=info["name"],
                    track_artist=info.get("artist"),
                    err_prefix=info["name"],
                )
            else:
                errors.append(f"Library ID {r.value}: Not in cache - use track name instead")

        elif r.input_type in (InputType.NAME, InputType.JSON_OBJECT):
            success, result = asc.remove_from_library(track_name=r.value, artist=r.artist or None)
            _record(success, result, name=r.value, track_artist=r.artist, err_prefix=r.value)

    # Log successful removes - this is destructive, important for audit
    if results:
        audit_log.log_action(
            "remove_from_library",
            {"tracks": results},
            undo_info={
                "tracks": results,
                "note": "Re-add via search_catalog and add_to_library",
            },
        )

    return _build_track_results(
        results, errors, success_verb="removed from library", error_verb="failed to remove"
    )


def _playlist_delete(playlist_name: str) -> str:
    """Delete a playlist entirely (macOS). PERMANENT, cannot be undone."""
    # Get track count before deletion for audit log
    track_count = 0
    track_names = []
    tracks_success, tracks = asc.get_playlist_tracks(playlist_name)
    if tracks_success and isinstance(tracks, list):
        track_count = len(tracks)
        track_names = [f"{t.get('name', '')} - {t.get('artist', '')}" for t in tracks[:20]]

    success, result = asc.delete_playlist(playlist_name)
    if success:
        # Log deletion with undo info
        audit_log.log_action(
            "delete_playlist",
            {"name": playlist_name, "track_count": track_count},
            undo_info={
                "playlist_name": playlist_name,
                "tracks": track_names,
                "note": "Recreate playlist and re-add tracks",
            },
        )
        return result
    return f"Error: {result}"


def _playlist_rename(playlist_name: str, new_name: str) -> str:
    """Rename a playlist (macOS)."""
    if not playlist_name:
        return "Error: playlist name required"
    if not new_name:
        return "Error: new_name required"

    success, result = asc.rename_playlist(playlist_name, new_name)
    if success:
        # Log rename for audit trail
        audit_log.log_action(
            "rename_playlist",
            {"old_name": playlist_name, "new_name": new_name},
            undo_info={"note": f"Rename back to '{playlist_name}'"},
        )
        return result
    return f"Error: {result}"


def _playback_reveal(track_name: str, artist: str = "") -> str:
    """Reveal a track in the Music app window (macOS)."""
    success, result = asc.reveal_track(track_name, artist if artist else None)
    if success:
        return result
    return f"Error: {result}"


def _playback_airplay(device_name: str = "") -> str:
    """List or switch AirPlay devices (macOS). Omit device_name to list."""
    if device_name:
        success, result = asc.set_airplay_device(device_name)
        if success:
            audit_log.log_action("airplay_switch", {"device": device_name})
            return result
        return f"Error: {result}"
    else:
        success, devices = asc.get_airplay_devices()
        if not success:
            return f"Error: {devices}"
        if not devices:
            return "No AirPlay devices found"
        return f"AirPlay devices ({len(devices)}):\n" + "\n".join(f"  - {d}" for d in devices)


def _shutdown_browser_engine():  # pragma: no cover - lifecycle, not exercised under test
    """Close the Chrome engine's persistent context cleanly so its profile is flushed
    and not left locked/corrupted. Playwright only persists reliably on a graceful
    ctx.close(); an abrupt kill is a known cause of the 'signed-out next launch' bug."""
    try:
        from . import browser

        browser._engine.shutdown(timeout=5.0)
    except Exception:
        pass


def main():
    """Run the MCP server."""
    # pragma: no cover  # entrypoint: starts the MCP server, not exercised under test
    import atexit
    import signal

    atexit.register(_shutdown_browser_engine)
    # MCP clients usually stop the server with SIGTERM, which by default skips atexit —
    # turn it into a normal exit so the Chrome profile flushes before we die.
    try:
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    except (ValueError, OSError):
        pass  # not the main thread / unsupported platform — atexit still covers normal exit
    mcp.run()


if __name__ == "__main__":
    main()
