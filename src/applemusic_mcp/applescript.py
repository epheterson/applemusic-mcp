"""AppleScript integration for Music.app on macOS.

This module provides direct control of the Music app via AppleScript,
enabling capabilities not available through the REST API like playback
control, deleting tracks from playlists, and deleting playlists.

Only available on macOS with the Music app installed.

Security Notes:
    - All user input (track names, playlist names, etc.) is escaped via
      _escape_for_applescript() which escapes backslashes first, then quotes,
      before embedding in AppleScript strings. This prevents injection attacks.
    - Scripts are executed via subprocess.run() with capture_output=True
      and a 30-second timeout to prevent hangs.
    - The osascript binary location is verified via shutil.which() before use.
"""

import re
import subprocess
import sys
import shutil
import time
from typing import Optional


def is_available() -> bool:
    """Check if AppleScript is available (macOS with osascript)."""
    return sys.platform == "darwin" and shutil.which("osascript") is not None


def _escape_for_applescript(s: str) -> str:
    """Escape a string for safe use in AppleScript.

    Backslashes must be escaped first, then quotes, to prevent
    injection attacks and handle edge cases like 'Playlist\\Test'.

    Also strips control characters (newlines, tabs, carriage returns)
    which could break out of AppleScript string literals. osascript
    accepts literal newlines inside quoted strings, so an unescaped
    newline followed by '& do shell script "..."' is a real injection
    vector — the shell command executes even if the overall expression
    errors out.
    """
    # Strip control characters that could break out of string literals
    s = s.replace("\r\n", " ").replace("\r", " ").replace("\n", " ").replace("\t", " ")
    return s.replace("\\", "\\\\").replace('"', '\\"')


# "Smart punctuation" the iTunes Store / typesetting interchanges with ASCII.
# Music.app stores many titles with typographic glyphs — the right single
# quote (U+2019, "That's a No No"), curly double quotes, or a one-character
# ellipsis (U+2026, "Wait…") — while users and the model type the ASCII forms
# ("That's", '"', "Wait..."). AppleScript's `contains` is glyph-exact for
# punctuation — there is no "ignoring punctuation" — so a literal
# `name contains "Wait..."` misses a title stored as "Wait…", and vice versa.
# We sidestep the character entirely by splitting on every ambiguous form.
#
# Deliberately EXCLUDED: hyphens / en- & em-dashes. Hyphens are far too common
# in ordinary titles ("Peek-A-Boo") — splitting on them would shatter the query
# into tiny fragments that false-match unrelated tracks. The apostrophe,
# double-quote, and ellipsis families above are the high-value, low-risk set.
# See issue #26.
_AMBIGUOUS_PUNCT = [
    "...",  # multi-char first so it's preferred over any single char below
    "'",
    "’",
    "‘",
    "ʼ",
    "‛",  # apostrophe family: U+0027 U+2019 U+2018 U+02BC U+201B
    '"',
    "“",
    "”",  # double-quote family: U+0022 U+201C U+201D
    "…",  # horizontal ellipsis U+2026 (ASCII equivalent is the "..." above)
]
_AMBIGUOUS_PUNCT_RE = re.compile("|".join(re.escape(p) for p in _AMBIGUOUS_PUNCT))


def _name_contains_clause(track_name: str) -> str:
    """Build a ``name contains …`` AppleScript clause robust to smart punctuation.

    For a title containing punctuation that the store may have typeset, we never
    match the punctuation glyph itself; instead we split on every ambiguous form
    and require the name to contain each non-empty surrounding fragment::

        "That's a No No" -> (name contains "That" and name contains "s a No No")
        "Wait..."        -> name contains "Wait"   # also matches stored "Wait…"

    That matches whether Music stored the ASCII form or the typographic one
    (curly apostrophe/quotes, one-char ellipsis), and is also immune to the
    glyph being normalized in transit between the client and this server.
    Titles with no ambiguous punctuation produce the simple single-``contains``
    clause unchanged.

    The fragments are ANDed together (and, at the call sites, further ANDed
    with an ``artist contains`` filter), so short fragments like ``"s"`` can't
    cause a false match on their own.
    """
    fragments = [f for f in _AMBIGUOUS_PUNCT_RE.split(track_name) if f.strip()]
    # No usable fragment (title was only punctuation/whitespace) — fall back to
    # the original so we still produce a valid, if glyph-exact, clause.
    if not fragments:
        return f'name contains "{_escape_for_applescript(track_name)}"'
    clauses = [f'name contains "{_escape_for_applescript(f)}"' for f in fragments]
    # One surviving fragment (no ambiguous punctuation, or it was only at the
    # edges) needs no parens; build from the FRAGMENT, not the original — the
    # original may still carry the stripped glyph (e.g. "Wait..." -> "Wait").
    if len(clauses) == 1:
        return clauses[0]
    return "(" + " and ".join(clauses) + ")"


def _find_playlist_applescript(safe_name: str) -> str:
    """Generate AppleScript code to find a playlist by name.

    Tries user playlists first (exact, then partial match), then falls
    back to folder playlists (exact, then partial match).

    Args:
        safe_name: Already-escaped playlist name

    Returns:
        AppleScript code snippet that sets targetPlaylist variable
    """
    return f"""
        try
            -- Try exact match on user playlists
            set targetPlaylist to first user playlist whose name is "{safe_name}"
        on error
            try
                -- Partial match on user playlists
                set targetPlaylist to first user playlist whose name contains "{safe_name}"
            on error
                try
                    -- Exact match on folder playlists
                    set targetPlaylist to first folder playlist whose name is "{safe_name}"
                on error
                    try
                        -- Partial match on folder playlists
                        set targetPlaylist to first folder playlist whose name contains "{safe_name}"
                    on error
                        return "ERROR:Playlist not found"
                    end try
                end try
            end try
        end try"""


def _parse_library_track_line(line: str) -> Optional[dict]:
    """Parse one ``|||``-delimited library track line into a dict.

    Format produced by the AppleScript blocks in get_library_songs /
    get_library_songs_page / search_library:
        name|||artist|||album|||duration_seconds|||genre|||year|||id|||explicit
    Returns None when the line is malformed (skipped by callers).
    The 8th `explicit` field is recent; older outputs may stop at 7 fields.
    """
    if "|||" not in line:
        return None
    parts = line.split("|||")
    if len(parts) < 7:
        return None
    try:
        dur_sec = float(parts[3])
        minutes = int(dur_sec) // 60
        seconds = int(dur_sec) % 60
        duration = f"{minutes}:{seconds:02d}"
    except (ValueError, TypeError):
        duration = ""
    explicit = "Unknown"
    if len(parts) >= 8:
        explicit = "Yes" if parts[7].lower() == "true" else "No"
    return {
        "name": parts[0],
        "artist": parts[1],
        "album": parts[2],
        "duration": duration,
        "genre": parts[4],
        "year": parts[5],
        "id": parts[6],
        "explicit": explicit,
    }


def _track_filter_clause(
    track_name: str = "", artist: Optional[str] = None, track_id: Optional[str] = None
) -> Optional[str]:
    """Build the ``whose ...`` AppleScript clause that selects one track.

    Used by remove_track_from_playlist / remove_from_library — both want
    "match by persistent ID exactly, or fall back to name (+ optional
    artist) contains".

    Returns the clause (without leading/trailing space) or None when the
    caller didn't provide anything to match on.
    """
    if track_id:
        return f'whose persistent ID is "{_escape_for_applescript(track_id)}"'
    if track_name:
        name_clause = _name_contains_clause(track_name)
        if artist:
            safe_artist = _escape_for_applescript(artist)
            return f'whose {name_clause} and artist contains "{safe_artist}"'
        return f"whose {name_clause}"
    return None


def _library_track_query(track_name: str, artist: Optional[str] = None) -> str:
    """Build the AppleScript fragment that resolves a single library track.

    Used by play/love/dislike/rating/reveal — all of them want the same
    "first track in library playlist 1 matching name (and optionally artist)"
    selector. Centralizing avoids drift between callers.

    Both inputs are escaped here; callers pass raw user-supplied strings.
    """
    name_clause = _name_contains_clause(track_name)
    if artist:
        safe_artist = _escape_for_applescript(artist)
        return (
            f"first track of library playlist 1 whose {name_clause} "
            f'and artist contains "{safe_artist}"'
        )
    return f"first track of library playlist 1 whose {name_clause}"


def _resolve_library_track_applescript(track_name: str, artist: Optional[str] = None) -> str:
    """Emit AppleScript that resolves ``targetTrack`` from library playlist 1.

    Two-stage matcher (issue #26), embedded inside a
    ``tell application "Music" … end tell`` block:

    1. **Fast pass** — a glyph-exact ``whose`` filter (punctuation-robust via
       :func:`_name_contains_clause`). Music resolves this in-engine at C speed
       with no data transfer, so the common case never pays for stage 2.
    2. **Fold fallback** — only on a miss. Bulk-fetch every track name (and
       artist, when given) in a SINGLE Apple Event, then match in-memory under
       ``ignoring punctuation and diacriticals``. That uses Apple's own Unicode
       equivalence tables, so curly quotes, ellipses, and accents (é≈e) all
       match their ASCII forms — generic, no hand-maintained glyph list.

       The bulk-fetch is the crux: ``name of t`` *inside* a repeat is one Apple
       Event per track (~17s on a 12k library), while ``get name of every
       track`` is one event for the whole list (~0s). Music ignores AppleScript's
       ``ignoring`` context inside a ``whose`` filter, so the fold must happen in
       this in-memory loop, not in stage 1.

    Emits ``set targetTrack to …`` on success, or
    ``return "ERROR:Track not found: …"`` on a total miss. The returned snippet
    is indented to sit directly under the ``tell`` line.
    """
    safe_track = _escape_for_applescript(track_name)
    fast_query = _library_track_query(track_name, artist)
    if artist:
        safe_artist = _escape_for_applescript(artist)
        artist_fetch = "\n            set allArtists to (get artist of every track of lib)"
        match_cond = (
            f'(((item i of allNames) as text) contains "{safe_track}") '
            f'and (((item i of allArtists) as text) contains "{safe_artist}")'
        )
    else:
        artist_fetch = ""
        match_cond = f'((item i of allNames) as text) contains "{safe_track}"'
    return f"""        try
            set targetTrack to {fast_query}
        on error
            set lib to library playlist 1
            set allNames to (get name of every track of lib){artist_fetch}
            set idx to 0
            ignoring punctuation and diacriticals
                repeat with i from 1 to (count of allNames)
                    if {match_cond} then
                        set idx to i
                        exit repeat
                    end if
                end repeat
            end ignoring
            if idx is 0 then return "ERROR:Track not found: {safe_track}"
            set targetTrack to track idx of library playlist 1
        end try"""


def run_applescript(script: str) -> tuple[bool, str]:
    """Execute AppleScript and return (success, output/error).

    Args:
        script: AppleScript code to execute

    Returns:
        Tuple of (success: bool, output: str)
        On success, output is the script's return value.
        On failure, output is the error message.
    """
    try:
        result = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            return True, result.stdout.strip()
        else:
            return False, result.stderr.strip()
    except subprocess.TimeoutExpired:
        return False, "AppleScript timed out after 30 seconds"
    except Exception as e:
        return False, str(e)


# AppleScript error categories. Used to map osascript stderr text to a
# stable category so callers can produce actionable user-facing messages
# without each callsite re-matching the same regexes.
ERROR_MUSIC_NOT_RUNNING = "music_not_running"
ERROR_AUTOMATION_DENIED = "automation_denied"
ERROR_TIMEOUT = "timeout"
ERROR_SYNTAX = "syntax"
ERROR_UNKNOWN = "unknown"


def classify_error(text: str) -> str:
    """Categorize an AppleScript error string.

    The osascript stderr surface is messy: error wording shifts across macOS
    versions, but the numeric error codes (-609, -1728, -1743, etc.) and a
    handful of stable phrases ("Not authorized", "isn't running") are
    reliable. Match those first; fall through to ``unknown`` so callers can
    still surface the raw text.

    Categories:
      - music_not_running: Music.app isn't running or has crashed (-609,
        -10810, "isn't running", "Connection is invalid")
      - automation_denied: parent process lacks Automation permission for
        Music.app (-1743, "Not authorized", "not allowed assistive access")
      - timeout: our 30s subprocess timeout fired
      - syntax: AppleScript itself rejected the script (developer bug)
      - unknown: anything else — caller should surface raw error
    """
    if not text:
        return ERROR_UNKNOWN
    t = text.lower()

    # Timeout is our own message — match exactly.
    if "applescript timed out" in t:
        return ERROR_TIMEOUT

    # Automation permissions denied. -1743 is the canonical code; phrasings
    # vary across macOS versions but consistently mention authorization.
    # NOTE: bare "not allowed" is too broad — Music.app emits "operation
    # not allowed on smart playlists" and similar logic-level errors.
    # Match the full Automation-denial phrases instead.
    if (
        "-1743" in t
        or "not authorized" in t
        or "not allowed assistive" in t
        or "assistive access" in t
        or "not authorised" in t  # British English variant in some locales
    ):
        return ERROR_AUTOMATION_DENIED

    # Music.app not running / connection invalid. -609 is "Connection is
    # invalid"; -10810 is "Application isn't running"; phrasing variants
    # cover both startup-time and mid-session crashes.
    if (
        "-609" in t
        or "-10810" in t
        or "isn't running" in t
        or "is not running" in t
        or "connection is invalid" in t
        or "can't get application" in t
    ):
        return ERROR_MUSIC_NOT_RUNNING

    # AppleScript-level syntax errors (means we have a bug, not the user).
    # Parens explicit so future readers don't have to remember Python's
    # `and` > `or` precedence rule.
    if ("syntax error" in t) or ("expected" in t and "but found" in t):
        return ERROR_SYNTAX

    # Note: -1728 ("can't get") deliberately classifies as ERROR_UNKNOWN.
    # It's a logic-level error (track/playlist doesn't exist) rather than
    # an environmental one, so callers with legitimate API fallback paths
    # should still cascade. v0.9.3 handled the broken-track iteration
    # case at the AppleScript level, not via this classifier.
    return ERROR_UNKNOWN


# =============================================================================
# Playback Control
# =============================================================================


def play() -> tuple[bool, str]:
    """Start or resume playback."""
    return run_applescript('tell application "Music" to play')


def pause() -> tuple[bool, str]:
    """Pause playback."""
    return run_applescript('tell application "Music" to pause')


def playpause() -> tuple[bool, str]:
    """Toggle play/pause."""
    return run_applescript('tell application "Music" to playpause')


def stop() -> tuple[bool, str]:
    """Stop playback."""
    return run_applescript('tell application "Music" to stop')


def next_track() -> tuple[bool, str]:
    """Skip to next track."""
    return run_applescript('tell application "Music" to next track')


def previous_track() -> tuple[bool, str]:
    """Go to previous track."""
    return run_applescript('tell application "Music" to previous track')


def get_player_state() -> tuple[bool, str]:
    """Get current player state (playing, paused, stopped)."""
    return run_applescript('tell application "Music" to get player state as string')


def get_current_track() -> tuple[bool, dict]:
    """Get info about currently playing track.

    Returns:
        Tuple of (success, track_info_dict or error_string)
    """
    script = """
    tell application "Music"
        if player state is stopped then
            return "STOPPED"
        end if
        set t to current track
        set output to ""
        set output to output & "name:" & (name of t) & "\\n"
        set output to output & "artist:" & (artist of t) & "\\n"
        set output to output & "album:" & (album of t) & "\\n"
        set output to output & "duration:" & (duration of t) & "\\n"
        set output to output & "position:" & (player position) & "\\n"
        try
            set output to output & "genre:" & (genre of t) & "\\n"
        end try
        try
            set output to output & "year:" & (year of t) & "\\n"
        end try
        return output
    end tell
    """
    success, output = run_applescript(script)
    if not success:
        return False, output
    if output == "STOPPED":
        return True, {"state": "stopped"}

    # Parse key:value pairs
    track_info = {"state": "playing"}
    for line in output.split("\n"):
        if ":" in line:
            key, value = line.split(":", 1)
            track_info[key.strip()] = value.strip()
    return True, track_info


