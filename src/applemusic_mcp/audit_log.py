"""Audit log for Apple Music MCP operations.

Logs all destructive/modifying operations to a JSONL file for history and undo reference.
Each line is a JSON object with: timestamp, action, details, and optional undo_info.

Operations logged:
- add_to_library: Adding tracks to library
- remove_from_library: Removing tracks from library
- add_to_playlist: Adding tracks to a playlist
- remove_from_playlist: Removing tracks from a playlist
- create_playlist: Creating a new playlist
- create_folder: Creating a new folder
- delete_playlist: Deleting a playlist
- delete_folder: Deleting a folder
- rename_playlist: Renaming a playlist
- rename_folder: Renaming a folder
- move_to_folder: Moving a playlist into a folder
- copy_playlist: Copying a playlist
- rating: Rating changes (love/dislike/stars)
- set_preference: Configuration preference changes
"""

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

MAX_LOG_SIZE = 10 * 1024 * 1024  # 10 MB
_log_lock = threading.Lock()


def get_audit_log_path() -> Path:
    """Get the audit log file path (env-overridable; see paths.cache_dir)."""
    from . import paths

    log_dir = paths.cache_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "audit_log.jsonl"


def _rotate_if_needed(log_path: Path) -> None:
    """Rotate the audit log if it exceeds MAX_LOG_SIZE."""
    try:
        if log_path.exists() and log_path.stat().st_size > MAX_LOG_SIZE:
            backup = log_path.with_suffix(".jsonl.1")
            if backup.exists():
                backup.unlink()
            log_path.rename(backup)
            logger.info(f"Rotated audit log ({MAX_LOG_SIZE // (1024*1024)}MB limit)")
    except Exception as e:
        logger.warning(f"Failed to rotate audit log: {e}")


def log_action(
    action: str,
    details: dict[str, Any],
    undo_info: Optional[dict[str, Any]] = None,
) -> None:
    """Log an action to the audit log.

    Args:
        action: The action type (e.g., "add_to_library", "remove_from_playlist")
        details: Action-specific details (tracks involved, playlist names, etc.)
        undo_info: Optional information needed to undo this action
    """
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "details": details,
    }
    if undo_info:
        entry["undo_info"] = undo_info

    try:
        log_path = get_audit_log_path()
        with _log_lock:
            _rotate_if_needed(log_path)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
    except Exception as e:
        logger.warning(f"Failed to write audit log: {e}")


def get_recent_entries(limit: int = 50) -> list[dict]:
    """Get the most recent audit log entries.

    Args:
        limit: Maximum number of entries to return

    Returns:
        List of audit log entries (most recent first)
    """
    log_path = get_audit_log_path()
    if not log_path.exists():
        return []

    entries = []
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except Exception as e:
        logger.warning(f"Failed to read audit log: {e}")
        return []

    # Return most recent first
    return entries[-limit:][::-1]


