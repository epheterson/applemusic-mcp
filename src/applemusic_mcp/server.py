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

from .auth import get_developer_token, get_user_token, get_config_dir, get_user_preferences
from . import applescript as asc
from .track_cache import get_track_cache, get_cache_dir
from . import audit_log

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


def get_storefront() -> str:
    """Get storefront from preferences, defaulting to 'us'."""
    prefs = get_user_preferences()
    return prefs.get("storefront", DEFAULT_STOREFRONT)


mcp = FastMCP("AppleMusicAPI")


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
        return "Invalid path"
    return file_path.read_text(encoding="utf-8")


def get_token_expiration_warning() -> str | None:
    """Check if developer token expires within 30 days. Returns warning message or None."""
    config_dir = get_config_dir()
    token_file = config_dir / "developer_token.json"

    if not token_file.exists():
        return None

    try:
        with open(token_file) as f:
            data = json.load(f)

        expires = data.get("expires", 0)
        days_left = (expires - time.time()) / 86400

        if days_left < 30:
            return f"⚠️ Developer token expires in {int(days_left)} days. Run: applemusic-mcp generate-token"
    except Exception:
        pass

    return None


def get_headers() -> dict:
    """Get headers for API requests."""
    return {
        "Authorization": f"Bearer {get_developer_token()}",
        "Music-User-Token": get_user_token(),
        "Content-Type": "application/json",
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
    2. Contains comma → CSV of names
    3. Otherwise → single value (ID or name auto-detected)

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
                return [
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

    # 2. CSV detection (contains comma, not JSON, and no artist specified)
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

    # 3. Single value - detect type
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
        return None, "Not found in catalog", None

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
        Empty list on error.
    """
    try:
        headers = get_headers()
        response = requests.get(
            f"{BASE_URL}/catalog/{get_storefront()}/search",
            headers=headers,
            params={"term": query, "types": "songs", "limit": min(limit, 25)},
            timeout=REQUEST_TIMEOUT,
        )
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

    # Probe token availability without leaking its error message
    api_ok = _has_developer_token()

    api_error: Optional[str] = None
    if api_ok:
        ok, result, api_steps = _auto_search_and_add_to_playlist(
            search_name, search_artist, playlist_name
        )
        steps.extend(api_steps)
        if ok and _verify_track_in_playlist(playlist_name, search_name, search_artist):
            return True, result, steps
        if ok:
            api_error = "API path claimed success but playlist verification failed"
            steps.append(api_error)
        else:
            api_error = result
            steps.append(f"API auto-search failed ({result}); trying UI fallback")

    if APPLESCRIPT_AVAILABLE:
        query = f"{search_name} {search_artist}".strip() if search_artist else search_name
        ok, msg = asc.ui_add_to_playlist(playlist_name, query, search_artist)
        if ok:
            if _verify_track_in_playlist(playlist_name, search_name, search_artist):
                return True, msg, steps
            ui_error = (
                f"UI path reported success but '{search_name}' is not in "
                f"'{playlist_name}' — likely clicked wrong result"
            )
            steps.append(ui_error)
            if api_error:
                return False, f"{ui_error} (API attempt: {api_error})", steps
            return False, ui_error, steps
        if api_error:
            return False, f"{msg} (API attempt: {api_error})", steps
        return False, msg, steps

    if api_error:
        return False, api_error, steps
    # Tell users what's actually missing for THEIR platform — the legacy
    # generic message ("need API token or macOS with Accessibility") was
    # confusing on macOS where the user obviously already had macOS.
    if sys.platform == "darwin":
        reason = "AppleScript unavailable (Music.app + Accessibility permissions required)"
    else:
        reason = "API token required on non-macOS platforms; run: applemusic-mcp generate-token"
    return (False, f"Not found in library; catalog search unavailable — {reason}", steps)


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

        # Get library ID from catalog song's library relationship
        lib_response = requests.get(
            f"{BASE_URL}/catalog/{get_storefront()}/songs/{catalog_id}/library",
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )

        library_id = None
        if lib_response.status_code == 200:
            lib_data = lib_response.json()
            lib_songs = lib_data.get("data", [])
            if lib_songs:
                library_id = lib_songs[0]["id"]

        if not library_id:
            return False, "Added to library but could not get library ID", steps

        # Add to playlist — prefer AppleScript on macOS so non-API-created
        # playlists work too. API playlist-add returns 500 for those.
        if APPLESCRIPT_AVAILABLE:
            # API add-to-library is async: the track lands in iCloud immediately
            # but Music.app's local library has to sync before AppleScript can
            # see it. Poll search_library until visible, then add.
            visible = False
            deadline = time.time() + _LIBRARY_SYNC_DEADLINE_S
            while time.time() < deadline:
                sok, s_hits = asc.search_library(found_name, "songs")
                if sok and s_hits:
                    if found_artist and len(found_artist) >= 2:
                        for h in s_hits:
                            if _loose_contains(found_artist, h.get("artist") or ""):
                                visible = True
                                break
                    else:
                        # No usable artist to disambiguate — trust the first
                        # library hit on name. Avoids an empty-string-in-any
                        # false match.
                        visible = True
                if visible:
                    break
                time.sleep(_LIBRARY_SYNC_TICK_S)

            if visible:
                ok, as_result = asc.add_track_to_playlist(playlist_name, found_name, found_artist)
                # Verify the track actually landed; retry once on verify miss
                # to absorb local state propagation lag.
                if ok and _verify_track_in_playlist(playlist_name, found_name, found_artist):
                    return True, f"{found_name} - {found_artist}", steps
                if ok:
                    steps.append("Playlist add reported success but verify failed; retrying")
                    time.sleep(_VERIFY_DELAY_S)
                    ok2, as_result2 = asc.add_track_to_playlist(
                        playlist_name, found_name, found_artist
                    )
                    if ok2 and _verify_track_in_playlist(playlist_name, found_name, found_artist):
                        return True, f"{found_name} - {found_artist}", steps
                    steps.append(
                        f"Retry also did not verify: "
                        f"{as_result2 if not ok2 else 'verify failed'}"
                    )
                else:
                    steps.append(f"AppleScript playlist add failed after local sync: {as_result}")
                # On macOS the API always returns 500 for user-created playlists.
                # Return now so _unified_auto_search_to_playlist can try the UI path.
                return False, "AppleScript add did not verify in playlist", steps
            else:
                steps.append(f"Library sync did not complete in {_LIBRARY_SYNC_DEADLINE_S:.0f}s")
                # Same: don't fall through to API for user-created playlists.
                return (
                    False,
                    f"Library sync timed out after {_LIBRARY_SYNC_DEADLINE_S:.0f}s; "
                    "track may have been added to your library but not to the playlist",
                    steps,
                )

        # Fall back to API playlist add (requires API-created playlist)
        if not playlist_id:
            pl_success, playlists = asc.get_playlists()
            if pl_success:
                for pl in playlists:
                    if _loose_contains(playlist_name, pl.get("name", "")):
                        playlist_id = pl.get("id")
                        break

        if not playlist_id:
            return False, f"Could not find playlist ID for '{playlist_name}'", steps

        pl_add_response = requests.post(
            f"{BASE_URL}/me/library/playlists/{playlist_id}/tracks",
            headers=headers,
            json={"data": [{"id": library_id, "type": "library-songs"}]},
            timeout=REQUEST_TIMEOUT,
        )

        if pl_add_response.status_code in (200, 201, 204):
            return True, f"{found_name} - {found_artist}", steps
        else:
            return False, f"Failed to add to playlist (status {pl_add_response.status_code})", steps

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
        return False, f"API returned status {response.status_code}"
    except Exception as e:
        return False, str(e)


# ============ PLAYLIST MANAGEMENT ============


def _playlist_list(
    format: str = "text",
    export: str = "none",
    full: bool = False,
) -> str:
    """Internal: Get all playlists."""
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

        # Add token warning if text format
        warning = get_token_expiration_warning()
        prefix = f"{warning}\n\n" if warning and format == "text" else ""

        return prefix + format_output(playlist_data, format, export, full, "playlists")

    except requests.exceptions.RequestException as e:
        return f"API Error: {str(e)}"
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
        return f"API Error: {str(e)}"
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

    # Use AppleScript (only if we don't have API ID)
    if use_applescript and not use_api:
        if not APPLESCRIPT_AVAILABLE:
            return "Error: playlist_name requires macOS"
        # Use native AppleScript search (fast, same as Music app search field)
        success, result = asc.search_playlist(resolved.applescript_name, query)
        if not success:
            return f"Error: {_format_applescript_error(str(result), 'search playlist')}"
        for t in result:
            track_id = t.get("id", "")
            matches.append({"name": t["name"], "artist": t["artist"], "id": track_id})
    else:
        # API path: manually filter tracks (cross-platform)
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
        return f"API Error: {str(e)}"
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
        return f"API Error: {str(e)}"


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
    auto_search: Optional[bool] = None,
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
            return resolved.error

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
                "token, run: applemusic-mcp generate-token"
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
            return "Error: Playlist name requires macOS (use playlist ID like 'p.XXX' for cross-platform)"

        # Apply auto_search preference once
        if auto_search is None:
            prefs = get_user_preferences()
            auto_search = prefs["auto_search"]

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
                            f"Some user-created playlists silently revert "
                            f"AppleScript edits server-side; adding manually via "
                            f"Music.app's right-click → Add to Playlist usually works."
                        )
            if success:
                if split_match:
                    s_name, s_artist = split_match
                    steps.append(f"Resolved '{name}' → '{s_name}' by '{s_artist}'")
                    added.append(f"{s_name} - {s_artist}")
                else:
                    added.append(f"{name} - {track_artist}" if track_artist else name)
            elif "Track not found" in result and auto_search:
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
                # Library miss without auto_search — common new-user surprise.
                # Hint at the right next step rather than just stopping at
                # "not found" with no path forward.
                errors.append(
                    f"{name}: not in your library. Set auto_search=True to "
                    "find it via the Apple Music catalog (uses API if a "
                    "token is configured, UI automation on macOS otherwise)."
                )
            else:
                errors.append(f"{name}: {result}")

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
                    "token, run: applemusic-mcp generate-token"
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
                    errors.append(f"{name}: {result}")
                elif not verify:
                    added.append(f"{name} - {artist_name}" if artist_name else name)
                elif _verify_track_in_playlist(
                    resolved.applescript_name, name, artist_name or None
                ):
                    added.append(f"{name} - {artist_name}" if artist_name else name)
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
                        added.append(f"{name} - {artist_name}" if artist_name else name)
                    else:
                        errors.append(
                            f"{name}: AppleScript reported success but track did not "
                            f"persist in playlist after retry. Some user-created "
                            f"playlists silently revert AppleScript edits server-side; "
                            f"adding manually via Music.app's right-click → Add to "
                            f"Playlist usually works. "
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
            if auto_search is False or (
                auto_search is None and not get_user_preferences().get("auto_search")
            ):
                msg += "\n\n💡 Tip: Enable auto_search to automatically find and add tracks from catalog"
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
                    # API catalog search failed — try UI fallback for add-to-playlist
                    if APPLESCRIPT_AVAILABLE and resolved.applescript_name:
                        search_q = f"{name} {track_artist}".strip() if track_artist else name
                        ui_ok, ui_msg = asc.ui_add_to_playlist(
                            resolved.applescript_name, search_q, track_artist
                        )
                        if ui_ok:
                            steps.append(f"[UI] {ui_msg}")
                            continue  # Process next track, don't return early
                    steps.append(f"Could not find '{name}' in library or catalog")

        if not ids_list:
            # Check if UI fallback handled all tracks successfully
            ui_successes = [s for s in steps if s.startswith("[UI]")]
            if ui_successes:
                return "\n".join(steps)
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
            return "Error: Playlist name requires macOS (use playlist ID like 'p.XXX' for cross-platform)"

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
        return f"API Error: {str(e)}"
    except (FileNotFoundError, ValueError) as e:
        return str(e)


@mcp.tool()
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
    auto_search: Optional[bool] = None,
) -> str:
    """Playlist and folder operations. Actions: list, tracks, search, create, add, copy, move (macOS), path (macOS), remove (macOS), delete (macOS), rename (macOS). Folders support slash-separated paths (e.g. 'Summer/Chill/Deep'). For action='add', set auto_search=True to find tracks not already in the user's library — this is required to add catalog songs the user doesn't own."""
    action = action.lower().strip().replace("-", "_")

    if action == "list":
        return _playlist_list(format, export, full)
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
            # folder only = create a folder
            return _playlist_create_folder(folder)
        elif folder and name:
            # both = create playlist inside folder (create folder if needed)
            return _playlist_create_in_folder(name, folder, description)
        elif name:
            return _playlist_create(name, description)
        else:
            return "Error: name and/or folder required for create"
    elif action == "add":
        return _playlist_add(playlist, track, album, artist, allow_duplicates, verify, auto_search)
    elif action == "copy":
        return _playlist_copy(source, new_name)
    elif action == "remove":
        if err := _macos_only("remove"):
            return err
        return _playlist_remove(playlist, track, artist)
    elif action == "delete":
        if err := _macos_only("delete"):
            return err
        if folder:
            return _playlist_delete_folder(folder)
        playlist_name = name or playlist
        if not playlist_name:
            return "Error: name, playlist, or folder required for delete"
        return _playlist_delete(playlist_name)
    elif action == "rename":
        if err := _macos_only("rename"):
            return err
        if not new_name:
            return "Error: new_name required for rename"
        if folder:
            return _playlist_rename_folder(folder, new_name)
        playlist_name = name or playlist
        if not playlist_name:
            return "Error: playlist, name, or folder required for rename"
        return _playlist_rename(playlist_name, new_name)
    elif action == "create_folder":
        # Backward compat — redirect to create(folder=...)
        if err := _macos_only("create_folder"):
            return err
        if not name:
            return "Error: name required for create_folder"
        return _playlist_create_folder(name)
    elif action == "move":
        if err := _macos_only("move"):
            return err
        if not playlist:
            return "Error: playlist required for move"
        folder_target = folder or name
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
    elif action == "path":
        target = playlist or folder
        if target:
            return _playlist_path(target)
        else:
            return _playlist_tree()
    else:
        return f"Unknown action: {action}. Use: list, tracks, search, create, add, copy, move, path, remove (macOS), delete (macOS), rename (macOS)"


# ============ LIBRARY MANAGEMENT ============


@mcp.tool()
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
    """Your library. Actions: search, add, recently_played, recently_added, browse, rate, remove (macOS), snapshot. action='search' searches the user's local library only — for catalog (Apple Music's full library) use catalog(action='search'). action='add' on tokenless macOS uses Music.app UI automation (Accessibility permissions required) to add catalog tracks; with an API token, uses the REST API."""
    action = action.lower().strip().replace("-", "_")

    if action == "search":
        if not query:
            return "Error: query is required for search action"
        return _library_search(
            query, types, limit, format, export, full, fetch_explicit, clean_only
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
    elif action == "rate":
        if not rate_action:
            return "Error: rate_action required (love, dislike, get, set)"
        return _library_rate(rate_action, track, artist, stars)
    elif action == "remove":
        if err := _macos_only("remove"):
            return err
        return _library_remove(track, artist)
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
        return f"Unknown action: {action}. Use: search, add, recently_played, recently_added, browse, rate, remove, snapshot"


def _library_search(
    query: str,
    types: str = "songs",
    limit: int = 25,
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

    # Try AppleScript on macOS (faster for local searches)
    asc_error: Optional[str] = None
    if APPLESCRIPT_AVAILABLE:
        success, results = asc.search_library(query, types)
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

            return format_output(results, format, export, full, f"search_{query[:20]}")
        if not success:
            # Capture so the API-fallback error path can surface what really
            # broke. Without this, an AS failure followed by missing-token
            # would only show "Developer token not found" — hiding the cause.
            asc_error = str(results) if results else "AppleScript search failed"
        else:
            # AS succeeded with zero results — the song is genuinely not in
            # the user's library. On a tokenless macOS host, cascading to
            # the API path here would raise "Developer token not found" and
            # a Claude session reading that error would (correctly) tell
            # the user to run generate-token+authorize — which is the EXACT
            # misleading-message bug v0.9.3 and v0.9.4 fought against. Only
            # cascade when a token is actually configured (the API may see
            # cloud-synced tracks AS hasn't seen yet).
            if not _has_developer_token():
                return (
                    f"No {types} found in library. "
                    f"To search the Apple Music catalog instead, use "
                    f"catalog(action='search', query='...')."
                )
        # AppleScript failed or returned empty (and we have a token) - fall
        # through to API.

    # API fallback (or primary on non-macOS)
    try:
        headers = get_headers()
        response = requests.get(
            f"{BASE_URL}/me/library/search",
            headers=headers,
            params={"term": query, "types": "library-songs", "limit": min(limit, 25)},
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


def _library_add_track_via_ui(query: str, search_artist: str) -> tuple[bool, str]:
    """Tokenless macOS path for ``library(action="add", track=...)``.

    Delegates to ``asc.ui_add_to_library``'s popover-canonical flow, which
    handles (name, artist) matching internally.
    """
    ok, msg = asc.ui_add_to_library(query, search_artist)
    if not ok:
        return False, f"Failed to add to library: {msg}"
    # Verify the track actually appears in the local library before reporting
    # success. iCloud sync from the UI add is normally near-instantaneous,
    # but a brief poll covers slow sync. Without this, a downstream playlist-
    # add would fail with the more confusing "Track not found in library" error.
    for attempt in range(3):
        if attempt > 0:
            time.sleep(1.0)
        lib_ok, lib_results = asc.search_library(query, "songs")
        if lib_ok and lib_results:
            for r in lib_results:
                if not _loose_contains(query, r.get("name", "")):
                    continue
                if search_artist and not _loose_contains(search_artist, r.get("artist", "")):
                    continue
                return True, f"{r.get('name', query)} by {r.get('artist', search_artist)}"
    return False, (
        f"Add to Library button was clicked for '{query}' but the track "
        f"did not appear in the local library after retry. iCloud Library sync "
        f"may be paused or this track may not be available for library-add."
    )


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

    # Tokenless macOS path: route to UI automation for songs. Albums via
    # UI aren't supported yet — tell the user instead of leaking the API
    # token error.
    use_ui_path = not _has_developer_token() and APPLESCRIPT_AVAILABLE
    if use_ui_path and album and not track:
        # Pure album-add request without a token; can't fulfill via UI.
        # (When BOTH album and track are present, album acts as
        # disambiguation context and the track add via UI proceeds —
        # don't early-return in that case.)
        return (
            "Error: Adding albums to library via UI automation isn't "
            "supported yet — only individual tracks. To add this album "
            "without a token, add each track by name via "
            "library(action='add', track='Track Name'). To configure "
            "an API token: applemusic-mcp generate-token."
        )

    # Helper to add a song by catalog search
    def _add_track_by_search(name: str, search_artist: str) -> None:
        # Tokenless macOS: route to UI automation. The UI search returns
        # a single best-match name+artist; we don't get a fuzzy_result
        # diagnostic from this path, but the alternative is "you need an
        # API token," which is wrong on macOS.
        if use_ui_path:
            ok, msg = _library_add_track_via_ui(name, search_artist)
            if ok:
                added.append(msg)
            else:
                errors.append(f"{name}: {msg}")
            return

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
        # When use_ui_path is True (tokenless macOS) AND we got here, the
        # caller passed BOTH track and album — the early-return for pure
        # album-add at the top of _library_add only fires when there's no
        # track. UI-add for albums isn't supported, but we can't leak the
        # token error either; record a clean step-error and move on so
        # the track loop's UI-add successes still surface.
        if use_ui_path:
            errors.append(
                f"Album '{name}': UI library-add doesn't support albums "
                "yet; tracks were added individually if provided."
            )
            return
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
                # Direct catalog ID — UI automation path can't use this
                # (UI search needs a human-readable query, not an opaque
                # catalog ID). Tell the caller to switch input shape
                # rather than leaking the API token error.
                if use_ui_path:
                    errors.append(
                        f"Track ID {r.value}: catalog IDs require an API "
                        "token. To add this track without a token, pass "
                        "it by name instead."
                    )
                    continue
                success, msg = _add_to_library_api([r.value], "songs")
                if success:
                    added.append(f"Track ID {r.value}")
                else:
                    errors.append(f"Track {r.value}: {msg}")
            elif r.input_type in (InputType.NAME, InputType.JSON_OBJECT):
                # Search by name (UI path on tokenless macOS, API otherwise)
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
                # Direct catalog ID — same as track catalog-ID path: UI
                # automation can't resolve opaque IDs, so on tokenless
                # macOS surface a clear "use name instead" message
                # rather than letting _add_to_library_api leak the
                # token error.
                if use_ui_path:
                    errors.append(
                        f"Album ID {r.value}: catalog IDs require an API "
                        "token. To add this album without a token, pass "
                        "it by name instead."
                    )
                    continue
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
        return "No items added"


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
        return f"API Error: {str(e)}"
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
        return f"API Error: {str(e)}"
    except (FileNotFoundError, ValueError) as e:
        return str(e)


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
        return "Error: Could not resolve album"

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
        return f"API Error: {str(e)}"
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
        return "Error: Could not resolve album"

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
        return f"API Error: {str(e)}"
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

    # Try AppleScript first for songs (local, instant, no auth required)
    if APPLESCRIPT_AVAILABLE and item_type == "songs":
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
        return f"API Error: {str(e)}"
    except (FileNotFoundError, ValueError) as e:
        return str(e)


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
        return f"API Error: {str(e)}"
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
        return f"API Error: {str(e)}"
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
        return f"API Error: {str(e)}"
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
        return f"API Error: {str(e)}"
    except (FileNotFoundError, ValueError) as e:
        return str(e)


@mcp.tool()
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
        return f"API Error: {str(e)}"
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
        return f"API Error: {str(e)}"
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
        return f"API Error: {str(e)}"
    except (FileNotFoundError, ValueError) as e:
        return str(e)


# ============ RATINGS ============


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
        return "Error: Could not resolve track"

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
            return "Error: Star ratings require macOS"
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
            return "Error: Star ratings require macOS"
        success, rating_val = asc.get_rating(track_name, track_artist if track_artist else None)
        if success:
            s = rating_val // 20
            return f"{track_name}: {'★' * s}{'☆' * (5 - s)} ({rating_val}/100)"
        return f"Error: {_format_applescript_error(str(rating_val), 'get star rating')}"

    if action == "set":
        if not APPLESCRIPT_AVAILABLE:
            return "Error: Star ratings require macOS"
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

    # Love/dislike by name - try AppleScript first
    if APPLESCRIPT_AVAILABLE:
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

    return f"Track not found: {track_name}"


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
        return f"API Error: {str(e)}"
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
        return f"API Error: {str(e)}"
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
        return f"API Error: {str(e)}"
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
        return f"API Error: {str(e)}"
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
        return f"API Error: {str(e)}"
    except (FileNotFoundError, ValueError) as e:
        return str(e)


@mcp.tool()
def catalog(
    action: str = "search",
    query: str = "",
    types: str = "songs",
    album: str = "",
    artist: str = "",
    song_id: str = "",
    chart_type: str = "songs",
    term: str = "",
    limit: int = 15,
    offset: int = 0,
    format: str = "text",
    export: str = "none",
    full: bool = False,
    clean_only: Optional[bool] = None,
) -> str:
    """Apple Music catalog. Actions: search, album_tracks, album_details, song_details, artist_details, genres, suggestions."""
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
        return f"Unknown action: {action}. Use: search, album_tracks, album_details, song_details, artist_details, genres, suggestions"


# ============ SYSTEM MANAGEMENT ============


@mcp.tool()
def config(
    action: str = "info",
    days_old: int = 0,
    preference: str = "",
    value: Optional[bool] = None,
    string_value: str = "",
    limit: int = 20,
) -> str:
    """Config and cache. Actions: info, auth-status, set-pref, list-storefronts, audit-log, clear-tracks, clear-exports, clear-audit-log."""
    try:
        action = action.lower()

        # === SET PREFERENCE ===
        if action == "set-pref":
            bool_prefs = ["fetch_explicit", "reveal_on_library_miss", "clean_only", "auto_search"]
            string_prefs = ["storefront"]
            all_prefs = bool_prefs + string_prefs

            if not preference:
                return f"Error: set-pref requires 'preference' parameter. Valid: {', '.join(all_prefs)}"

            if preference not in all_prefs:
                return f"Error: preference must be one of: {', '.join(all_prefs)}"

            # Determine the value to set
            if preference in string_prefs:
                if not string_value:
                    return f"Error: '{preference}' requires 'string_value' parameter (e.g., string_value='gb')"
                pref_value = string_value.lower()
            else:
                if value is None:
                    return f"Error: '{preference}' requires 'value' parameter (true or false)"
                pref_value = value

            # Load current config
            from .auth import load_config, get_config_dir as get_auth_config_dir

            config = load_config()

            # Update preferences
            if "preferences" not in config:
                config["preferences"] = {}
            old_value = config.get("preferences", {}).get(preference)
            config["preferences"][preference] = pref_value

            # Save back
            config_file = get_auth_config_dir() / "config.json"
            with open(config_file, "w") as f:
                json.dump(config, f, indent=2)

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
            output.append(f"  reveal_on_library_miss: {prefs['reveal_on_library_miss']}")
            output.append(f"  clean_only: {prefs['clean_only']}")
            output.append(f"  auto_search: {prefs['auto_search']}")
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

        # === AUTH STATUS ===
        if action in ("auth-status", "auth_status"):
            return _config_auth_status()

        # === UNKNOWN ACTION ===
        valid_actions = "info, set-pref, list-storefronts, audit-log, clear-tracks, clear-exports, clear-audit-log, auth-status"
        return f"Error: Unknown action '{action}'. Valid: {valid_actions}"

    except Exception as e:
        return f"Error: {str(e)}"


def _config_auth_status() -> str:
    """Check if authentication tokens are valid and API is accessible."""
    config_dir = get_config_dir()
    dev_token_file = config_dir / "developer_token.json"
    user_token_file = config_dir / "music_user_token.json"

    status = []

    # Check developer token
    if dev_token_file.exists():
        try:
            with open(dev_token_file) as f:
                data = json.load(f)
            expires = data.get("expires", 0)
            days_left = (expires - time.time()) / 86400

            if days_left < 0:
                status.append("Developer Token: EXPIRED - Run: applemusic-mcp generate-token")
            elif days_left < 30:
                status.append(
                    f"Developer Token: ⚠️ EXPIRES IN {int(days_left)} DAYS - Run: applemusic-mcp generate-token"
                )
            else:
                status.append(f"Developer Token: OK ({int(days_left)} days remaining)")
        except Exception:
            status.append("Developer Token: ERROR reading file")
    else:
        status.append("Developer Token: MISSING - Run: applemusic-mcp generate-token")

    # Check user token
    if user_token_file.exists():
        status.append("Music User Token: OK")
    else:
        status.append("Music User Token: MISSING - Run: applemusic-mcp authorize")

    # Test API connection
    if dev_token_file.exists() and user_token_file.exists():
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
            elif response.status_code == 401:
                status.append(
                    "API Connection: UNAUTHORIZED - Token may be expired. Run: applemusic-mcp authorize"
                )
            else:
                status.append(f"API Connection: FAILED ({response.status_code})")
        except Exception as e:
            status.append(f"API Connection: ERROR - {str(e)}")

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
    snap_dir = Path.home() / ".cache" / "applemusic-mcp" / "snapshots"
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
# AppleScript-powered tools (macOS only)
# =============================================================================
# These tools provide capabilities not available through the REST API:
# - Playback control (play, pause, skip)
# - Delete tracks from playlists
# - Delete playlists
# - Volume and shuffle control
# - Get currently playing track

if APPLESCRIPT_AVAILABLE:

    @mcp.tool()
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
    ) -> str:
        """Playback (macOS). Actions: play, control, now_playing, settings, reveal, airplay."""
        action = action.lower().strip().replace("-", "_")

        if action == "play":
            return _playback_play(
                track, playlist, album, artist, shuffle, reveal, add_to_library, url
            )
        elif action == "control":
            if not control:
                return "Error: control param required. Use: play, pause, stop, next, previous, seek"
            return _playback_control(control, seconds)
        elif action == "now_playing":
            return _playback_now_playing()
        elif action == "settings":
            return _playback_settings(volume, shuffle_mode, repeat)
        elif action == "reveal":
            name = track_name or track
            if not name:
                return "Error: track_name or track required for reveal action"
            return _playback_reveal(name, artist)
        elif action == "airplay":
            return _playback_airplay(device_name)
        else:
            return f"Unknown action: {action}. Use: play, control, now_playing, settings, reveal, airplay"

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

            success, result = asc.open_catalog_and_play(url, shuffle=shuffle)
            if success:
                audit_log.log_action("play_url", {"url": url, "result": result})
                return result
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
            # Apply user preferences for reveal when library miss
            if reveal is None:
                prefs = get_user_preferences()
                reveal = prefs["reveal_on_library_miss"]

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
                        audit_log.log_action(
                            "play_album", {"album": lib_album, "artist": lib_artist}
                        )
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
                                for attempt in range(PLAY_TRACK_MAX_ATTEMPTS):
                                    if attempt > 0:
                                        time.sleep(PLAY_TRACK_RETRY_DELAY)
                                    search_ok2, lib_results2 = asc.search_library(
                                        album_name, "albums"
                                    )
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
                                return f"[Catalog→Library] Added but sync pending: {album_name} by {album_artist}"
                            return f"[Catalog] Failed to add: {add_msg}"

                        # Option 2: Open in Music app (user must click play)
                        if reveal and album_url:
                            success, msg = asc.open_catalog_song(album_url)
                            if success:
                                return (
                                    f"[Catalog] Opened: {album_name} by {album_artist} (click play)"
                                )
                            return f"[Catalog] {msg}"

                        # Neither flag set - explain options
                        return (
                            f"[Catalog] Found: {album_name} by {album_artist}. "
                            f"Use reveal=True to open in Music, or add_to_library=True to save & play."
                        )
            except requests.exceptions.RequestException as e:
                return f"API Error searching catalog: {str(e)}"
            except (FileNotFoundError, ValueError) as e:
                return f"Error: {str(e)}"
            return f"Album not found: {album}"

        # === TRACK ===
        # Apply user preferences for reveal when library miss
        if reveal is None:
            prefs = get_user_preferences()
            reveal = prefs["reveal_on_library_miss"]

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
                                        return f"[Catalog→Library] Playing: {track_name} by {track_artist}"
                                return f"[Catalog→Library] Added but sync pending: {track_name} by {track_artist}"
                            return f"[Catalog] Failed to add: {add_msg}"

                        # UI play first — before reveal so reveal_on_library_miss
                        # preference doesn't shadow direct playback.
                        ui_ok, ui_msg = _try_ui_catalog_play(track_name, track_artist)
                        if ui_ok:
                            return ui_msg

                        if reveal:
                            if song_url:
                                success, msg = asc.open_catalog_song(song_url)
                                if success:
                                    return f"[Catalog] Opened: {track_name} by {track_artist} (click play)"
                                return f"[Catalog] {msg}"
                            return f"[Catalog] No URL available for: {track_name}"

                        return (
                            f"[Catalog] Found: {track_name} by {track_artist}. "
                            f"Use reveal=True to open in Music, or add_to_library=True to save & play."
                        )
            except Exception:
                pass
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
                    return f"[Catalog→Library] Added but sync pending: {song_name} by {song_artist}"
                return f"[Catalog] Failed to add: {add_msg}"

            # Option 2: UI play — works without adding to library.
            # Runs before reveal so reveal_on_library_miss preference doesn't
            # shadow direct playback when Music.app automation is available.
            ui_ok, ui_msg = _try_ui_catalog_play(song_name, song_artist)
            if ui_ok:
                return ui_msg
            if ui_msg is not None:
                # UI was attempted and failed — surface the reason rather than
                # falling through to reveal/error. APPLESCRIPT_AVAILABLE=False
                # returns (False, None), in which case we do fall through.
                return f"[UI Catalog failed: {ui_msg}] Falling back — {song_name} by {song_artist}"

            # Option 3: Open in Music app (user must click play)
            if reveal:
                if song_url:
                    success, msg = asc.open_catalog_song(song_url)
                    if success:
                        return f"[Catalog] Opened: {song_name} by {song_artist} (click play)"
                    return f"[Catalog] {msg}"
                return f"[Catalog] No URL available for: {song_name}"

            return (
                f"[Catalog] Found: {song_name} by {song_artist}. "
                f"Use reveal=True to open in Music, or add_to_library=True to save & play."
            )

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
            return (
                f"Invalid action: {action}. Use: play, pause, playpause, stop, next, previous, seek"
            )

        success, result = action_map[action]()
        if success:
            audit_log.log_action("playback_control", {"control": action, "seconds": None})
            return f"Playback: {action}"
        return f"Error: {result}"

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
                    f"{err_prefix}: AppleScript reported success but track still "
                    f"appears in '{resolved.applescript_name}'. Some user-created "
                    f"playlists silently revert AppleScript edits server-side; "
                    f"removing manually via Music.app usually works."
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
                        t
                        for t in lib_results
                        if _loose_contains(track_artist, t.get("artist") or "")
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
                success, result = asc.remove_from_library(
                    track_name=r.value, artist=r.artist or None
                )
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


def main():
    """Run the MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