def get_volume() -> tuple[bool, int | str]:
    """Get current volume (0-100).

    Returns:
        Tuple of (success, volume 0-100 or error message string)
    """
    success, output = run_applescript('tell application "Music" to get sound volume')
    if success:
        try:
            return True, int(output)
        except ValueError:
            return False, f"Invalid volume value: {output}"
    return False, output


def set_volume(volume: int) -> tuple[bool, str]:
    """Set volume (0-100)."""
    volume = max(0, min(100, volume))
    return run_applescript(f'tell application "Music" to set sound volume to {volume}')


def get_shuffle() -> tuple[bool, bool | str]:
    """Get shuffle state.

    Returns:
        Tuple of (success, shuffle enabled bool or error message string)
    """
    success, output = run_applescript('tell application "Music" to get shuffle enabled')
    if success:
        return True, output.lower() == "true"
    return False, output


def set_shuffle(enabled: bool) -> tuple[bool, str]:
    """Set shuffle on/off."""
    value = "true" if enabled else "false"
    return run_applescript(f'tell application "Music" to set shuffle enabled to {value}')


def get_repeat() -> tuple[bool, str]:
    """Get repeat mode (off, one, all)."""
    return run_applescript('tell application "Music" to get song repeat as string')


def set_repeat(mode: str) -> tuple[bool, str]:
    """Set repeat mode (off, one, all)."""
    if mode not in ("off", "one", "all"):
        return False, f"Invalid repeat mode: {mode}. Use 'off', 'one', or 'all'"
    return run_applescript(f'tell application "Music" to set song repeat to {mode}')


def seek(position: float) -> tuple[bool, str]:
    """Seek to position in seconds."""
    return run_applescript(f'tell application "Music" to set player position to {position}')


# =============================================================================
# Playlist Operations
# =============================================================================


def get_playlists() -> tuple[bool, list[dict]]:
    """Get all user playlists with details.

    Returns:
        Tuple of (success, list of playlist dicts or error string)
    """
    script = """
    tell application "Music"
        set output to ""
        repeat with p in user playlists
            set pName to name of p
            set pId to persistent ID of p
            set pSmart to smart of p
            set pCount to count of tracks of p
            try
                set pTime to time of p
            on error
                set pTime to "0:00"
            end try
            set output to output & pName & "|||" & pId & "|||" & pSmart & "|||" & pCount & "|||" & pTime & "\\n"
        end repeat
        return output
    end tell
    """
    success, output = run_applescript(script)
    if not success:
        return False, output

    playlists = []
    for line in output.split("\n"):
        if "|||" in line:
            parts = line.split("|||")
            if len(parts) >= 5:
                playlists.append(
                    {
                        "name": parts[0],
                        "id": parts[1],
                        "smart": parts[2].lower() == "true",
                        "track_count": int(parts[3]) if parts[3].isdigit() else 0,
                        "duration": parts[4],
                    }
                )
    return True, playlists


def _get_playlist_tracks_bulk(safe_name: str, limit: int) -> tuple[bool, str]:
    """Try bulk property fetch for playlist tracks (fast path).

    Returns (success, output) where output is raw AppleScript output or error.
    """
    script = f"""
    tell application "Music"
{_find_playlist_applescript(safe_name)}

        set allTracks to tracks of targetPlaylist
        set trackCount to count of allTracks
        if trackCount is 0 then return ""

        -- Bulk fetch all properties at once (much faster than per-track)
        set allNames to name of allTracks
        set allArtists to artist of allTracks
        set allAlbums to album of allTracks
        set allDurations to duration of allTracks
        set allGenres to genre of allTracks
        set allYears to year of allTracks
        set allIds to persistent ID of allTracks

        -- Combine into output
        set output to ""
        set maxTracks to {limit}
        if trackCount < maxTracks then set maxTracks to trackCount
        repeat with i from 1 to maxTracks
            set tName to item i of allNames
            set tArtist to item i of allArtists
            set tAlbum to item i of allAlbums
            set tDuration to item i of allDurations
            set tGenre to item i of allGenres
            set tYear to item i of allYears
            set tId to item i of allIds
            set output to output & tName & "|||" & tArtist & "|||" & tAlbum & "|||" & tDuration & "|||" & tGenre & "|||" & tYear & "|||" & tId & "\\n"
        end repeat
        return output
    end tell
    """
    return run_applescript(script)


def _get_playlist_tracks_slow(safe_name: str, limit: int) -> tuple[bool, str]:
    """Per-track iteration fallback for playlists with shared tracks (slow path).

    Optimized for shared tracks: skips genre/year (saves ~33% time).
    Returns (success, output) where output is raw AppleScript output or error.
    """
    script = f"""
    tell application "Music"
{_find_playlist_applescript(safe_name)}

        set allTracks to tracks of targetPlaylist
        set trackCount to count of allTracks
        if trackCount is 0 then return ""

        -- Per-track iteration (slower but handles shared tracks)
        -- Optimized: skip genre/year to reduce try/catch overhead
        set output to ""
        set maxTracks to {limit}
        if trackCount < maxTracks then set maxTracks to trackCount
        repeat with i from 1 to maxTracks
            set t to item i of allTracks
            try
                set tName to name of t
                set tArtist to artist of t
                set tAlbum to album of t
                set tDuration to duration of t
                set tId to persistent ID of t
                -- Skip genre/year for speed (shared tracks typically have them but try/catch is expensive)
                set output to output & tName & "|||" & tArtist & "|||" & tAlbum & "|||" & tDuration & "|||||||||" & tId & "\\n"
            on error
                -- Skip tracks that can't be read (extremely rare)
            end try
        end repeat
        return output
    end tell
    """
    return run_applescript(script)


def get_playlist_tracks(playlist_name: str, limit: int = 500) -> tuple[bool, list[dict]]:
    """Get tracks in a playlist by name.

    Uses fast bulk fetch when possible, falls back to per-track iteration
    for playlists containing shared tracks (Apple Music subscription tracks).

    Args:
        playlist_name: Name of the playlist
        limit: Maximum number of tracks to return (default 500)

    Returns:
        Tuple of (success, list of track dicts or error string)
    """
    safe_name = _escape_for_applescript(playlist_name)

    # Try bulk fetch first (150x faster)
    success, output = _get_playlist_tracks_bulk(safe_name, limit)

    # If bulk fetch fails (e.g., shared tracks), fall back to per-track
    # Note: AppleScript uses straight apostrophe in "Can't get"
    if not success and "Can" in output and "get" in output:
        success, output = _get_playlist_tracks_slow(safe_name, limit)

    if not success:
        return False, output
    if output.startswith("ERROR:"):
        return False, output[6:]

    tracks = []
    for line in output.split("\n"):
        if "|||" in line:
            parts = line.split("|||")
            if len(parts) >= 7:
                # Format duration
                try:
                    dur_sec = float(parts[3])
                    minutes = int(dur_sec) // 60
                    seconds = int(dur_sec) % 60
                    duration = f"{minutes}:{seconds:02d}"
                except (ValueError, TypeError):
                    duration = ""

                tracks.append(
                    {
                        "name": parts[0],
                        "artist": parts[1],
                        "album": parts[2],
                        "duration": duration,
                        "genre": parts[4],
                        "year": parts[5],
                        "id": parts[6],
                    }
                )
    return True, tracks


def create_playlist(name: str, description: str = "") -> tuple[bool, str]:
    """Create a new playlist.

    Args:
        name: Playlist name
        description: Optional description

    Returns:
        Tuple of (success, playlist_id or error)
    """
    safe_name = _escape_for_applescript(name)
    safe_desc = _escape_for_applescript(description)

    if description:
        script = f"""
        tell application "Music"
            set newPlaylist to make new user playlist with properties {{name:"{safe_name}", description:"{safe_desc}"}}
            return persistent ID of newPlaylist
        end tell
        """
    else:
        script = f"""
        tell application "Music"
            set newPlaylist to make new user playlist with properties {{name:"{safe_name}"}}
            return persistent ID of newPlaylist
        end tell
        """
    return run_applescript(script)


def _resolve_folder_path_applescript(path: str) -> str:
    """Generate AppleScript to resolve a slash-separated folder path.

    e.g. "Summer/Chill/Deep" resolves to the "Deep" folder inside "Chill" inside "Summer".
    Sets `targetFolder` to the resolved folder playlist.

    Args:
        path: Slash-separated folder path

    Returns:
        AppleScript code block that sets targetFolder
    """
    parts = [p.strip() for p in path.split("/") if p.strip()]
    if not parts:
        return '        return "ERROR:Empty folder path"'

    safe_parts = [_escape_for_applescript(p) for p in parts]

    if len(safe_parts) == 1:
        return f"""        try
            set targetFolder to first folder playlist whose name is "{safe_parts[0]}"
        on error
            return "ERROR:Folder not found: {safe_parts[0]}"
        end try"""

    # Multi-level: walk down the tree
    lines = []
    lines.append(f"""        try
            set targetFolder to first folder playlist whose name is "{safe_parts[0]}"
        on error
            return "ERROR:Folder not found: {safe_parts[0]}"
        end try""")

    for part in safe_parts[1:]:
        lines.append(f"""        try
            set targetFolder to first folder playlist of targetFolder whose name is "{part}"
        on error
            return "ERROR:Subfolder not found: {part}"
        end try""")

    return "\n".join(lines)


def get_folder_tree() -> tuple[bool, str]:
    """Get the folder hierarchy as indented text (up to 3 levels deep).

    Note: Currently hardcoded to display root folders, their immediate
    subfolders, and playlists within those subfolders. Deeper nesting
    is not shown in the tree output (though it can be created and
    navigated via slash paths).

    Returns:
        Tuple of (success, indented tree string)
    """
    script = """
    tell application "Music"
        set output to ""
        set allFolders to every folder playlist
        set allPlaylists to every user playlist

        -- Build tree: for each folder, list its children
        repeat with f in allFolders
            set fName to name of f
            -- Check if this folder has a parent (is nested)
            try
                set pName to name of parent of f
                -- Skip nested folders in top-level listing, they'll appear under parent
            on error
                -- Top-level folder
                set output to output & "[" & fName & "]" & linefeed
                -- List folder's direct children
                repeat with p in allPlaylists
                    try
                        if name of parent of p is fName then
                            set output to output & "  " & name of p & linefeed
                        end if
                    end try
                end repeat
                -- List nested folders
                repeat with f2 in allFolders
                    try
                        if name of parent of f2 is fName then
                            set output to output & "  [" & name of f2 & "]" & linefeed
                            -- One more level deep
                            repeat with p2 in allPlaylists
                                try
                                    if name of parent of p2 is name of f2 then
                                        set output to output & "    " & name of p2 & linefeed
                                    end if
                                end try
                            end repeat
                        end if
                    end try
                end repeat
            end try
        end repeat

        -- Top-level playlists (no parent folder)
        repeat with p in allPlaylists
            try
                set pParent to parent of p
                -- Has parent, skip (already listed under folder)
            on error
                -- Check it's not a folder itself
                set isFolder to false
                repeat with f in allFolders
                    if name of f is name of p then
                        set isFolder to true
                        exit repeat
                    end if
                end repeat
                if not isFolder then
                    set output to output & name of p & linefeed
                end if
            end try
        end repeat

        return output
    end tell
    """
    return run_applescript(script)


def create_folder_path(path: str) -> tuple[bool, str]:
    """Create a folder path, creating intermediate folders as needed.

    e.g. "Summer/Chill/Deep" creates Summer, then Chill inside it, then Deep inside that.

    Args:
        path: Slash-separated folder path

    Returns:
        Tuple of (success, leaf folder ID or error)
    """
    parts = [p.strip() for p in path.split("/") if p.strip()]
    if not parts:
        return False, "Empty folder path"

    safe_parts = [_escape_for_applescript(p) for p in parts]

    # Build AppleScript that creates each level
    create_lines = []
    for i, part in enumerate(safe_parts):
        if i == 0:
            # Top-level: create if not exists
            create_lines.append(f"""
        try
            set folder{i} to first folder playlist whose name is "{part}"
        on error
            set folder{i} to make new folder playlist with properties {{name:"{part}"}}
        end try""")
        else:
            # Nested: create inside parent if not exists
            create_lines.append(f"""
        try
            set folder{i} to first folder playlist of folder{i-1} whose name is "{part}"
        on error
            set folder{i} to make new folder playlist with properties {{name:"{part}"}}
            move folder{i} to folder{i-1}
        end try""")

    last_idx = len(safe_parts) - 1
    script = f"""
    tell application "Music"
{"".join(create_lines)}
        return persistent ID of folder{last_idx}
    end tell
    """
    return run_applescript(script)


def create_folder(name: str) -> tuple[bool, str]:
    """Create a new folder playlist.

    Args:
        name: Folder name

    Returns:
        Tuple of (success, folder_id or error)
    """
    safe_name = _escape_for_applescript(name)
    script = f"""
    tell application "Music"
        set newFolder to make new folder playlist with properties {{name:"{safe_name}"}}
        return persistent ID of newFolder
    end tell
    """
    return run_applescript(script)


def move_to_folder(item_name: str, folder_path: str) -> tuple[bool, str]:
    """Move a playlist or folder into a folder.

    Args:
        item_name: Name of the playlist or folder to move
        folder_path: Target folder name or slash-separated path (e.g. "Summer/Chill")

    Returns:
        Tuple of (success, message or error)
    """
    safe_item = _escape_for_applescript(item_name)
    script = f"""
    tell application "Music"
{_resolve_folder_path_applescript(folder_path)}
{_find_playlist_applescript(safe_item)}
        move targetPlaylist to targetFolder
        return "Moved '" & name of targetPlaylist & "' to folder '" & name of targetFolder & "'"
    end tell
    """
    success, output = run_applescript(script)
    if output.startswith("ERROR:"):
        return False, output[6:]
    return success, output


def move_to_root(item_name: str) -> tuple[bool, str]:
    """Move a playlist out of its parent folder to the top level.

    Note: Music.app's AppleScript interface does not support moving playlists
    out of folders. This recreates the playlist at root with the same tracks.
    The playlist's persistent ID will change.

    Args:
        item_name: Name of the playlist to move to root

    Returns:
        Tuple of (success, message or error)
    """
    safe_item = _escape_for_applescript(item_name)
    script = f"""
    tell application "Music"
{_find_playlist_applescript(safe_item)}
        try
            set pParent to parent of targetPlaylist
        on error
            return "ERROR:Playlist is already at top level"
        end try
        set origName to name of targetPlaylist
        set tempName to origName & " _MOVING_"
        set newPlaylist to make new user playlist with properties {{name:tempName}}
        try
            repeat with t in tracks of targetPlaylist
                duplicate t to newPlaylist
            end repeat
        end try
        delete targetPlaylist
        set name of newPlaylist to origName
        return "Moved '" & origName & "' to top level (playlist recreated)"
    end tell
    """
    success, output = run_applescript(script)
    if output.startswith("ERROR:"):
        return False, output[6:]
    return success, output


