"""Fast, fully-mocked unit tests for applemusic_mcp.applescript.

These tests NEVER shell out to osascript or launch Music.app. Every I/O
boundary (run_applescript, subprocess.run, the JXA/CoreGraphics helpers, the
Quartz lock probe) is monkeypatched, so coverage is hardware-independent and
runs in well under a second.

They are deliberately NOT marked `slow` so they run in the default fast subset.
The companion file tests/test_applescript.py keeps the live/slow integration
tests; this file only exercises parsing, formatting, and control-flow.
"""

import sys
import types

import pytest

from applemusic_mcp import applescript as asc

# Keep a handle to the real implementation so its own logic can still be tested
# in isolation (with subprocess mocked). The conftest session sweep guards itself
# with `is_available()`, so we do NOT stub run_applescript globally — that would
# leak into the live tests in tests/test_applescript.py. Instead every test here
# defaults to a no-op via the function-scoped autouse fixture below.
_real_run_applescript = asc.run_applescript


@pytest.fixture(autouse=True)
def _default_stub_run_applescript(monkeypatch):
    """Default every test in THIS module to a no-op run_applescript, overridden
    per-test by a Router/Seq. monkeypatch auto-reverts, so it never leaks into
    other modules' live tests."""
    monkeypatch.setattr(asc, "run_applescript", lambda script: (False, ""))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class Router:
    """Route run_applescript calls to canned (ok, text) by substring match."""

    def __init__(self, rules, default=(True, "")):
        self.rules = rules
        self.default = default
        self.calls = []

    def __call__(self, *args):
        script = args[0] if args else ""
        self.calls.append(script)
        for sub, resp in self.rules:
            if sub in script:
                return resp
        return self.default


class Seq:
    """Return queued responses in order; default once exhausted.

    Accepts zero-arg calls so it doubles as a fake time.monotonic/time.time.
    """

    def __init__(self, responses, default=(True, "")):
        self.responses = list(responses)
        self.default = default
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args[0] if args else None)
        if self.responses:
            return self.responses.pop(0)
        return self.default


class Recorder:
    """run_applescript stub that records every script and returns a constant."""

    def __init__(self, ok, text=""):
        self.resp = (ok, text)
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args[0] if args else "")
        return self.resp


def const(ok, text=""):
    return lambda *args: (ok, text)


