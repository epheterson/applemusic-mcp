#!/usr/bin/env python3
"""Assert every version surface in the repo agrees with pyproject.toml.

A release touches SEVEN places, and two of them silently resist the obvious
bump method:

  * ``server.json`` is the MCP registry manifest. No workflow reads or writes it,
    so nothing notices when it drifts — it sat at ``1.0.0`` while the package
    shipped 0.16.0, which would have advertised a 1.0 that does not exist.
  * ``uv.lock`` does NOT follow an edit to pyproject. It only changes when ``uv``
    itself runs, which can happen after you commit — 0.18.0 shipped with the
    lockfile still naming 0.17.0.

Both are cosmetic right up until they aren't, and neither is visible in a diff you
weren't already looking at. So: check them mechanically, in the pre-release gate
(fast feedback) and again in release.yml (blocks a bad tag).

Exit 0 if consistent, 1 otherwise, listing every mismatch rather than the first.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _pyproject_version() -> str:
    m = re.search(r'^version = "([^"]+)"', _read("pyproject.toml"), re.M)
    if not m:
        raise SystemExit('could not find `version = "..."` in pyproject.toml')
    return m.group(1)


def _surfaces(expected: str) -> list[tuple[str, str | None]]:
    """(label, found_version) for each surface. None means 'could not determine'."""
    out: list[tuple[str, str | None]] = []

    m = re.search(r'^__version__ = "([^"]+)"', _read("src/applemusic_mcp/__init__.py"), re.M)
    out.append(("src/applemusic_mcp/__init__.py __version__", m.group(1) if m else None))

    # The lockfile records the editable package's own version in its stanza.
    m = re.search(
        r'\[\[package\]\]\nname = "applemusic-mcp"\nversion = "([^"]+)"', _read("uv.lock")
    )
    out.append(("uv.lock applemusic-mcp stanza", m.group(1) if m else None))

    m = re.search(r"^version: (.+)$", _read("SKILL.md"), re.M)
    out.append(("SKILL.md frontmatter", m.group(1).strip() if m else None))

    server = json.loads(_read("server.json"))
    out.append(("server.json version", server.get("version")))
    packages = server.get("packages") or [{}]
    out.append(("server.json packages[0].version", packages[0].get("version")))

    # The changelog needs a section for this version or release.yml silently falls
    # back to auto-generated notes.
    has_section = re.search(rf"^## \[{re.escape(expected)}\]", _read("CHANGELOG.md"), re.M)
    out.append(("CHANGELOG.md section heading", expected if has_section else None))

    return out


def main() -> int:
    expected = _pyproject_version()
    problems = []
    for label, found in _surfaces(expected):
        if found is None:
            problems.append(f"  {label}: NOT FOUND (expected {expected})")
        elif found != expected:
            problems.append(f"  {label}: {found} (expected {expected})")

    if problems:
        print(f"Version surfaces disagree with pyproject.toml ({expected}):", file=sys.stderr)
        print("\n".join(problems), file=sys.stderr)
        print(
            "\nFix them, and remember `uv lock` is what updates uv.lock — editing "
            "pyproject.toml alone leaves it behind.",
            file=sys.stderr,
        )
        return 1

    print(f"Version surfaces all agree: {expected}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
