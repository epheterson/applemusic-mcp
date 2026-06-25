"""Live pre-release gate for the native **Music.app UI-automation** paths.

WHY THIS FILE EXISTS
--------------------
The native catalog-playback path drives Music.app through System Events +
CoreGraphics. It is inherently version-fragile — element hierarchies and the
behaviour of synthetic clicks differ across macOS / Music.app releases (this is
exactly how the AXPress-no-op playback bug shipped). GitHub CI can't exercise
any of it: there's no Music.app, no display, no Accessibility grant. So these
tests are the local gate that must pass **on every Mac/Music version we support
before a release that touches UI logic** — run them on the iMac (macOS 15 /
Music 1.5) AND the mini (macOS 26 / Music 26).

They drive the REAL Music.app and start playback, so:
  * gated behind ``TEST_UI=1`` and skipped otherwise (CI, headless, non-mac),
  * SKIP cleanly (never fail spuriously) when the environment isn't ready
    (not macOS, no Accessibility permission, Music.app unreachable, or the
    catalog probe can't resolve a song),
  * run **muted** (Music volume forced to 0) and **pause** playback in teardown,
    so running the gate never blasts audio on the test machine.

Run locally on a signed-in, unlocked Mac with Accessibility granted::

    TEST_UI=1 uv run pytest tests/test_live_ui.py -v

The suite is marked ``ui_live`` so the default ``-m 'not ... and not ui_live'``
run skips it.
"""

import os
import sys
import time

import pytest

from applemusic_mcp import applescript as asc
from applemusic_mcp import server

pytestmark = [pytest.mark.ui_live]


def _ui_skip_reason() -> str:
    """Empty when the live UI environment is ready; otherwise a clean skip reason.
    Skips (never spurious failures) cover: not opted in, not macOS, no AppleScript,
    and — the common one — a **locked screen / no active console session**, where
    AppleScript still works but synthetic clicks and the AX window tree don't
    (so the CG-driven tests would fail for an environment reason, not a bug).
    Run this gate from an unlocked console session (Screen Sharing GUI login,
    not SSH)."""
    if not os.environ.get("TEST_UI"):
        return "live UI gate is opt-in; run with TEST_UI=1"
    if sys.platform != "darwin":
        return "Music.app UI automation is macOS-only"
    if not getattr(server, "APPLESCRIPT_AVAILABLE", False):
        return "AppleScript not available on this host"
    # Make sure Music is running/reachable, then use the canonical check
    # (Quartz screen-lock signal + AX window probe).
    asc.run_applescript('tell application "Music" to launch')
    ok, why = asc.check_ui_accessible()
    if not ok:
        return f"Music.app UI not drivable — {why}"
    return ""


def _player_state() -> str:
    ok, s = asc.run_applescript('tell application "Music" to return player state as text')
    return s.strip() if ok else ""


def _current_track() -> str:
    ok, s = asc.run_applescript(
        'tell application "Music"\n'
        "  try\n"
        "    return name of current track\n"
        "  on error\n"
        '    return ""\n'
        "  end try\n"
        "end tell"
    )
    return s.strip() if ok else ""


def _wait_playing(timeout: float = 12.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _player_state() == "playing":
            return True
        time.sleep(0.5)
    return False


@pytest.fixture(autouse=True)
def _muted_and_ready():
    """Skip if the env isn't ready; otherwise force volume to 0 for the test and
    pause playback afterward so the gate is silent and leaves nothing playing."""
    reason = _ui_skip_reason()
    if reason:
        pytest.skip(reason)
    ok, prev = asc.run_applescript('tell application "Music" to get sound volume')
    asc.run_applescript('tell application "Music" to set sound volume to 0')
    try:
        yield
    finally:
        asc.run_applescript('tell application "Music" to pause')
        if ok and prev.strip().isdigit():
            asc.run_applescript(f'tell application "Music" to set sound volume to {prev.strip()}')


# --- probe ------------------------------------------------------------------


def _probe() -> dict:
    """Resolve a stable catalog song (name + album-with-?i= URL) for this
    storefront. Skips (not fails) if the catalog can't resolve one."""
    for name, artist in (
        ("Bohemian Rhapsody", "Queen"),
        ("Billie Jean", "Michael Jackson"),
        ("Yesterday", "The Beatles"),
    ):
        hit = server._resolve_catalog_track_itunes(name, artist)
        if hit and hit.get("url") and "?i=" in hit["url"]:
            return hit
    pytest.skip("catalog probe couldn't resolve a song (storefront/network)")


def _album_url(track_url: str) -> str:
    """Strip the ?i= so the URL targets the whole album (album Play button)."""
    return track_url.split("?")[0]


# --- tests ------------------------------------------------------------------


def test_ui_catalog_search_returns_results():
    """Music.app UI catalog search (the tokenless macOS search path)."""
    ok, results, why = asc.ui_search_catalog("Bohemian Rhapsody Queen")
    asc.ui_clear_search()
    assert ok, f"UI search failed: {why}"
    assert results, "UI search returned no results"


def test_native_play_album_clicks_play_button():
    """Deep-link an album and start it via the CoreGraphics Play-button click."""
    probe = _probe()
    ok, msg = asc.open_catalog_and_play(_album_url(probe["url"]), timeout=20)
    assert ok, f"album play reported failure: {msg}"
    assert _wait_playing(), f"player did not reach 'playing' (msg: {msg})"


def test_native_play_specific_track_double_clicks_row():
    """Deep-link a specific ?i= track and play exactly it via the name-matched
    CoreGraphics double-click — not the whole album from track 1."""
    probe = _probe()
    ok, msg = asc.open_catalog_and_play(probe["url"], track_name=probe["name"], timeout=20)
    assert ok, f"track play reported failure: {msg}"
    assert _wait_playing(), f"player did not reach 'playing' (msg: {msg})"
    # The exact requested track should be current (case/concat tolerant).
    cur = _current_track().lower()
    assert (
        probe["name"].lower() in cur or cur in probe["name"].lower()
    ), f"expected '{probe['name']}' playing, got '{cur}'"


def test_native_play_library_track():
    """Play a track already in the library by name (the AppleScript play path)."""
    ok, songs = asc.get_library_songs(5)
    if not ok or not songs:
        pytest.skip("library has no songs to play")
    track = songs[0]
    pok, msg = asc.play_track(track.get("name", ""), track.get("artist") or None)
    assert pok, f"play_track failed: {msg}"
    assert _wait_playing(), "player did not reach 'playing'"


def test_playback_controls_pause_resume():
    """Transport controls act on the live player."""
    _probe()  # ensure env; reuse whatever is queued
    asc.run_applescript('tell application "Music" to play')
    assert _wait_playing(), "couldn't start playback to test controls"
    asc.pause()
    time.sleep(0.5)
    assert _player_state() == "paused", "pause did not take"
    asc.play()
    assert _wait_playing(), "resume did not take"


def test_reveal_opens_catalog_page():
    """Reveal-in-app opens the catalog page (no playback assertion — it just
    navigates Music to the item)."""
    probe = _probe()
    ok, msg = asc.open_catalog_song(probe["url"])
    assert ok, f"reveal failed: {msg}"
