"""Permission levels: what this server may reach, fixed by the user at launch.

The level comes from the MCP client config (`serve --level api`), never from
the model. The config tool cannot raise it, so text inside a track or
playlist name cannot either.

  music       Music.app via AppleScript. No network, no browser.
  api         + Apple's music APIs over the network.
  web         + the Safari and Chrome web players (Up Next queue).
  full        everything, Accessibility included.
  --allow-ui  + Accessibility UI scripting (clicks, keystrokes), any level.

Unset means unrestricted: the same as full, the behavior before levels existed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

LEVELS = ("music", "api", "web", "full")
ENV_LEVEL = "APPLEMUSIC_LEVEL"
ENV_UI = "APPLEMUSIC_ALLOW_UI"

# The `mode` preference values each level may select.
MODES_BY_LEVEL = {
    "music": ("auto", "native"),
    "api": ("auto", "native", "api"),
    "web": ("auto", "native", "api", "safari", "chrome", "web"),
    "full": ("auto", "native", "api", "safari", "chrome", "web"),
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
    if name == "full":
        return Level("full", True, True, True, True)
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


def install_network_guard() -> bool:
    """At the music level, refuse every HTTP request the process makes.

    The per-feature checks give friendlier messages; this is the backstop for
    any path they miss. Every requests.get/post goes through Session.request.
    Returns True if the guard was installed."""
    if current().network:
        return False
    import requests
    from urllib.parse import urlsplit

    def _denied(self, method, url, *args, **kwargs):
        host = urlsplit(str(url)).hostname or str(url)
        raise CapabilityDenied(denied_message(f"Reaching {host}", "api"))

    requests.Session.request = _denied
    return True