def get_playlist_path(playlist_name: str) -> tuple[bool, str]:
    """Get the full folder path of a playlist or folder.

    Args:
        playlist_name: Name of the playlist or folder

    Returns:
        Tuple of (success, slash-separated path or error)
        e.g. "Summer/Chill/Road Trip" or just "Road Trip" if at root
    """
    safe_name = _escape_for_applescript(playlist_name)
    script = f"""
    tell application "Music"
{_find_playlist_applescript(safe_name)}
        set pathParts to {{name of targetPlaylist}}
        set current to targetPlaylist
        repeat
            try
                set current to parent of current
                set beginning of pathParts to name of current
            on error
                exit repeat
            end try
        end repeat
        set AppleScript's text item delimiters to "/"
        return pathParts as text
    end tell
    """
    success, output = run_applescript(script)
    if output.startswith("ERROR:"):
        return False, output[6:]
    return success, output


def delete_folder(folder_path: str) -> tuple[bool, str]:
    """Delete a folder by name or slash-separated path.

    Args:
        folder_path: Folder name or path (e.g. "Summer" or "Summer/Chill")

    Returns:
        Tuple of (success, message or error)
    """
    script = f"""
    tell application "Music"
{_resolve_folder_path_applescript(folder_path)}
        set folderName to name of targetFolder
        delete targetFolder
        return "Deleted folder: " & folderName
    end tell
    """
    success, output = run_applescript(script)
    if output.startswith("ERROR:"):
        return False, output[6:]
    return success, output


def delete_playlist(playlist_name: str) -> tuple[bool, str]:
    """Delete a playlist by name.

    Args:
        playlist_name: Name of the playlist to delete

    Returns:
        Tuple of (success, message or error)
    """
    safe_name = _escape_for_applescript(playlist_name)
    script = f"""
    tell application "Music"
{_find_playlist_applescript(safe_name)}
        set playlistName to name of targetPlaylist
        delete targetPlaylist
        return "Deleted playlist: " & playlistName
    end tell
    """
    success, output = run_applescript(script)
    if output.startswith("ERROR:"):
        return False, output[6:]
    return success, output


def rename_playlist(playlist_name: str, new_name: str) -> tuple[bool, str]:
    """Rename a playlist.

    Args:
        playlist_name: Current name of the playlist
        new_name: New name for the playlist

    Returns:
        Tuple of (success, message or error)
    """
    safe_old = _escape_for_applescript(playlist_name)
    safe_new = _escape_for_applescript(new_name)
    script = f"""
    tell application "Music"
{_find_playlist_applescript(safe_old)}
        set oldName to name of targetPlaylist
        set name of targetPlaylist to "{safe_new}"
        return "Renamed: " & oldName & " → {safe_new}"
    end tell
    """
    success, output = run_applescript(script)
    if output.startswith("ERROR:"):
        return False, output[6:]
    return success, output


def track_exists_in_playlist(
    playlist_name: str, track_name: str, artist: Optional[str] = None
) -> tuple[bool, bool | str]:
    """Quick check if a track exists in a playlist.

    Args:
        playlist_name: Playlist to check
        track_name: Track name to look for
        artist: Optional artist to match

    Returns:
        Tuple of (success, exists: bool | error: str)
        On success, second element is True/False for exists.
        On failure, second element is error message.
    """
    safe_playlist = _escape_for_applescript(playlist_name)
    name_clause = _name_contains_clause(track_name)

    if artist:
        safe_artist = _escape_for_applescript(artist)
        track_filter = f'whose {name_clause} and artist contains "{safe_artist}"'
    else:
        track_filter = f"whose {name_clause}"

    script = f"""
    tell application "Music"
{_find_playlist_applescript(safe_playlist)}
        set matchingTracks to (every track of targetPlaylist {track_filter})
        if (count of matchingTracks) > 0 then
            return "FOUND:" & name of (item 1 of matchingTracks) & " - " & artist of (item 1 of matchingTracks)
        else
            return "NOT_FOUND"
        end if
    end tell
    """
    success, output = run_applescript(script)
    if not success:
        return False, output
    if output.startswith("ERROR:"):
        return False, output[6:]
    if output.startswith("FOUND:"):
        return True, output[6:]  # Return the matched track info
    return True, False  # NOT_FOUND


def add_track_to_playlist(
    playlist_name: str, track_name: str, artist: Optional[str] = None, album: Optional[str] = None
) -> tuple[bool, str]:
    """Add a track from library to a playlist.

    Args:
        playlist_name: Target playlist name
        track_name: Name of the track to add (partial match supported)
        artist: Optional artist name to disambiguate (prefers exact match, falls back to contains)
        album: Optional album name to disambiguate (partial match supported)

    Returns:
        Tuple of (success, message or error)
    """
    safe_playlist = _escape_for_applescript(playlist_name)
    safe_track = _escape_for_applescript(track_name)

    # Primary query uses `artist is` (exact); fallback (when artist is given)
    # relaxes to `artist contains` so partial matches still resolve.
    name_clause = _name_contains_clause(track_name)
    conditions = [name_clause]
    if artist:
        safe_artist = _escape_for_applescript(artist)
        conditions.append(f'artist is "{safe_artist}"')
    if album:
        safe_album = _escape_for_applescript(album)
        conditions.append(f'album contains "{safe_album}"')

    track_query = f'first track of library playlist 1 whose {" and ".join(conditions)}'

    fallback_query = None
    if artist:
        fallback_conditions = [name_clause, f'artist contains "{safe_artist}"']
        if album:
            fallback_conditions.append(f'album contains "{safe_album}"')
        fallback_query = (
            f'first track of library playlist 1 whose {" and ".join(fallback_conditions)}'
        )

    if fallback_query:
        script = f"""
    tell application "Music"
{_find_playlist_applescript(safe_playlist)}
        try
            set targetTrack to {track_query}
        on error
            try
                set targetTrack to {fallback_query}
            on error
                return "ERROR:Track not found: {safe_track}"
            end try
        end try
        duplicate targetTrack to targetPlaylist
        return "Added " & name of targetTrack & " (" & album of targetTrack & ") by " & artist of targetTrack & " to " & name of targetPlaylist
    end tell
    """
    else:
        script = f"""
    tell application "Music"
{_find_playlist_applescript(safe_playlist)}
        try
            set targetTrack to {track_query}
        on error
            return "ERROR:Track not found: {safe_track}"
        end try
        duplicate targetTrack to targetPlaylist
        return "Added " & name of targetTrack & " (" & album of targetTrack & ") by " & artist of targetTrack & " to " & name of targetPlaylist
    end tell
    """
    success, output = run_applescript(script)
    if output.startswith("ERROR:"):
        return False, output[6:]
    return success, output


def remove_track_from_playlist(
    playlist_name: str,
    track_name: str = "",
    artist: Optional[str] = None,
    track_id: Optional[str] = None,
) -> tuple[bool, str]:
    """Remove a track from a playlist (not from library).

    Args:
        playlist_name: Playlist to remove from
        track_name: Name of the track to remove (partial match supported)
        artist: Optional artist name to disambiguate (partial match)
        track_id: Optional persistent ID (exact match, overrides name/artist)

    Returns:
        Tuple of (success, message or error)
    """
    safe_playlist = _escape_for_applescript(playlist_name)
    track_filter = _track_filter_clause(track_name, artist, track_id)
    if track_filter is None:
        return False, "Must provide track_name or track_id"

    script = f"""
    tell application "Music"
{_find_playlist_applescript(safe_playlist)}
        try
            set targetTrack to (first track of targetPlaylist {track_filter})
        on error
            return "ERROR:Track not found in playlist"
        end try
        set trackName to name of targetTrack
        set trackArtist to artist of targetTrack
        delete targetTrack
        return "Removed " & trackName & " by " & trackArtist & " from {safe_playlist}"
    end tell
    """
    success, output = run_applescript(script)
    if output.startswith("ERROR:"):
        return False, output[6:]
    return success, output


def remove_from_library(
    track_name: str = "", artist: Optional[str] = None, track_id: Optional[str] = None
) -> tuple[bool, str]:
    """Remove a track from the library entirely.

    Args:
        track_name: Name of the track to remove (partial match)
        artist: Optional artist name to disambiguate (partial match)
        track_id: Optional persistent ID (exact match, overrides name/artist)

    Returns:
        Tuple of (success, message or error)
    """
    track_filter = _track_filter_clause(track_name, artist, track_id)
    if track_filter is None:
        return False, "Must provide track_name or track_id"

    script = f"""
    tell application "Music"
        try
            set targetTrack to (first track of library playlist 1 {track_filter})
        on error
            return "ERROR:Track not found in library"
        end try
        set trackName to name of targetTrack
        set trackArtist to artist of targetTrack
        delete targetTrack
        return "Removed from library: " & trackName & " by " & trackArtist
    end tell
    """
    success, output = run_applescript(script)
    if output.startswith("ERROR:"):
        return False, output[6:]
    return success, output


def search_playlist(playlist_name: str, query: str) -> tuple[bool, list[dict]]:
    """Search for tracks in a playlist using native AppleScript search.

    Uses Music app's native search (same as typing in search field).
    Much faster than manually iterating through all tracks.

    Args:
        playlist_name: Name of the playlist to search
        query: Search term (matches name, artist, album, etc.)

    Returns:
        Tuple of (success, list of matching tracks or error message)
    """
    safe_name = _escape_for_applescript(playlist_name)
    safe_query = _escape_for_applescript(query)

    script = f"""
    tell application "Music"
{_find_playlist_applescript(safe_name)}
        set foundTracks to search targetPlaylist for "{safe_query}"
        set output to ""
        repeat with t in foundTracks
            try
                set trackName to name of t
                set trackArtist to artist of t
                set trackAlbum to album of t
                set trackId to persistent ID of t
                try
                    set trackExplicit to explicit of t
                on error
                    set trackExplicit to false
                end try
                set output to output & trackName & "|||" & trackArtist & "|||" & trackAlbum & "|||" & trackId & "|||" & trackExplicit & "\\n"
            on error
                -- skip inaccessible tracks (broken file references, error -1728)
            end try
        end repeat
        return output
    end tell
    """
    success, output = run_applescript(script)

    if not success:
        return False, output

    if output.startswith("ERROR:"):
        return False, output[6:]

    # Parse results
    tracks = []
    for line in output.strip().split("\n"):
        if not line:
            continue
        parts = line.split("|||")
        if len(parts) >= 4:
            # Parse explicit field (added in 5th position)
            explicit = "Unknown"
            if len(parts) >= 5:
                explicit = "Yes" if parts[4].lower() == "true" else "No"

            tracks.append(
                {
                    "name": parts[0],
                    "artist": parts[1],
                    "album": parts[2],
                    "id": parts[3],
                    "explicit": explicit,
                }
            )

    return True, tracks


def download_tracks(track_ids: str = "", playlist_name: str = "") -> tuple[bool, str]:
    """Download cloud tracks or playlist for offline playback.

    Args:
        track_ids: Comma-separated persistent IDs to download
        playlist_name: Name of playlist to download all tracks from

    Returns:
        Tuple of (success, message or error)
    """
    if track_ids and playlist_name:
        return False, "Error: Provide either track_ids or playlist_name, not both"
    if not track_ids and not playlist_name:
        return False, "Error: Provide track_ids or playlist_name"

    if playlist_name:
        # Download entire playlist
        safe_name = _escape_for_applescript(playlist_name)
        script = f"""
        tell application "Music"
{_find_playlist_applescript(safe_name)}
            download targetPlaylist
            return "Downloading playlist: " & name of targetPlaylist
        end tell
        """
    else:
        # Download individual tracks by ID
        ids = [tid.strip() for tid in track_ids.split(",") if tid.strip()]
        if not ids:
            return False, "Error: No valid track IDs provided"

        # Build AppleScript to download each track
        download_cmds = []
        for track_id in ids:
            safe_id = _escape_for_applescript(track_id)
            download_cmds.append(
                f'download (first track of library playlist 1 whose persistent ID is "{safe_id}")'
            )

        script = f"""
        tell application "Music"
            {chr(10).join(f"            {cmd}" for cmd in download_cmds)}
            return "Downloading {len(ids)} track(s)"
        end tell
        """

    success, output = run_applescript(script)
    if output.startswith("ERROR:"):
        return False, output[6:]
    return success, output


def play_playlist(playlist_name: str, shuffle: bool = False) -> tuple[bool, str]:
    """Start playing a playlist.

    Args:
        playlist_name: Name of the playlist to play
        shuffle: Whether to shuffle the playlist

    Returns:
        Tuple of (success, message or error)
    """
    safe_name = _escape_for_applescript(playlist_name)
    shuffle_cmd = "set shuffle enabled to true" if shuffle else "set shuffle enabled to false"

    script = f"""
    tell application "Music"
{_find_playlist_applescript(safe_name)}
        {shuffle_cmd}
        play targetPlaylist
        return "Now playing: " & name of targetPlaylist
    end tell
    """
    success, output = run_applescript(script)
    if output.startswith("ERROR:"):
        return False, output[6:]
    return success, output


def find_library_track(name: str, artist: str = "") -> tuple[bool, str]:
    """Direct lookup of a library track by name (+ optional artist), via the
    Music object model.

    Unlike :func:`search_library` (which runs Music's native ``search`` command
    and depends on the search index), this is a ``whose name contains`` object
    query, so it finds a track the instant it lands in the library — without
    waiting for the index to update after a UI add. Used to verify add-to-library
    succeeded. Apostrophe/quote-robust via :func:`_name_contains_clause`.

    Returns (True, "name|||artist") on a match, else (False, "").
    """
    if not name.strip():
        return False, ""
    cond = _name_contains_clause(name)
    if artist:
        cond = f'{cond} and artist contains "{_escape_for_applescript(artist)}"'
    ok, out = run_applescript(f"""
    tell application "Music"
        try
            set t to (first track of library playlist 1 whose {cond})
            return (name of t) & "|||" & (artist of t)
        on error
            return "NOT_FOUND"
        end try
    end tell""")
    if ok and "|||" in out.strip():
        return True, out.strip()
    return False, ""


def play_track(track_name: str, artist: Optional[str] = None) -> tuple[bool, str]:
    """Play a specific track from library.

    Args:
        track_name: Name of the track to play
        artist: Optional artist name to disambiguate

    Returns:
        Tuple of (success, message or error)
    """
    track_resolve = _resolve_library_track_applescript(track_name, artist)

    script = f"""
    tell application "Music"
{track_resolve}
        play targetTrack
        return "Now playing: " & name of targetTrack & " by " & artist of targetTrack
    end tell
    """
    success, output = run_applescript(script)
    if output.startswith("ERROR:"):
        return False, output[6:]
    return success, output


def open_catalog_song(song_url: str) -> tuple[bool, str]:
    """Open a catalog song in the Music app (user must click play).

    Note: macOS cannot programmatically play catalog songs not in library.
    This function reveals the song in Music for manual playback.

    Args:
        song_url: The song URL from Apple Music API (https://music.apple.com/...)

    Returns:
        Tuple of (success, message or error)
    """
    import subprocess

    # Validate URL format - must be an Apple Music URL
    if not song_url or not isinstance(song_url, str):
        return False, "Invalid URL: empty or not a string"

    # Normalize the URL - handle both https:// and music:// schemes
    if song_url.startswith("music://"):
        music_url = song_url
        https_url = song_url.replace("music://", "https://")
    elif song_url.startswith("https://music.apple.com"):
        https_url = song_url
        music_url = song_url.replace("https://", "music://")
    elif song_url.startswith("https://"):
        # Non-Apple Music https URL - reject it
        return False, f"Not an Apple Music URL: {song_url}"
    else:
        # Assume it might be a bare URL without scheme
        return False, f"Invalid URL format: {song_url}"

    # Try music:// scheme first - opens directly in Music app
    try:
        subprocess.run(["open", music_url], check=True, capture_output=True)
        return True, "Opened in Music"
    except subprocess.CalledProcessError:
        pass

    # Fallback: https:// opens in browser which redirects to Music
    try:
        subprocess.run(["open", https_url], check=True, capture_output=True)
        return True, "Opened via browser"
    except subprocess.CalledProcessError:
        return False, f"Failed to open: {song_url}"


