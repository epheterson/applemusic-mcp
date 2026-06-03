"""Live pre-release integration gate for the tokenless catalog→library/playlist
add flows.

WHY THIS FILE EXISTS
--------------------
GitHub CI cannot validate these flows: they require a Mac signed into Apple
Music (active subscription), an unlocked GUI session, and Accessibility
permission. That gap is exactly how #28 shipped — and how a one-word AppleScript
reserved-word bug (`by`) silently broke the entire macOS-15 deep-link add path
without any test noticing. These tests are the local gate that runs on a real,
signed-in Mac BEFORE a release (see ``scripts/preflight.sh`` / ``RELEASING.md``).

They drive real Music.app UI automation and MUTATE the real library, so:
  * they are gated behind ``TEST_UI=1`` and skipped otherwise (CI, headless),
  * they SKIP cleanly (never fail spuriously) when the environment isn't ready
    (screen locked, Music not running, no signed-in catalog access),
  * every test removes whatever it added in teardown, and the playlist name uses
    the ``_UI_TEST_`` prefix that ``conftest.py``'s session sweep also cleans.

Run locally::

    TEST_UI=1 uv run pytest tests/test_live_integration.py -v

OS-version awareness: Apple's two add surfaces are split across versions —
the autocomplete pop-over is in the accessibility tree on macOS 26 (new Music)
but not on macOS 15; catalog deep-links navigate on macOS 15 but not on macOS 26.
The full-flow test auto-selects the right surface and runs on BOTH; the
surface-specific tests skip on the version where their surface doesn't apply.
"""

import os
import platform
import time

import pytest

from applemusic_mcp import applescript as asc
from applemusic_mcp import server

# --- environment preflight -------------------------------------------------


def _macos_major() -> int:
    try:
        return int(platform.mac_ver()[0].split(".")[0])
    except (ValueError, IndexError):
        return 0


def _music_running() -> bool:
    ok, out = asc.run_applescript('return (application "Music" is running)')
    return ok and out.strip() == "true"


def _env_skip_reason() -> str:
    """Return a non-empty reason to skip, or '' when the live environment is
    ready. Skips are CLEAN (env not ready) — never spurious failures."""
    if not os.environ.get("TEST_UI"):
        return "live UI gate is opt-in; run with TEST_UI=1"
    if not asc.is_available():
        return "AppleScript/Music.app not available on this platform"
    if not _music_running():
        return "Music.app is not running — open it and sign in"
    if asc.is_screen_locked() is True:
        return "screen is locked — unlock the Mac (UI automation needs the active display)"
    return ""


pytestmark = [
    pytest.mark.ui,
    pytest.mark.skipif(bool(_env_skip_reason()), reason=_env_skip_reason()),
]


# Deliberately obscure-but-indexed ALBUM tracks (deep cuts, not lead singles):
# unlikely to be in the tester's library (so the add is observable), stable on
# Apple Music, and — importantly — tracks on multi-song albums so the song page
# exposes a per-row "Add to Library" button. The fixture additionally PROBES
# each candidate's page (see `_candidate_is_addable`) and skips any that don't
# expose a row-level add control (e.g. standalone singles, whose add is
# album-level) or whose row shows a stale "Download" from a prior run.
# Genre/era-diverse so they're unlikely to ALL be in any one tester's library
# (the fixture skips owned tracks; too narrow a list exhausts on a big library).
_CANDIDATES = [
    ("Casimir Pulaski Day", "Sufjan Stevens"),  # Illinois (22 tracks)
    ("The Boy in the Bubble", "Paul Simon"),  # Graceland
    ("Sleeping Lessons", "The Shins"),  # Wincing the Night Away
    ("Mykonos", "Fleet Foxes"),  # Sun Giant EP
    ("Bigmouth Strikes Again", "The Smiths"),  # The Queen Is Dead
    ("Emmylou", "First Aid Kit"),  # The Lion's Roar
    ("Cath...", "Death Cab for Cutie"),  # Narrow Stairs
    ("Naeem", "Bon Iver"),  # i,i
    ("Digital Witness", "St. Vincent"),  # St. Vincent
    ("Gosh", "Jamie xx"),  # In Colour
    ("Bubblegum", "Clairo"),  # diary 001
    ("Seventeen Going Under", "Sam Fender"),  # newer
    ("Brother", "Lord Huron"),  # Lonesome Dreams
    ("Nara", "alt-J"),  # This Is All Yours
    ("Avant Gardener", "Courtney Barnett"),  # The Double EP
    ("Three Rings", "TV on the Radio"),  # Seeds
]


