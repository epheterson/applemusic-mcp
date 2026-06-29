"""Filesystem roots for all persistent state — env-overridable.

Three roots, matching the three things the tool stores:
  - config  (~/.config/applemusic-mcp)    : credentials, config.json, the .p8 key
  - data    (~/.applemusic-mcp)            : the Chrome profile (signed-in session)
  - cache   (~/.cache/applemusic-mcp)      : track cache, exports, snapshots, audit log

Precedence per root: the root-specific env var wins, else ``APPLEMUSIC_MCP_HOME``
relocates all three under one base, else the platform default under ``$HOME``.

Why this exists: the paths were hardcoded to ``Path.home()`` with no override, which
(a) let the test suite write into the user's real dirs and (b) gave users no way to
relocate data. Honoring env here unblocks full test isolation (point one base at a
temp dir) and a clean ``reset --all`` / uninstall.
"""

from __future__ import annotations

import os
from pathlib import Path


def _home() -> Path:
    base = os.environ.get("APPLEMUSIC_MCP_HOME")
    return Path(base).expanduser() if base else Path.home()


def config_dir() -> Path:
    """Credentials + config.json + .p8 key."""
    env = os.environ.get("APPLEMUSIC_CONFIG_DIR")
    return Path(env).expanduser() if env else _home() / ".config" / "applemusic-mcp"


def data_dir() -> Path:
    """Durable app data — the Chrome profile (the signed-in Apple Music session)."""
    env = os.environ.get("APPLEMUSIC_DATA_DIR")
    return Path(env).expanduser() if env else _home() / ".applemusic-mcp"


def cache_dir() -> Path:
    """Regenerable state — track cache, exports, snapshots, audit log."""
    env = os.environ.get("APPLEMUSIC_CACHE_DIR")
    return Path(env).expanduser() if env else _home() / ".cache" / "applemusic-mcp"


def all_state_dirs() -> list[Path]:
    """The three roots, for ``reset --all`` / uninstall."""
    return [config_dir(), data_dir(), cache_dir()]