# =============================================================================
# UI Path Constants
# =============================================================================
# Centralized UI element paths for Music.app System Events automation.
# These paths are used by multiple functions to interact with the Music UI.

_SCROLL_AREA = 'scroll area 2 of splitter group 1 of window "Music"'

# macOS 15: search field lives in the sidebar outline hierarchy
# macOS 26 (Tahoe): search field moved to the window toolbar group
_SEARCH_FIELD_SIDEBAR = (
    "text field 1 of UI element 1 of row 1 of outline 1"
    ' of scroll area 1 of splitter group 1 of window "Music"'
)
_SEARCH_FIELD_TOOLBAR = 'text field 1 of group 1 of toolbar 1 of window "Music"'
# macOS 26 build variant: some builds put the text field directly under the
# toolbar with no wrapping group element.
_SEARCH_FIELD_TOOLBAR_FLAT = 'text field 1 of toolbar 1 of window "Music"'

_search_field_cache: str | None = None


def _get_search_field() -> str:
    """Return the correct search field path for the running Music.app version.

    macOS 15: toolbar search field is always visible — probe succeeds immediately.
    macOS 26: search field only appears in the toolbar after the Search sidebar
    item is clicked, so we navigate there on a probe miss before retrying.

    Tries multiple toolbar element-path variants (some macOS 26 builds skip
    the wrapping `group 1`), then falls back to the sidebar layout. Only
    caches when a toolbar path is confirmed — never caches the sidebar
    fallback so a transient cold-start miss won't permanently pin the wrong
    path.
    """
    global _search_field_cache
    if _search_field_cache is not None:
        return _search_field_cache

    def _probe() -> str | None:
        """Return the first toolbar variant that exists, or None."""
        ok, result = run_applescript(f"""
tell application "System Events"
    tell process "Music"
        if exists ({_SEARCH_FIELD_TOOLBAR}) then
            return "grouped"
        end if
        if exists ({_SEARCH_FIELD_TOOLBAR_FLAT}) then
            return "flat"
        end if
        return "none"
    end tell
end tell""")
        if not ok:
            return None
        kind = result.strip()
        if kind == "grouped":
            return _SEARCH_FIELD_TOOLBAR
        if kind == "flat":
            return _SEARCH_FIELD_TOOLBAR_FLAT
        return None

    found = _probe()
    if found:
        _search_field_cache = found
        return found

    # Toolbar search field not yet visible — send Cmd+F to activate search mode.
    # On macOS 26, Cmd+F from the Search view activates the toolbar text field.
    run_applescript("""
tell application "System Events"
    tell process "Music"
        try
            keystroke "f" using command down
            delay 0.5
        end try
    end tell
end tell""")

    found = _probe()
    if found:
        _search_field_cache = found
        return found

    # Neither probe succeeded — return sidebar path without caching so the next
    # call retries (handles transient Music.app startup / accessibility timing).
    return _SEARCH_FIELD_SIDEBAR


_GITHUB_ISSUES = "https://github.com/epheterson/applemusic-mcp/issues"


def _classify_as_error(error_text: str) -> str:
    """Map a raw AppleScript error string to a human-readable, actionable message."""
    if "-1743" in error_text:
        return (
            "Accessibility permission not granted. Open System Settings → "
            "Privacy & Security → Accessibility and enable your terminal app."
        )
    if "-1728" in error_text:
        return (
            f"Music.app UI element not found — the layout may have changed after a macOS "
            f"update. Please file a bug at {_GITHUB_ISSUES} with your macOS version and "
            f"Music.app version."
        )
    snippet = error_text.strip()[:100]
    return (
        f"Unexpected UI automation error: {snippet}. "
        f"If this persists, please file a bug at {_GITHUB_ISSUES}."
    )


def is_screen_locked() -> Optional[bool]:
    """Whether the login session's screen is locked / not on the active console.

    UI automation fundamentally cannot work on a locked screen: the window
    server stops vending app windows, so System Events reports zero windows,
    the AX tree collapses, menu items go disabled, and window captures come
    back blank. Detecting this up front turns those confusing, flaky, hang-prone
    symptoms into one clear, actionable error.

    Uses Quartz ``CGSessionCopyCurrentDictionary`` — the canonical, instant
    signal (``CGSSessionScreenIsLocked``), far more reliable than probing the
    loginwindow process via AppleScript (which the old heuristic did and which
    breaks across macOS versions).

    Returns True if locked / fast-user-switched away / no GUI session,
    False if unlocked and on-console, and None if Quartz isn't available
    (caller should fall back to a heuristic rather than assume either way).
    """
    try:
        import Quartz
    except Exception:
        return None
    d = Quartz.CGSessionCopyCurrentDictionary()
    if not d:
        # No GUI session dictionary at all — not an automatable desktop.
        return True
    if d.get("CGSSessionScreenIsLocked", 0):
        return True
    # Session exists but isn't the active console session (fast-user-switched
    # to another login) — UI automation would target a background session.
    on_console = d.get("kCGSSessionOnConsoleKey", 1)
    return not on_console


def check_ui_accessible() -> tuple[bool, str]:
    """Check whether Music.app's UI windows are accessible via System Events.

    Returns (True, "") when UI automation can proceed. Returns (False, reason)
    with a concrete, actionable explanation when it cannot — distinguishing
    locked screen from missing Accessibility permission from no open window.
    """
    # Cleanest signal first: a locked / inactive session can't be automated at
    # all, and probing it via AppleScript just produces misleading errors.
    if is_screen_locked() is True:
        return False, (
            "Screen is locked (or no active desktop session). UI automation "
            "requires the Mac to be unlocked and logged in on the active "
            "display. Unlock the screen and try again."
        )
    ok, result = run_applescript(
        'tell application "System Events" to tell process "Music" to return count of windows'
    )
    if not ok:
        return False, _classify_as_error(result)
    if result.strip() != "0":
        return True, ""
    # No visible windows — best-effort check for locked screen via loginwindow.
    # On macOS 10–15 the lock screen runs as a window of the loginwindow process.
    # On macOS 26+ this may live elsewhere (e.g., ScreenSaver.engine) — if the
    # heuristic misses, the user just gets the generic "no visible windows"
    # message instead of "screen is locked", which is degraded but not wrong.
    ok2, lock_result = run_applescript(
        'tell application "System Events" to tell process "loginwindow" '
        "to return (count of windows) > 0"
    )
    if ok2 and lock_result.strip() == "true":
        return False, (
            "Music.app window not accessible — screen is locked. "
            "UI automation requires an unlocked screen."
        )
    return False, "Music.app has no visible windows. Open Music.app or unminimize its window."


def _check_playing() -> bool:
    """Check if Music is currently playing."""
    ok, state = run_applescript('tell application "Music" to get player state')
    return ok and state.strip() == "playing"


def _click_play_or_shuffle(shuffle: bool = False) -> tuple[bool, str]:
    """Find and click the Play or Shuffle button across different Music.app page layouts.

    Tries multiple known UI paths since albums, editorial playlists, and personal
    playlists each have different accessibility hierarchies.

    Args:
        shuffle: If True, click Shuffle instead of Play

    Returns:
        Tuple of (success, message or error)
    """
    button_name = "Shuffle" if shuffle else "Play"
    base = 'tell application "System Events" to tell process "Music"'
    sa = _SCROLL_AREA

    # Path 1: Album / editorial playlist layout (nested lists)
    script1 = f'{base} to click button "{button_name}" of UI element 1 of list 1 of list 1 of {sa}'
    # Path 2: Personal playlist layout (playlist header group)
    script2 = f'{base} to click button "{button_name}" of group 1 of {sa}'

    for script in [script1, script2]:
        ok, _ = run_applescript(script)
        if ok:
            time.sleep(1)
            if _check_playing():
                mode = "shuffling" if shuffle else "playing"
                return True, f"Playing ({mode} via UI click)"

    return False, f"Could not find {button_name} button"


def _ensure_music_frontmost() -> None:
    """Bring Music.app to the foreground with a visible main-library window.

    Recovers from three observed states: process not running, zero windows
    (user closed the last one; background playback keeps the process alive),
    and window-without-splitter-group (song-detail/album page where UI
    primitives that hardcode ``splitter group 1 of window 1`` fail with
    "Can't get splitter group 1 of window Music of").
    """
    try:
        subprocess.run(["open", "-a", "Music"], check=False, timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        pass
    # Cold launches can take 1-3s before Music is reachable via System Events.
    for _ in range(20):
        try:
            r = subprocess.run(
                [
                    "osascript",
                    "-e",
                    'tell application "System Events" to ' '(name of processes) contains "Music"',
                ],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if r.returncode == 0 and r.stdout.strip() == "true":
                break
        except (subprocess.TimeoutExpired, OSError):
            pass
        time.sleep(0.15)

    run_applescript("""
tell application "Music" to activate
delay 0.5
tell application "System Events"
    tell process "Music"
        set frontmost to true
        delay 0.3
        set needsRestore to false
        if (count of windows) is 0 then
            set needsRestore to true
        else
            try
                get splitter group 1 of window 1
            on error
                set needsRestore to true
            end try
        end if
        if needsRestore then
            try
                click menu item "Music" of menu "Window" of menu bar 1
            end try
            delay 1
            try
                get splitter group 1 of window 1
            on error
                keystroke "1" using command down
                delay 1
            end try
        end if
    end tell
end tell""")


def _jxa_mouse_move(x: float, y: float) -> bool:
    """Move the mouse cursor via CoreGraphics (JXA). Triggers hover effects.

    Uses osascript -l JavaScript to call CoreGraphics CGEventCreateMouseEvent,
    which generates real mouse events that trigger hover-dependent UI elements
    in Music.app (like the per-track play checkbox).

    Returns True if the command succeeded.
    """
    # 0.05s lets the OS commit the CGEvent before osascript exits.
    script = f'''ObjC.import("CoreGraphics");
var p = $.CGPointMake({x}, {y});
var e = $.CGEventCreateMouseEvent($(), $.kCGEventMouseMoved, p, 0);
$.CGEventPost($.kCGHIDEventTap, e);
delay(0.05);
"ok"'''
    try:
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, Exception):
        return False


def _jxa_mouse_click(x: float, y: float) -> bool:
    """Left-click at coordinates via CoreGraphics (JXA).

    Posts down+up CGEvents at the given pixel. Bypasses AppleScript's
    element resolution — useful when re-finding an element by description
    is unreliable (duplicate descriptions, re-ordered elements).

    Returns True if the command succeeded.
    """
    # 0.05s down→up gap is the minimum that registers as a click (lower
    # is treated as separate down/up events).
    script = f'''ObjC.import("CoreGraphics");
var p = $.CGPointMake({x}, {y});
var down = $.CGEventCreateMouseEvent($(), $.kCGEventLeftMouseDown, p, $.kCGMouseButtonLeft);
$.CGEventPost($.kCGHIDEventTap, down);
delay(0.05);
var up = $.CGEventCreateMouseEvent($(), $.kCGEventLeftMouseUp, p, $.kCGMouseButtonLeft);
$.CGEventPost($.kCGHIDEventTap, up);
"ok"'''
    try:
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, Exception):
        return False


def _hover_with_nudge(cx: float, cy: float) -> bool:
    """Move to target with a 2-pixel nudge first to guarantee a mouseMoved event.

    On macOS 26, posting a CGEventMouseMoved to the same coordinates as the
    previous position can be silently dropped. Moving to (cx+2, cy) first forces
    a real delta and ensures hover-dependent buttons (Add to Library, play
    checkbox) are revealed before the 1.5-second dwell.
    """
    _jxa_mouse_move(cx + 2, cy)
    return _jxa_mouse_move(cx, cy)


def _jxa_scroll_down(x: float, y: float, amount: int = 10) -> bool:
    """Scroll down at coordinates via CoreGraphics (JXA).

    Moves mouse to position first, then sends scroll wheel events.
    Used to bring off-screen track rows into view.

    Returns True if the command succeeded.
    """
    script = f'''ObjC.import("CoreGraphics");
var p = $.CGPointMake({x}, {y});
var m = $.CGEventCreateMouseEvent($(), $.kCGEventMouseMoved, p, 0);
$.CGEventPost($.kCGHIDEventTap, m);
delay(0.3);
for (var i = 0; i < {amount}; i++) {{
    var s = $.CGEventCreateScrollWheelEvent($(), 0, 1, -3);
    $.CGEventPost($.kCGHIDEventTap, s);
    delay(0.1);
}}
delay(0.5);
"ok"'''
    try:
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", script],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, Exception):
        return False


def _find_highlighted_track_position() -> Optional[tuple[float, float, str]]:
    """Find the track row highlighted by ?i= parameter (has Favorite button).

    Searches all track groups across disc sections for a group containing
    a button with description "Favorite" — Music.app adds this to the
    track highlighted by the ?i= URL parameter.

    Returns (center_x, center_y, track_name) or None if not found.
    """
    ok, result = run_applescript("""
tell application "System Events"
    tell process "Music"
        set sg to splitter group 1 of window "Music"
        set sa to scroll area 2 of sg
        repeat with subList in (every list of list 1 of sa)
            repeat with g in (every group of subList)
                try
                    repeat with b in (every button of g)
                        if description of b is "Favorite" then
                            set {x, y} to position of g
                            set {w, h} to size of g
                            set cx to (x + w / 2)
                            set cy to (y + h / 2)
                            return (cx as text) & "," & (cy as text) & "," & description of g
                        end if
                    end repeat
                end try
            end repeat
        end repeat
        return "NOT_FOUND"
    end tell
end tell""")
    if not ok or not result or result.strip() == "NOT_FOUND":
        return None
    parts = result.strip().split(",", 2)
    if len(parts) < 3:
        return None
    try:
        return float(parts[0]), float(parts[1]), parts[2]
    except ValueError:
        return None


def _get_window_bottom() -> Optional[float]:
    """Get the bottom y-coordinate of the Music window."""
    ok, result = run_applescript("""
tell application "System Events"
    tell process "Music"
        set {wx, wy} to position of window "Music"
        set {ww, wh} to size of window "Music"
        return ((wy + wh) as text)
    end tell
end tell""")
    if ok and result:
        try:
            return float(result.strip())
        except ValueError:
            pass
    return None


def _play_specific_track() -> tuple[bool, str]:
    """Play the track highlighted by ?i= URL parameter via hover + click.

    Finds the highlighted track row (marked with Favorite button), scrolls
    it into view if off-screen, hovers via CoreGraphics to reveal the
    per-track play checkbox, and clicks it.

    Requires Accessibility permissions for System Events.

    Returns:
        Tuple of (success, message or error)
    """
    # Ensure Music is frontmost before any CoreGraphics interaction
    _ensure_music_frontmost()

    # Find the highlighted track
    pos = _find_highlighted_track_position()
    if pos is None:
        return False, "Could not find highlighted track row"
    cx, cy, track_name = pos

    # Scroll into view if off-screen
    win_bottom = _get_window_bottom()
    if win_bottom and cy > win_bottom - 30:
        _ensure_music_frontmost()
        _jxa_scroll_down(cx, win_bottom - 200, amount=10)
        # Re-find position after scroll
        pos = _find_highlighted_track_position()
        if pos is None:
            return False, "Lost track row after scrolling"
        cx, cy, track_name = pos

    # Ensure Music is still frontmost (user may have clicked away during scroll)
    _ensure_music_frontmost()

    # Hover to reveal the play checkbox (nudge first to guarantee mouseMoved on macOS 26)
    if not _hover_with_nudge(cx, cy):
        return False, "Failed to move mouse for hover"
    time.sleep(1.5)

    # Click the play checkbox that appears on hover
    ok, result = run_applescript("""
tell application "System Events"
    tell process "Music"
        set sg to splitter group 1 of window "Music"
        set sa to scroll area 2 of sg
        repeat with subList in (every list of list 1 of sa)
            repeat with g in (every group of subList)
                try
                    repeat with b in (every button of g)
                        if description of b is "Favorite" then
                            click checkbox 1 of g
                            return description of g
                        end if
                    end repeat
                end try
            end repeat
        end repeat
        return "NOT_FOUND"
    end tell
end tell""")
    if ok and result and result.strip() != "NOT_FOUND":
        time.sleep(1)
        if _check_playing():
            return True, f"Playing: {result.strip()}"

    return False, f"Hover+click attempted on {track_name} but playback did not start"