def _candidate_is_addable(resolved: dict) -> bool:
    """Navigate to the resolved track's page and confirm a per-row
    'Add to Library' button is present after hover.

    Filters out (a) standalone singles whose add control is album-level (no
    row button) and (b) candidates whose row shows a stale 'Download' from a
    prior add/remove cycle (iCloud cache lag). Mirrors the freshness probe the
    older TestUIFlowsLive uses — without it, one bad candidate fails the gate."""
    ok, _ = asc.open_catalog_song(resolved["url"])
    if not ok:
        return False
    asc._ensure_music_frontmost()
    pos = None
    for _ in range(8):
        pos = asc._find_highlighted_track_position()
        if pos:
            break
        time.sleep(1.0)
    if pos is None:
        return False
    asc._hover_with_nudge(pos[0], pos[1])
    time.sleep(0.8)
    # Require a fresh, row-level Add button (not album-level, not stale Download).
    return asc._find_add_button_in_highlighted_row("Add to Library") is not None


def _delete_from_library(name: str, artist: str) -> None:
    """Best-effort removal of a track we added, by name + artist."""
    safe_name = name.replace('"', "")
    safe_artist = (artist.split() or [""])[0].replace('"', "")
    asc.run_applescript(f"""
tell application "Music"
    repeat with t in (every track of library playlist 1 whose name is "{safe_name}" and artist contains "{safe_artist}")
        delete t
    end repeat
end tell""")


def _in_library_index(name: str, artist: str, timeout: float = 8.0) -> bool:
    """Poll the local library index (`library playlist 1`) for the track."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found, _ = asc.find_library_track(name, artist)
        if found:
            return True
        time.sleep(1.0)
    return False


def _in_library_via_current_page(name: str) -> bool:
    """Confirm on the CURRENTLY-DISPLAYED song page that the track is in the
    library: its row's add control has flipped from 'Add to Library' to
    'Download'. This is Music's IMMEDIATE signal (no iCloud round-trip) and —
    crucially — needs no navigation, so it works on macOS 26 too (where
    deep-links no longer navigate). Both add paths leave Music on the song page,
    so we read it where the add just happened."""
    group_path = asc._wait_for_song_page(name, timeout=3.0)
    if group_path is None:
        return False
    found, _ = asc._hover_and_find_button(group_path, "Download")
    return found


def _confirm_added(resolved: dict, artist: str) -> bool:
    """Robustly confirm a track landed in the library. Tries the fast local
    index first, then falls back to the immediate current-page 'Download' signal
    — so a genuine add is never a false failure (iCloud index lag), but a no-op
    add (nothing landed by either signal) still fails the gate."""
    if _in_library_index(resolved["name"], artist):
        return True
    return _in_library_via_current_page(resolved["name"])


@pytest.fixture
def fresh_track():
    """Yield (name, artist, resolved) for the first candidate that is NOT in the
    library AND resolves via the free iTunes API. Teardown removes the track
    from the library iff it wasn't there before the test (i.e. we added it)."""
    for name, artist in _CANDIDATES:
        already, _ = asc.find_library_track(name, artist)
        if already:
            continue
        resolved = server._resolve_catalog_track_itunes(name, artist)
        if not resolved or not resolved.get("url"):
            continue
        if not _candidate_is_addable(resolved):
            # Single (album-level add) or stale-cached row — try the next one.
            asc.ui_clear_search()
            continue
        try:
            yield name, artist, resolved
        finally:
            # Remove only what we added (the candidate was not in library above).
            _delete_from_library(resolved.get("name", name), artist)
            asc.ui_clear_search()
        return
    pytest.skip(
        "no candidate that is both absent from the library and resolvable via "
        "the free iTunes API (library may already contain all candidates, or "
        "the storefront lacks them)"
    )


