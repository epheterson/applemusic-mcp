# Releasing applemusic-mcp

Releases are automatic: pushing a **version bump** to `main` makes
`.github/workflows/release.yml` tag it and create a GitHub Release, which
dispatches `publish.yml` to push the package to PyPI. There is no manual release
step — which means **the version bump itself is the point of no return.**

## Why there is a LOCAL gate

GitHub CI runs on Ubuntu with no Music.app and no Apple Music sign-in, so it can
only test the mocked logic. The tokenless catalog→library/playlist add — the
whole reason this tool's macOS path exists — is UI automation against a real,
signed-in, unlocked Music.app. **CI structurally cannot validate it.** That gap
is exactly how issue #28 shipped, and how a one-word AppleScript reserved-word
bug (`by`) silently broke the entire macOS-15 add path with every unit test
still green.

So the gate is local and manual, and it is **mandatory before a version bump.**

## The ritual

On a Mac signed into Apple Music (active subscription), unlocked, with
Accessibility granted to your terminal:

```bash
make preflight
```

This runs, in order:

1. the fast/mocked suite (same as CI),
2. a live-environment check (Music running, screen unlocked, catalog reachable)
   that **fails loudly** instead of letting the live tests silently skip,
3. the live UI integration suite (`tests/test_live_integration.py` +
   `tests/test_applescript.py::TestUI*Live`) against your real library —
   self-cleaning, but it does add and remove a throwaway catalog track and a
   `_UI_TEST_…` playlist.

It refuses to pass if the live suite ran **zero** tests (a half-ready
environment that skips everything is a false green).

**Run it on both a macOS 15 and a macOS 26 machine when you can.** Apple split
the add surfaces across versions — the deep-link path runs on macOS 15, the
pop-over path on macOS 26 — and a given machine only exercises one of them.

## Only when `make preflight` is green:

1. Bump the version in **all three** places (they must match or the lock/release
   drifts): `pyproject.toml`, `src/applemusic_mcp/__init__.py`, and the
   `applemusic-mcp` entry in `uv.lock`.
2. Add a `CHANGELOG.md` entry under the new version.
3. Update `SKILL.md` if any user-facing behavior changed (errors, setup,
   add/lookup flow).
4. Merge to `main`. The release + PyPI publish fire automatically.

## If you can't run the live gate

Don't bump the version. A docs-only or tooling-only change that touches no
runtime behavior can skip the live gate (note that in the PR), but anything that
touches `applescript.py` or the add/resolve/playback flow in `server.py` must
pass `make preflight` first.