def format_entries_for_display(entries: list[dict], limit: int = 20) -> str:
    """Format audit log entries for human-readable display.

    Args:
        entries: List of audit log entries
        limit: Maximum entries to show

    Returns:
        Formatted string representation
    """
    if not entries:
        return "No audit log entries found."

    lines = [f"Audit Log ({len(entries)} recent entries):"]
    lines.append("-" * 60)

    for entry in entries[:limit]:
        ts = entry.get("timestamp", "unknown")
        # Parse and format timestamp more nicely
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            ts_display = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
        except Exception:
            ts_display = ts

        action = entry.get("action", "unknown")
        details = entry.get("details", {})

        # Format based on action type
        if action == "add_to_library":
            tracks = details.get("tracks", [])
            lines.append(f"[{ts_display}] ADD TO LIBRARY: {len(tracks)} track(s)")
            for t in tracks[:5]:
                lines.append(f"    + {t}")
            if len(tracks) > 5:
                lines.append(f"    ... and {len(tracks) - 5} more")

        elif action == "remove_from_library":
            tracks = details.get("tracks", [])
            lines.append(f"[{ts_display}] REMOVE FROM LIBRARY: {len(tracks)} track(s)")
            for t in tracks[:5]:
                lines.append(f"    - {t}")
            if len(tracks) > 5:
                lines.append(f"    ... and {len(tracks) - 5} more")

        elif action == "add_to_playlist":
            playlist = details.get("playlist", "unknown")
            tracks = details.get("tracks", [])
            lines.append(f"[{ts_display}] ADD TO PLAYLIST '{playlist}': {len(tracks)} track(s)")
            for t in tracks[:5]:
                lines.append(f"    + {t}")
            if len(tracks) > 5:
                lines.append(f"    ... and {len(tracks) - 5} more")

        elif action == "remove_from_playlist":
            playlist = details.get("playlist", "unknown")
            tracks = details.get("tracks", [])
            lines.append(
                f"[{ts_display}] REMOVE FROM PLAYLIST '{playlist}': {len(tracks)} track(s)"
            )
            for t in tracks[:5]:
                lines.append(f"    - {t}")
            if len(tracks) > 5:
                lines.append(f"    ... and {len(tracks) - 5} more")

        elif action == "create_playlist":
            name = details.get("name", "unknown")
            playlist_id = details.get("playlist_id", "")
            lines.append(f"[{ts_display}] CREATE PLAYLIST: '{name}' (ID: {playlist_id})")

        elif action == "delete_playlist":
            name = details.get("name", "unknown")
            track_count = details.get("track_count", 0)
            lines.append(f"[{ts_display}] DELETE PLAYLIST: '{name}' ({track_count} tracks)")

        elif action == "copy_playlist":
            source = details.get("source", "unknown")
            dest = details.get("destination", "unknown")
            track_count = details.get("track_count", 0)
            lines.append(
                f"[{ts_display}] COPY PLAYLIST: '{source}' -> '{dest}' ({track_count} tracks)"
            )

        elif action == "rating":
            track = details.get("track", "unknown")
            rating_type = details.get("type", "unknown")
            value = details.get("value", "")
            lines.append(f"[{ts_display}] RATING: {rating_type} '{track}' {value}")

        elif action == "create_folder":
            name = details.get("name", "unknown")
            lines.append(f"[{ts_display}] CREATE FOLDER: '{name}'")

        elif action == "delete_folder":
            name = details.get("name", "unknown")
            lines.append(f"[{ts_display}] DELETE FOLDER: '{name}'")

        elif action == "rename_folder":
            old = details.get("old_name", "unknown")
            new = details.get("new_name", "unknown")
            lines.append(f"[{ts_display}] RENAME FOLDER: '{old}' -> '{new}'")

        elif action == "move_to_root":
            playlist = details.get("playlist", "unknown")
            lines.append(f"[{ts_display}] MOVE TO ROOT: '{playlist}'")

        elif action == "move_to_folder":
            playlist = details.get("playlist", "unknown")
            folder = details.get("folder", "unknown")
            lines.append(f"[{ts_display}] MOVE TO FOLDER: '{playlist}' -> '{folder}'")

        elif action == "set_preference":
            pref = details.get("preference", "unknown")
            old_val = details.get("old_value")
            new_val = details.get("new_value")
            lines.append(f"[{ts_display}] SET PREFERENCE: {pref} = {new_val} (was: {old_val})")

        elif action == "playlist_query":
            playlist = details.get("playlist", "unknown")
            track_count = details.get("track_count", 0)
            duration = details.get("duration_sec", 0)
            cache_hits = details.get("cache_hits", 0)
            cache_misses = details.get("cache_misses", 0)
            api_calls = details.get("api_calls", 0)
            lines.append(f"[{ts_display}] PLAYLIST QUERY: '{playlist}' ({track_count} tracks)")
            lines.append(
                f"    {duration}s | Cache: {cache_hits} hits, {cache_misses} misses | API: {api_calls} calls"
            )

        else:
            lines.append(f"[{ts_display}] {action.upper()}: {json.dumps(details)}")

        lines.append("")

    if len(entries) > limit:
        lines.append(
            f"... {len(entries) - limit} more entries (use config(action='audit-log', limit=N) for more)"
        )

    return "\n".join(lines)


def clear_audit_log() -> bool:
    """Clear the audit log file.

    Returns:
        True if successful, False otherwise
    """
    try:
        log_path = get_audit_log_path()
        if log_path.exists():
            log_path.unlink()
        return True
    except Exception as e:
        logger.warning(f"Failed to clear audit log: {e}")
        return False