@pytest.fixture
def temp_playlist():
    """Create a uniquely-named throwaway playlist (swept by conftest's
    `_UI_TEST_` prefix sweep too) and delete it in teardown."""
    name = "_UI_TEST_LIVE_INTEGRATION_"
    asc.delete_playlist(name)  # clear any prior debris
    ok, err = asc.create_playlist(name, "live integration test playlist")
    if not ok:
        pytest.skip(f"could not create test playlist: {err}")
    try:
        yield name
    finally:
        asc.delete_playlist(name)


# --- the gate tests --------------------------------------------------------


class TestTokenlessAddLive:
    """End-to-end tokenless add, exercising the production server flow that
    auto-selects the right UI surface for the running macOS version."""

    def test_resolver_returns_canonical_for_named_artist(self):
        """Free iTunes API resolves a known track with the right artist (the
        resolver is the foundation of every tokenless add)."""
        resolved = server._resolve_catalog_track_itunes("Re: Stacks", "Bon Iver")
        if not resolved:
            pytest.skip("iTunes API did not resolve the probe track (storefront/network)")
        assert "bon iver" in resolved["artist"].lower()
        assert resolved["url"].startswith("https://music.apple.com")

    def test_library_add_full_flow(self, fresh_track):
        """server._library_add_track_via_ui → track lands in the library.

        This is the PRIMARY gate: it resolves the canonical title, picks the
        pop-over (macOS 26) or deep-link (macOS 15) surface, clicks Add, and the
        track must actually appear in the library. Would have caught #28 (the
        deep-link `by` syntax error fails this on macOS 15)."""
        name, artist, resolved = fresh_track
        ok, msg = server._library_add_track_via_ui(name, artist)
        assert ok, f"tokenless library-add reported failure: {msg}"
        assert _confirm_added(resolved, artist), (
            f"add reported success ({msg!r}) but {resolved['name']!r} never "
            f"appeared in the library within the sync window"
        )

    def test_add_to_playlist_full_flow(self, fresh_track, temp_playlist):
        """The reporter's actual flow (#28): add a catalog track to a playlist.

        Composes library-add (above) + duplicate-into-playlist + verify."""
        name, artist, resolved = fresh_track
        ok, msg = asc.ui_add_to_playlist(
            temp_playlist, resolved["name"], artist, song_url=resolved["url"]
        )
        assert ok, f"add-to-playlist reported failure: {msg}"
        # The track must be in the target playlist.
        base = resolved["name"].split(" (")[0].replace('"', "")
        deadline = time.monotonic() + 12.0
        in_playlist = False
        while time.monotonic() < deadline:
            okc, out = asc.run_applescript(
                f'tell application "Music" to return (count of (every track of '
                f'user playlist "{temp_playlist}" whose name contains "{base}"))'
            )
            if okc and out.strip().isdigit() and int(out.strip()) > 0:
                in_playlist = True
                break
            time.sleep(1.0)
        assert in_playlist, f"track {resolved['name']!r} not found in {temp_playlist!r}"


class TestSurfaceSpecificLive:
    """Directly exercise each OS-specific add surface so a break in either is
    caught even when the full-flow test happens to route around it."""

    @pytest.mark.skipif(
        _macos_major() >= 26,
        reason="deep-link navigation doesn't work on macOS 26 (new Music) — "
        "that surface is macOS 15 only",
    )
    def test_deep_link_add_path(self, fresh_track):
        """asc.ui_add_to_library_via_url — the macOS-15 deep-link path. This is
        the function the `by` reserved-word bug lived in; this test is its guard."""
        name, artist, resolved = fresh_track
        ok, msg = asc.ui_add_to_library_via_url(resolved["name"], resolved["url"], artist)
        assert ok, f"deep-link add reported failure: {msg}"
        assert _confirm_added(
            resolved, artist
        ), f"deep-link add reported success ({msg!r}) but the track never landed"

    @pytest.mark.skipif(
        _macos_major() < 26,
        reason="search autocomplete pop-over is not in the accessibility tree on "
        "macOS 15 (old Music) — that surface is macOS 26 only",
    )
    def test_popover_add_path(self, fresh_track):
        """asc.ui_add_to_library — the macOS-26 pop-over path."""
        name, artist, resolved = fresh_track
        ok, msg = asc.ui_add_to_library(resolved["name"], artist)
        assert ok, f"pop-over add reported failure: {msg}"
        assert _confirm_added(
            resolved, artist
        ), f"pop-over add reported success ({msg!r}) but the track never landed"
