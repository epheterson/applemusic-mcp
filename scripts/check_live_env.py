#!/usr/bin/env python3
"""Preflight check for the live integration gate.

Exits 0 when the machine can actually run the tokenless UI-automation tests
(macOS, Music.app running + signed in, screen unlocked, catalog reachable),
and non-zero with guidance otherwise. The point is to FAIL LOUDLY rather than
let the live suite silently skip and produce a false-green release gate.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from applemusic_mcp import applescript as asc  # noqa: E402
from applemusic_mcp import server  # noqa: E402


def main() -> int:
    problems = []

    if not asc.is_available():
        problems.append("Not on macOS / AppleScript unavailable — the live gate must run on a Mac.")
    else:
        ok, out = asc.run_applescript('return (application "Music" is running)')
        if not (ok and out.strip() == "true"):
            problems.append("Music.app is not running. Open it and sign in to Apple Music.")

        if asc.is_screen_locked() is True:
            problems.append(
                "Screen is locked. Unlock the Mac — UI automation needs the active display."
            )

        # Free iTunes Search API reachable? (the resolver every tokenless add depends on)
        try:
            resolved = server._resolve_catalog_track_itunes("Bohemian Rhapsody", "Queen")
        except Exception as e:  # network or import-time failure
            resolved = None
            problems.append(f"iTunes Search API call raised: {e}")
        if resolved is None:
            problems.append(
                "Free iTunes Search API did not resolve a known track "
                "(network/storefront issue) — resolution is the basis of every add."
            )

    if problems:
        print("LIVE ENVIRONMENT NOT READY:")
        for p in problems:
            print(f"  - {p}")
        print(
            "\nFix the above, then re-run. (The live gate also needs an ACTIVE "
            "Apple Music subscription — the add tests prove that by actually adding "
            "a catalog track.)"
        )
        return 1

    print("Live environment ready: macOS + Music.app running + unlocked + catalog reachable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