def open_catalog_and_play(
    url: str, shuffle: bool = False, timeout: float = 15.0
) -> tuple[bool, str]:
    """Open an Apple Music URL and attempt to start playback via UI scripting.

    Supports albums, playlists (editorial and personal), and specific tracks
    via ?i= parameter. Uses multiple UI automation strategies depending on
    the URL type and page layout.

    For albums/playlists: clicks the Play or Shuffle button.
    For ?i= song URLs: hovers over the highlighted track row via CoreGraphics
    to reveal the per-track play checkbox, then clicks it.

    Uses adaptive polling — checks every second and attempts playback as soon
    as the page loads, rather than waiting fixed delays. Fast networks get
    fast response, slow networks get up to `timeout` seconds.

    Requires Accessibility permissions for System Events.
    Song URLs (/song/name/id) are not supported via deep link — the server
    layer converts them to album URLs with ?i= via API when available.

    Args:
        url: Apple Music URL (https://music.apple.com/... or music://...)
        shuffle: If True, click Shuffle instead of Play (albums/playlists only)
        timeout: Maximum seconds to wait for content to load (default 15)

    Returns:
        Tuple of (success, message or error)
    """
    # Reject /song/ URLs — they show "Something went wrong" in Music.app
    url_stripped = url.strip()
    if "/song/" in url_stripped and "?i=" not in url_stripped:
        return False, (
            "Song URLs (/song/id) are not supported by Music.app via deep link. "
            "Use an album URL with ?i= parameter instead: "
            "/album/name/albumId?i=songId"
        )

    # Detect if this is a specific track request
    has_track_param = "?i=" in url_stripped

    # Reuse existing URL opening logic
    open_ok, open_msg = open_catalog_song(url)
    if not open_ok:
        return False, open_msg

    # Adaptive polling: check frequently, attempt playback as soon as page loads
    # Initial wait for Music.app to start loading content
    time.sleep(2)
    deadline = time.time() + timeout
    attempt = 0

    while time.time() < deadline:
        # Check if Music already started playing on its own
        if _check_playing():
            return True, "Playing (auto-started after opening URL)"

        if has_track_param:
            ok, msg = _play_specific_track()
            if ok:
                return True, msg
        else:
            ok, msg = _click_play_or_shuffle(shuffle)
            if ok:
                return True, msg

        # Wait before retrying — longer gaps on early attempts (content loading),
        # shorter gaps on later attempts (UI may just need another try)
        attempt += 1
        wait = 2.0 if attempt <= 2 else 1.0
        time.sleep(wait)

    if has_track_param:
        return (
            True,
            "Opened URL in Music. Could not auto-play the specific track — try clicking it manually.",
        )
    return (
        True,
        "Opened URL in Music. Auto-play attempted but could not confirm playback started — may need Accessibility permissions for System Events.",
    )


# =============================================================================
# Library Search
# =============================================================================


def get_library_songs(limit: int = 100) -> tuple[bool, list[dict]]:
    """Get songs from the library (no search query required).

    Args:
        limit: Maximum number of songs to return (default 100, 0 for all)

    Returns:
        Tuple of (success, list of track dicts or error)

    Note: Large libraries (10,000+ tracks) with limit=0 may timeout (30s).
    """
    if limit < 0:
        return False, "limit must be >= 0 (use 0 for all songs)"
    limit_clause = f"if resultCount >= {limit} then exit repeat" if limit > 0 else ""

    script = f"""
    tell application "Music"
        set output to ""
        set resultCount to 0
        repeat with t in tracks of library playlist 1
            {limit_clause}
            try
                set tName to name of t
                set tArtist to artist of t
                set tAlbum to album of t
                set tDuration to duration of t
                set tId to persistent ID of t
                try
                    set tGenre to genre of t
                on error
                    set tGenre to ""
                end try
                try
                    set tYear to year of t as string
                on error
                    set tYear to ""
                end try
                try
                    set tExplicit to explicit of t
                on error
                    set tExplicit to false
                end try
                set output to output & tName & "|||" & tArtist & "|||" & tAlbum & "|||" & tDuration & "|||" & tGenre & "|||" & tYear & "|||" & tId & "|||" & tExplicit & "\\n"
                set resultCount to resultCount + 1
            on error
                -- skip inaccessible tracks (broken file references, error -1728)
            end try
        end repeat
        return output
    end tell
    """
    success, output = run_applescript(script)
    if not success:
        return False, output

    tracks = [t for t in (_parse_library_track_line(line) for line in output.split("\n")) if t]
    return True, tracks


def get_library_songs_page(offset: int, limit: int) -> tuple[bool, list[dict], int, str]:
    """Get a single page of songs from the library using O(limit) range access.

    Args:
        offset: Zero-based starting position
        limit: Number of songs to return (must be > 0)

    Returns:
        Tuple of (success, tracks, total_count, error)
    """
    if limit <= 0:
        return False, [], 0, "limit must be > 0"
    start_pos = offset + 1
    end_pos = offset + limit

    script = f"""
    tell application "Music"
        set total to count of tracks of library playlist 1
        set output to "total:" & total & "\\n"
        if total is 0 or {offset} >= total then
            return output
        end if
        set endPos to {end_pos}
        if endPos > total then set endPos to total
        set trackList to tracks of library playlist 1
        repeat with t in items {start_pos} through endPos of trackList
            try
                set tName to name of t
                set tArtist to artist of t
                set tAlbum to album of t
                set tDuration to duration of t
                set tId to persistent ID of t
                try
                    set tGenre to genre of t
                on error
                    set tGenre to ""
                end try
                try
                    set tYear to year of t as string
                on error
                    set tYear to ""
                end try
                try
                    set tExplicit to explicit of t
                on error
                    set tExplicit to false
                end try
                set output to output & tName & "|||" & tArtist & "|||" & tAlbum & "|||" & tDuration & "|||" & tGenre & "|||" & tYear & "|||" & tId & "|||" & tExplicit & "\\n"
            on error
                -- skip inaccessible tracks (broken file references, error -1728)
            end try
        end repeat
        return output
    end tell
    """
    success, output = run_applescript(script)
    if not success:
        return False, [], 0, output

    total = 0
    tracks = []
    for line in output.split("\n"):
        if line.startswith("total:"):
            try:
                total = int(line[6:].strip())
            except ValueError:
                pass
            continue
        parsed = _parse_library_track_line(line)
        if parsed:
            tracks.append(parsed)
    return True, tracks, total, ""


def search_library(query: str, types: str = "all") -> tuple[bool, list[dict]]:
    """Search the local library.

    Args:
        query: Search query
        types: Type of search - "all", "artists", "albums", "songs"

    Returns:
        Tuple of (success, list of track dicts or error)
    """
    safe_query = _escape_for_applescript(query)

    # Map search types to AppleScript search kinds
    search_map = {
        "all": "",
        "artists": "only artists",
        "albums": "only albums",
        "songs": "only songs",
    }
    search_modifier = search_map.get(types, "")

    script = f"""
    tell application "Music"
        set searchResults to search library playlist 1 for "{safe_query}" {search_modifier}
        set output to ""
        set maxResults to 100
        set resultCount to 0
        repeat with t in searchResults
            if resultCount >= maxResults then exit repeat
            try
                set tName to name of t
                set tArtist to artist of t
                set tAlbum to album of t
                set tDuration to duration of t
                set tId to persistent ID of t
                try
                    set tGenre to genre of t
                on error
                    set tGenre to ""
                end try
                try
                    set tYear to year of t as string
                on error
                    set tYear to ""
                end try
                try
                    set tExplicit to explicit of t
                on error
                    set tExplicit to false
                end try
                set output to output & tName & "|||" & tArtist & "|||" & tAlbum & "|||" & tDuration & "|||" & tGenre & "|||" & tYear & "|||" & tId & "|||" & tExplicit & "\\n"
                set resultCount to resultCount + 1
            on error
                -- skip inaccessible tracks (broken file references, error -1728)
            end try
        end repeat
        return output
    end tell
    """
    success, output = run_applescript(script)
    if not success:
        return False, output

    tracks = [t for t in (_parse_library_track_line(line) for line in output.split("\n")) if t]
    return True, tracks


# =============================================================================
# Track Metadata
# =============================================================================


def love_track(track_name: str, artist: Optional[str] = None) -> tuple[bool, str]:
    """Mark a track as loved.

    Args:
        track_name: Name of the track (partial match supported)
        artist: Optional artist name to disambiguate (partial match supported)

    Returns:
        Tuple of (success, message or error)
    """
    track_resolve = _resolve_library_track_applescript(track_name, artist)

    script = f"""
    tell application "Music"
{track_resolve}
        set loved of targetTrack to true
        set disliked of targetTrack to false
        return "Loved: " & name of targetTrack
    end tell
    """
    success, output = run_applescript(script)
    if output.startswith("ERROR:"):
        return False, output[6:]
    return success, output


def dislike_track(track_name: str, artist: Optional[str] = None) -> tuple[bool, str]:
    """Mark a track as disliked.

    Args:
        track_name: Name of the track (partial match supported)
        artist: Optional artist name to disambiguate (partial match supported)

    Returns:
        Tuple of (success, message or error)
    """
    track_resolve = _resolve_library_track_applescript(track_name, artist)

    script = f"""
    tell application "Music"
{track_resolve}
        set disliked of targetTrack to true
        set loved of targetTrack to false
        return "Disliked: " & name of targetTrack
    end tell
    """
    success, output = run_applescript(script)
    if output.startswith("ERROR:"):
        return False, output[6:]
    return success, output


def get_rating(track_name: str, artist: Optional[str] = None) -> tuple[bool, int | str]:
    """Get track rating (0-100, where 20=1 star, 40=2 stars, etc).

    Args:
        track_name: Name of the track (partial match supported)
        artist: Optional artist name to disambiguate (partial match supported)

    Returns:
        Tuple of (success, rating 0-100 or error message string)
    """
    track_resolve = _resolve_library_track_applescript(track_name, artist)

    script = f"""
    tell application "Music"
{track_resolve}
        return rating of targetTrack as integer
    end tell
    """
    success, output = run_applescript(script)
    if output.startswith("ERROR:"):
        return False, output[6:]
    try:
        return True, int(output)
    except (ValueError, TypeError):
        return False, f"Invalid rating value: {output}"


def set_rating(track_name: str, rating: int, artist: Optional[str] = None) -> tuple[bool, str]:
    """Set track rating (0-100, where 20=1 star, 40=2 stars, etc).

    Args:
        track_name: Name of the track (partial match supported)
        rating: Rating value 0-100
        artist: Optional artist name to disambiguate (partial match supported)

    Returns:
        Tuple of (success, message or error)
    """
    rating = max(0, min(100, rating))
    track_resolve = _resolve_library_track_applescript(track_name, artist)

    script = f"""
    tell application "Music"
{track_resolve}
        set rating of targetTrack to {rating}
        return "Set rating to {rating} for: " & name of targetTrack
    end tell
    """
    success, output = run_applescript(script)
    if output.startswith("ERROR:"):
        return False, output[6:]
    return success, output


# =============================================================================
# AirPlay
# =============================================================================


def get_airplay_devices() -> tuple[bool, list[str]]:
    """Get list of available AirPlay devices."""
    script = """
    tell application "Music"
        set deviceNames to name of every AirPlay device
        set output to ""
        repeat with d in deviceNames
            set output to output & d & "\\n"
        end repeat
        return output
    end tell
    """
    success, output = run_applescript(script)
    if not success:
        return False, output

    devices = [d.strip() for d in output.split("\n") if d.strip()]
    return True, devices


def set_airplay_device(device_name: str) -> tuple[bool, str]:
    """Switch audio output to a specific AirPlay device.

    Args:
        device_name: Name of the AirPlay device (or partial match)

    Returns:
        Tuple of (success, message or error)
    """
    safe_name = _escape_for_applescript(device_name)

    script = f"""
    tell application "Music"
        try
            set targetDevice to first AirPlay device whose name contains "{safe_name}"
        on error
            return "ERROR:Device not found: {safe_name}"
        end try
        set current AirPlay devices to {{targetDevice}}
        return "Switched to: " & name of targetDevice
    end tell
    """
    success, output = run_applescript(script)
    if output.startswith("ERROR:"):
        return False, output[6:]
    return success, output


# =============================================================================
# Utilities
# =============================================================================


def reveal_track(track_name: str, artist: Optional[str] = None) -> tuple[bool, str]:
    """Reveal a track in the Music app window.

    Args:
        track_name: Name of the track
        artist: Optional artist name to disambiguate

    Returns:
        Tuple of (success, message or error)
    """
    track_resolve = _resolve_library_track_applescript(track_name, artist)

    script = f"""
    tell application "Music"
{track_resolve}
        reveal targetTrack
        activate
        return "Revealed: " & name of targetTrack
    end tell
    """
    success, output = run_applescript(script)
    if output.startswith("ERROR:"):
        return False, output[6:]
    return success, output


def get_library_stats() -> tuple[bool, dict]:
    """Get library statistics."""
    script = """
    tell application "Music"
        set trackCount to count of tracks of library playlist 1
        set playlistCount to count of user playlists
        set playerState to player state as string
        set shuffleState to shuffle enabled
        set repeatState to song repeat as string
        set vol to sound volume

        return trackCount & "|||" & playlistCount & "|||" & playerState & "|||" & shuffleState & "|||" & repeatState & "|||" & vol
    end tell
    """
    success, output = run_applescript(script)
    if not success:
        return False, output

    parts = output.split("|||")
    if len(parts) >= 6:
        return True, {
            "track_count": int(parts[0]) if parts[0].isdigit() else 0,
            "playlist_count": int(parts[1]) if parts[1].isdigit() else 0,
            "player_state": parts[2],
            "shuffle": parts[3].lower() == "true",
            "repeat": parts[4],
            "volume": int(parts[5]) if parts[5].isdigit() else 0,
        }
    return False, "Failed to parse library stats"


# =============================================================================
# UI Catalog Automation (no API required)
# =============================================================================
# These functions control Music.app through its UI (System Events + CoreGraphics)
# to provide catalog search, add-to-library, and play functionality without
# needing an Apple Developer account or API token.
#
# Requirements: macOS, Accessibility permissions for System Events,
# Music.app visible (not minimized), display attached (not headless).
#
# Architecture: every public ui_* entrypoint is a thin composition of the
# private primitives below. Each macOS-specific quirk (toolbar layout
# variants, autocomplete popover dismissal, hover-then-click row mismatches)
# gets fixed in exactly one place.

# -----------------------------------------------------------------------------
# Internal primitives — do not call directly from server.py.
# -----------------------------------------------------------------------------


