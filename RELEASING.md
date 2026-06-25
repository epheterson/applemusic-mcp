# Releasing applemusic-mcp

Releases are automatic: pushing a **version bump** to `main` makes
`.github/workflows/release.yml` tag it and create a GitHub Release, which
dispatches `publish.yml` to push the package to PyPI. There is no manual release
step — which means **the version bump itself is the point of no return.**

## Why there is a LOCAL gate

GitHub CI runs on Ubuntu with no Apple Music account and no tokens, so it can
only test the mocked logic. The catalog→library/playlist add, folders, move, and
ratings — the core of this tool — run as live mutations against a real,
signed-in account. **CI structurally cannot validate it.** That gap is exactly
how issue #28/#37 shipped, and how the branch-A "DELETE is broken on the public
host" bug reached a release with every unit test still green.

So the gate is local and manual, and it is **mandatory before a version bump.**

## The ritual

On a machine with tokens for a signed-in Apple Music account (active
subscription) — a developer token (`applemusic-mcp generate-token`, or a
harvested one) plus a media-user-token (`applemusic-mcp signin`):

```bash
make preflight
```

This runs, in order:

1. the fast/mocked suite (same as CI),
2. a live-environment check (developer token + media-user-token + catalog
   reachable) that **fails loudly** instead of letting the live tests silently
   skip,
3. the live API integration suite (`tests/test_live_integration.py`, `TEST_API=1`)
   against your real account — it creates/deletes `_UI_TEST_…` playlists and
   folders, clears any rating it sets, and removes the probe song it adds. Fully
   self-cleaning — no residue.

It refuses to pass if the core live tests **skipped** rather than passed (a
half-ready environment that skips everything is a false green).

This gate is cross-platform — it no longer needs macOS or the local Music.app.

## Native UI gate — `make preflight-ui` (UI-touching releases)

`make preflight` validates the API engine. It does **not** exercise the native
Music.app UI-automation paths (catalog deep-link playback via CoreGraphics, UI
catalog search, transport controls) — those are version-fragile and have no CI
coverage. Any release that changes `applescript.py` UI logic or the native
playback flow must **also** pass `make preflight-ui` on **both** support
machines:

- the **iMac** (macOS 15 / Music 1.5)
- the **mini** (macOS 26 / Music 26)

Run it from an **unlocked, active console session** (Screen Sharing's GUI login,
**not** SSH — synthetic mouse clicks need a real WindowServer session), signed
into Apple Music, with Accessibility granted. It plays **muted** and pauses after
each test. Like the API gate, it refuses to read as green if the core playback
tests **skipped** (a locked screen skips everything — that is not validation).

## Only when `make preflight` is green (plus `make preflight-ui` on both Macs for UI changes):

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
