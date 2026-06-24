#!/usr/bin/env python3
"""Preflight check for the live API integration gate.

Exits 0 when the machine can actually run the live API tests (a developer token
— generated or harvestable — a captured media-user-token, and a reachable
catalog), and non-zero with guidance otherwise. The point is to FAIL LOUDLY
rather than let the live suite silently skip and produce a false-green release
gate.

Unlike the old UI gate this is cross-platform: it needs tokens + network, not
Music.app or an unlocked Mac.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from applemusic_mcp import amp_api  # noqa: E402
from applemusic_mcp import auth  # noqa: E402


def main() -> int:
    problems = []

    if not auth.has_any_developer_token():
        problems.append(
            "No developer token. Run `applemusic-mcp generate-token` (App Store devs) "
            "or rely on the harvested token — neither was obtainable."
        )

    try:
        auth.get_user_token()
    except Exception:
        problems.append(
            "No media-user-token. Run `applemusic-mcp signin` (browser sign-in) "
            "or `applemusic-mcp authorize`."
        )

    # Catalog reachable through the amp host? (every add/rate depends on resolution)
    if not problems:
        try:
            songs = amp_api.search_catalog_songs("Bohemian Rhapsody Queen", 1)
        except Exception as e:  # network / token failure
            songs = []
            problems.append(f"amp-api catalog search raised: {e}")
        if not songs:
            problems.append(
                "amp-api catalog search resolved no known track "
                "(expired token / storefront / network) — resolution is the basis of every add."
            )

    if problems:
        print("LIVE API ENVIRONMENT NOT READY:")
        for p in problems:
            print(f"  - {p}")
        print(
            "\nFix the above, then re-run. (The live gate also needs an ACTIVE "
            "Apple Music subscription — the add/playlist tests prove that by actually "
            "mutating the account.)"
        )
        return 1

    print("Live API environment ready: developer token + media-user-token + catalog reachable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