def _focus_search_field(query: str) -> tuple[bool, str]:
    """Navigate to Search view, activate the search field, type query, commit.

    Single consolidated AppleScript so window state can't drift between calls.
    On macOS 26 the sidebar click switches context from playlist→Search and
    Cmd+F makes the toolbar text field appear; on macOS 15 the toolbar field
    is always visible so the sidebar click and Cmd+F are harmless no-ops.

    Returns (ok, error). On error, environmental causes (locked screen,
    Accessibility) are checked first so the user gets the right diagnosis.
    """
    if not query or not query.strip():
        return False, "Empty query"

    _ensure_music_frontmost()

    # Errors meaning "cached UI path no longer resolves" (vs environmental
    # causes like locked screen / missing Accessibility) — invalidate + retry.
    PATH_ERROR_MARKERS = ("Can't get", "AppleEvent handler failed", "-10000", "-1728")

    def _emit(field_path: str) -> tuple[bool, str]:
        return run_applescript(f"""
tell application "System Events"
    tell process "Music"
        try
            set sg to splitter group 1 of window 1
            click UI element 1 of row 1 of outline 1 of scroll area 1 of sg
            delay 0.4
        end try
        keystroke "f" using command down
        delay 0.5
        set searchField to {field_path}
        set focused of searchField to true
        delay 0.2
        set value of searchField to "{_escape_for_applescript(query)}"
        delay 1.0
        -- Two Enters: first commits the search (navigates to results page);
        -- second dismisses the autocomplete popover that lingers and blocks
        -- subsequent hover-to-click on result rows. Eric's manual workflow
        -- requires two Enters to clear the popover; mirroring that here.
        key code 36
        delay 0.4
        key code 36
    end tell
end tell""")

    ok, err = _emit(_get_search_field())
    if not ok and any(m in err for m in PATH_ERROR_MARKERS):
        global _search_field_cache
        _search_field_cache = None
        ok, err = _emit(_get_search_field())
    if ok:
        return True, ""

    accessible, access_reason = check_ui_accessible()
    if not accessible:
        return False, access_reason
    return False, _classify_as_error(err)


_PARSE_TOP_RESULTS_QUERY = f"""
tell application "System Events"
    tell process "Music"
        set sa to {_SCROLL_AREA}
        set resultList to list 1 of sa
        try
            set topResults to list 1 of resultList
        on error
            return "NO_RESULTS"
        end try
        set ec to every UI element of topResults
        set r to ""
        set idx to 0
        repeat with e in ec
            try
                set c to class of e as text
                if c is "UI element" then
                    set d to description of e
                    if d is not "Top Results" and d is not "group" then
                        set idx to idx + 1
                        -- macOS 26 prepends an empty static text, so search instead of using item 2
                        set typeLine to ""
                        set stTexts to every static text of e
                        repeat with stEl in stTexts
                            set stName to name of stEl
                            if stName contains "·" then
                                set typeLine to stName
                                exit repeat
                            end if
                        end repeat
                        set r to r & idx & "|||" & d & "|||" & typeLine & return
                    end if
                end if
            end try
        end repeat
        return r
    end tell
end tell"""


def _wait_for_top_results(timeout: float = 5.0) -> tuple[bool, str]:
    """Poll for Top Results to render. Returns (ok, raw_text_or_error).

    If results don't appear within ~1.2s the autocomplete popover may still
    be covering them; sends one recovery Enter to dismiss it (matches the
    manual "two Enters" pattern Eric observed). On AppleScript failure,
    classifies environmental causes (locked screen, Accessibility) before
    blaming the element path. On clean timeout with no results, returns
    (True, "") to signal an empty result set rather than an error.
    """
    ok, raw = False, ""
    start = time.monotonic()
    second_enter_sent = False
    while time.monotonic() - start < timeout:
        ok, raw = run_applescript(_PARSE_TOP_RESULTS_QUERY)
        if ok and raw and raw.strip() != "NO_RESULTS":
            return True, raw
        if not second_enter_sent and time.monotonic() - start > 1.2:
            run_applescript(
                'tell application "System Events" to tell process "Music" to key code 36'
            )
            second_enter_sent = True
        time.sleep(0.3)
    if not ok:
        accessible, access_reason = check_ui_accessible()
        if not accessible:
            return False, access_reason
        return False, _classify_as_error(raw)
    return True, ""


# Type-line separators Apple uses inside strings like "Song · Radiohead".
# U+2004 (three-per-em space) is the current macOS form; regular ASCII space
# is kept as a fallback for forward/backward-compat across macOS versions.
_TYPE_LINE_SEPS = (" · ", " · ")


def _parse_top_results(raw: str) -> list[dict]:
    """Parse the '|||'-delimited output from _wait_for_top_results into dicts."""
    results = []
    for line in raw.strip().split("\n"):
        line = line.strip()
        if not line or "|||" not in line:
            continue
        parts = line.split("|||")
        if len(parts) < 3:
            continue
        name = parts[1].strip()
        type_line = parts[2].strip()
        # Music.app's "No results for X. Try a new search." placeholder uses
        # the row pattern with an empty type-line — skip so callers see [].
        if not type_line and name.lower().startswith("no results for"):
            continue
        result_type = type_line
        artist = ""
        for sep in _TYPE_LINE_SEPS:
            if sep in type_line:
                result_type, artist = type_line.split(sep, 1)
                break
        results.append(
            {
                "name": name,
                "type": result_type.strip(),
                "artist": artist.strip(),
                "index": int(parts[0]),
            }
        )
    return results


def _find_top_result_position(name: str) -> Optional[tuple[float, float]]:
    """Find the row in Top Results matching `name` (by description) and
    return its center (cx, cy). Returns None if not found.
    """
    safe_name = _escape_for_applescript(name)
    ok, pos_str = run_applescript(f"""
tell application "System Events"
    tell process "Music"
        set sa to {_SCROLL_AREA}
        set resultList to list 1 of sa
        try
            set topResults to list 1 of resultList
        on error
            return "NOT_FOUND"
        end try
        repeat with e in (every UI element of topResults)
            try
                if description of e is "{safe_name}" then
                    set {{x, y}} to position of e
                    set {{w, h}} to size of e
                    return ((x + w / 2) as text) & "," & ((y + h / 2) as text)
                end if
            end try
        end repeat
        return "NOT_FOUND"
    end tell
end tell""")
    if not ok or not pos_str or pos_str.strip() in ("NOT_FOUND", "NO_RESULTS"):
        return None
    try:
        cx, cy = [float(v) for v in pos_str.strip().split(",")]
        return cx, cy
    except ValueError:
        return None


def _hover_then_click_subelement(
    name: str,
    inner_setter: str,
    max_wait: float = 1.5,
) -> tuple[bool, str]:
    """Hover the result row matching `name`, poll for an inner sub-element to
    become queryable, then CoreGraphics-click its center.

    CoreGraphics is used (not AppleScript `click`) because re-finding a row
    by description in a separate AppleScript can land on the wrong row when
    descriptions are similar — the play-checkbox row-mismatch bug.

    Args:
        name: result name (matched against description in Top Results).
        inner_setter: AppleScript fragment that, in the scope where `e` is
            the row UI element, sets the variable `inner` to the target
            sub-element. Examples:
              ``"set inner to checkbox 1 of e"``
              ``'set inner to (first button of e whose description is "Add to Library")'``
        max_wait: max seconds to poll for the inner element after hover.

    Returns:
        (True, "") on successful click; (False, error) otherwise. The error
        string ``"sub-element not visible after hover"`` is reserved for the
        common "row found but inner element didn't appear" case so callers
        can map it to a domain-specific message.
    """
    pos = _find_top_result_position(name)
    if pos is None:
        return False, f"Could not find '{name}' in search results"
    cx, cy = pos

    _ensure_music_frontmost()
    if not _hover_with_nudge(cx, cy):
        return False, "Failed to hover"

    safe_name = _escape_for_applescript(name)
    poll_query = f"""
tell application "System Events"
    tell process "Music"
        set sa to {_SCROLL_AREA}
        set resultList to list 1 of sa
        try
            set topResults to list 1 of resultList
        on error
            return "NOT_FOUND"
        end try
        repeat with e in (every UI element of topResults)
            try
                if description of e is "{safe_name}" then
                    {inner_setter}
                    set {{ix, iy}} to position of inner
                    set {{iw, ih}} to size of inner
                    return ((ix + iw / 2) as text) & "," & ((iy + ih / 2) as text)
                end if
            end try
        end repeat
        return "NOT_FOUND"
    end tell
end tell"""
    inner_pos = ""
    deadline = time.monotonic() + max_wait
    found = False
    rehover_at = time.monotonic() + (max_wait / 2)  # re-hover halfway through
    while time.monotonic() < deadline:
        ok, inner_pos = run_applescript(poll_query)
        if ok and inner_pos and inner_pos.strip() not in ("NOT_FOUND", "NO_RESULTS"):
            found = True
            break
        # If the first hover didn't take (macOS 26 sometimes drops mouseMoved
        # events even with the nudge), try once more partway through the
        # polling window. This is the difference between a flaky test and a
        # reliable one — the button reveal is hover-event-dependent and a
        # single hover isn't always enough.
        if time.monotonic() > rehover_at:
            _hover_with_nudge(cx, cy)
            rehover_at = float("inf")  # only retry once
        time.sleep(0.1)
    if not found:
        return False, "sub-element not visible after hover"

    try:
        ix, iy = [float(v) for v in inner_pos.strip().split(",")]
    except ValueError:
        return False, f"Invalid sub-element position: {inner_pos}"

    if not _jxa_mouse_click(ix, iy):
        return False, "CoreGraphics click failed"
    return True, ""


def _verify_track_playing(name: str, timeout: float = 2.0) -> tuple[bool, str]:
    """Poll for Music.app's current track name to contain `name` (case-insensitive).

    Returns (True, current_track_name) on match; (False, last_known_name) on
    timeout. The (False, ...) case lets callers distinguish "playing the
    wrong track" from "not playing at all" via a follow-up _check_playing().
    """
    target = name.lower()
    now_playing = ""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ok, now_playing = run_applescript('tell application "Music" to get name of current track')
        if ok and now_playing and target in now_playing.lower():
            return True, now_playing.strip()
        time.sleep(0.15)
    return False, now_playing.strip() if now_playing else ""


# -----------------------------------------------------------------------------
# Popover-canonical-match primitives (validate-before-act flow)
# -----------------------------------------------------------------------------
#
# When you type into Music.app's search field, an autocomplete pop-over
# appears at the PROCESS level (not inside any window) with structured
# rows. Each row exposes:
#   - text[0] = result name (e.g. "Holocene")
#   - text[1] = "{Type} · {Artist}" (e.g. "Song · Bon Iver", "Album · X")
#
# This is THE canonical match path: we walk the popover rows, find the one
# whose name and artist match exactly, click it via CoreGraphics — Music.app
# navigates to the song detail page where the per-row "Add to Library"
# button is reliably labeled.
#
# This bypasses Top Results' opaque ranking and the row-substitution bugs
# that hardened ui_add_to_playlist with strict matching but couldn't fully
# eliminate.


def _open_search_popover(query: str) -> tuple[bool, str]:
    """Activate Music, navigate to Search, type the query, validate the
    autocomplete pop-over appeared.

    Validate-before-act: returns (True, "") only when the pop-over is
    confirmed visible. Caller can then walk it via _find_popover_song_row.
    """
    if not query or not query.strip():
        return False, "Empty query"

    _ensure_music_frontmost()

    # Step 1: navigate to Search context FIRST. Sidebar click forces global
    # Search (so Cmd+F opens catalog search, not a per-playlist filter).
    # Don't resolve the search field path yet — when Music is on a song-
    # detail page the toolbar layout is different and the path probe would
    # return the wrong fallback.
    run_applescript("""
tell application "System Events"
    tell process "Music"
        try
            set sg to splitter group 1 of window 1
            click UI element 1 of row 1 of outline 1 of scroll area 1 of sg
            delay 0.4
        end try
        keystroke "f" using command down
        delay 0.6
    end tell
end tell""")

    # Step 2: NOW resolve the search field path. Music is in Search view,
    # so the toolbar variants probe should hit on this macOS build.
    search_field_path = _get_search_field()

    # Step 3: focus the field + type via keystroke (not `set value of`,
    # which silently updates the field without triggering the autocomplete
    # suggestions provider).
    ok, err = run_applescript(f"""
tell application "System Events"
    tell process "Music"
        click {search_field_path}
        delay 0.3
        keystroke "{_escape_for_applescript(query)}"
    end tell
end tell""")
    if not ok:
        accessible, access_reason = check_ui_accessible()
        if not accessible:
            return False, access_reason
        return False, _classify_as_error(err)

    # Validate: poll for pop-over to appear (up to ~2.5s — Apple's autocomplete
    # is usually <1s but can lag on cold queries).
    deadline = time.monotonic() + 2.5
    while time.monotonic() < deadline:
        ok, has = run_applescript(
            'tell application "System Events" to tell process "Music" '
            "to return (exists (first pop over))"
        )
        if ok and has.strip() == "true":
            return True, ""
        time.sleep(0.1)
    return False, "Search popover did not appear within 2.5s"


def _find_popover_song_row(name: str, artist: str = "") -> Optional[tuple[int, str]]:
    """Walk the autocomplete pop-over and pick the best canonical Song match.

    Score-and-pick over Song rows only. Album rows are rejected — clicking
    them navigates to an album page (extra hop, brittle row layout); when
    Apple's pop-over doesn't surface a Song row that matches, that's itself
    a signal the request is ambiguous and we fail loudly instead.

    Score tiers (per Song row):
        2000  EXACT title match (canonical track)
        1700  Canonical-marker parenthetical
              (e.g. "Brown Sugar (Remaster)" — parens are editorial metadata,
              not a different recording)
         800  Non-canonical parenthetical
              (e.g. "Brown Sugar (Soul Inside 808 Mix)" — remix variant)
         400  Title contains the requested name elsewhere
        None  Wrong artist, no name overlap, or non-Song row

    Within a tier, lower row index wins (Apple's relevance signal).

    Returns (row_index_1based, resolved_artist) on a match. None if nothing
    qualifies — caller surfaces a clear "no match" error.
    """
    safe_name = name.lower()
    safe_artist = artist.lower()
    ok, raw = run_applescript("""
tell application "System Events"
    tell process "Music"
        try
            set t to table 1 of scroll area 1 of (first pop over)
        on error
            return "NO_POPOVER"
        end try
        set acc to ""
        set i to 0
        repeat with r in (rows of t)
            set i to i + 1
            try
                set cell to UI element 1 of r
                set txts to (every static text of cell)
                if (count of txts) >= 2 then
                    set v1 to (value of (item 1 of txts)) as text
                    set v2 to (value of (item 2 of txts)) as text
                    set acc to acc & i & "|||" & v1 & "|||" & v2 & linefeed
                end if
            end try
        end repeat
        return acc
    end tell
end tell""")
    if not ok or not raw or raw.strip() == "NO_POPOVER":
        return None

    # Apple's "Type · Artist" labels use U+00B7 surrounded by U+2004
    # (THREE-PER-EM SPACE), sometimes U+00A0 (NBSP). Normalize to ASCII.
    SONG_PREFIX = "song · "
    CANONICAL_PAREN_MARKERS = (
        "original",
        "album version",
        "remaster",
        "stereo",
        "mono",
        "single version",
        "radio edit",
        "studio version",
    )

    # Parse rows into (idx, v1_lower, v2_lower, v2_norm).
    candidates = []
    for line in raw.strip().split("\n"):
        parts = line.split("|||")
        if len(parts) < 3:
            continue
        try:
            idx = int(parts[0])
        except ValueError:
            continue
        v1_lower = parts[1].replace("￼", "").strip().lower()
        v2_norm = parts[2].replace(" ", " ").replace(" ", " ").strip()
        v2_lower = v2_norm.lower()
        candidates.append((idx, v1_lower, v2_lower, v2_norm))

    def _score(row) -> Optional[tuple[int, int, int]]:
        idx, v1_lower, v2_lower, _v2_norm = row
        if not v2_lower.startswith(SONG_PREFIX):
            return None  # Album/Artist/MusicVideo/Playlist all rejected
        if safe_artist and safe_artist not in v2_lower:
            return None
        if v1_lower == safe_name:
            tier = 2000
        elif v1_lower.startswith(safe_name + " ("):
            rest = v1_lower[len(safe_name) + 2 :]
            tier = 1700 if any(rest.startswith(m) for m in CANONICAL_PAREN_MARKERS) else 800
        elif safe_name in v1_lower:
            tier = 400
        else:
            return None
        rank_bonus = max(0, 11 - min(idx, 11))
        return (tier, rank_bonus, idx)

    scored = [(s, row) for row in candidates if (s := _score(row)) is not None]
    if not scored:
        return None
    scored.sort(key=lambda x: (-x[0][0], -x[0][1], x[0][2]))
    _best_score, best_row = scored[0]
    idx, _v1, _v2l, v2_norm = best_row
    return idx, v2_norm[len(SONG_PREFIX) :].strip()