def proc(returncode=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture(autouse=True)
def _fast_env(monkeypatch):
    """Kill real sleeps and reset the module-level search-field cache."""
    monkeypatch.setattr(asc.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(asc, "_search_field_cache", None, raising=False)
    yield


def setrun(monkeypatch, fake):
    monkeypatch.setattr(asc, "run_applescript", fake)
    return fake


# ===========================================================================
# Pure string / parsing helpers
# ===========================================================================
def test_is_available(monkeypatch):
    monkeypatch.setattr(asc.sys, "platform", "darwin")
    monkeypatch.setattr(asc.shutil, "which", lambda _: "/usr/bin/osascript")
    assert asc.is_available() is True
    monkeypatch.setattr(asc.shutil, "which", lambda _: None)
    assert asc.is_available() is False
    monkeypatch.setattr(asc.sys, "platform", "linux")
    assert asc.is_available() is False


def test_escape_for_applescript():
    # control chars stripped, backslash then quote escaped
    out = asc._escape_for_applescript('a\r\nb\tc"d\\e')
    assert "\n" not in out and "\t" not in out and "\r" not in out
    assert '\\"' in out and "\\\\" in out


def test_name_contains_clause_variants():
    # Title that is only punctuation -> fallback single clause from original
    only_punct = asc._name_contains_clause("...")
    assert only_punct.startswith("name contains")
    # Single surviving fragment -> no parens
    single = asc._name_contains_clause("Wait...")
    assert single == 'name contains "Wait"'
    # Multiple fragments -> parenthesized AND
    multi = asc._name_contains_clause("That's a No No")
    assert multi.startswith("(") and " and " in multi


@pytest.mark.parametrize(
    "line,expected_name",
    [
        ("no delimiter here", None),
        ("a|||b|||c", None),  # < 7 parts
    ],
)
def test_parse_library_track_line_rejects(line, expected_name):
    assert asc._parse_library_track_line(line) is None


def test_parse_library_track_line_full():
    d = asc._parse_library_track_line("N|||Ar|||Al|||125|||Rock|||2020|||PID|||true")
    assert d["name"] == "N" and d["duration"] == "2:05" and d["explicit"] == "Yes"
    # bad duration -> empty; only 7 fields -> Unknown explicit
    d2 = asc._parse_library_track_line("N|||Ar|||Al|||xx|||Rock|||2020|||PID")
    assert d2["duration"] == "" and d2["explicit"] == "Unknown"
    # explicit false -> No
    d3 = asc._parse_library_track_line("N|||Ar|||Al|||10|||G|||Y|||P|||false")
    assert d3["explicit"] == "No"


def test_track_filter_clause():
    assert asc._track_filter_clause(track_id="X").startswith("whose persistent ID")
    assert "artist contains" in asc._track_filter_clause("Song", artist="A")
    assert asc._track_filter_clause("Song").startswith("whose ")
    assert asc._track_filter_clause() is None


def test_library_track_query_and_resolve():
    assert "artist contains" in asc._library_track_query("S", "A")
    assert asc._library_track_query("S").startswith("first track")
    with_artist = asc._resolve_library_track_applescript("S", "A")
    assert "allArtists" in with_artist
    no_artist = asc._resolve_library_track_applescript("S")
    assert "allArtists" not in no_artist and "targetTrack" in no_artist


# ===========================================================================
# run_applescript (the real implementation) + classify_error
# ===========================================================================
def test_run_applescript_paths(monkeypatch):
    monkeypatch.setattr(asc.subprocess, "run", lambda *a, **k: proc(0, " hi \n"))
    assert _real_run_applescript("x") == (True, "hi")

    monkeypatch.setattr(asc.subprocess, "run", lambda *a, **k: proc(1, "", " boom "))
    assert _real_run_applescript("x") == (False, "boom")

    def _timeout(*a, **k):
        raise asc.subprocess.TimeoutExpired(cmd="osascript", timeout=30)

    monkeypatch.setattr(asc.subprocess, "run", _timeout)
    ok, msg = _real_run_applescript("x")
    assert ok is False and "timed out" in msg

    def _boom(*a, **k):
        raise RuntimeError("nope")

    monkeypatch.setattr(asc.subprocess, "run", _boom)
    ok, msg = _real_run_applescript("x")
    assert ok is False and "nope" in msg


@pytest.mark.parametrize(
    "text,expected",
    [
        ("", asc.ERROR_UNKNOWN),
        ("AppleScript timed out after 30 seconds", asc.ERROR_TIMEOUT),
        ("error -1743 Not authorized", asc.ERROR_AUTOMATION_DENIED),
        ("not authorised", asc.ERROR_AUTOMATION_DENIED),
        ("Music isn't running (-10810)", asc.ERROR_MUSIC_NOT_RUNNING),
        ("syntax error: expected end but found x", asc.ERROR_SYNTAX),
        ("can't get track 99", asc.ERROR_UNKNOWN),
    ],
)
def test_classify_error(text, expected):
    assert asc.classify_error(text) == expected


# ===========================================================================
# Playback one-liners
# ===========================================================================
def test_playback_oneliners(monkeypatch):
    r = setrun(monkeypatch, Recorder(True, "ok"))
    for fn in (
        asc.play,
        asc.pause,
        asc.playpause,
        asc.stop,
        asc.next_track,
        asc.previous_track,
        asc.get_player_state,
        asc.get_repeat,
    ):
        assert fn() == (True, "ok")
    assert asc.seek(12.5) == (True, "ok")
    assert asc.set_volume(250) == (True, "ok")  # clamped, no raise
    assert asc.set_shuffle(True) == (True, "ok")
    assert "tell application" in r.calls[-1]


def test_get_current_track(monkeypatch):
    setrun(monkeypatch, const(False, "err"))
    assert asc.get_current_track() == (False, "err")

    setrun(monkeypatch, const(True, "STOPPED"))
    assert asc.get_current_track() == (True, {"state": "stopped"})

    setrun(monkeypatch, const(True, "name:Song\nartist:A\nbad-line\nyear:2020"))
    ok, info = asc.get_current_track()
    assert ok and info["state"] == "playing" and info["name"] == "Song" and info["year"] == "2020"


def test_get_volume(monkeypatch):
    setrun(monkeypatch, const(True, "42"))
    assert asc.get_volume() == (True, 42)
    setrun(monkeypatch, const(True, "loud"))
    ok, msg = asc.get_volume()
    assert ok is False and "Invalid volume" in msg
    setrun(monkeypatch, const(False, "err"))
    assert asc.get_volume() == (False, "err")


def test_get_shuffle(monkeypatch):
    setrun(monkeypatch, const(True, "true"))
    assert asc.get_shuffle() == (True, True)
    setrun(monkeypatch, const(False, "err"))
    assert asc.get_shuffle() == (False, "err")


def test_set_repeat(monkeypatch):
    r = setrun(monkeypatch, Recorder(True, "ok"))
    assert asc.set_repeat("none") == (True, "ok")  # synonym -> off
    assert "to off" in r.calls[-1]
    assert asc.set_repeat("one") == (True, "ok")
    ok, msg = asc.set_repeat("bogus")
    assert ok is False and "Invalid repeat" in msg


# ===========================================================================
# Playlist operations
# ===========================================================================
def test_get_playlists(monkeypatch):
    setrun(monkeypatch, const(False, "err"))
    assert asc.get_playlists() == (False, "err")
    setrun(monkeypatch, const(True, "P|||ID|||true|||5|||3:00\nshort|||line"))
    ok, pls = asc.get_playlists()
    assert ok and len(pls) == 1 and pls[0]["smart"] is True and pls[0]["track_count"] == 5


def test_get_playlist_tracks_bulk_and_slow(monkeypatch):
    # bulk fails with Can't get -> slow fallback succeeds
    seq = Seq([(False, "Can't get foo"), (True, "N|||Ar|||Al|||bad|||||||||PID")])
    setrun(monkeypatch, seq)
    ok, tracks = asc.get_playlist_tracks("PL")
    assert ok and tracks[0]["duration"] == "" and tracks[0]["id"] == "PID"
    assert len(seq.calls) == 2  # both bulk + slow ran

    # ERROR: prefix
    setrun(monkeypatch, const(True, "ERROR:Playlist not found"))
    ok, msg = asc.get_playlist_tracks("PL")
    assert ok is False and msg == "Playlist not found"

    # hard failure not matching Can't get
    setrun(monkeypatch, const(False, "boom"))
    assert asc.get_playlist_tracks("PL") == (False, "boom")

    # success bulk with good duration
    setrun(monkeypatch, const(True, "N|||Ar|||Al|||125|||G|||2020|||PID"))
    ok, tracks = asc.get_playlist_tracks("PL")
    assert ok and tracks[0]["duration"] == "2:05"


def test_create_playlist(monkeypatch):
    r = setrun(monkeypatch, Recorder(True, "PID"))
    assert asc.create_playlist("X") == (True, "PID")
    assert "description" not in r.calls[-1]
    asc.create_playlist("X", "desc")
    assert "description" in r.calls[-1]


def test_resolve_folder_path_applescript():
    assert "Empty folder path" in asc._resolve_folder_path_applescript("  /  ")
    assert "Folder not found" in asc._resolve_folder_path_applescript("Solo")
    multi = asc._resolve_folder_path_applescript("A/B/C")
    assert "Subfolder not found: B" in multi and "Subfolder not found: C" in multi


def test_get_folder_tree(monkeypatch):
    setrun(monkeypatch, const(True, "[Folder]\n  Child"))
    assert asc.get_folder_tree() == (True, "[Folder]\n  Child")


def test_create_folder_path(monkeypatch):
    assert asc.create_folder_path("  ") == (False, "Empty folder path")
    r = setrun(monkeypatch, Recorder(True, "LEAF"))
    assert asc.create_folder_path("A") == (True, "LEAF")
    asc.create_folder_path("A/B")
    assert "folder1" in r.calls[-1] and "move folder1 to folder0" in r.calls[-1]


def test_create_folder(monkeypatch):
    setrun(monkeypatch, const(True, "FID"))
    assert asc.create_folder("F") == (True, "FID")


def test_move_to_folder(monkeypatch):
    setrun(monkeypatch, const(True, "Moved"))
    assert asc.move_to_folder("PL", "Dest") == (True, "Moved")
    setrun(monkeypatch, const(True, "ERROR:Folder not found: Dest"))
    assert asc.move_to_folder("PL", "Dest") == (False, "Folder not found: Dest")


def test_move_to_root(monkeypatch):
    setrun(monkeypatch, const(True, "Moved 'X' to top level (playlist recreated)"))
    ok, msg = asc.move_to_root("X")
    assert ok and "top level" in msg
    setrun(monkeypatch, const(True, "ERROR:Playlist is already at top level"))
    assert asc.move_to_root("X") == (False, "Playlist is already at top level")


def test_get_playlist_path(monkeypatch):
    setrun(monkeypatch, const(True, "A/B/PL"))
    assert asc.get_playlist_path("PL") == (True, "A/B/PL")
    setrun(monkeypatch, const(True, "ERROR:Playlist not found"))
    assert asc.get_playlist_path("PL") == (False, "Playlist not found")


def test_delete_folder_and_playlist_and_rename(monkeypatch):
    setrun(monkeypatch, const(True, "Deleted folder: F"))
    assert asc.delete_folder("F") == (True, "Deleted folder: F")
    setrun(monkeypatch, const(True, "ERROR:Folder not found: F"))
    assert asc.delete_folder("F") == (False, "Folder not found: F")

    setrun(monkeypatch, const(True, "Deleted playlist: P"))
    assert asc.delete_playlist("P") == (True, "Deleted playlist: P")
    setrun(monkeypatch, const(True, "ERROR:Playlist not found"))
    assert asc.delete_playlist("P") == (False, "Playlist not found")

    setrun(monkeypatch, const(True, "Renamed: A -> B"))
    assert asc.rename_playlist("A", "B")[0] is True
    setrun(monkeypatch, const(True, "ERROR:Playlist not found"))
    assert asc.rename_playlist("A", "B") == (False, "Playlist not found")


def test_track_exists_in_playlist(monkeypatch):
    setrun(monkeypatch, const(False, "boom"))
    assert asc.track_exists_in_playlist("PL", "T") == (False, "boom")
    setrun(monkeypatch, const(True, "ERROR:Playlist not found"))
    assert asc.track_exists_in_playlist("PL", "T") == (False, "Playlist not found")
    setrun(monkeypatch, const(True, "FOUND:T - A"))
    assert asc.track_exists_in_playlist("PL", "T", artist="A") == (True, "T - A")
    setrun(monkeypatch, const(True, "NOT_FOUND"))
    assert asc.track_exists_in_playlist("PL", "T") == (True, False)


def test_add_track_to_playlist(monkeypatch):
    # with artist -> fallback query branch + album
    r = setrun(monkeypatch, Recorder(True, "Added"))
    assert asc.add_track_to_playlist("PL", "T", artist="A", album="Al") == (True, "Added")
    assert "artist contains" in r.calls[-1]  # fallback path present
    # without artist -> simple branch
    r = setrun(monkeypatch, Recorder(True, "Added"))
    asc.add_track_to_playlist("PL", "T")
    assert "artist contains" not in r.calls[-1]
    # ERROR
    setrun(monkeypatch, const(True, "ERROR:Track not found: T"))
    assert asc.add_track_to_playlist("PL", "T") == (False, "Track not found: T")


def test_remove_track_from_playlist(monkeypatch):
    assert asc.remove_track_from_playlist("PL") == (False, "Must provide track_name or track_id")
    setrun(monkeypatch, const(True, "Removed T by A from PL"))
    assert asc.remove_track_from_playlist("PL", "T")[0] is True
    setrun(monkeypatch, const(True, "ERROR:Track not found in playlist"))
    assert asc.remove_track_from_playlist("PL", track_id="X") == (
        False,
        "Track not found in playlist",
    )


def test_remove_from_library(monkeypatch):
    assert asc.remove_from_library() == (False, "Must provide track_name or track_id")
    setrun(monkeypatch, const(True, "Removed from library: T by A"))
    assert asc.remove_from_library("T")[0] is True
    setrun(monkeypatch, const(True, "ERROR:Track not found in library"))
    assert asc.remove_from_library(track_id="X") == (False, "Track not found in library")


def test_search_playlist(monkeypatch):
    setrun(monkeypatch, const(False, "boom"))
    assert asc.search_playlist("PL", "q") == (False, "boom")
    setrun(monkeypatch, const(True, "ERROR:Playlist not found"))
    assert asc.search_playlist("PL", "q") == (False, "Playlist not found")
    setrun(monkeypatch, const(True, "N|||Ar|||Al|||PID|||true\n\nN2|||Ar2|||Al2|||PID2"))
    ok, tracks = asc.search_playlist("PL", "q")
    assert ok and tracks[0]["explicit"] == "Yes" and tracks[1]["explicit"] == "Unknown"


def test_download_tracks(monkeypatch):
    assert asc.download_tracks("1", "PL")[0] is False  # both
    assert asc.download_tracks()[0] is False  # neither
    setrun(monkeypatch, const(True, "Downloading playlist: PL"))
    assert asc.download_tracks(playlist_name="PL")[0] is True
    assert asc.download_tracks(track_ids=" , ")[0] is False  # no valid ids
    r = setrun(monkeypatch, Recorder(True, "Downloading 2 track(s)"))
    assert asc.download_tracks(track_ids="a,b")[0] is True
    assert "download (first track" in r.calls[-1]
    setrun(monkeypatch, const(True, "ERROR:Playlist not found"))
    assert asc.download_tracks(playlist_name="PL") == (False, "Playlist not found")


def test_play_playlist(monkeypatch):
    r = setrun(monkeypatch, Recorder(True, "Now playing: PL"))
    asc.play_playlist("PL", shuffle=True)
    assert "shuffle enabled to true" in r.calls[-1]
    asc.play_playlist("PL")
    assert "shuffle enabled to false" in r.calls[-1]
    setrun(monkeypatch, const(True, "ERROR:Playlist not found"))
    assert asc.play_playlist("PL") == (False, "Playlist not found")


def test_find_library_track(monkeypatch):
    assert asc.find_library_track("  ") == (False, "")
    setrun(monkeypatch, const(True, "Name|||Artist"))
    assert asc.find_library_track("Name", artist="Artist") == (True, "Name|||Artist")
    setrun(monkeypatch, const(True, "NOT_FOUND"))
    assert asc.find_library_track("Name") == (False, "")


def test_play_track(monkeypatch):
    setrun(monkeypatch, const(True, "Now playing: T by A"))
    assert asc.play_track("T")[0] is True
    setrun(monkeypatch, const(True, "ERROR:Track not found: T"))
    assert asc.play_track("T") == (False, "Track not found: T")


# ===========================================================================
# open_catalog_song (subprocess-backed)
# ===========================================================================
def test_open_catalog_song(monkeypatch):
    assert asc.open_catalog_song("")[0] is False
    assert asc.open_catalog_song(123)[0] is False  # not a str
    assert asc.open_catalog_song("https://example.com")[0] is False  # non-apple https
    assert asc.open_catalog_song("ftp://x")[0] is False  # bare/other

    # music:// success on first try
    monkeypatch.setattr(asc.subprocess, "run", lambda *a, **k: proc(0))
    assert asc.open_catalog_song("music://music.apple.com/x") == (True, "Opened in Music")

    # https apple, music:// fails -> https fallback succeeds
    calls = {"n": 0}

    def _run(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise asc.subprocess.CalledProcessError(1, a[0])
        return proc(0)

    monkeypatch.setattr(asc.subprocess, "run", _run)
    assert asc.open_catalog_song("https://music.apple.com/x") == (True, "Opened via browser")

    # both fail
    def _fail(*a, **k):
        raise asc.subprocess.CalledProcessError(1, a[0])

    monkeypatch.setattr(asc.subprocess, "run", _fail)
    ok, msg = asc.open_catalog_song("https://music.apple.com/x")
    assert ok is False and "Failed to open" in msg


# ===========================================================================
# Search-field resolution + UI accessibility / lock
# ===========================================================================
def test_get_search_field(monkeypatch):
    # cache hit
    monkeypatch.setattr(asc, "_search_field_cache", "CACHED")
    assert asc._get_search_field() == "CACHED"

    # probe grouped
    monkeypatch.setattr(asc, "_search_field_cache", None)
    setrun(monkeypatch, const(True, "grouped"))
    assert asc._get_search_field() == asc._SEARCH_FIELD_TOOLBAR

    # probe flat
    monkeypatch.setattr(asc, "_search_field_cache", None)
    setrun(monkeypatch, const(True, "flat"))
    assert asc._get_search_field() == asc._SEARCH_FIELD_TOOLBAR_FLAT

    # none -> cmd+f -> grouped
    monkeypatch.setattr(asc, "_search_field_cache", None)
    setrun(monkeypatch, Seq([(True, "none"), (True, ""), (True, "grouped")]))
    assert asc._get_search_field() == asc._SEARCH_FIELD_TOOLBAR

    # probe not ok (returns None) twice -> sidebar fallback, no caching
    monkeypatch.setattr(asc, "_search_field_cache", None)
    setrun(monkeypatch, Seq([(False, "x"), (True, ""), (False, "y")]))
    assert asc._get_search_field() == asc._SEARCH_FIELD_SIDEBAR
    assert asc._search_field_cache is None


def test_classify_as_error():
    assert "Accessibility permission" in asc._classify_as_error("err -1743 here")
    assert "UI element not found" in asc._classify_as_error("err -1728 here")
    assert "Unexpected UI automation error" in asc._classify_as_error("weird thing")


def test_is_screen_locked(monkeypatch):
    # Quartz import fails -> None
    monkeypatch.setitem(sys.modules, "Quartz", None)
    assert asc.is_screen_locked() is None

    def fake_quartz(d):
        return types.SimpleNamespace(CGSessionCopyCurrentDictionary=lambda: d)

    # No dict -> True
    monkeypatch.setitem(sys.modules, "Quartz", fake_quartz(None))
    assert asc.is_screen_locked() is True
    # Locked flag -> True
    monkeypatch.setitem(sys.modules, "Quartz", fake_quartz({"CGSSessionScreenIsLocked": 1}))
    assert asc.is_screen_locked() is True
    # Not on console -> True
    monkeypatch.setitem(sys.modules, "Quartz", fake_quartz({"kCGSSessionOnConsoleKey": 0}))
    assert asc.is_screen_locked() is True
    # Unlocked + on console -> False
    monkeypatch.setitem(sys.modules, "Quartz", fake_quartz({"kCGSSessionOnConsoleKey": 1}))
    assert asc.is_screen_locked() is False


def test_check_ui_accessible(monkeypatch):
    # locked
    monkeypatch.setattr(asc, "is_screen_locked", lambda: True)
    ok, reason = asc.check_ui_accessible()
    assert ok is False and "locked" in reason.lower()

    monkeypatch.setattr(asc, "is_screen_locked", lambda: False)
    # run_applescript not ok -> classified
    setrun(monkeypatch, const(False, "err -1743"))
    ok, reason = asc.check_ui_accessible()
    assert ok is False and "Accessibility" in reason

    # windows > 0 -> accessible
    setrun(monkeypatch, const(True, "2"))
    assert asc.check_ui_accessible() == (True, "")

    # zero windows, loginwindow has windows -> locked message
    setrun(monkeypatch, Seq([(True, "0"), (True, "true")]))
    ok, reason = asc.check_ui_accessible()
    assert ok is False and "screen is locked" in reason.lower()

    # zero windows, loginwindow false -> generic no windows
    setrun(monkeypatch, Seq([(True, "0"), (True, "false")]))
    ok, reason = asc.check_ui_accessible()
    assert ok is False and "no visible windows" in reason


def test_check_playing(monkeypatch):
    setrun(monkeypatch, const(True, "playing"))
    assert asc._check_playing() is True
    setrun(monkeypatch, const(True, "paused"))
    assert asc._check_playing() is False


# ===========================================================================
# CoreGraphics / JXA helpers (subprocess-backed)
# ===========================================================================
def test_jxa_helpers_success(monkeypatch):
    monkeypatch.setattr(asc.subprocess, "run", lambda *a, **k: proc(0))
    assert asc._jxa_mouse_move(1, 2) is True
    assert asc._jxa_mouse_click(1, 2) is True
    assert asc._jxa_mouse_double_click(1, 2) is True
    assert asc._jxa_scroll_down(1, 2) is True
    # _hover_with_nudge delegates to _jxa_mouse_move
    assert asc._hover_with_nudge(3, 4) is True


def test_jxa_helpers_failure(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(asc.subprocess, "run", _boom)
    assert asc._jxa_mouse_move(1, 2) is False
    assert asc._jxa_mouse_click(1, 2) is False
    assert asc._jxa_mouse_double_click(1, 2) is False
    assert asc._jxa_scroll_down(1, 2) is False


def test_ensure_music_frontmost(monkeypatch):
    # open raises (caught), process check returns true -> break, then run_applescript
    calls = {"n": 0}

    def _run(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise asc.subprocess.TimeoutExpired(cmd="open", timeout=5)
        return proc(0, "true")

    monkeypatch.setattr(asc.subprocess, "run", _run)
    setrun(monkeypatch, const(True, ""))
    asc._ensure_music_frontmost()  # should not raise

    # process check always raises OSError -> loop exhausts, still completes
    def _run2(*a, **k):
        raise OSError("no")

    monkeypatch.setattr(asc.subprocess, "run", _run2)
    setrun(monkeypatch, const(True, ""))
    asc._ensure_music_frontmost()


# ===========================================================================
# Click-to-play orchestration
# ===========================================================================
def test_click_play_or_shuffle(monkeypatch):
    monkeypatch.setattr(asc, "_ensure_music_frontmost", lambda: None)

    # NOT_FOUND
    setrun(monkeypatch, const(True, "NOT_FOUND"))
    ok, msg = asc._click_play_or_shuffle()
    assert ok is False and "Could not find Play" in msg

    # invalid position
    setrun(monkeypatch, const(True, "not,a,number"))
    ok, msg = asc._click_play_or_shuffle(shuffle=True)
    assert ok is False and "Invalid Shuffle button position" in msg

    # click fails
    setrun(monkeypatch, const(True, "10.0,20.0"))
    monkeypatch.setattr(asc, "_jxa_mouse_click", lambda x, y: False)
    assert asc._click_play_or_shuffle()[0] is False

    # click ok + playing
    monkeypatch.setattr(asc, "_jxa_mouse_click", lambda x, y: True)
    monkeypatch.setattr(asc, "_check_playing", lambda: True)
    ok, msg = asc._click_play_or_shuffle()
    assert ok is True and "playing via UI click" in msg

    # click ok but not playing
    monkeypatch.setattr(asc, "_check_playing", lambda: False)
    ok, msg = asc._click_play_or_shuffle()
    assert ok is False and "playback did not start" in msg


def test_find_highlighted_track_position(monkeypatch):
    setrun(monkeypatch, const(True, "NOT_FOUND"))
    assert asc._find_highlighted_track_position() is None
    setrun(monkeypatch, const(True, "1,2"))  # < 3 parts
    assert asc._find_highlighted_track_position() is None
    setrun(monkeypatch, const(True, "x,y,Name"))  # ValueError
    assert asc._find_highlighted_track_position() is None
    setrun(monkeypatch, const(True, "10.0,20.0,My Track"))
    assert asc._find_highlighted_track_position() == (10.0, 20.0, "My Track")


def test_get_window_bottom(monkeypatch):
    setrun(monkeypatch, const(True, "500.0"))
    assert asc._get_window_bottom() == 500.0
    setrun(monkeypatch, const(True, "nope"))
    assert asc._get_window_bottom() is None
    setrun(monkeypatch, const(False, ""))
    assert asc._get_window_bottom() is None


def test_play_by_track_name(monkeypatch):
    monkeypatch.setattr(asc, "_ensure_music_frontmost", lambda: None)
    assert asc._play_by_track_name("  ") == (False, "No track name to match")

    setrun(monkeypatch, const(True, "NOT_FOUND"))
    assert asc._play_by_track_name("T")[0] is False

    setrun(monkeypatch, const(True, "bad,pos,extra"))
    ok, msg = asc._play_by_track_name("T")
    assert ok is False and "Invalid track row position" in msg

    setrun(monkeypatch, const(True, "10.0,20.0"))
    monkeypatch.setattr(asc, "_jxa_mouse_double_click", lambda x, y: False)
    assert asc._play_by_track_name("T")[0] is False

    monkeypatch.setattr(asc, "_jxa_mouse_double_click", lambda x, y: True)
    monkeypatch.setattr(asc, "_check_playing", lambda: True)
    assert asc._play_by_track_name("T") == (True, "Playing: T")
    monkeypatch.setattr(asc, "_check_playing", lambda: False)
    assert asc._play_by_track_name("T")[0] is False


def test_play_specific_track(monkeypatch):
    monkeypatch.setattr(asc, "_ensure_music_frontmost", lambda: None)

    # primary name-match succeeds
    monkeypatch.setattr(asc, "_play_by_track_name", lambda n: (True, "Playing: " + n))
    assert asc._play_specific_track("T") == (True, "Playing: T")

    # primary fails -> fallback; highlighted not found
    monkeypatch.setattr(asc, "_play_by_track_name", lambda n: (False, "no"))
    monkeypatch.setattr(asc, "_find_highlighted_track_position", lambda: None)
    assert asc._play_specific_track("T")[0] is False

    # fallback with scroll branch, lost after scroll
    seq_pos = Seq([])
    positions = [(10.0, 600.0, "T"), None]

    def _find():
        return positions.pop(0)

    monkeypatch.setattr(asc, "_find_highlighted_track_position", _find)
    monkeypatch.setattr(asc, "_get_window_bottom", lambda: 500.0)  # cy>bottom-30 triggers scroll
    monkeypatch.setattr(asc, "_jxa_scroll_down", lambda *a, **k: True)
    assert asc._play_specific_track("T") == (False, "Lost track row after scrolling")

    # full fallback success: no scroll needed, hover ok, checkbox click playing
    monkeypatch.setattr(asc, "_find_highlighted_track_position", lambda: (10.0, 100.0, "T"))
    monkeypatch.setattr(asc, "_get_window_bottom", lambda: 500.0)
    monkeypatch.setattr(asc, "_hover_with_nudge", lambda x, y: True)
    setrun(monkeypatch, const(True, "T"))
    monkeypatch.setattr(asc, "_check_playing", lambda: True)
    ok, msg = asc._play_specific_track("T")
    assert ok is True and msg == "Playing: T"

    # hover fails
    monkeypatch.setattr(asc, "_hover_with_nudge", lambda x, y: False)
    assert asc._play_specific_track("T") == (False, "Failed to move mouse for hover")

    # hover ok, checkbox NOT_FOUND -> failure tail
    monkeypatch.setattr(asc, "_hover_with_nudge", lambda x, y: True)
    setrun(monkeypatch, const(True, "NOT_FOUND"))
    assert asc._play_specific_track("T")[0] is False


def test_play_specific_track_scroll_success(monkeypatch):
    """Off-screen row: scroll, re-find a valid position, hover+click, play."""
    monkeypatch.setattr(asc, "_ensure_music_frontmost", lambda: None)
    monkeypatch.setattr(asc, "_play_by_track_name", lambda n: (False, "no"))
    finds = [(10.0, 600.0, "T"), (10.0, 120.0, "T2")]
    monkeypatch.setattr(asc, "_find_highlighted_track_position", lambda: finds.pop(0))
    monkeypatch.setattr(asc, "_get_window_bottom", lambda: 500.0)  # 600 > 470 -> scroll
    monkeypatch.setattr(asc, "_jxa_scroll_down", lambda *a, **k: True)
    monkeypatch.setattr(asc, "_hover_with_nudge", lambda x, y: True)
    setrun(monkeypatch, const(True, "T2"))
    monkeypatch.setattr(asc, "_check_playing", lambda: True)
    ok, msg = asc._play_specific_track("T")
    assert ok is True and msg == "Playing: T2"


def test_open_catalog_and_play(monkeypatch):
    # /song/ without ?i= rejected
    ok, msg = asc.open_catalog_and_play("https://music.apple.com/song/x")
    assert ok is False and "not supported" in msg

    # open fails
    monkeypatch.setattr(asc, "open_catalog_song", lambda url: (False, "openfail"))
    assert asc.open_catalog_and_play("https://music.apple.com/album/x") == (False, "openfail")

    monkeypatch.setattr(asc, "open_catalog_song", lambda url: (True, "opened"))
    monkeypatch.setattr(asc.time, "time", lambda: 0.0)

    # auto-started
    monkeypatch.setattr(asc, "_check_playing", lambda: True)
    ok, msg = asc.open_catalog_and_play("https://music.apple.com/album/x")
    assert ok is True and "auto-started" in msg

    # track param play success
    monkeypatch.setattr(asc, "_check_playing", lambda: False)
    monkeypatch.setattr(asc, "_play_specific_track", lambda n: (True, "trackplay"))
    ok, msg = asc.open_catalog_and_play("https://music.apple.com/album/x?i=1", track_name="T")
    assert ok and msg == "trackplay"

    # non-track click success
    monkeypatch.setattr(asc, "_click_play_or_shuffle", lambda s: (True, "clicked"))
    ok, msg = asc.open_catalog_and_play("https://music.apple.com/album/x")
    assert ok and msg == "clicked"

    # timeout failure paths: run the retry/wait body once, then exit past deadline
    monkeypatch.setattr(asc, "_check_playing", lambda: False)
    monkeypatch.setattr(asc, "_play_specific_track", lambda n: (False, "no"))
    monkeypatch.setattr(asc, "_click_play_or_shuffle", lambda s: (False, "no"))

    monkeypatch.setattr(asc.time, "time", Seq([0.0, 0.0], default=999.0))
    ok, msg = asc.open_catalog_and_play("https://music.apple.com/album/x?i=1", track_name="T")
    assert ok is False and "Opened the track" in msg

    monkeypatch.setattr(asc.time, "time", Seq([0.0, 0.0], default=999.0))
    ok, msg = asc.open_catalog_and_play("https://music.apple.com/album/x")
    assert ok is False and "Opened it in Music" in msg


# ===========================================================================
# Library search / listing
# ===========================================================================
def test_get_library_songs(monkeypatch):
    assert asc.get_library_songs(limit=-1)[0] is False
    setrun(monkeypatch, const(False, "err"))
    assert asc.get_library_songs() == (False, "err")
    setrun(monkeypatch, const(True, "N|||Ar|||Al|||60|||G|||2020|||P|||true\nbad"))
    ok, tracks = asc.get_library_songs(limit=5)
    assert ok and len(tracks) == 1 and tracks[0]["duration"] == "1:00"


def test_get_loved_songs(monkeypatch):
    assert asc.get_loved_songs(limit=-1)[0] is False
    setrun(monkeypatch, const(False, "err"))
    assert asc.get_loved_songs() == (False, "err")
    setrun(monkeypatch, const(True, "N|||Ar|||Al|||60|||G|||2020|||P|||false"))
    ok, tracks = asc.get_loved_songs(limit=10)
    assert ok and tracks[0]["explicit"] == "No"


def test_get_library_songs_page(monkeypatch):
    assert asc.get_library_songs_page(0, 0) == (False, [], 0, "limit must be > 0")
    setrun(monkeypatch, const(False, "err"))
    assert asc.get_library_songs_page(0, 10) == (False, [], 0, "err")
    setrun(monkeypatch, const(True, "total:bad\ntotal:3\nN|||Ar|||Al|||60|||G|||2020|||P|||true"))
    ok, tracks, total, err = asc.get_library_songs_page(0, 10)
    assert ok and total == 3 and len(tracks) == 1 and err == ""


def test_library_search_source():
    assert "genre contains" in asc._library_search_source("Rock", "genre")
    assert "only songs" in asc._library_search_source("q", "songs")
    assert asc._library_search_source("q", "all").endswith('"q" ')


def test_search_library_page_and_wrapper(monkeypatch):
    assert asc.search_library_page("q", limit=0) == (False, [], 0, "limit must be > 0")
    setrun(monkeypatch, const(False, "err"))
    assert asc.search_library_page("q") == (False, [], 0, "err")
    setrun(monkeypatch, const(True, "total:bad\ntotal:1\nN|||Ar|||Al|||60|||G|||2020|||P|||true"))
    ok, tracks, total, err = asc.search_library_page("q")
    assert ok and total == 1 and tracks[0]["name"] == "N"

    # wrapper success + failure
    setrun(monkeypatch, const(True, "total:0\n"))
    assert asc.search_library("q") == (True, [])
    setrun(monkeypatch, const(False, "boom"))
    assert asc.search_library("q") == (False, "boom")


# ===========================================================================
# Track metadata
# ===========================================================================
def test_love_dislike(monkeypatch):
    setrun(monkeypatch, const(True, "Loved: T"))
    assert asc.love_track("T")[0] is True
    setrun(monkeypatch, const(True, "ERROR:Track not found: T"))
    assert asc.love_track("T") == (False, "Track not found: T")
    setrun(monkeypatch, const(True, "Disliked: T"))
    assert asc.dislike_track("T", artist="A")[0] is True
    setrun(monkeypatch, const(True, "ERROR:Track not found: T"))
    assert asc.dislike_track("T") == (False, "Track not found: T")


def test_rating(monkeypatch):
    setrun(monkeypatch, const(True, "60"))
    assert asc.get_rating("T") == (True, 60)
    setrun(monkeypatch, const(True, "ERROR:Track not found: T"))
    assert asc.get_rating("T") == (False, "Track not found: T")
    setrun(monkeypatch, const(True, "notanint"))
    ok, msg = asc.get_rating("T")
    assert ok is False and "Invalid rating" in msg

    setrun(monkeypatch, const(True, "Set rating to 100 for: T"))
    assert asc.set_rating("T", 250)[0] is True  # clamped
    setrun(monkeypatch, const(True, "ERROR:Track not found: T"))
    assert asc.set_rating("T", 40) == (False, "Track not found: T")


# ===========================================================================
# AirPlay / utilities / stats
# ===========================================================================
def test_airplay(monkeypatch):
    setrun(monkeypatch, const(False, "err"))
    assert asc.get_airplay_devices() == (False, "err")
    setrun(monkeypatch, const(True, "Living Room\n\nKitchen"))
    assert asc.get_airplay_devices() == (True, ["Living Room", "Kitchen"])

    setrun(monkeypatch, const(True, "Switched to: Living Room"))
    assert asc.set_airplay_device("Living")[0] is True
    setrun(monkeypatch, const(True, "ERROR:Device not found: X"))
    assert asc.set_airplay_device("X") == (False, "Device not found: X")


def test_reveal_track(monkeypatch):
    setrun(monkeypatch, const(True, "Revealed: T"))
    assert asc.reveal_track("T")[0] is True
    setrun(monkeypatch, const(True, "ERROR:Track not found: T"))
    assert asc.reveal_track("T") == (False, "Track not found: T")


def test_get_library_stats(monkeypatch):
    setrun(monkeypatch, const(False, "err"))
    assert asc.get_library_stats() == (False, "err")
    setrun(monkeypatch, const(True, "100|||5|||playing|||true|||all|||50"))
    ok, stats = asc.get_library_stats()
    assert ok and stats["track_count"] == 100 and stats["shuffle"] is True
    setrun(monkeypatch, const(True, "only|||three|||parts"))
    assert asc.get_library_stats()[0] is False


# ===========================================================================
# UI catalog automation primitives
# ===========================================================================
def test_focus_search_field(monkeypatch):
    monkeypatch.setattr(asc, "_ensure_music_frontmost", lambda: None)
    monkeypatch.setattr(asc, "_get_search_field", lambda: "FIELD")
    assert asc._focus_search_field("  ") == (False, "Empty query")

    # success
    setrun(monkeypatch, const(True, ""))
    assert asc._focus_search_field("q") == (True, "")

    # path error -> retry (invalidate cache), then success
    seq = Seq([(False, "Can't get group 1"), (True, "")])
    setrun(monkeypatch, seq)
    assert asc._focus_search_field("q") == (True, "")
    assert len(seq.calls) == 2

    # failure not accessible
    setrun(monkeypatch, const(False, "boom"))
    monkeypatch.setattr(asc, "check_ui_accessible", lambda: (False, "locked"))
    assert asc._focus_search_field("q") == (False, "locked")

    # failure accessible -> classify
    monkeypatch.setattr(asc, "check_ui_accessible", lambda: (True, ""))
    setrun(monkeypatch, const(False, "weird -1743"))
    ok, msg = asc._focus_search_field("q")
    assert ok is False and "Accessibility" in msg


def test_wait_for_top_results(monkeypatch):
    # immediate results
    setrun(monkeypatch, const(True, "1|||Name|||Song · A"))
    ok, raw = asc._wait_for_top_results()
    assert ok and "Name" in raw

    # timeout with NO_RESULTS, triggers second enter, clean empty -> (True, "")
    clock = Seq([0.0, 0.0, 1.5, 1.6], default=99.0)
    monkeypatch.setattr(asc.time, "monotonic", clock)
    setrun(monkeypatch, const(True, "NO_RESULTS"))
    ok, raw = asc._wait_for_top_results(timeout=5.0)
    assert ok is True and raw == ""

    # not ok at the end -> not accessible
    clock2 = Seq([0.0, 0.0], default=99.0)
    monkeypatch.setattr(asc.time, "monotonic", clock2)
    setrun(monkeypatch, const(False, "boom"))
    monkeypatch.setattr(asc, "check_ui_accessible", lambda: (False, "locked"))
    assert asc._wait_for_top_results(timeout=1.0) == (False, "locked")

    # not ok -> accessible -> classify
    clock3 = Seq([0.0, 0.0], default=99.0)
    monkeypatch.setattr(asc.time, "monotonic", clock3)
    setrun(monkeypatch, const(False, "err -1743"))
    monkeypatch.setattr(asc, "check_ui_accessible", lambda: (True, ""))
    ok, msg = asc._wait_for_top_results(timeout=1.0)
    assert ok is False and "Accessibility" in msg


def test_parse_top_results():
    raw = (
        "1|||Holocene|||Song · Bon Iver\n"
        "bad line no delim\n"
        "2|||short\n"
        "3|||No results for zzz|||\n"
        "4|||Some Album|||Album\n"
    )
    results = asc._parse_top_results(raw)
    names = {r["name"] for r in results}
    assert "Holocene" in names and "No results for zzz" not in names
    holo = next(r for r in results if r["name"] == "Holocene")
    assert holo["type"] == "Song" and holo["artist"] == "Bon Iver"
    album = next(r for r in results if r["name"] == "Some Album")
    assert album["type"] == "Album" and album["artist"] == ""


def test_find_top_result_position(monkeypatch):
    setrun(monkeypatch, const(True, "NOT_FOUND"))
    assert asc._find_top_result_position("X") is None
    setrun(monkeypatch, const(True, "10.0,20.0"))
    assert asc._find_top_result_position("X") == (10.0, 20.0)
    setrun(monkeypatch, const(True, "a,b"))
    assert asc._find_top_result_position("X") is None


def test_hover_then_click_subelement(monkeypatch):
    monkeypatch.setattr(asc, "_ensure_music_frontmost", lambda: None)

    # row not found
    monkeypatch.setattr(asc, "_find_top_result_position", lambda n: None)
    ok, msg = asc._hover_then_click_subelement("X", "set inner to checkbox 1 of e")
    assert ok is False and "Could not find" in msg

    monkeypatch.setattr(asc, "_find_top_result_position", lambda n: (10.0, 20.0))

    # hover fails
    monkeypatch.setattr(asc, "_hover_with_nudge", lambda x, y: False)
    assert asc._hover_then_click_subelement("X", "s")[1] == "Failed to hover"

    monkeypatch.setattr(asc, "_hover_with_nudge", lambda x, y: True)

    # poll never finds -> sub-element not visible (force time past deadline)
    clock = Seq([0.0, 0.0], default=99.0)
    monkeypatch.setattr(asc.time, "monotonic", clock)
    setrun(monkeypatch, const(True, "NOT_FOUND"))
    ok, msg = asc._hover_then_click_subelement("X", "s", max_wait=1.5)
    assert ok is False and msg == "sub-element not visible after hover"

    # found, invalid pos
    monkeypatch.setattr(asc.time, "monotonic", Seq([0.0, 0.0], default=0.5))
    setrun(monkeypatch, const(True, "a,b"))
    ok, msg = asc._hover_then_click_subelement("X", "s", max_wait=10)
    assert ok is False and "Invalid sub-element position" in msg

    # found, click fails
    monkeypatch.setattr(asc.time, "monotonic", Seq([0.0, 0.0], default=0.5))
    setrun(monkeypatch, const(True, "5.0,6.0"))
    monkeypatch.setattr(asc, "_jxa_mouse_click", lambda x, y: False)
    assert asc._hover_then_click_subelement("X", "s", max_wait=10)[1] == "CoreGraphics click failed"

    # found, click ok -> success
    monkeypatch.setattr(asc.time, "monotonic", Seq([0.0, 0.0], default=0.5))
    monkeypatch.setattr(asc, "_jxa_mouse_click", lambda x, y: True)
    assert asc._hover_then_click_subelement("X", "s", max_wait=10) == (True, "")


def test_hover_then_click_rehover(monkeypatch):
    """Exercise the mid-poll re-hover branch."""
    monkeypatch.setattr(asc, "_ensure_music_frontmost", lambda: None)
    monkeypatch.setattr(asc, "_find_top_result_position", lambda n: (10.0, 20.0))
    hovers = {"n": 0}

    def _hover(x, y):
        hovers["n"] += 1
        return True

    monkeypatch.setattr(asc, "_hover_with_nudge", _hover)
    # monotonic: start=0; deadline=0+1.5; rehover_at=0+0.75.
    # iter1 check 0<1.5, time 0 not > 0.75; iter2 time 1.0 > 0.75 -> rehover;
    # then deadline check at 2.0 -> exit -> not found.
    monkeypatch.setattr(asc.time, "monotonic", Seq([0.0, 0.0, 0.0, 1.0, 1.0, 2.0], default=99.0))
    setrun(monkeypatch, const(True, "NOT_FOUND"))
    ok, msg = asc._hover_then_click_subelement("X", "s", max_wait=1.5)
    assert ok is False and hovers["n"] >= 2


def test_verify_track_playing(monkeypatch):
    setrun(monkeypatch, const(True, "My Song"))
    assert asc._verify_track_playing("my song") == (True, "My Song")
    # timeout, returns last seen
    clock = Seq([0.0, 0.0], default=99.0)
    monkeypatch.setattr(asc.time, "monotonic", clock)
    setrun(monkeypatch, const(True, "Other"))
    ok, last = asc._verify_track_playing("nomatch", timeout=1.0)
    assert ok is False and last == "Other"


def test_open_search_popover(monkeypatch):
    monkeypatch.setattr(asc, "_ensure_music_frontmost", lambda: None)
    monkeypatch.setattr(asc, "_get_search_field", lambda: "FIELD")
    assert asc._open_search_popover("  ") == (False, "Empty query")

    # navigate ok, popover appears
    setrun(monkeypatch, Router([("exists (first pop over)", (True, "true"))], default=(True, "")))
    assert asc._open_search_popover("q") == (True, "")

    # navigate path error -> retry then popover appears
    seq = Seq(
        [
            (True, ""),  # nav step 1
            (False, "Can't get group 1"),  # type step fails -> retry
            (True, ""),  # nav step 1 (retry)
            (True, ""),  # type step ok
            (True, "true"),  # popover exists
        ]
    )
    setrun(monkeypatch, seq)
    assert asc._open_search_popover("q") == (True, "")

    # navigate fails, not accessible
    setrun(monkeypatch, Seq([(True, ""), (False, "boom")], default=(False, "boom")))
    monkeypatch.setattr(asc, "check_ui_accessible", lambda: (False, "locked"))
    assert asc._open_search_popover("q") == (False, "locked")

    # navigate fails, accessible -> classify
    setrun(monkeypatch, Seq([(True, ""), (False, "err -1743")], default=(False, "err -1743")))
    monkeypatch.setattr(asc, "check_ui_accessible", lambda: (True, ""))
    ok, msg = asc._open_search_popover("q")
    assert ok is False and "Accessibility" in msg

    # navigate ok but popover never appears -> poll body runs once, then timeout
    monkeypatch.setattr(asc.time, "monotonic", Seq([0.0, 0.0], default=99.0))
    setrun(monkeypatch, Router([("exists (first pop over)", (True, "false"))], default=(True, "")))
    ok, msg = asc._open_search_popover("q")
    assert ok is False and "did not appear" in msg


def test_find_popover_song_row(monkeypatch):
    setrun(monkeypatch, const(True, "NO_POPOVER"))
    assert asc._find_popover_song_row("X") is None

    # mix of rows: exact song wins; album & wrong-artist rejected
    raw = (
        "1|||Brown Sugar (Soul Inside 808 Mix)|||Song · The Rolling Stones\n"
        "2|||Brown Sugar|||Song · The Rolling Stones\n"
        "3|||Brown Sugar (Remastered)|||Song · The Rolling Stones\n"
        "4|||Brown Sugar|||Album · The Rolling Stones\n"
        "badrow\n"
        "x|||y|||z\n"  # idx not int
    )
    setrun(monkeypatch, const(True, raw))
    idx, artist = asc._find_popover_song_row("Brown Sugar", artist="Rolling Stones")
    assert idx == 2 and "Rolling Stones" in artist

    # contains-tier match (400) when no exact
    setrun(monkeypatch, const(True, "1|||Sugar Brown Live|||Song · X\n"))
    res = asc._find_popover_song_row("Sugar Brown", artist="X")
    assert res is not None and res[0] == 1

    # no song rows -> None
    setrun(monkeypatch, const(True, "1|||Thing|||Album · X\n"))
    assert asc._find_popover_song_row("Thing") is None

    # wrong artist filter -> None
    setrun(monkeypatch, const(True, "1|||Song|||Song · Real\n"))
    assert asc._find_popover_song_row("Song", artist="Other") is None

    # song row, artist matches, but title has no name overlap -> _score else None
    setrun(monkeypatch, const(True, "1|||Totally Different|||Song · X\n"))
    assert asc._find_popover_song_row("Brown Sugar", artist="X") is None


def test_click_popover_row(monkeypatch):
    setrun(monkeypatch, const(True, "ERR"))
    assert asc._click_popover_row(1)[0] is False
    setrun(monkeypatch, const(True, "a,b"))
    ok, msg = asc._click_popover_row(1)
    assert ok is False and "Invalid row position" in msg
    setrun(monkeypatch, const(True, "5.0,6.0"))
    monkeypatch.setattr(asc, "_jxa_mouse_click", lambda x, y: False)
    assert asc._click_popover_row(1) == (False, "CoreGraphics click failed")
    monkeypatch.setattr(asc, "_jxa_mouse_click", lambda x, y: True)
    assert asc._click_popover_row(1) == (True, "")


def test_wait_for_song_page(monkeypatch):
    setrun(monkeypatch, const(True, "2"))
    path = asc._wait_for_song_page("T")
    assert path is not None and "list 2" in path
    # never found -> poll body runs once (sleep), then None on timeout
    monkeypatch.setattr(asc.time, "monotonic", Seq([0.0, 0.0], default=99.0))
    setrun(monkeypatch, const(True, "0"))
    assert asc._wait_for_song_page("T") is None


# ===========================================================================
# Public ui_* entrypoints
# ===========================================================================
def test_ui_search_catalog(monkeypatch):
    assert asc.ui_search_catalog("  ") == (False, [], "Empty query")

    # focus fails both attempts
    monkeypatch.setattr(asc, "_focus_search_field", lambda q: (False, "focuserr"))
    assert asc.ui_search_catalog("q") == (False, [], "focuserr")

    # focus ok, wait fails both
    monkeypatch.setattr(asc, "_focus_search_field", lambda q: (True, ""))
    monkeypatch.setattr(asc, "_wait_for_top_results", lambda: (False, "waiterr"))
    assert asc.ui_search_catalog("q") == (False, [], "waiterr")

    # focus ok, empty raw -> []
    monkeypatch.setattr(asc, "_wait_for_top_results", lambda: (True, ""))
    assert asc.ui_search_catalog("q") == (True, [], "")

    # focus ok, raw parsed
    monkeypatch.setattr(asc, "_wait_for_top_results", lambda: (True, "1|||N|||Song · A"))
    ok, results, err = asc.ui_search_catalog("q")
    assert ok and results[0]["name"] == "N" and err == ""


def test_ui_clear_search(monkeypatch):
    monkeypatch.setattr(asc, "_get_search_field", lambda: "FIELD")
    r = setrun(monkeypatch, Recorder(True, ""))
    assert asc.ui_clear_search() is None
    assert "key code 53" in r.calls[-1]


def test_ui_play_result(monkeypatch):
    # hover -> sub-element not visible
    monkeypatch.setattr(
        asc,
        "_hover_then_click_subelement",
        lambda n, s: (False, "sub-element not visible after hover"),
    )
    ok, msg = asc.ui_play_result("X")
    assert ok is False and "Play checkbox not visible" in msg

    # hover -> other error
    monkeypatch.setattr(asc, "_hover_then_click_subelement", lambda n, s: (False, "other"))
    assert asc.ui_play_result("X") == (False, "other")

    # hover ok, verify playing
    monkeypatch.setattr(asc, "_hover_then_click_subelement", lambda n, s: (True, ""))
    monkeypatch.setattr(asc, "_verify_track_playing", lambda n: (True, n))
    assert asc.ui_play_result("X") == (True, "Playing: X")

    # hover ok, wrong track playing
    monkeypatch.setattr(asc, "_verify_track_playing", lambda n: (False, "Other"))
    monkeypatch.setattr(asc, "_check_playing", lambda: True)
    ok, msg = asc.ui_play_result("X")
    assert ok is False and "instead of" in msg

    # hover ok, nothing playing
    monkeypatch.setattr(asc, "_check_playing", lambda: False)
    ok, msg = asc.ui_play_result("X")
    assert ok is False and "didn't start" in msg


def test_ui_play_result_by_query(monkeypatch):
    monkeypatch.setattr(asc, "ui_clear_search", lambda: None)

    # no results
    monkeypatch.setattr(asc, "ui_search_catalog", lambda q: (False, [], "why"))
    assert asc.ui_play_result_by_query("q") == (False, "why")

    # results found, song target
    monkeypatch.setattr(
        asc,
        "ui_search_catalog",
        lambda q: (True, [{"type": "Album", "name": "A"}, {"type": "Song", "name": "S"}], ""),
    )
    monkeypatch.setattr(asc, "ui_play_result", lambda name: (True, "Playing: " + name))
    assert asc.ui_play_result_by_query("q") == (True, "Playing: S")

    # results found, no song -> fallback to first
    monkeypatch.setattr(
        asc, "ui_search_catalog", lambda q: (True, [{"type": "Album", "name": "A"}], "")
    )
    assert asc.ui_play_result_by_query("q") == (True, "Playing: A")


# ===========================================================================
# library_snapshot / library_diff
# ===========================================================================
def test_library_snapshot_failures(monkeypatch):
    setrun(monkeypatch, const(False, "countfail"))
    ok, d = asc.library_snapshot()
    assert ok is False and "Failed to count" in d["error"]

    setrun(monkeypatch, const(True, "notanint"))
    ok, d = asc.library_snapshot()
    assert ok is False and "Invalid track count" in d["error"]

    # count ok, playback ok, playlist fetch fails
    setrun(
        monkeypatch,
        Router(
            [
                ("count of tracks of library playlist 1) as text", (True, "100")),
                ("set ps to player state", (True, "playing\n50\ntrue\nall\nSong\nArtist\nAlbum")),
                ("repeat with p in user playlists", (False, "plfail")),
            ]
        ),
    )
    ok, d = asc.library_snapshot()
    assert ok is False and "Failed to get playlists" in d["error"]


def test_library_snapshot_success(monkeypatch):
    playlist_data = (
        "PLAYLIST:My List|||FOLDER:\n"
        "T1|||A1|||Alb1\n"
        "PLAYLIST:Nested|||FOLDER:Summer\n"
        "T2|||A2|||Alb2\n"
        "FOLDER:Summer|||PATH:\n"
        "\n"
        "shortline\n"
    )
    setrun(
        monkeypatch,
        Router(
            [
                ("count of tracks of library playlist 1) as text", (True, "100")),
                ("set ps to player state", (True, "playing\n50\ntrue\nall\nSong\nArtist\nAlbum")),
                ("repeat with p in user playlists", (True, playlist_data)),
            ]
        ),
    )
    ok, snap = asc.library_snapshot()
    assert ok and snap["track_count"] == 100
    assert snap["playback"]["volume"] == 50 and snap["playback"]["shuffle"] is True
    assert "My List" in snap["playlists"]
    assert snap["playlists"]["My List"]["tracks"][0]["name"] == "T1"
    assert "Summer/Nested" in snap["playlists"]
    assert "Summer" in snap["folders"]


def test_library_snapshot_empty_playback(monkeypatch):
    """Playback query returns not-ok -> playback_state stays {}."""
    setrun(
        monkeypatch,
        Router(
            [
                ("count of tracks of library playlist 1) as text", (True, "5")),
                ("set ps to player state", (False, "pbfail")),
                ("repeat with p in user playlists", (True, "")),
            ]
        ),
    )
    ok, snap = asc.library_snapshot()
    assert ok and snap["playback"] == {} and snap["playlists"] == {}


def test_library_diff():
    before = {
        "track_count": 10,
        "playback": {"player_state": "stopped", "volume": 40},
        "playlists": {
            "Keep": {"tracks": [{"name": "T1", "artist": "A1"}]},
            "Gone": [{"name": "X", "artist": "Y"}],  # old list format
            "ListBoth": [{"name": "L1", "artist": "LA"}],  # list format, in both
            "Weird": None,  # neither list nor dict -> _get_tracks returns []
        },
        "folders": {"OldFolder": {}},
    }
    after = {
        "track_count": 12,
        "playback": {"player_state": "playing", "volume": 40},
        "playlists": {
            "Keep": {"tracks": [{"name": "T1", "artist": "A1"}, {"name": "T2", "artist": "A2"}]},
            "New": {"tracks": []},
            "ListBoth": [{"name": "L1", "artist": "LA"}, {"name": "L2", "artist": "LB"}],
            "Weird": None,
        },
        "folders": {"NewFolder": {}},
    }
    diff = asc.library_diff(before, after)
    assert diff["track_count_change"] == 2
    assert "player_state" in diff["playback_changes"]
    assert "New" in diff["playlists_added"] and "Gone" in diff["playlists_removed"]
    assert "Keep" in diff["playlists_changed"]
    assert "NewFolder" in diff["folders_added"] and "OldFolder" in diff["folders_removed"]
    assert diff["is_clean"] is False

    # clean case
    same = {"track_count": 1, "playback": {}, "playlists": {}, "folders": {}}
    assert asc.library_diff(same, same)["is_clean"] is True
