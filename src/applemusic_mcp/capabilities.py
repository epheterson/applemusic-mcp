"""Permission levels: what this server may reach, fixed by the user at launch.

The level comes from the MCP client config (`serve --level api`), never from
the model. The config tool cannot raise it, so text inside a track or
playlist name cannot either.

  music       Music.app via AppleScript. No network, no browser.
  api         + Apple's music APIs over the network.
  web         + the Safari and Chrome web players (Up Next queue).
  --allow-ui  + Accessibility UI scripting (clicks, keystrokes), any level.

Unset means unrestricted, the behavior before levels existed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

LEVELS = ("music", "api", "web")
ENV_LEVEL = "APPLEMUSIC_LEVEL"
ENV_UI = "APPLEMUSIC_ALLOW_UI"

# The `mode` preference values each level may select.
MODES_BY_LEVEL = {
    "music": ("auto", "native"),
    "api": ("auto", "native", "api"),
    "web": ("auto", "native", "api", "safari", "chrome", "web"),
}

_NEEDS = {"network": "api", "web": "web"}


class CapabilityDenied(RuntimeError):
    """Raised when a call needs more than the configured level allows."""


@dataclass(frozen=True)
class Level:
    name: str
    network: bool
    web: bool
    ui: bool
    explicit: bool


def parse(level: str | None, ui: str | None) -> Level:
    """Build a Level from the raw env values. Raises ValueError on an unknown
    level, so a typo stops the server instead of silently allowing everything."""
    if level is None:
        return Level("unrestricted", True, True, True, False)
    name = level.strip().lower()
    if name not in LEVELS:
        raise ValueError(f"Unknown level {level!r}. Choose one of: {', '.join(LEVELS)}.")
    allow_ui = (ui or "").strip().lower() in ("1", "true", "yes")
    return Level(name, name in ("api", "web"), name == "web", allow_ui, True)


def current() -> Level:
    return parse(os.environ.get(ENV_LEVEL), os.environ.get(ENV_UI))


def denied_message(feature: str, needs: str) -> str:
    """The refusal text. It tells the user where the level lives; it never
    invites the model to change it."""
    lvl = current()
    change = "add --allow-ui" if needs == "ui" else f"use --level {needs}"
    return (
        f"{feature} isn't allowed at this server's permission level ({lvl.name}). "
        f"The user chose that level in their MCP client config; to allow this they "
        f"can {change} there and restart the client."
    )


def require(cap: str, feature: str) -> None:
    """Raise CapabilityDenied unless the level allows ``cap`` ("network", "web" or "ui")."""
    if getattr(current(), cap):
        return
    raise CapabilityDenied(denied_message(feature, _NEEDS.get(cap, "ui")))