def _click_popover_row(idx: int) -> tuple[bool, str]:
    """CoreGraphics-click the center of pop-over row ``idx`` (1-based).

    AppleScript's ``click row N`` has been observed to behave differently
    than a real human click on this UI — CoreGraphics events match the
    real-click behavior of NAVIGATING to the song page (rather than
    starting playback).
    """
    ok, pos = run_applescript(f"""
tell application "System Events"
    tell process "Music"
        try
            set t to table 1 of scroll area 1 of (first pop over)
            set rp to position of (row {idx} of t)
            set rs to size of (row {idx} of t)
            set cx to (item 1 of rp) + (item 1 of rs) / 2
            set cy to (item 2 of rp) + (item 2 of rs) / 2
            return (cx as text) & "," & (cy as text)
        on error
            return "ERR"
        end try
    end tell
end tell""")
    if not ok or pos.strip() == "ERR":
        return False, f"Could not get position of pop-over row {idx}"
    try:
        cx, cy = [float(v) for v in pos.strip().split(",")]
    except ValueError:
        return False, f"Invalid row position: {pos}"
    if not _jxa_mouse_click(cx, cy):
        return False, "CoreGraphics click failed"
    return True, ""


def _wait_for_song_page(name: str, timeout: float = 5.0) -> Optional[str]:
    """After clicking a pop-over Song row, poll for the song detail page
    to render — confirmed by the presence of a track group whose description
    matches ``name`` inside the album-context list.

    Returns the AppleScript path to the matched track group, or None on
    timeout. The path is suitable for subsequent position/hover queries.
    """
    safe_name = _escape_for_applescript(name)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # Section index varies by page layout (sometimes album section is
        # list 1, sometimes list 2 with header sections preceding it).
        # Search every list and return the index of the one containing our
        # track. Returns "0" if no section has the track yet.
        ok, found_idx = run_applescript(f"""
tell application "System Events"
    tell process "Music"
        try
            set lst to list 1 of scroll area 2 of splitter group 1 of window 1
            set sectionCount to count of lists of lst
            repeat with i from 1 to sectionCount
                try
                    set sec to list i of lst
                    if exists (first group of sec whose description is "{safe_name}") then
                        return i as text
                    end if
                end try
            end repeat
            return "0"
        on error
            return "0"
        end try
    end tell
end tell""")
        if ok and found_idx.strip().isdigit() and int(found_idx.strip()) > 0:
            i = int(found_idx.strip())
            return (
                f"first group of list {i} of list 1 of scroll area 2 of "
                f'splitter group 1 of window 1 whose description is "{safe_name}"'
            )
        time.sleep(0.2)
    return None


def _hover_and_find_button(
    group_path: str,
    expected_desc: str,
    max_wait: float = 2.0,
) -> tuple[bool, Optional[tuple[float, float]]]:
    """Hover the center of the row at ``group_path``, then poll for a
    button whose description EXACTLY matches ``expected_desc``. Returns
    (True, (x, y)) with the button's center coords on success, or
    (False, None) if the button never becomes visible.

    This is the validate-before-act primitive: we don't click until we've
    confirmed the right element is there. Lets callers distinguish a
    stale-state miss (no Add to Library button → track may already be in
    library) from a real failure.
    """
    # First get the row position so we know where to hover.
    ok, pos = run_applescript(f"""
tell application "System Events"
    tell process "Music"
        try
            set g to {group_path}
            set gp to position of g
            set gs to size of g
            return ((item 1 of gp) + (item 1 of gs) / 2 as text) & "," & ((item 2 of gp) + (item 2 of gs) / 2 as text)
        on error
            return "ERR"
        end try
    end tell
end tell""")
    if not ok or pos.strip() == "ERR":
        return False, None
    try:
        hcx, hcy = [float(v) for v in pos.strip().split(",")]
    except ValueError:
        return False, None

    if not _hover_with_nudge(hcx, hcy):
        return False, None

    # Poll for the expected button to appear. macOS 26 sometimes drops the
    # initial mouseMoved event, so re-hover ONCE at the halfway point. Don't
    # re-hover repeatedly — each _hover_with_nudge is two CG events; flooding
    # them confuses Music.app's hover-state machine.
    start = time.monotonic()
    deadline = start + max_wait
    rehovered = False
    while time.monotonic() < deadline:
        ok, bpos = run_applescript(f"""
tell application "System Events"
    tell process "Music"
        try
            set g to {group_path}
            set b to (first button of g whose description is "{expected_desc}")
            set bp to position of b
            set bs to size of b
            return ((item 1 of bp) + (item 1 of bs) / 2 as text) & "," & ((item 2 of bp) + (item 2 of bs) / 2 as text)
        on error
            return "NOT_FOUND"
        end try
    end tell
end tell""")
        if ok and bpos.strip() != "NOT_FOUND":
            try:
                bx, by = [float(v) for v in bpos.strip().split(",")]
                return True, (bx, by)
            except ValueError:
                pass
        if not rehovered and time.monotonic() > start + max_wait / 2:
            _hover_with_nudge(hcx, hcy)
            rehovered = True
        time.sleep(0.05)
    return False, None


def _find_add_button_in_highlighted_row(
    desc: str = "Add to Library",
) -> Optional[tuple[float, float]]:
    """Return the center coords of the ``desc`` button (default "Add to Library")
    on the highlighted (``?i=``) track row's group, or None.

    Call AFTER hovering the row (the button is hover-revealed). Mirrors
    :func:`_find_highlighted_track_position`'s cross-version group walk — finding
    the row by the "Favorite" button Music adds to the ``?i=`` track — so it works
    on both old and new Music. Returns None if the row has no such button.

    Pass ``desc="Download"`` to detect the already-in-library state: Music swaps
    "Add to Library" for "Download" once a track is in the library.
    """
    ok, result = run_applescript(f"""
tell application "System Events"
    tell process "Music"
        set sg to splitter group 1 of window "Music"
        set sa to scroll area 2 of sg
        repeat with subList in (every list of list 1 of sa)
            repeat with g in (every group of subList)
                try
                    set hasFav to false
                    repeat with b in (every button of g)
                        if description of b is "Favorite" then set hasFav to true
                    end repeat
                    if hasFav then
                        repeat with b in (every button of g)
                            if description of b is "{desc}" then
                                -- NB: 'by' is a reserved word in AppleScript
                                -- (repeat ... by); using it as a variable is a
                                -- syntax error that silently killed this whole
                                -- finder. Use bposx/bposy.
                                set {{bposx, bposy}} to position of b
                                set {{bw, bh}} to size of b
                                return ((bposx + bw / 2) as text) & "," & ((bposy + bh / 2) as text)
                            end if
                        end repeat
                        return "NO_ADD"
                    end if
                end try
            end repeat
        end repeat
        return "NOT_FOUND"
    end tell
end tell""")
    if ok and "," in (result or ""):
        try:
            x, y = [float(v) for v in result.strip().split(",")]
            return x, y
        except ValueError:
            pass
    return None


def _row_has_button(group_path: str, desc: str) -> bool:
    """Return True if the song-detail row at ``group_path`` exposes a button
    whose description is exactly ``desc`` (e.g. "Download").

    Used to disambiguate the step-6 miss: when "Add to Library" isn't present,
    a "Download" button means the track is ALREADY in the library (the add
    control is replaced by the offline-download control) — a success, not a
    failure. Call after hovering the row so hover-revealed controls are live.
    """
    ok, result = run_applescript(f"""
tell application "System Events"
    tell process "Music"
        try
            if exists (first button of ({group_path}) whose description is "{desc}") then
                return "YES"
            end if
            return "NO"
        on error
            return "NO"
        end try
    end tell
end tell""")
    return ok and result.strip() == "YES"


def ui_add_to_library_via_url(name: str, song_url: str, artist: str = "") -> tuple[bool, str]:
    """Add a catalog track to the library by deep-linking to its album page and
    clicking the hover-revealed "Add to Library" button on the highlighted row.

    Used as the macOS-15 add path (where the search autocomplete pop-over isn't
    in the accessibility tree, so :func:`ui_add_to_library` can't work). The
    caller resolves the track tokenlessly via the iTunes Search API and passes
    its Apple Music ``song_url``; ``?i=`` makes Music highlight that track on the
    album page.

    Reuses the SAME proven, cross-version row finder as play-by-URL
    (:func:`_find_highlighted_track_position` — locates the row by the "Favorite"
    button Music marks the highlighted track with, not by text, so it works on
    both old and new Music), then hovers it and CoreGraphics-clicks the
    "Add to Library" button (a real click, since the SwiftUI button's ``AXPress``
    doesn't fire).

    Returns (success, message).
    """
    if not name.strip():
        return False, "Empty track name"
    if not song_url.strip():
        return False, "Empty track URL"
    if is_screen_locked() is True:
        return False, (
            "Screen is locked — UI automation needs the Mac unlocked and on the active display."
        )

    ok, msg = open_catalog_song(song_url)
    if not ok:
        return False, f"Could not open track: {msg}"
    _ensure_music_frontmost()

    # Poll for the highlighted track row to render (catalog pages can take a few
    # seconds to load — same adaptive wait play-by-URL uses).
    deadline = time.monotonic() + 12.0
    pos = _find_highlighted_track_position()
    while pos is None and time.monotonic() < deadline:
        time.sleep(1.0)
        pos = _find_highlighted_track_position()
    if pos is None:
        return False, (
            f"Could not find {name!r} on its album page — the highlighted track "
            f"row never appeared (page may not have loaded, or the URL didn't "
            f"resolve to a track)."
        )
    cx, cy, _desc = pos

    # Scroll the row into view if it's below the fold, then re-find it.
    win_bottom = _get_window_bottom()
    if win_bottom and cy > win_bottom - 30:
        _ensure_music_frontmost()
        _jxa_scroll_down(cx, win_bottom - 200, amount=10)
        pos = _find_highlighted_track_position()
        if pos is None:
            return False, "Lost the track row after scrolling it into view."
        cx, cy, _desc = pos

    # Hover (nudge first to guarantee a mouseMoved event) to reveal the row's
    # Add to Library button, then click it for real. The button is hover-revealed
    # and the reveal is racy — a single hover+query intermittently misses (the
    # query can run before the button renders, or the row shifts after a prior
    # search attempt left the window scrolled). Retry hover+find a few times,
    # re-finding the row position each round in case it moved. Mirrors the
    # pop-over path's _hover_and_find_button resilience.
    bpos = None
    for _attempt in range(4):
        _ensure_music_frontmost()
        if not _hover_with_nudge(cx, cy):
            continue
        time.sleep(0.8)
        bpos = _find_add_button_in_highlighted_row()
        if bpos is not None:
            break
        repos = _find_highlighted_track_position()
        if repos is not None:
            cx, cy, _desc = repos
    if bpos is None:
        # No "Add to Library" button. If the row shows "Download" instead, the
        # track is already in the library (success), not a failure — mirror the
        # pop-over path's disambiguation.
        if _find_add_button_in_highlighted_row("Download") is not None:
            suffix = f" by {artist!r}" if artist else ""
            return True, f"{name!r}{suffix} already in library"
        return False, (
            f"{name!r}: no 'Add to Library' button on the track row — it may "
            f"be a single whose add control is album-level, or the page didn't "
            f"fully render."
        )
    # Click the validated button. A second click is cheap insurance against the
    # occasional CoreGraphics click that lands a frame before the hover-revealed
    # SwiftUI button is interactive — but we do NOT gate success on the button
    # flipping to "Download", because that flip can lag the add by several
    # seconds (iCloud) and a slow flip would otherwise false-negative a real add.
    # The caller's library-verify (with trust-on-lag) is the source of truth.
    if not _jxa_mouse_click(*bpos):
        return False, "CoreGraphics click on 'Add to Library' failed."
    time.sleep(0.8)
    if _find_add_button_in_highlighted_row("Add to Library") is not None:
        # Button still present — the first click may not have registered; re-click.
        _jxa_mouse_click(*bpos)

    suffix = f" by {artist!r}" if artist else ""
    return True, f"Added {name!r}{suffix} to library"


# -----------------------------------------------------------------------------
# Public ui_* entrypoints — thin compositions of the primitives above.
# -----------------------------------------------------------------------------


def ui_search_catalog(query: str) -> tuple[bool, list[dict], str]:
    """Search the Apple Music catalog via Music.app's search field.

    Types the query into the search field, submits it, and parses
    the "Top Results" section from the results page.

    Wraps the search flow in a single transparent retry: if the first
    attempt fails (transient UI flake — popover state, focus jitter,
    user moved the mouse mid-flow, cached search-field path went stale),
    sleep ~1s and try once more. Two attempts total. Real users WILL
    interact with the keyboard/mouse during automation; this absorbs
    the most common interference modes without surfacing as a hard fail.

    Args:
        query: Search query (e.g. "Radiohead Creep", "Taylor Swift")

    Returns:
        Tuple of (success, results, error).
        Each result dict has: name, type ("Song", "Album", "Artist", etc.),
        artist (if applicable), and index (position in results).
        error is an empty string on success; a human-readable message otherwise.
    """
    if not query or not query.strip():
        return False, [], "Empty query"

    last_err = ""
    for attempt in range(2):
        if attempt > 0:
            time.sleep(1.0)
        ok, err = _focus_search_field(query)
        if not ok:
            last_err = err
            continue
        ok, raw = _wait_for_top_results()
        if not ok:
            last_err = raw
            continue
        if not raw:
            return True, [], ""
        return True, _parse_top_results(raw), ""
    return False, [], last_err


def ui_clear_search() -> None:
    """Clear the Music.app search field and dismiss search."""
    run_applescript(f"""
tell application "System Events"
    tell process "Music"
        set searchField to {_get_search_field()}
        set focused of searchField to true
        delay 0.2
        set value of searchField to ""
        delay 0.2
        key code 53
    end tell
end tell""")


def ui_add_to_library(name: str, artist: str = "") -> tuple[bool, str]:
    """Add a catalog track to library via Music.app UI — popover-canonical flow.

    The flow is validate-before-act at every step:
    1. Foreground Music + open search popover with "{name} {artist}" query
    2. Validate popover appeared
    3. Walk popover rows for canonical "Song · {artist}" match (refuses
       Album/Artist/MusicVideo rows since those navigate to non-track pages)
    4. CoreGraphics-click the matched row → song detail page
    5. Validate navigation: track group with `name` exists in album list
    6. Hover the track row, validate "Add to Library" button (exact desc)
       appears — refuses to click anything else (Favorite, Download, More)
    7. CoreGraphics-click the validated button

    Replaces the previous Top Results-based approach which suffered from
    Apple's opaque ranking lottery (track sometimes only surfaces as Album
    row, "Hong Kong"-style B-sides exposing no Add to Library button on
    Top Results rows, etc.).

    ``artist`` is required for canonical matching. Empty artist falls back
    to picking the first Song-prefix row, but with a warning in the result
    message — caller should provide artist whenever known.

    Returns:
        Tuple of (success, message)
    """
    if not name.strip():
        return False, "Empty track name"

    query = f"{name} {artist}".strip()

    # Step 1+2: open popover with validation
    ok, err = _open_search_popover(query)
    if not ok:
        return False, f"Popover did not appear: {err}"

    # Step 3: find canonical Song row (also resolves artist if not given)
    match = _find_popover_song_row(name, artist)
    if match is None:
        ui_clear_search()
        suffix = f" by {artist!r}" if artist else ""
        return False, (
            f"No Song-row match in autocomplete pop-over for {name!r}{suffix}. "
            f"Track may not exist on Apple Music or pop-over hasn't surfaced "
            f"it. Try a more specific query."
        )
    idx, resolved_artist = match
    # Adopt the popover's resolved artist when caller didn't supply one,
    # so downstream messages and verification use the canonical name.
    if not artist:
        artist = resolved_artist

    # Step 4: click row → navigates
    ok, err = _click_popover_row(idx)
    if not ok:
        return False, f"Failed to click pop-over row: {err}"

    # Step 5: validate landing on song page
    group_path = _wait_for_song_page(name)
    if group_path is None:
        return False, (
            f"Pop-over navigation didn't reach a song page with track "
            f"{name!r} — Music.app may have shown a different view"
        )

    # Step 6: hover + validate Add to Library button is present
    ok, btn_pos = _hover_and_find_button(group_path, "Add to Library")
    if not ok:
        # No "Add to Library" button. Two very different causes:
        #   (a) the track is ALREADY in the library — Music replaces the add
        #       control with a "Download" (offline) button. That's a success;
        #       report it so the caller's library-verify confirms instead of
        #       falling through to the (slower, lossy) deep-link path.
        #   (b) a genuine hover/timing miss with no button at all — a real
        #       failure the caller should surface.
        if _row_has_button(group_path, "Download"):
            return True, f"'{name}' already in library"
        return False, (
            f"'Add to Library' button not visible after hover on {name!r}. "
            f"Track may already be in library (button would be 'Download' "
            f"instead) — try search_library to confirm."
        )

    # Step 7: click validated button
    bx, by = btn_pos
    if not _jxa_mouse_click(bx, by):
        return False, "CoreGraphics click on Add to Library button failed"

    return True, f"Added '{name}' by '{artist}' to library"


def ui_play_result(result_name: str) -> tuple[bool, str]:
    """Play a catalog item from search results via Music.app UI.

    Must be called after ui_search_catalog() with results visible.
    Hovers over the result to reveal the play checkbox and clicks it.

    Args:
        result_name: Exact name of the result to play (as returned by ui_search_catalog)

    Returns:
        Tuple of (success, message)
    """
    ok, err = _hover_then_click_subelement(
        result_name,
        "set inner to checkbox 1 of e",
    )
    if not ok:
        if err == "sub-element not visible after hover":
            return False, f"Play checkbox not visible after hover for '{result_name}'"
        return False, err

    playing, now_playing = _verify_track_playing(result_name)
    if playing:
        return True, f"Playing: {result_name}"
    if _check_playing():
        return False, f"Clicked play but got '{now_playing}' instead of '{result_name}'"
    return False, f"Clicked play on '{result_name}' but playback didn't start"


def ui_play_result_by_query(query: str) -> tuple[bool, str]:
    """Search catalog via UI and play the first song result.

    Convenience function that combines ui_search_catalog + ui_play_result.

    Args:
        query: Search query (e.g. "Radiohead Creep")

    Returns:
        Tuple of (success, message)
    """
    ok, results, why = ui_search_catalog(query)
    if not ok or not results:
        ui_clear_search()
        return False, why or f"No results found for '{query}'"

    # Find first song result
    target = None
    for r in results:
        if r["type"] == "Song":
            target = r
            break
    if target is None:
        target = results[0]

    ok, msg = ui_play_result(target["name"])
    ui_clear_search()
    return ok, msg


def ui_add_to_playlist(
    playlist_name: str, query: str, artist: str = "", song_url: str = ""
) -> tuple[bool, str]:
    """Add a catalog track to a playlist via UI automation (no API required).

    Composite flow that uses popover-canonical-match for the catalog lookup,
    so we never substitute a wrong track:

    1. Parse query into (name, artist) — split on " - " when artist not given
    2. ui_add_to_library(name, artist) — popover -> song page -> per-row "+"
    3. Wait for iCloud sync, verify track is in library
    4. add_track_to_playlist (AppleScript backend)
    5. Verify track lands in playlist (sleep past Music.app's local-reconcile
       rollback window, then poll)

    Args:
        playlist_name: Target playlist name
        query: Search query (e.g. "Track Name" or "Track Name - Artist")
        artist: Optional artist; recommended for canonical matching

    Returns:
        Tuple of (success, message)
    """
    # Derive (name, artist). Two common caller patterns:
    #   (a) query="Track Name Artist Name", artist="Artist Name" —
    #       strip the artist token from the end of query so name is
    #       just the track title (the popover row labels track names
    #       only, not "Track - Artist").
    #   (b) query="Track - Artist", no artist — split on " - ".
    name = query.strip()
    if artist:
        artist_lc = artist.lower()
        if name.lower().endswith(" " + artist_lc):
            name = name[: -(len(artist) + 1)].strip()
        elif name.lower() == artist_lc:
            return False, f"Query {query!r} is just the artist; need a track name too"
    elif " - " in query:
        parts = query.split(" - ", 1)
        if len(parts) == 2:
            name, artist = parts[0].strip(), parts[1].strip()

    # Step 1+2: Add to library. Try the pop-over flow first — it's the validated
    # path on macOS 26 (new Music), where the search autocomplete IS in the
    # accessibility tree. If the pop-over isn't accessible (macOS 15 / old Music)
    # and the caller resolved a catalog URL, deep-link to the album page instead.
    ok, msg = ui_add_to_library(name, artist)
    if not ok and song_url:
        ok, msg = ui_add_to_library_via_url(name, song_url, artist)
    if not ok:
        return False, f"Failed to add to library: {msg}"

    # Step 3: Verify the track is in the library before the playlist add, using
    # a DIRECT object lookup (find_library_track) rather than the native search
    # index — the index lags noticeably after a fresh UI add (a just-added track
    # can be missing from `search` for >12s while the object query already finds
    # it). Poll briefly to cover any genuine sync lag.
    track_name = name
    track_artist = artist
    library_visible = False
    sync_deadline = time.monotonic() + 12.0
    while time.monotonic() < sync_deadline:
        ok, _info = find_library_track(track_name, track_artist)
        if ok:
            library_visible = True
            break
        time.sleep(0.4)
    if not library_visible:
        return False, (
            f"Added '{track_name}' to library but it didn't appear in the local "
            f"library within 12s — iCloud Library sync may be delayed."
        )

    # Step 4: Add to playlist via existing AppleScript backend
    ok, result = add_track_to_playlist(playlist_name, track_name, track_artist)
    if not ok:
        return False, f"Added to library but failed to add to playlist: {result}"

    # Step 5: Verify it persisted. Some user-created playlists silently
    # revert local AppleScript edits within ~1s; settle past the rollback
    # window before the first probe.
    time.sleep(0.6)
    verify_deadline = time.monotonic() + 3.0
    while time.monotonic() < verify_deadline:
        exists_ok, exists = track_exists_in_playlist(
            playlist_name, track_name, track_artist or None
        )
        if exists_ok and exists:
            return True, f"Added {track_name} by {track_artist} to {playlist_name}"
        time.sleep(0.3)
    return False, (
        f"Added '{track_name}' to library but it did not persist in "
        f"'{playlist_name}' after retry. Some user-created playlists silently "
        f"revert AppleScript edits server-side; adding manually via Music.app's "
        f"right-click → Add to Playlist usually works."
    )


# =============================================================================
# Library Snapshot & Diff
# =============================================================================


def library_snapshot() -> tuple[bool, dict]:
    """Capture a full snapshot of the Music library for integrity checking.

    Returns a dict with:
        - track_count: total library tracks
        - playback: dict with player state, volume, shuffle, repeat, current track
        - playlists: dict mapping playlist name -> list of {name, artist, album}

    This is intentionally thorough (captures full track lists) so diffs can
    detect any accidental additions, removals, or reorders.
    """
    # Get total track count
    ok, count_str = run_applescript(
        'tell application "Music" to return (count of tracks of library playlist 1) as text'
    )
    if not ok:
        return False, {"error": f"Failed to count tracks: {count_str}"}
    try:
        track_count = int(count_str.strip())
    except ValueError:
        return False, {"error": f"Invalid track count: {count_str}"}

    # Get playback state
    ok, pb_str = run_applescript("""
tell application "Music"
    set ps to player state as text
    set v to (sound volume) as text
    set sh to (shuffle enabled) as text
    set rp to song repeat as text
    set ct to ""
    set ca to ""
    set calb to ""
    try
        set ct to name of current track
        set ca to artist of current track
        set calb to album of current track
    end try
    return ps & return & v & return & sh & return & rp & return & ct & return & ca & return & calb
end tell""")
    playback_state = {}
    if ok and pb_str:
        lines = pb_str.strip().split("\n")
        playback_state = {
            "player_state": lines[0] if len(lines) > 0 else "unknown",
            "volume": int(lines[1]) if len(lines) > 1 and lines[1].strip().isdigit() else 0,
            "shuffle": lines[2].strip() == "true" if len(lines) > 2 else False,
            "repeat": lines[3].strip() if len(lines) > 3 else "unknown",
            "current_track": lines[4].strip() if len(lines) > 4 and lines[4].strip() else None,
            "current_artist": lines[5].strip() if len(lines) > 5 and lines[5].strip() else None,
            "current_album": lines[6].strip() if len(lines) > 6 and lines[6].strip() else None,
        }

    # Get all user playlists and their contents, with folder paths
    ok, playlist_data = run_applescript("""
tell application "Music"
    set r to ""
    repeat with p in user playlists
        set pName to name of p
        set pKind to smart of p
        if pKind is false and pName is not "Music" and pName is not "Music Videos" then
            -- Build folder path
            set folderPath to ""
            try
                set current to p
                set pathParts to {}
                repeat
                    try
                        set current to parent of current
                        set beginning of pathParts to name of current
                    on error
                        exit repeat
                    end try
                end repeat
                if (count of pathParts) > 0 then
                    set AppleScript's text item delimiters to "/"
                    set folderPath to pathParts as text
                    set AppleScript's text item delimiters to ""
                end if
            end try
            set r to r & "PLAYLIST:" & pName & "|||FOLDER:" & folderPath & return
            repeat with t in tracks of p
                try
                    set r to r & name of t & "|||" & artist of t & "|||" & album of t & return
                on error
                    -- skip inaccessible tracks (broken file references, error -1728)
                end try
            end repeat
        end if
    end repeat
    -- Also list folder playlists
    repeat with f in folder playlists
        set fPath to ""
        try
            set current to f
            set pathParts to {}
            repeat
                try
                    set current to parent of current
                    set beginning of pathParts to name of current
                on error
                    exit repeat
                end try
            end repeat
            if (count of pathParts) > 0 then
                set AppleScript's text item delimiters to "/"
                set fPath to pathParts as text
                set AppleScript's text item delimiters to ""
            end if
        end try
        set r to r & "FOLDER:" & name of f & "|||PATH:" & fPath & return
    end repeat
    return r
end tell""")
    if not ok:
        return False, {"error": f"Failed to get playlists: {playlist_data}"}

    playlists = {}
    folders = {}
    current_playlist = None
    for line in playlist_data.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("PLAYLIST:"):
            # Format: PLAYLIST:name|||FOLDER:path
            parts = line.split("|||FOLDER:")
            pl_name = parts[0][9:]
            folder_path = parts[1] if len(parts) > 1 else ""
            full_path = f"{folder_path}/{pl_name}" if folder_path else pl_name
            current_playlist = full_path
            playlists[full_path] = {"folder": folder_path, "tracks": []}
        elif line.startswith("FOLDER:"):
            # Format: FOLDER:name|||PATH:parent_path
            current_playlist = None  # Folder lines end any playlist track context
            parts = line.split("|||PATH:")
            f_name = parts[0][7:]
            parent_path = parts[1] if len(parts) > 1 else ""
            full_path = f"{parent_path}/{f_name}" if parent_path else f_name
            folders[full_path] = {"name": f_name, "parent": parent_path}
        elif current_playlist is not None and "|||" in line:
            parts = line.split("|||")
            if len(parts) >= 3:
                playlists[current_playlist]["tracks"].append(
                    {
                        "name": parts[0],
                        "artist": parts[1],
                        "album": parts[2],
                    }
                )

    return True, {
        "track_count": track_count,
        "playback": playback_state,
        "playlists": playlists,
        "folders": folders,
    }


def library_diff(before: dict, after: dict) -> dict:
    """Compare two library snapshots and return differences.

    Args:
        before: snapshot dict from library_snapshot()
        after: snapshot dict from library_snapshot()

    Returns:
        Dict with:
            - track_count_change: int (positive = added, negative = removed)
            - playback_changes: dict of changed playback settings
            - playlists_added: list of playlist names
            - playlists_removed: list of playlist names
            - playlists_changed: dict of {name: {added: [...], removed: [...]}}
            - is_clean: True if no library changes detected (playback state changes are tracked separately)
    """
    # Compare playback state
    before_pb = before.get("playback", {})
    after_pb = after.get("playback", {})
    playback_changes = {}
    for key in ["player_state", "volume", "shuffle", "repeat", "current_track", "current_artist"]:
        if before_pb.get(key) != after_pb.get(key):
            playback_changes[key] = {"before": before_pb.get(key), "after": after_pb.get(key)}

    result = {
        "track_count_change": after.get("track_count", 0) - before.get("track_count", 0),
        "playback_changes": playback_changes,
        "playlists_added": [],
        "playlists_removed": [],
        "playlists_changed": {},
        "is_clean": True,
    }

    before_pl = before.get("playlists", {})
    after_pl = after.get("playlists", {})

    # Find added/removed playlists
    for name in after_pl:
        if name not in before_pl:
            result["playlists_added"].append(name)
    for name in before_pl:
        if name not in after_pl:
            result["playlists_removed"].append(name)

    # Compare track lists for playlists that exist in both
    # Handle both old format (list) and new format (dict with "tracks" key)
    def _get_tracks(pl_entry):
        if isinstance(pl_entry, list):
            return pl_entry
        if isinstance(pl_entry, dict):
            return pl_entry.get("tracks", [])
        return []

    for name in before_pl:
        if name in after_pl:
            before_tracks = {f"{t['name']}|{t['artist']}" for t in _get_tracks(before_pl[name])}
            after_tracks = {f"{t['name']}|{t['artist']}" for t in _get_tracks(after_pl[name])}
            added = after_tracks - before_tracks
            removed = before_tracks - after_tracks
            if added or removed:
                result["playlists_changed"][name] = {
                    "added": list(added),
                    "removed": list(removed),
                }

    # Compare folders
    before_folders = set(before.get("folders", {}).keys())
    after_folders = set(after.get("folders", {}).keys())
    result["folders_added"] = list(after_folders - before_folders)
    result["folders_removed"] = list(before_folders - after_folders)

    # Determine if clean (library changes only — playback state changes don't count)
    if (
        result["track_count_change"] != 0
        or result["playlists_added"]
        or result["playlists_removed"]
        or result["playlists_changed"]
        or result["folders_added"]
        or result["folders_removed"]
    ):
        result["is_clean"] = False

    return result
