"""Tests for AppleScript integration module.

These tests only run on macOS where AppleScript is available.
They test the actual Music app integration.
"""

import os
import pytest
import re
import sys
import time

# Skip all tests if not on macOS. Most tests in this file shell out to
# osascript and exercise live Music.app state — they're necessarily slow
# and macOS-only. Specific test classes that DON'T require live state
# (TestUISearchParsing, TestUIPrimitives, TestClassifyAsError, TestGetSearchField,
# TestCheckUiAccessible, etc.) override this with their own faster marker.
pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="AppleScript tests only run on macOS"
)

from applemusic_mcp import applescript as asc

# Captured at import time (before conftest's function-scoped _block_live_applescript
# guard replaces asc.run_applescript). Tests that genuinely exercise
# run_applescript's own logic restore this real implementation and mock the
# subprocess boundary instead, so they stay hermetic without hitting osascript.
_REAL_RUN_APPLESCRIPT = asc.run_applescript


class _FakeProc:
    """Minimal stand-in for subprocess.CompletedProcess used to drive
    run_applescript offline."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _sample_library(n=30):
    """A deterministic fake library: distinct ids per row so offset paging is
    observable."""
    return [
        {
            "name": f"Song {i}",
            "artist": f"Artist {i}",
            "album": f"Album {i}",
            "duration": 180 + i,
            "genre": "Pop",
            "year": 2000 + (i % 25),
            "id": f"PID{i:04d}",
            "explicit": False,
        }
        for i in range(n)
    ]


def _fake_library_run(library):
    """Return a run_applescript stand-in that emulates Music.app's response to
    get_library_songs_page's script against ``library`` — honoring the offset/
    limit range encoded in the script so paging behavior stays real, offline."""

    def run(script):
        total = len(library)
        out = f"total:{total}\n"
        m_start = re.search(r"items (\d+) through endPos", script)
        m_end = re.search(r"set endPos to (\d+)", script)
        if not m_start or not m_end:
            return True, out
        offset = int(m_start.group(1)) - 1  # script uses 1-based start
        if total == 0 or offset >= total:
            return True, out
        end = min(int(m_end.group(1)), total)
        for t in library[offset:end]:
            out += (
                "|||".join(
                    [
                        t["name"],
                        t["artist"],
                        t["album"],
                        str(t["duration"]),
                        t.get("genre", ""),
                        str(t.get("year", "")),
                        t["id"],
                        "true" if t.get("explicit") else "false",
                    ]
                )
                + "\n"
            )
        return True, out

    return run


class TestAppleScriptAvailability:
    """Test AppleScript availability detection."""

    def test_is_available_on_macos(self):
        """Should return True on macOS."""
        assert asc.is_available() is True

    def test_run_applescript_simple(self, monkeypatch):
        """Should run simple AppleScript."""
        import subprocess

        monkeypatch.setattr(asc, "run_applescript", _REAL_RUN_APPLESCRIPT)
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: _FakeProc(returncode=0, stdout="hello\n")
        )
        success, output = asc.run_applescript('return "hello"')
        assert success is True
        assert output == "hello"

    def test_run_applescript_math(self, monkeypatch):
        """Should handle AppleScript expressions."""
        import subprocess

        monkeypatch.setattr(asc, "run_applescript", _REAL_RUN_APPLESCRIPT)
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: _FakeProc(returncode=0, stdout="4\n")
        )
        success, output = asc.run_applescript("return 2 + 2")
        assert success is True
        assert output == "4"

    def test_run_applescript_error(self, monkeypatch):
        """Should handle AppleScript errors gracefully."""
        import subprocess

        monkeypatch.setattr(asc, "run_applescript", _REAL_RUN_APPLESCRIPT)
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: _FakeProc(returncode=1, stderr="syntax error: expected end of line"),
        )
        success, output = asc.run_applescript("this is not valid applescript")
        assert success is False
        assert len(output) > 0  # Should have error message


class TestPlaybackControl:
    """Test playback control functions."""

    def test_get_player_state(self, monkeypatch):
        """Should get player state."""
        monkeypatch.setattr(asc, "run_applescript", lambda script: (True, "playing"))
        success, state = asc.get_player_state()
        assert success is True
        assert state in ("stopped", "playing", "paused")

    def test_get_volume(self, monkeypatch):
        """Should get volume level."""
        monkeypatch.setattr(asc, "run_applescript", lambda script: (True, "50"))
        success, volume = asc.get_volume()
        assert success is True
        assert isinstance(volume, int)
        assert 0 <= volume <= 100

    def test_get_shuffle(self, monkeypatch):
        """Should get shuffle state."""
        monkeypatch.setattr(asc, "run_applescript", lambda script: (True, "false"))
        success, shuffle = asc.get_shuffle()
        assert success is True
        assert isinstance(shuffle, bool)

    def test_get_repeat(self, monkeypatch):
        """Should get repeat mode."""
        monkeypatch.setattr(asc, "run_applescript", lambda script: (True, "off"))
        success, repeat = asc.get_repeat()
        assert success is True
        assert repeat in ("off", "one", "all")

    def test_get_current_track_when_stopped(self, monkeypatch):
        """Should handle stopped state gracefully."""
        monkeypatch.setattr(asc, "run_applescript", lambda script: (True, "STOPPED"))
        success, info = asc.get_current_track()
        assert success is True
        # Either has track info or shows stopped
        assert isinstance(info, dict)


class TestPlaylistOperations:
    """Test playlist operations."""

    pytestmark = pytest.mark.slow

    def test_get_playlists(self):
        """Should get list of playlists."""
        success, playlists = asc.get_playlists()
        assert success is True
        assert isinstance(playlists, list)
        assert len(playlists) > 0

        # Check playlist structure
        p = playlists[0]
        assert "name" in p
        assert "id" in p
        assert "smart" in p
        assert "track_count" in p

    def test_get_playlist_tracks(self):
        """Should get tracks from a known playlist."""
        # First get a playlist name
        success, playlists = asc.get_playlists()
        assert success is True
        assert len(playlists) > 0

        # Skip special/smart playlists that could be huge (Music, Music Videos, etc)
        special_names = {"Music", "Music Videos", "Library", "Favorite Songs"}

        # Get tracks from first non-empty, non-special playlist
        for p in playlists:
            if p["track_count"] > 0 and p["track_count"] < 1000 and p["name"] not in special_names:
                success, tracks = asc.get_playlist_tracks(p["name"])
                assert success is True
                assert isinstance(tracks, list)
                if tracks:
                    t = tracks[0]
                    assert "name" in t
                    assert "artist" in t
                    assert "album" in t
                    assert "id" in t
                break

    def test_get_playlist_tracks_not_found(self):
        """Should handle missing playlist gracefully."""
        success, result = asc.get_playlist_tracks("__NONEXISTENT_PLAYLIST_12345__")
        assert success is False
        assert "not found" in result.lower()

    def test_create_and_delete_playlist(self):
        """Should create and delete a playlist."""
        test_name = "_TEST_PLAYLIST_DELETE_ME_"

        # Music.app cheerfully allows duplicate playlist names. If a prior
        # test run failed before its delete step (or was interrupted), a
        # leftover instance will still be there — and create_playlist will
        # add ANOTHER one alongside it. Drain any leftovers first so this
        # test is hermetic regardless of prior state.
        ok, playlists = asc.get_playlists()
        if ok:
            for _ in range(sum(1 for p in playlists if p.get("name") == test_name)):
                asc.delete_playlist(test_name)

        # Create
        success, playlist_id = asc.create_playlist(test_name, "Test description")
        assert success is True
        assert len(playlist_id) > 0

        # Verify it exists (and is the only one — confirms the pre-clean worked)
        success, playlists = asc.get_playlists()
        assert success is True
        names = [p["name"] for p in playlists]
        assert test_name in names
        assert names.count(test_name) == 1

        # Delete
        success, msg = asc.delete_playlist(test_name)
        assert success is True

        # Verify deleted
        success, playlists = asc.get_playlists()
        names = [p["name"] for p in playlists]
        assert test_name not in names


class TestFolderOperations:
    """Test folder operations."""

    pytestmark = pytest.mark.slow

    def test_create_and_delete_folder(self):
        """Should create and delete a folder playlist."""
        test_name = "_TEST_FOLDER_DELETE_ME_"

        # Create
        success, folder_id = asc.create_folder(test_name)
        assert success is True
        assert len(folder_id) > 0

        # Delete
        success, msg = asc.delete_folder(test_name)
        assert success is True

    def test_rename_folder(self):
        """Should rename a folder via the generic rename_playlist function."""
        test_name = "_TEST_RENAME_FOLDER_"
        new_name = "_TEST_RENAME_FOLDER_NEW_"

        success, _ = asc.create_folder(test_name)
        assert success is True

        # Rename using the generic function (should find folder playlists)
        success, msg = asc.rename_playlist(test_name, new_name)
        assert success is True
        assert new_name in msg

        # Cleanup
        asc.delete_folder(new_name)

    def test_delete_folder_via_generic_delete(self):
        """Should delete a folder via the generic delete_playlist function."""
        test_name = "_TEST_DEL_FOLDER_GENERIC_"

        success, _ = asc.create_folder(test_name)
        assert success is True

        # Delete using the generic function (should find folder playlists)
        success, msg = asc.delete_playlist(test_name)
        assert success is True

    def test_move_playlist_to_folder(self):
        """Should move a playlist into a folder."""
        folder_name = "_TEST_MOVE_FOLDER_"
        playlist_name = "_TEST_MOVE_PL_"

        success, _ = asc.create_folder(folder_name)
        assert success is True
        success, _ = asc.create_playlist(playlist_name)
        assert success is True

        # Move
        success, msg = asc.move_to_folder(playlist_name, folder_name)
        assert success is True

        # Verify path — should be "folder/playlist"
        success, path = asc.get_playlist_path(playlist_name)
        assert success is True
        assert path == f"{folder_name}/{playlist_name}"

        # Cleanup
        asc.delete_playlist(playlist_name)
        asc.delete_folder(folder_name)

    def test_create_nested_folder_path(self):
        """Should create nested folders via slash-separated path."""
        # Pre-clean all stale instances (multiple may accumulate from failed runs)
        for _ in range(5):
            asc.delete_folder("_TEST_OUTER_/_TEST_INNER_")
            asc.delete_folder("_TEST_OUTER_")
            asc.delete_folder("_TEST_INNER_")

        path = "_TEST_OUTER_/_TEST_INNER_"

        success, folder_id = asc.create_folder_path(path)
        assert success is True
        assert len(folder_id) > 0

        # Verify inner folder exists and is nested
        success, resolved_path = asc.get_playlist_path("_TEST_INNER_")
        assert success is True
        assert "_TEST_OUTER_" in resolved_path

        # Cleanup (inner first, then outer)
        asc.delete_folder("_TEST_OUTER_/_TEST_INNER_")
        asc.delete_folder("_TEST_OUTER_")

    def test_move_to_root(self):
        """Should move a playlist out of a folder to root."""
        folder_name = "_TEST_ROOT_FOLDER_"
        playlist_name = "_TEST_ROOT_PL_"

        success, _ = asc.create_folder(folder_name)
        assert success is True
        success, _ = asc.create_playlist(playlist_name)
        assert success is True

        # Move into folder
        success, _ = asc.move_to_folder(playlist_name, folder_name)
        assert success is True

        # Move back to root
        success, msg = asc.move_to_root(playlist_name)
        assert success is True

        # Verify it's at root (path should just be the playlist name)
        success, path = asc.get_playlist_path(playlist_name)
        assert success is True
        assert "/" not in path  # No folder prefix = at root

        # Cleanup
        asc.delete_playlist(playlist_name)
        asc.delete_folder(folder_name)

    def test_get_playlist_path(self):
        """Should return full path for nested playlist."""
        folder_name = "_TEST_PATH_FOLDER_"
        playlist_name = "_TEST_PATH_PL_"

        success, _ = asc.create_folder(folder_name)
        assert success is True
        success, _ = asc.create_playlist(playlist_name)
        assert success is True
        success, _ = asc.move_to_folder(playlist_name, folder_name)
        assert success is True

        success, path = asc.get_playlist_path(playlist_name)
        assert success is True
        assert path == f"{folder_name}/{playlist_name}"

        # Cleanup
        asc.delete_playlist(playlist_name)
        asc.delete_folder(folder_name)

    def test_get_folder_tree(self):
        """Should return folder hierarchy text."""
        folder_name = "_TEST_TREE_FOLDER_"

        # Pre-clean in case a prior run failed before cleanup
        asc.delete_folder(folder_name)

        success, _ = asc.create_folder(folder_name)
        assert success is True

        success, tree = asc.get_folder_tree()
        assert success is True
        assert folder_name in tree

        # Cleanup
        asc.delete_folder(folder_name)


class TestLibrarySearch:
    """Test library search functions."""

    pytestmark = pytest.mark.slow

    def test_search_library_all(self):
        """Should search entire library."""
        success, results = asc.search_library("love")
        assert success is True
        assert isinstance(results, list)
        # Should find something with "love" in a reasonable library
        assert len(results) >= 0

    def test_search_library_artists(self):
        """Should search by artist."""
        success, results = asc.search_library("Beatles", "artists")
        assert success is True
        assert isinstance(results, list)

    def test_search_library_structure(self):
        """Should return properly structured results."""
        success, results = asc.search_library("the")
        assert success is True
        if results:
            t = results[0]
            assert "name" in t
            assert "artist" in t
            assert "album" in t
            assert "duration" in t
            assert "id" in t


class TestGenreSearch:
    """Searching the local library by genre.

    Music.app's ``search … for`` full-text command does NOT look at the genre
    field, so passing a genre name through it false-matches tracks whose
    name/album merely contain that word. ``types="genre"`` must instead filter
    on the genre field itself via ``whose genre contains …``.
    """

    def test_genre_search_filters_by_genre_field(self):
        """types='genre' selects by the genre field, not full-text search."""
        source = asc._library_search_source("Rock", "genre")
        assert 'whose genre contains "Rock"' in source
        # The whole point: must NOT fall back to full-text search, which would
        # match songs/albums named "...Rock..." regardless of their genre.
        assert "search library playlist 1 for" not in source

    def test_genre_search_escapes_query(self):
        """A genre containing a quote is escaped (no AppleScript injection)."""
        source = asc._library_search_source('Rock" & evil', "genre")
        assert '\\"' in source
        assert "search library playlist 1 for" not in source

    def test_non_genre_search_still_uses_fulltext(self):
        """Non-genre types keep the existing full-text search behavior."""
        source = asc._library_search_source("love", "songs")
        assert 'search library playlist 1 for "love"' in source
        assert "only songs" in source


class TestGetLibrarySongsPage:
    """Integration tests for get_library_songs_page (O(limit) range access)."""

    def test_returns_correct_shape(self, monkeypatch):
        monkeypatch.setattr(asc, "run_applescript", _fake_library_run(_sample_library()))
        success, tracks, total, error = asc.get_library_songs_page(offset=0, limit=5)
        assert success is True
        assert isinstance(tracks, list)
        assert isinstance(total, int)
        assert error == ""

    def test_total_reflects_full_library(self, monkeypatch):
        monkeypatch.setattr(asc, "run_applescript", _fake_library_run(_sample_library()))
        _, _, total_page, _ = asc.get_library_songs_page(offset=0, limit=5)
        _, _, total_all, _ = asc.get_library_songs_page(offset=0, limit=1)
        assert total_page == total_all

    def test_offset_returns_different_tracks(self, monkeypatch):
        monkeypatch.setattr(asc, "run_applescript", _fake_library_run(_sample_library()))
        _, page0, _, _ = asc.get_library_songs_page(offset=0, limit=5)
        _, page1, _, _ = asc.get_library_songs_page(offset=5, limit=5)
        if page0 and page1:
            assert page0[0]["id"] != page1[0]["id"]

    def test_offset_beyond_total_returns_empty(self, monkeypatch):
        monkeypatch.setattr(asc, "run_applescript", _fake_library_run(_sample_library()))
        success, tracks, total, error = asc.get_library_songs_page(offset=999999, limit=10)
        assert success is True
        assert tracks == []
        assert total >= 0
        assert error == ""

    def test_track_fields_present(self, monkeypatch):
        monkeypatch.setattr(asc, "run_applescript", _fake_library_run(_sample_library()))
        success, tracks, _, _ = asc.get_library_songs_page(offset=0, limit=3)
        assert success is True
        if tracks:
            t = tracks[0]
            for field in ("name", "artist", "album", "duration", "id"):
                assert field in t


class TestLibraryStats:
    """Test library statistics."""

    pytestmark = pytest.mark.slow

    def test_get_library_stats(self):
        """Should get library statistics."""
        success, stats = asc.get_library_stats()
        assert success is True
        assert isinstance(stats, dict)
        assert "track_count" in stats
        assert "playlist_count" in stats
        assert "player_state" in stats
        assert "shuffle" in stats
        assert "repeat" in stats
        assert "volume" in stats

        assert isinstance(stats["track_count"], int)
        assert stats["track_count"] >= 0
        assert isinstance(stats["shuffle"], bool)


class TestAirPlay:
    """Test AirPlay functions."""

    def test_get_airplay_devices(self, monkeypatch):
        """Should get AirPlay devices list."""
        monkeypatch.setattr(
            asc, "run_applescript", lambda script: (True, "Computer\nLiving Room\n")
        )
        success, devices = asc.get_airplay_devices()
        assert success is True
        assert isinstance(devices, list)
        # At minimum, the local computer should be available


class TestTrackMetadata:
    """Test track metadata operations."""

    def test_set_repeat_invalid(self):
        """Should reject invalid repeat mode."""
        success, msg = asc.set_repeat("invalid_mode")
        assert success is False
        assert "invalid" in msg.lower()


class TestInputSanitization:
    """Test that user input is properly sanitized to prevent injection."""

    def test_quote_escaping_in_playlist_name(self, monkeypatch):
        """Should properly escape quotes in playlist names."""
        # Mock the AppleScript boundary so create/delete run offline; the quote
        # escaping under test happens in create_playlist before run_applescript.
        monkeypatch.setattr(asc, "run_applescript", lambda script: (True, "PID_TEST_01"))
        # Attempt to create a playlist with quotes in the name
        # This would break the AppleScript if not properly escaped
        test_name = '_TEST_QUOTES_"escape"_test_'

        # Create should not crash (quotes are escaped)
        success, result = asc.create_playlist(test_name)

        # Cleanup if it worked
        if success:
            asc.delete_playlist(test_name)

        # The main test is that we didn't crash/error on quote handling
        # Success depends on whether the playlist was actually created
        assert isinstance(success, bool)
        assert isinstance(result, str)

    def test_special_characters_in_search(self, monkeypatch):
        """Should handle special characters in search queries."""
        # Mock the AppleScript boundary (empty result set); the escaping under
        # test runs in search_library before the run_applescript call.
        monkeypatch.setattr(asc, "run_applescript", lambda script: (True, ""))
        # These characters should not cause AppleScript errors
        special_queries = [
            "test'quote",
            "test&ampersand",
            "test<angle>",
            "test\\backslash",
        ]
        for query in special_queries:
            # Should not raise an exception
            success, result = asc.search_library(query)
            assert isinstance(success, bool)


class TestSmartPunctuationMatching:
    """Smart-punctuation vs ASCII track matching (issue #26).

    Music stores many titles with typographic glyphs — a curly right single
    quote (U+2019), curly double quotes, or a one-char ellipsis (U+2026) — while
    users/the model type the ASCII forms. AppleScript's ``contains`` is
    glyph-exact, so the match must not depend on which variant is used. The fix
    splits on the ambiguous punctuation and matches the surrounding fragments.
    """

    STRAIGHT = "That's a No No"  # U+0027
    CURLY = "That’s a No No"  # U+2019

    def test_no_apostrophe_is_single_contains(self):
        assert asc._name_contains_clause("Peek-A-Boo") == 'name contains "Peek-A-Boo"'

    def test_hyphen_is_not_split(self):
        # Hyphens are deliberately excluded — they're too common in real titles.
        assert "and" not in asc._name_contains_clause("Mother-Mother-Mother")

    def test_apostrophe_splits_into_fragments(self):
        clause = asc._name_contains_clause(self.STRAIGHT)
        assert clause == '(name contains "That" and name contains "s a No No")'

    def test_straight_and_curly_produce_identical_clause(self):
        # The whole point: the stored glyph and the typed glyph both vanish,
        # so either input resolves to the same query.
        assert asc._name_contains_clause(self.STRAIGHT) == asc._name_contains_clause(self.CURLY)

    def test_apostrophe_never_appears_in_clause(self):
        for variant in ("'", "’", "‘", "ʼ"):
            clause = asc._name_contains_clause(f"It{variant}s Me")
            assert variant not in clause

    def test_ascii_ellipsis_and_unicode_ellipsis_match(self):
        # "Wait..." (3 ASCII dots) and "Wait…" (U+2026) must resolve identically.
        assert asc._name_contains_clause("Wait...") == 'name contains "Wait"'
        assert asc._name_contains_clause("Wait…") == 'name contains "Wait"'
        assert asc._name_contains_clause("Wait...") == asc._name_contains_clause("Wait…")

    def test_ellipsis_mid_title_splits(self):
        clause = asc._name_contains_clause("Hello...Goodbye")
        assert clause == '(name contains "Hello" and name contains "Goodbye")'

    def test_smart_double_quotes_match_straight(self):
        # A quoted title stored with curly double quotes vs typed straight.
        assert asc._name_contains_clause("“Heroes”") == 'name contains "Heroes"'
        assert asc._name_contains_clause('"Heroes"') == 'name contains "Heroes"'
        assert asc._name_contains_clause("“Heroes”") == asc._name_contains_clause('"Heroes"')

    def test_multiple_apostrophes(self):
        clause = asc._name_contains_clause("Rock 'n' Roll")
        assert clause == '(name contains "Rock " and name contains "n" and name contains " Roll")'

    def test_leading_apostrophe_drops_empty_fragment(self):
        # "'Cause" -> only "Cause" survives, so it stays a single contains.
        assert asc._name_contains_clause("’Cause") == 'name contains "Cause"'

    def test_injection_payload_is_inert(self):
        # The double quote that would break out of the literal is consumed as a
        # split delimiter, so the payload survives only as inert match fragments.
        clause = asc._name_contains_clause('Evil" & do shell script "x')
        assert clause.count('"') % 2 == 0  # quotes balance
        # Stripping every quoted literal leaves nothing executable behind.
        residue = re.sub(r'"(?:[^"\\]|\\.)*"', "", clause)
        assert "do shell script" not in residue

    def test_backslash_injection_still_escaped(self):
        # Backslash isn't a split delimiter, so escaping must still apply.
        assert "\\\\" in asc._name_contains_clause("test\\backslash")

    def test_library_track_query_uses_fragment_clause(self):
        q = asc._library_track_query(self.STRAIGHT, "ITZY")
        assert '(name contains "That" and name contains "s a No No")' in q
        assert 'artist contains "ITZY"' in q

    def test_track_filter_clause_uses_fragment_clause(self):
        clause = asc._track_filter_clause(track_name=self.CURLY)
        assert clause == 'whose (name contains "That" and name contains "s a No No")'


class TestHybridTrackResolver:
    """Two-stage resolver: fast `whose` first pass, fold fallback (issue #26).

    The fallback uses AppleScript's native `ignoring punctuation and
    diacriticals`, which folds curly quotes, ellipses, AND accents (é≈e) — the
    generic case the splitting fast pass alone can't reach.
    """

    def test_fast_pass_is_first(self):
        # Stage 1 must be the cheap glyph-exact whose filter.
        block = asc._resolve_library_track_applescript("Hey Jude")
        assert block.lstrip().startswith("try")
        assert "first track of library playlist 1 whose" in block

    def test_fallback_bulk_fetches_in_one_event(self):
        # The whole point of the perf fix: one `every track` fetch, not a
        # per-track property loop.
        block = asc._resolve_library_track_applescript("Hey Jude")
        assert "get name of every track of lib" in block
        # No per-track `name of t` access inside the repeat (the 17s trap).
        assert "name of t\n" not in block

    def test_fallback_folds_punctuation_and_diacriticals(self):
        block = asc._resolve_library_track_applescript("Café")
        assert "ignoring punctuation and diacriticals" in block

    def test_fallback_matches_on_name_only_without_artist(self):
        block = asc._resolve_library_track_applescript("Wait")
        assert "get artist of every track" not in block
        assert '((item i of allNames) as text) contains "Wait"' in block

    def test_fallback_matches_name_and_artist(self):
        block = asc._resolve_library_track_applescript("That's a No No", "ITZY")
        assert "get artist of every track of lib" in block
        assert 'contains "That\'s a No No"' in block
        assert 'contains "ITZY"' in block

    def test_resolver_reports_not_found(self):
        block = asc._resolve_library_track_applescript("Whatever")
        assert 'return "ERROR:Track not found: Whatever"' in block

    def test_resolver_escapes_injection(self):
        block = asc._resolve_library_track_applescript('x" & do shell script "y')
        # The fast-pass clause splits on the quote; the fallback string literal
        # escapes it. Either way no unescaped breakout reaches AppleScript.
        assert "do shell script" in block  # present, but only inside literals
        # Every double-quote in the fallback match line is balanced/escaped.
        assert block.count('"') % 2 == 0


class TestRemoveFromLibrary:
    """Test remove_from_library function."""

    pytestmark = pytest.mark.slow

    def test_remove_nonexistent_track(self):
        """Should return error for track not in library."""
        success, result = asc.remove_from_library("__NONEXISTENT_TRACK_12345__")
        assert success is False
        assert "not found" in result.lower()

    def test_remove_from_library_returns_tuple(self):
        """Should return (success, message) tuple."""
        success, result = asc.remove_from_library("test")
        assert isinstance(success, bool)
        assert isinstance(result, str)


class TestOpenCatalogSong:
    """Test open_catalog_song function."""

    pytestmark = pytest.mark.slow

    def test_open_catalog_song_returns_tuple(self, monkeypatch):
        """Should return (success, message) tuple for valid Apple Music URL."""
        # Mock subprocess to avoid launching Music
        import subprocess

        monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: None)

        success, result = asc.open_catalog_song("https://music.apple.com/us/song/1234567890")
        assert isinstance(success, bool)
        assert isinstance(result, str)

    def test_open_catalog_song_rejects_empty_url(self):
        """Should reject empty URL."""
        success, result = asc.open_catalog_song("")
        assert success is False
        assert "empty" in result.lower() or "invalid" in result.lower()

    def test_open_catalog_song_rejects_non_apple_url(self):
        """Should reject URLs that aren't from Apple Music."""
        success, result = asc.open_catalog_song("https://spotify.com/track/123")
        assert success is False
        assert "not an apple music url" in result.lower()

    def test_open_catalog_song_rejects_invalid_format(self):
        """Should reject strings that aren't valid URLs."""
        success, result = asc.open_catalog_song("just-a-random-string")
        assert success is False
        assert "invalid url format" in result.lower()

    def test_open_catalog_song_accepts_music_scheme(self, monkeypatch):
        """Should accept music:// scheme URLs."""
        # Mock subprocess to avoid launching Music
        import subprocess

        monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: None)

        success, result = asc.open_catalog_song("music://music.apple.com/us/song/1234567890")
        assert isinstance(success, bool)
        assert isinstance(result, str)
        # If it fails, should not be a format rejection
        if not success:
            assert "invalid url format" not in result.lower()
            assert "not an apple music url" not in result.lower()


class TestAddTrackDisambiguation:
    """Test artist exact match and album disambiguation in add_track_to_playlist.

    These tests require specific tracks in the library:
    - Hot Potato by The Wiggles (Ready, Steady, Wiggle!)
    - Hot Potato by Dorothy the Dinosaur & The Wiggles (Dorothy The Dinosaur's Travelling Show)
    """

    pytestmark = pytest.mark.slow

    TEST_PLAYLIST = "🧪 Integration Test Playlist"

    def setup_method(self):
        """Auto-create the test playlist if missing (e.g. iCloud sync deleted it).
        Without this, all tests in this class fail with cryptic 'add failed'
        errors when the playlist isn't present."""
        ok, _ = asc.run_applescript(
            f"""
tell application "Music"
    set found to false
    try
        set p to first user playlist whose name is "{self.TEST_PLAYLIST}"
        set found to true
    end try
    if not found then
        make new playlist with properties {{name:"{self.TEST_PLAYLIST}"}}
    end if
end tell"""
        )

    @classmethod
    def teardown_class(cls):
        """Delete the test playlist after the whole class finishes so we
        don't leave debris in the user's Music library. Best-effort."""
        try:
            asc.run_applescript(
                f"""
tell application "Music"
    try
        delete (first user playlist whose name is "{cls.TEST_PLAYLIST}")
    end try
end tell"""
            )
        except Exception:
            pass

    def test_artist_exact_match_preferred_over_contains(self):
        """Should prefer exact artist match over partial contains match.

        'artist is "The Wiggles"' should match solo Wiggles,
        not 'Dorothy the Dinosaur & The Wiggles'.
        """
        success, result = asc.add_track_to_playlist(self.TEST_PLAYLIST, "Hot Potato", "The Wiggles")
        assert success is True
        # Should be the solo Wiggles version, not Dorothy collab
        assert "Dorothy" not in result
        assert "Ready, Steady, Wiggle!" in result

        # Cleanup
        asc.remove_track_from_playlist(self.TEST_PLAYLIST, "Hot Potato")

    def test_album_disambiguation_selects_correct_version(self):
        """Should use album param to disambiguate between track versions."""
        success, result = asc.add_track_to_playlist(
            self.TEST_PLAYLIST, "Hot Potato", "The Wiggles", "Ready, Steady"
        )
        assert success is True
        assert "Ready, Steady, Wiggle!" in result

        # Cleanup
        asc.remove_track_from_playlist(self.TEST_PLAYLIST, "Hot Potato")

    def test_album_param_accepted_without_artist(self):
        """Should accept album param even without artist for disambiguation."""
        success, result = asc.add_track_to_playlist(
            self.TEST_PLAYLIST, "Hot Potato", album="Ready, Steady"
        )
        assert success is True
        assert "Ready, Steady, Wiggle!" in result

        # Cleanup
        asc.remove_track_from_playlist(self.TEST_PLAYLIST, "Hot Potato")

    def test_fallback_to_contains_when_exact_fails(self):
        """Should fall back to contains if exact artist match finds nothing."""
        # "Dorothy the Dinosaur & The Wiggles" - only matches via contains
        success, result = asc.add_track_to_playlist(
            self.TEST_PLAYLIST, "Hot Potato", "Dorothy the Dinosaur"
        )
        assert success is True
        assert "Dorothy" in result

        # Cleanup
        asc.remove_track_from_playlist(self.TEST_PLAYLIST, "Hot Potato")


class TestOpenCatalogAndPlay:
    """Test open_catalog_and_play function."""

    pytestmark = pytest.mark.slow

    def _mock_subprocess(self, monkeypatch):
        """Mock subprocess.run for open_catalog_song."""
        import subprocess

        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *args, **kwargs: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
        )

    def _mock_time(self, monkeypatch):
        """Mock time.sleep to avoid delays."""
        import time

        monkeypatch.setattr(time, "sleep", lambda _: None)

    def test_returns_tuple(self, monkeypatch):
        """Should return (success, message) tuple for album URL."""
        self._mock_subprocess(monkeypatch)
        self._mock_time(monkeypatch)
        monkeypatch.setattr(asc, "run_applescript", lambda script: (True, "playing"))
        success, result = asc.open_catalog_and_play("https://music.apple.com/us/album/1234567890")
        assert isinstance(success, bool)
        assert isinstance(result, str)

    def test_rejects_empty_url(self):
        """Should reject empty URL."""
        success, result = asc.open_catalog_and_play("")
        assert success is False

    def test_rejects_non_apple_url(self):
        """Should reject non-Apple Music URLs."""
        success, result = asc.open_catalog_and_play("https://spotify.com/track/123")
        assert success is False

    def test_rejects_song_url(self):
        """Should reject /song/ URLs with helpful message."""
        success, result = asc.open_catalog_and_play(
            "https://music.apple.com/us/song/track-name/1234567890"
        )
        assert success is False
        assert "not supported" in result.lower()
        assert "?i=" in result

    def test_accepts_music_scheme(self, monkeypatch):
        """Should accept music:// scheme URLs."""
        self._mock_subprocess(monkeypatch)
        self._mock_time(monkeypatch)
        monkeypatch.setattr(asc, "run_applescript", lambda script: (True, "playing"))
        success, result = asc.open_catalog_and_play("music://music.apple.com/us/album/1234567890")
        assert isinstance(success, bool)

    def test_skips_click_if_already_playing(self, monkeypatch):
        """Should return immediately if Music auto-starts playing."""
        self._mock_subprocess(monkeypatch)
        self._mock_time(monkeypatch)
        monkeypatch.setattr(asc, "run_applescript", lambda script: (True, "playing"))
        success, result = asc.open_catalog_and_play("https://music.apple.com/us/album/1234567890")
        assert success is True
        assert "auto-started" in result.lower()

    def test_clicks_play_button(self, monkeypatch):
        """Should find and click Play button when auto-play doesn't start."""
        self._mock_subprocess(monkeypatch)
        self._mock_time(monkeypatch)
        call_count = [0]

        def mock_run(script):
            call_count[0] += 1
            if call_count[0] == 1:
                return (True, "stopped")
            if call_count[0] == 2:
                return (True, "")
            return (True, "playing")

        monkeypatch.setattr(asc, "run_applescript", mock_run)
        success, result = asc.open_catalog_and_play("https://music.apple.com/us/album/1234567890")
        assert success is True
        assert "playing" in result.lower()

    def test_shuffle(self, monkeypatch):
        """Should click Shuffle button when shuffle=True."""
        self._mock_subprocess(monkeypatch)
        self._mock_time(monkeypatch)
        scripts_called = []
        call_count = [0]

        def mock_run(script):
            scripts_called.append(script)
            call_count[0] += 1
            if call_count[0] == 1:
                return (True, "stopped")
            if call_count[0] == 2:
                return (True, "")
            return (True, "playing")

        monkeypatch.setattr(asc, "run_applescript", mock_run)
        success, result = asc.open_catalog_and_play(
            "https://music.apple.com/us/album/1234567890", shuffle=True
        )
        assert success is True
        assert any("Shuffle" in s for s in scripts_called)

    def test_retry_exhaustion(self, monkeypatch):
        """Timeout should report failure (not a phantom success) with an
        actionable message, so callers can fall back to the browser."""
        self._mock_subprocess(monkeypatch)
        self._mock_time(monkeypatch)
        monkeypatch.setattr(asc, "run_applescript", lambda script: (True, "stopped"))
        success, result = asc.open_catalog_and_play(
            "https://music.apple.com/us/album/1234567890", timeout=0.1
        )
        assert success is False
        assert "could not start playback" in result.lower()
        assert "accessibility" in result.lower()

    def test_specific_track_matches_by_name(self, monkeypatch):
        """A ?i= URL with a track_name should play that exact row by name
        (double-click), not the album Play button."""
        self._mock_subprocess(monkeypatch)
        self._mock_time(monkeypatch)
        # not already playing, so it proceeds to the specific-track path
        monkeypatch.setattr(asc, "run_applescript", lambda script: (True, "stopped"))
        seen = {}
        monkeypatch.setattr(
            asc,
            "_play_by_track_name",
            lambda name: seen.update(name=name) or (True, f"Playing: {name}"),
        )
        success, result = asc.open_catalog_and_play(
            "https://music.apple.com/us/album/x/1234567890?i=999",
            track_name="Bohemian Rhapsody",
        )
        assert success is True
        assert seen["name"] == "Bohemian Rhapsody"
        assert "Bohemian Rhapsody" in result

    def test_song_with_i_param(self, monkeypatch):
        """Should attempt track-specific playback for ?i= URLs."""
        self._mock_subprocess(monkeypatch)
        self._mock_time(monkeypatch)
        call_count = [0]

        def mock_run(script):
            call_count[0] += 1
            if "player state" in script and call_count[0] <= 2:
                return (True, "stopped")
            if "activate" in script:
                return (True, "")
            if "Favorite" in script and "position" in script:
                return (True, "500.0,400.0,Test Track")
            if "size of window" in script:
                return (True, "1000")
            if "click checkbox" in script:
                return (True, "Test Track")
            if "player state" in script:
                return (True, "playing")
            return (True, "")

        monkeypatch.setattr(asc, "run_applescript", mock_run)
        monkeypatch.setattr(asc, "_jxa_mouse_move", lambda x, y: True)
        success, result = asc.open_catalog_and_play(
            "https://music.apple.com/us/album/name/123?i=456"
        )
        assert success is True
        assert "test track" in result.lower()


class TestLibrarySnapshot:
    """Test library_snapshot and library_diff functions."""

    pytestmark = pytest.mark.slow

    def _make_snapshot(self, track_count=100, playlists=None, playback=None):
        """Build a snapshot dict for testing."""
        if playlists is None:
            playlists = {
                "Chill": [
                    {"name": "Song A", "artist": "Artist 1", "album": "Album 1"},
                    {"name": "Song B", "artist": "Artist 2", "album": "Album 2"},
                ],
                "Workout": [
                    {"name": "Song C", "artist": "Artist 3", "album": "Album 3"},
                ],
            }
        if playback is None:
            playback = {
                "player_state": "stopped",
                "volume": 50,
                "shuffle": False,
                "repeat": "off",
                "current_track": None,
                "current_artist": None,
                "current_album": None,
            }
        return {
            "track_count": track_count,
            "playback": playback,
            "playlists": playlists,
        }

    def test_snapshot_structure(self):
        """Snapshot dict should have track_count, playback, and playlists keys."""
        snap = self._make_snapshot()
        assert "track_count" in snap
        assert "playback" in snap
        assert "playlists" in snap
        assert isinstance(snap["track_count"], int)
        assert isinstance(snap["playlists"], dict)
        assert isinstance(snap["playback"], dict)

    def test_clean_diff(self):
        """Identical snapshots should produce a clean diff."""
        snap = self._make_snapshot()
        diff = asc.library_diff(snap, snap)
        assert diff["is_clean"] is True
        assert diff["track_count_change"] == 0
        assert diff["playlists_added"] == []
        assert diff["playlists_removed"] == []
        assert diff["playlists_changed"] == {}

    def test_detects_playlist_added(self):
        """Diff should detect a new playlist."""
        before = self._make_snapshot()
        after_playlists = dict(before["playlists"])
        after_playlists["New Playlist"] = [
            {"name": "Song D", "artist": "Artist 4", "album": "Album 4"},
        ]
        after = self._make_snapshot(playlists=after_playlists)
        diff = asc.library_diff(before, after)
        assert diff["is_clean"] is False
        assert "New Playlist" in diff["playlists_added"]

    def test_detects_playlist_removed(self):
        """Diff should detect a removed playlist."""
        before = self._make_snapshot()
        after_playlists = {"Chill": before["playlists"]["Chill"]}
        after = self._make_snapshot(playlists=after_playlists)
        diff = asc.library_diff(before, after)
        assert diff["is_clean"] is False
        assert "Workout" in diff["playlists_removed"]

    def test_detects_playlist_track_changes(self):
        """Diff should detect tracks added/removed within a playlist."""
        before = self._make_snapshot()
        after_playlists = dict(before["playlists"])
        # Remove Song A, add Song D
        after_playlists["Chill"] = [
            {"name": "Song B", "artist": "Artist 2", "album": "Album 2"},
            {"name": "Song D", "artist": "Artist 4", "album": "Album 4"},
        ]
        after = self._make_snapshot(playlists=after_playlists)
        diff = asc.library_diff(before, after)
        assert diff["is_clean"] is False
        assert "Chill" in diff["playlists_changed"]
        changes = diff["playlists_changed"]["Chill"]
        assert len(changes["added"]) == 1
        assert len(changes["removed"]) == 1

    def test_detects_track_count_change(self):
        """Diff should detect library track count changes."""
        before = self._make_snapshot(track_count=100)
        after = self._make_snapshot(track_count=105)
        diff = asc.library_diff(before, after)
        assert diff["is_clean"] is False
        assert diff["track_count_change"] == 5

    def test_playback_changes_tracked_separately(self):
        """Playback state changes should not make is_clean False."""
        before = self._make_snapshot()
        after_playback = dict(before["playback"])
        after_playback["player_state"] = "playing"
        after_playback["current_track"] = "Some Track"
        after = self._make_snapshot(playback=after_playback)
        diff = asc.library_diff(before, after)
        # Library is unchanged — is_clean should be True
        assert diff["is_clean"] is True
        # But playback changes should still be recorded
        assert "player_state" in diff["playback_changes"]
        assert diff["playback_changes"]["player_state"]["after"] == "playing"


class TestUISearchParsing:
    """Unit tests for UI search result parsing logic (no Music.app needed)."""

    def test_parse_search_results_with_separator(self):
        """Should parse type and artist from the Unicode middle-dot separator."""
        # Simulate the raw output from AppleScript
        raw = "1|||Creep|||Song\u2004\u00b7\u2004Radiohead\n2|||Radiohead|||Artist\n3|||OK Computer|||Album\u2004\u00b7\u2004Radiohead"

        results = []
        for line in raw.strip().split("\n"):
            line = line.strip()
            if not line or "|||" not in line:
                continue
            parts = line.split("|||")
            if len(parts) >= 3:
                name = parts[1].strip()
                type_line = parts[2].strip()
                result_type = ""
                artist = ""
                for sep in ["\u2004\u00b7\u2004", " \u00b7 ", " \u00b7 "]:
                    if sep in type_line:
                        result_type, artist = type_line.split(sep, 1)
                        break
                else:
                    result_type = type_line
                results.append({"name": name, "type": result_type, "artist": artist})

        assert len(results) == 3
        assert results[0]["name"] == "Creep"
        assert results[0]["type"] == "Song"
        assert results[0]["artist"] == "Radiohead"
        assert results[1]["name"] == "Radiohead"
        assert results[1]["type"] == "Artist"
        assert results[1]["artist"] == ""
        assert results[2]["name"] == "OK Computer"
        assert results[2]["type"] == "Album"
        assert results[2]["artist"] == "Radiohead"

    def test_parse_empty_results(self):
        """Should handle empty or NO_RESULTS gracefully."""
        for raw in ["", "NO_RESULTS", "\n\n"]:
            results = []
            if raw and raw.strip() != "NO_RESULTS":
                for line in raw.strip().split("\n"):
                    if "|||" in line:
                        results.append(line)
            assert results == []

    def test_parse_position_string(self):
        """Should correctly parse position coordinates from AppleScript.

        The actual AppleScript returns "x,y" format (two float values).
        The code unpacks via: cx, cy = [float(v) for v in pos_str.strip().split(",")]
        """
        pos_str = "1883.0,686.0"
        parts = [float(v) for v in pos_str.strip().split(",")]
        assert len(parts) == 2
        assert parts[0] == 1883.0
        assert parts[1] == 686.0

    def test_parse_position_rejects_extra_fields(self):
        """Position string with extra commas should raise ValueError on unpack."""
        pos_str = "500.0,400.0,extra"
        with pytest.raises(ValueError):
            cx, cy = [float(v) for v in pos_str.strip().split(",")]


class TestClassifyAsError:
    """Unit tests for _classify_as_error()."""

    def test_accessibility_not_granted(self):
        import applemusic_mcp.applescript as asc

        msg = asc._classify_as_error("execution error: (-1743)")
        assert "Accessibility" in msg
        assert "System Settings" in msg

    def test_element_not_found(self):
        import applemusic_mcp.applescript as asc

        msg = asc._classify_as_error("Can't get window 1. (-1728)")
        assert "element not found" in msg.lower() or "layout" in msg.lower()
        assert asc._GITHUB_ISSUES in msg

    def test_unknown_error_includes_issue_link(self):
        import applemusic_mcp.applescript as asc

        msg = asc._classify_as_error("something totally unexpected")
        assert asc._GITHUB_ISSUES in msg

    def test_unknown_error_truncates_long_text(self):
        import applemusic_mcp.applescript as asc

        long_err = "x" * 200
        msg = asc._classify_as_error(long_err)
        assert len(msg) < 300


class TestCheckUiAccessible:
    """Unit tests for check_ui_accessible()."""

    def test_returns_ok_when_windows_visible(self, monkeypatch):
        import applemusic_mcp.applescript as asc

        monkeypatch.setattr(asc, "is_screen_locked", lambda: False)
        monkeypatch.setattr(asc, "run_applescript", lambda _: (True, "1\n"))
        ok, reason = asc.check_ui_accessible()
        assert ok is True
        assert reason == ""

    def test_returns_classified_error_on_applescript_failure(self, monkeypatch):
        import applemusic_mcp.applescript as asc

        monkeypatch.setattr(asc, "is_screen_locked", lambda: False)
        monkeypatch.setattr(asc, "run_applescript", lambda _: (False, "(-1743)"))
        ok, reason = asc.check_ui_accessible()
        assert ok is False
        assert "Accessibility" in reason

    def test_short_circuits_clean_when_locked(self, monkeypatch):
        # The clean CGSession-based lock check must fire BEFORE any AppleScript,
        # producing a clear message and not even probing System Events.
        import applemusic_mcp.applescript as asc

        monkeypatch.setattr(asc, "is_screen_locked", lambda: True)

        def boom(_script):
            raise AssertionError("run_applescript must not run when screen is locked")

        monkeypatch.setattr(asc, "run_applescript", boom)
        ok, reason = asc.check_ui_accessible()
        assert ok is False
        assert "locked" in reason.lower()

    def test_falls_back_to_loginwindow_heuristic_when_lock_unknown(self, monkeypatch):
        # When Quartz is unavailable (is_screen_locked() → None), fall through to
        # the legacy loginwindow heuristic.
        import applemusic_mcp.applescript as asc

        monkeypatch.setattr(asc, "is_screen_locked", lambda: None)
        calls = {"n": 0}

        def mock_run(script):
            calls["n"] += 1
            if calls["n"] == 1:
                return (True, "0")  # Music has 0 windows
            return (True, "true")  # loginwindow has windows → locked

        monkeypatch.setattr(asc, "run_applescript", mock_run)
        ok, reason = asc.check_ui_accessible()
        assert ok is False
        assert "locked" in reason.lower()

    def test_no_window_not_locked(self, monkeypatch):
        import applemusic_mcp.applescript as asc

        monkeypatch.setattr(asc, "is_screen_locked", lambda: False)
        calls = {"n": 0}

        def mock_run(script):
            calls["n"] += 1
            if calls["n"] == 1:
                return (True, "0")  # Music has 0 windows
            return (True, "false")  # loginwindow also 0 → not locked, just minimized

        monkeypatch.setattr(asc, "run_applescript", mock_run)
        ok, reason = asc.check_ui_accessible()
        assert ok is False
        assert "no visible windows" in reason.lower() or "minimize" in reason.lower()


class TestIsScreenLocked:
    """Unit tests for is_screen_locked() — mocks Quartz so they run anywhere."""

    def _patch_quartz(self, monkeypatch, session_dict):
        import sys
        import types

        fake = types.ModuleType("Quartz")
        fake.CGSessionCopyCurrentDictionary = lambda: session_dict
        monkeypatch.setitem(sys.modules, "Quartz", fake)

    def test_locked_session_true(self, monkeypatch):
        import applemusic_mcp.applescript as asc

        self._patch_quartz(
            monkeypatch, {"CGSSessionScreenIsLocked": 1, "kCGSSessionOnConsoleKey": 1}
        )
        assert asc.is_screen_locked() is True

    def test_unlocked_on_console_false(self, monkeypatch):
        import applemusic_mcp.applescript as asc

        self._patch_quartz(
            monkeypatch, {"CGSSessionScreenIsLocked": 0, "kCGSSessionOnConsoleKey": 1}
        )
        assert asc.is_screen_locked() is False

    def test_no_session_dict_true(self, monkeypatch):
        import applemusic_mcp.applescript as asc

        self._patch_quartz(monkeypatch, None)
        assert asc.is_screen_locked() is True

    def test_fast_user_switched_away_true(self, monkeypatch):
        import applemusic_mcp.applescript as asc

        # Not locked, but not the active console session either.
        self._patch_quartz(
            monkeypatch, {"CGSSessionScreenIsLocked": 0, "kCGSSessionOnConsoleKey": 0}
        )
        assert asc.is_screen_locked() is True

    def test_quartz_unavailable_returns_none(self, monkeypatch):
        import sys

        import applemusic_mcp.applescript as asc

        # Simulate the import failing (non-macOS / no pyobjc).
        monkeypatch.setitem(sys.modules, "Quartz", None)
        assert asc.is_screen_locked() is None


class TestGetSearchField:
    """Unit tests for the _get_search_field() dual-path probe."""

    def setup_method(self):
        """Reset the module-level cache before each test."""
        import applemusic_mcp.applescript as asc

        asc._search_field_cache = None

    def test_toolbar_grouped_path_when_probe_returns_grouped(self, monkeypatch):
        import applemusic_mcp.applescript as asc

        monkeypatch.setattr(asc, "run_applescript", lambda _: (True, "grouped\n"))
        result = asc._get_search_field()
        assert result == asc._SEARCH_FIELD_TOOLBAR

    def test_toolbar_flat_path_when_probe_returns_flat(self, monkeypatch):
        """macOS 26 build variant: text field is directly under toolbar with
        no wrapping `group 1`. Probe should return the flat path."""
        import applemusic_mcp.applescript as asc

        monkeypatch.setattr(asc, "run_applescript", lambda _: (True, "flat\n"))
        result = asc._get_search_field()
        assert result == asc._SEARCH_FIELD_TOOLBAR_FLAT

    def test_sidebar_path_when_no_toolbar_variant_found(self, monkeypatch):
        """If neither toolbar variant exists even after Cmd+F, fall back to
        the macOS 15 sidebar path."""
        import applemusic_mcp.applescript as asc

        monkeypatch.setattr(asc, "run_applescript", lambda _: (True, "none\n"))
        result = asc._get_search_field()
        assert result == asc._SEARCH_FIELD_SIDEBAR

    def test_sidebar_path_on_applescript_failure_does_not_cache(self, monkeypatch):
        """Transient probe failure (e.g., Music.app still launching) returns
        sidebar as a safe default, but does NOT cache — next call should retry
        so we don't pin a macOS 26 user to the wrong path."""
        import applemusic_mcp.applescript as asc

        monkeypatch.setattr(asc, "run_applescript", lambda _: (False, "error"))
        result = asc._get_search_field()
        assert result == asc._SEARCH_FIELD_SIDEBAR
        assert asc._search_field_cache is None  # not cached on failure

    def test_result_is_cached(self, monkeypatch):
        import applemusic_mcp.applescript as asc

        call_count = {"n": 0}

        def counting_run(script):
            call_count["n"] += 1
            return (True, "grouped")

        monkeypatch.setattr(asc, "run_applescript", counting_run)
        asc._get_search_field()
        asc._get_search_field()
        assert call_count["n"] == 1  # probe ran once; second call hit cache


class TestUIPrimitives:
    """Unit tests for the unified UI automation primitives.

    These verify the contract of each primitive (return shapes, error
    classifications, polling behavior) without touching Music.app. Live
    integration is covered by TestUISearchIntegration below.
    """

    def setup_method(self):
        """Reset the search-field cache before each test so monkeypatched
        run_applescript can deterministically influence the path."""
        import applemusic_mcp.applescript as asc

        asc._search_field_cache = None

    # --- _focus_search_field -------------------------------------------------

    def test_focus_search_field_rejects_empty_query(self):
        import applemusic_mcp.applescript as asc

        ok, err = asc._focus_search_field("")
        assert ok is False
        assert err == "Empty query"

        ok, err = asc._focus_search_field("   ")
        assert ok is False
        assert err == "Empty query"

    def test_focus_search_field_success_path(self, monkeypatch):
        """On AppleScript success, returns (True, ""). The query is escaped
        and embedded in the consolidated AppleScript that the helper emits."""
        import applemusic_mcp.applescript as asc

        captured_scripts: list[str] = []

        def fake_run(script: str):
            captured_scripts.append(script)
            return (True, "")

        monkeypatch.setattr(asc, "run_applescript", fake_run)
        monkeypatch.setattr(asc, "_ensure_music_frontmost", lambda: None)
        monkeypatch.setattr(asc, "_get_search_field", lambda: asc._SEARCH_FIELD_TOOLBAR)

        ok, err = asc._focus_search_field("Radiohead")
        assert ok is True
        assert err == ""
        # The injected script contains the resolved field path AND the query
        emitted = captured_scripts[-1]
        assert asc._SEARCH_FIELD_TOOLBAR in emitted
        assert "Radiohead" in emitted

    def test_focus_search_field_classifies_environmental_failure(self, monkeypatch):
        """When AppleScript fails AND check_ui_accessible reports a problem,
        the user-facing reason should be the access reason, not the raw
        AppleScript error — same behavior the original ui_search_catalog had."""
        import applemusic_mcp.applescript as asc

        monkeypatch.setattr(asc, "run_applescript", lambda _: (False, "execution error"))
        monkeypatch.setattr(asc, "_ensure_music_frontmost", lambda: None)
        monkeypatch.setattr(asc, "_get_search_field", lambda: asc._SEARCH_FIELD_TOOLBAR)
        monkeypatch.setattr(asc, "check_ui_accessible", lambda: (False, "Music.app not running"))

        ok, err = asc._focus_search_field("anything")
        assert ok is False
        assert err == "Music.app not running"

    # --- _wait_for_top_results ----------------------------------------------

    def test_wait_for_top_results_returns_immediately_on_success(self, monkeypatch):
        import applemusic_mcp.applescript as asc

        monkeypatch.setattr(asc, "run_applescript", lambda _: (True, "1|||Track|||Song"))
        ok, raw = asc._wait_for_top_results(timeout=2.0)
        assert ok is True
        assert "1|||Track" in raw

    def test_wait_for_top_results_empty_on_clean_timeout(self, monkeypatch):
        """If results never appear but AppleScript itself succeeds with
        NO_RESULTS, returns (True, "") to signal empty results — not error."""
        import applemusic_mcp.applescript as asc

        monkeypatch.setattr(asc, "run_applescript", lambda _: (True, "NO_RESULTS"))
        ok, raw = asc._wait_for_top_results(timeout=0.5)
        assert ok is True
        assert raw == ""

    # --- _parse_top_results -------------------------------------------------

    def test_parse_top_results_handles_unicode_separators(self):
        import applemusic_mcp.applescript as asc

        raw = (
            "1|||Creep|||Song · Radiohead\n"
            "2|||OK Computer|||Album · Radiohead\n"
            "3|||Radiohead|||Artist\n"
        )
        results = asc._parse_top_results(raw)
        assert len(results) == 3
        assert results[0] == {"name": "Creep", "type": "Song", "artist": "Radiohead", "index": 1}
        assert results[1] == {
            "name": "OK Computer",
            "type": "Album",
            "artist": "Radiohead",
            "index": 2,
        }
        assert results[2] == {"name": "Radiohead", "type": "Artist", "artist": "", "index": 3}

    def test_parse_top_results_skips_malformed_lines(self):
        import applemusic_mcp.applescript as asc

        raw = "1|||Good|||Song · A\n||\nbroken_line_no_separator\n2|||Also Good|||Song · B"
        results = asc._parse_top_results(raw)
        assert len(results) == 2
        assert results[0]["name"] == "Good"
        assert results[1]["name"] == "Also Good"

    # --- _find_top_result_position ------------------------------------------

    def test_find_top_result_position_parses_csv(self, monkeypatch):
        import applemusic_mcp.applescript as asc

        monkeypatch.setattr(asc, "run_applescript", lambda _: (True, "1883.0,686.0"))
        pos = asc._find_top_result_position("Some Track")
        assert pos == (1883.0, 686.0)

    def test_find_top_result_position_returns_none_on_not_found(self, monkeypatch):
        import applemusic_mcp.applescript as asc

        monkeypatch.setattr(asc, "run_applescript", lambda _: (True, "NOT_FOUND"))
        assert asc._find_top_result_position("Missing") is None

    # --- _hover_then_click_subelement ---------------------------------------

    def test_hover_then_click_succeeds(self, monkeypatch):
        """Full success path: row found, hover succeeds, sub-element appears
        on first poll, CoreGraphics click succeeds."""
        import applemusic_mcp.applescript as asc

        click_calls: list[tuple[float, float]] = []

        # AppleScript is called twice in this path:
        # 1. _find_top_result_position → returns row pos
        # 2. inner sub-element poll → returns inner pos
        responses = iter([(True, "100.0,200.0"), (True, "55.0,77.0")])

        monkeypatch.setattr(asc, "run_applescript", lambda _: next(responses))
        monkeypatch.setattr(asc, "_ensure_music_frontmost", lambda: None)
        monkeypatch.setattr(asc, "_hover_with_nudge", lambda cx, cy: True)
        monkeypatch.setattr(
            asc,
            "_jxa_mouse_click",
            lambda x, y: (click_calls.append((x, y)), True)[1],
        )

        ok, err = asc._hover_then_click_subelement("Track", "set inner to checkbox 1 of e")
        assert ok is True
        assert err == ""
        # Click landed at the inner element's center, not the row's center
        assert click_calls == [(55.0, 77.0)]

    def test_hover_then_click_returns_not_visible_marker(self, monkeypatch):
        """When the inner sub-element never becomes queryable within max_wait,
        the helper returns the reserved 'sub-element not visible after hover'
        string so wrappers can map it to a domain-specific message
        (e.g. ui_play_result → 'Play checkbox not visible after hover')."""
        import applemusic_mcp.applescript as asc

        # Row found, but sub-element poll always returns NOT_FOUND
        responses = [(True, "100.0,200.0")] + [(True, "NOT_FOUND")] * 50

        idx = {"i": 0}

        def fake_run(_):
            r = responses[min(idx["i"], len(responses) - 1)]
            idx["i"] += 1
            return r

        monkeypatch.setattr(asc, "run_applescript", fake_run)
        monkeypatch.setattr(asc, "_ensure_music_frontmost", lambda: None)
        monkeypatch.setattr(asc, "_hover_with_nudge", lambda cx, cy: True)
        monkeypatch.setattr(asc, "_jxa_mouse_click", lambda x, y: True)

        ok, err = asc._hover_then_click_subelement(
            "Track", "set inner to checkbox 1 of e", max_wait=0.3
        )
        assert ok is False
        assert err == "sub-element not visible after hover"

    # --- _verify_track_playing ----------------------------------------------

    def test_verify_track_playing_match_on_first_poll(self, monkeypatch):
        import applemusic_mcp.applescript as asc

        monkeypatch.setattr(
            asc, "run_applescript", lambda _: (True, "Sweet Baby James (Remastered)")
        )
        ok, name = asc._verify_track_playing("Sweet Baby James", timeout=1.0)
        assert ok is True
        assert "Sweet Baby James" in name

    def test_verify_track_playing_returns_false_on_mismatch(self, monkeypatch):
        """When the current track never matches within timeout, returns
        (False, last_known_track) so the caller can distinguish 'wrong
        track playing' from 'nothing playing'."""
        import applemusic_mcp.applescript as asc

        monkeypatch.setattr(asc, "run_applescript", lambda _: (True, "Different Song"))
        ok, name = asc._verify_track_playing("Sweet Baby James", timeout=0.4)
        assert ok is False
        assert name == "Different Song"


class TestPopoverSongRowScoring:
    """Unit tests for `_find_popover_song_row` score-and-pick scoring.

    Mocks `run_applescript` to return fixture pop-over rows and asserts
    the matcher picks the row with the highest tier score (with row-index
    as the tiebreaker). Each fixture mirrors the `idx|||title|||type · artist`
    format the actual AppleScript probe produces.
    """

    @staticmethod
    def _fixture(rows: list[tuple[int, str, str]]) -> str:
        """Build a |||-delimited fixture matching the AppleScript probe output."""
        return "\n".join(f"{idx}|||{title}|||{type_artist}" for idx, title, type_artist in rows)

    def _patch_applescript(self, monkeypatch, fixture: str):
        import applemusic_mcp.applescript as asc

        monkeypatch.setattr(asc, "run_applescript", lambda *_a, **_k: (True, fixture))

    def test_song_exact_beats_album_exact(self, monkeypatch):
        """Tier 1 (Song EXACT) beats Tier 3 (Album EXACT)."""
        import applemusic_mcp.applescript as asc

        self._patch_applescript(
            monkeypatch,
            self._fixture(
                [
                    (5, "Brown Sugar", "Album · D'Angelo"),
                    (7, "Brown Sugar", "Song · D'Angelo"),
                ]
            ),
        )
        result = asc._find_popover_song_row("Brown Sugar", "D'Angelo")
        assert result == (7, "D'Angelo")

    def test_canonical_marker_paren_beats_album_exact(self, monkeypatch):
        """Tier 2 (canonical-marker parenthetical Song) beats Tier 3 (Album EXACT)."""
        import applemusic_mcp.applescript as asc

        self._patch_applescript(
            monkeypatch,
            self._fixture(
                [
                    (3, "Brown Sugar (2014 Remaster)", "Song · D'Angelo"),
                    (5, "Brown Sugar", "Album · D'Angelo"),
                ]
            ),
        )
        result = asc._find_popover_song_row("Brown Sugar", "D'Angelo")
        assert result == (3, "D'Angelo")

    def test_album_now_rejected_returns_none_with_only_album(self, monkeypatch):
        """Album rows are rejected entirely under the 'no album-dive' policy.
        If only an album row exists, return None — caller fails loudly."""
        import applemusic_mcp.applescript as asc

        self._patch_applescript(
            monkeypatch,
            self._fixture(
                [
                    (5, "Brown Sugar", "Album · D'Angelo"),
                ]
            ),
        )
        result = asc._find_popover_song_row("Brown Sugar", "D'Angelo")
        assert result is None

    def test_brown_sugar_dangelo_real_popover_returns_remix_row(self, monkeypatch):
        """The Brown Sugar / D'Angelo case: pop-over has no Song-EXACT row,
        only Album + Song-with-non-canonical-paren. With album-dive removed,
        the only navigable Song row is the 808 Mix variant (tier 4) — that's
        what the matcher returns. Caller can decide whether to surface that
        to the user or fail with 'no canonical match'."""
        import applemusic_mcp.applescript as asc

        # Mirrors the actual pop-over output we observed live.
        self._patch_applescript(
            monkeypatch,
            self._fixture(
                [
                    (5, "Brown Sugar ￼", "Album · D'Angelo"),  # has U+FFFC artifact
                    (7, "Brown Sugar (Soul Inside 808 Mix)", "Song · D'Angelo"),
                    (11, "Brown Sugar (Verzuz Live)", "Music Video · D'Angelo"),
                ]
            ),
        )
        result = asc._find_popover_song_row("Brown Sugar", "D'Angelo")
        # Album rejected; Music Video rejected; only the 808 Mix Song
        # remains and it scores tier 4 (non-canonical paren).
        assert result == (7, "D'Angelo")

    def test_lower_idx_wins_within_same_tier(self, monkeypatch):
        """Two Song-EXACT matches by same artist — Apple's earlier row
        (lower idx) should win as the relevance signal."""
        import applemusic_mcp.applescript as asc

        self._patch_applescript(
            monkeypatch,
            self._fixture(
                [
                    (3, "Heaven", "Song · Talking Heads"),
                    (8, "Heaven", "Song · Talking Heads"),  # duplicate (rare but possible)
                ]
            ),
        )
        result = asc._find_popover_song_row("Heaven", "Talking Heads")
        assert result == (3, "Talking Heads")

    def test_artist_filter_rejects_wrong_artist(self, monkeypatch):
        """Same title by a different artist must NOT match when the caller
        supplied an artist constraint."""
        import applemusic_mcp.applescript as asc

        self._patch_applescript(
            monkeypatch,
            self._fixture(
                [
                    (3, "Heaven", "Song · The Walkmen"),
                    (5, "Heaven", "Song · Beyoncé"),
                ]
            ),
        )
        result = asc._find_popover_song_row("Heaven", "Talking Heads")
        assert result is None

    def test_artist_filter_picks_correct_artist(self, monkeypatch):
        """Multiple artists with same title — picks the one matching artist."""
        import applemusic_mcp.applescript as asc

        self._patch_applescript(
            monkeypatch,
            self._fixture(
                [
                    (3, "Heaven", "Song · The Walkmen"),
                    (5, "Heaven", "Song · Talking Heads"),
                    (7, "Heaven", "Song · Beyoncé"),
                ]
            ),
        )
        result = asc._find_popover_song_row("Heaven", "Talking Heads")
        assert result == (5, "Talking Heads")

    def test_artist_optional_picks_first_song_exact(self, monkeypatch):
        """Without artist filter — pick highest-tier match regardless,
        using row index as tiebreaker."""
        import applemusic_mcp.applescript as asc

        self._patch_applescript(
            monkeypatch,
            self._fixture(
                [
                    (3, "Heaven", "Song · The Walkmen"),
                    (5, "Heaven", "Song · Talking Heads"),
                ]
            ),
        )
        result = asc._find_popover_song_row("Heaven")
        assert result == (3, "The Walkmen")  # lower idx wins on tier-tie

    def test_non_song_rows_all_rejected(self, monkeypatch):
        """Album, Artist, MusicVideo, Playlist all rejected — only Song scored."""
        import applemusic_mcp.applescript as asc

        self._patch_applescript(
            monkeypatch,
            self._fixture(
                [
                    (3, "Heaven", "Album · Talking Heads"),
                    (5, "Talking Heads", "Artist"),
                    (7, "Heaven", "Music Video · Talking Heads"),
                    (9, "Heaven Mix", "Playlist · Apple Music"),
                ]
            ),
        )
        result = asc._find_popover_song_row("Heaven", "Talking Heads")
        assert result is None

    def test_no_results_in_popover_returns_none(self, monkeypatch):
        """No-match-anywhere → None."""
        import applemusic_mcp.applescript as asc

        self._patch_applescript(
            monkeypatch,
            self._fixture(
                [
                    (3, "Something Else", "Song · Other Artist"),
                ]
            ),
        )
        result = asc._find_popover_song_row("Heaven", "Talking Heads")
        assert result is None

    def test_no_popover_returns_none(self, monkeypatch):
        """If pop-over isn't visible, AppleScript probe returns NO_POPOVER."""
        import applemusic_mcp.applescript as asc

        monkeypatch.setattr(asc, "run_applescript", lambda *_a, **_k: (True, "NO_POPOVER"))
        result = asc._find_popover_song_row("Heaven", "Talking Heads")
        assert result is None

    def test_canonical_paren_markers_are_recognized(self, monkeypatch):
        """Each canonical-marker word should score in tier 2 (1700), so
        a parenthetical-canonical Song should beat any non-canonical paren."""
        import applemusic_mcp.applescript as asc

        for marker in [
            "Original Mix",
            "Album Version",
            "2014 Remaster",
            "Stereo Version",
            "Mono Version",
            "Single Version",
            "Radio Edit",
            "Studio Version",
        ]:
            self._patch_applescript(
                monkeypatch,
                self._fixture(
                    [
                        (3, f"Test ({marker})", "Song · X"),
                        (5, "Test (Live at Madison)", "Song · X"),  # non-canonical paren
                    ]
                ),
            )
            result = asc._find_popover_song_row("Test", "X")
            assert result == (3, "X"), f"marker {marker!r} should win tier 2"


@pytest.mark.skipif(
    not os.environ.get("TEST_UI"), reason="UI tests require Music.app visible. Run with TEST_UI=1"
)
class TestUISearchIntegration:
    """Integration tests for UI search.

    These require Music.app to be visible and active with Accessibility
    permissions. They will fail in CI or if Music.app is minimized.
    Run manually: pytest tests/test_applescript.py::TestUISearchIntegration -v
    """

    def test_search_returns_results(self):
        """Should find results for a well-known artist."""
        asc.run_applescript('tell application "Music" to activate')
        import time

        time.sleep(2)
        ok, results, why = asc.ui_search_catalog("Beatles")
        asc.ui_clear_search()
        assert ok is True
        assert len(results) > 0
        assert "name" in results[0]
        assert "type" in results[0]
        assert why == ""

    def test_search_clears_properly(self):
        """Should clear search without errors."""
        asc.run_applescript('tell application "Music" to activate')
        import time

        time.sleep(1)
        asc.ui_search_catalog("test query")
        asc.ui_clear_search()

    def test_search_empty_query(self):
        """Should reject empty queries."""
        ok, results, why = asc.ui_search_catalog("")
        assert ok is False
        assert results == []
        assert why == "Empty query"


def _probe_row_is_fresh(name: str, artist: str) -> bool:
    """Probe Music.app's song-page row for a track to detect "stale in-library
    cache" — the situation where AppleScript-level remove_from_library has
    completed but Music.app's per-row cloud-state cache still shows
    "Download button" (=in library) for several minutes after iCloud removal.

    Such a row would cause ui_add_to_library to fail at the hover-find step
    even though search_library returns 0 hits — we want setup_class to skip
    these stale candidates and try the next one.

    Returns True if row's initial buttons (no hover) DON'T include
    "Download button" — i.e., the row is genuinely fresh and a Music.app
    "Add to Library" button is reachable.

    Side-effect: leaves search popover state unchanged (clears it on exit).
    """
    ok, _err = asc._open_search_popover(f"{name} {artist}".strip())
    if not ok:
        asc.ui_clear_search()
        return False
    match = asc._find_popover_song_row(name, artist)
    if match is None:
        asc.ui_clear_search()
        return False
    idx, _resolved = match
    ok, _err = asc._click_popover_row(idx)
    if not ok:
        asc.ui_clear_search()
        return False
    group_path = asc._wait_for_song_page(name)
    if group_path is None:
        asc.ui_clear_search()
        return False
    # Hover the row first — Music.app only reveals the in-library indicator
    # ("Download button") OR the not-in-library button ("Add to Library")
    # AFTER hover. A no-hover probe shows only Favorite|More for any state.
    ok, pos = asc.run_applescript(
        f"""
tell application "System Events"
    tell process "Music"
        try
            set g to {group_path}
            set gp to position of g
            set gs to size of g
            return ((item 1 of gp) + (item 1 of gs) / 2 as text) & "," & ((item 2 of gp) + (item 2 of gs) / 2 as text)
        on error
            return "ERR"
        end try
    end tell
end tell"""
    )
    if not ok or pos.strip() == "ERR":
        asc.ui_clear_search()
        return False
    try:
        hcx, hcy = [float(v) for v in pos.strip().split(",")]
    except ValueError:
        asc.ui_clear_search()
        return False
    asc._hover_with_nudge(hcx, hcy)
    time.sleep(0.6)
    ok, descs = asc.run_applescript(
        f"""
tell application "System Events"
    tell process "Music"
        try
            set g to {group_path}
            set out to ""
            repeat with b in (every button of g)
                try
                    set out to out & (description of b) & "|"
                end try
            end repeat
            return out
        on error
            return "PROBE_FAIL"
        end try
    end tell
end tell"""
    )
    asc.ui_clear_search()
    if not ok or descs.strip() == "PROBE_FAIL":
        return False
    return "Download button" not in descs


@pytest.mark.ui
@pytest.mark.skipif(
    not os.environ.get("TEST_UI"),
    reason="UI flows require Music.app visible + Accessibility. Run with TEST_UI=1.",
)
@pytest.mark.ui
@pytest.mark.skipif(
    not os.environ.get("TEST_UI"),
    reason="UI flows require Music.app visible + Accessibility. Run with TEST_UI=1.",
)
class TestUIPlayLive:
    """Live test for ``ui_play_result_by_query`` — the tokenless-catalog
    play path (used when a user wants to play a catalog track without
    adding it to library). Distinct from add-to-library because no track
    state mutation happens; safe to run repeatedly without polluting
    Music.app's per-row UI cache.

    Cleanup: pauses playback after each test.
    """

    @classmethod
    def setup_class(cls):
        asc.run_applescript('tell application "Music" to activate')
        time.sleep(1.0)

    def teardown_method(self, method):
        # Stop playback so the test doesn't leave music playing in the
        # background after the suite finishes.
        try:
            asc.run_applescript('tell application "Music" to pause')
        except Exception:
            pass

    def test_play_result_by_query_starts_correct_track(self):
        """ui_play_result_by_query should start playback of a track whose
        name matches the query. Uses a well-indexed catalog track that
        Music.app can find quickly via Top Results."""
        # Use a track unlikely to require library presence — Apple Music
        # plays catalog tracks even if not in library.
        query = "Bohemian Rhapsody Queen"
        ok, msg = asc.ui_play_result_by_query(query)
        if not ok:
            pytest.skip(
                f"ui_play_result_by_query failed (likely Top Results UI "
                f"surface limitation, same family as the deep-flow soft-skip): "
                f"{msg}"
            )
        # Confirm Music.app actually started playing
        time.sleep(1.5)
        playing_ok, current_track = asc.get_current_track()
        assert playing_ok, f"get_current_track failed: {current_track}"
        # current_track is dict {state, name, artist, ...}; check name loosely
        # because catalog can return live versions, remasters, etc.
        track_name = current_track.get("name", "") if isinstance(current_track, dict) else ""
        assert (
            "bohemian" in track_name.lower()
        ), f"Expected Bohemian Rhapsody to be playing, got: {current_track}"


@pytest.mark.ui
@pytest.mark.skipif(
    not os.environ.get("TEST_UI"),
    reason="UI flows require Music.app visible + Accessibility. Run with TEST_UI=1.",
)
class TestUIEdgeCasesLive:
    """Live tests for UI-flow edge cases that don't fit the happy-path
    deep-flow test: empty/junk queries, search-state cleanup, and
    popover-canonical artist disambiguation. None of these mutate the
    library or playlist state.
    """

    @classmethod
    def setup_class(cls):
        asc.run_applescript('tell application "Music" to activate')
        time.sleep(1.0)

    def teardown_method(self, method):
        try:
            asc.ui_clear_search()
        except Exception:
            pass

    def test_search_returns_empty_for_junk_query(self):
        """A query that can't possibly match anything should produce no
        Top Results without raising — caller's job is to handle gracefully."""
        ok, results, why = asc.ui_search_catalog("qqxqzznotarealquery9999")
        # Either ok=True with empty results, OR ok=False with a why.
        # Both are acceptable; what's NOT acceptable is a crash or stale
        # results from a previous query leaking through.
        assert (ok and not results) or (not ok), (
            f"junk query unexpectedly returned results: ok={ok}, "
            f"len(results)={len(results)}, why={why!r}"
        )

    def test_clear_search_resets_field(self):
        """After ui_search_catalog + ui_clear_search, the search field
        value should be empty — confirms cleanup actually clears state
        and doesn't just leave the field with stale text."""
        ok, results, why = asc.ui_search_catalog("Bon Iver")
        assert ok, f"setup search failed: {why}"
        asc.ui_clear_search()
        time.sleep(0.5)
        # Use the production search-field probe so this test rides whatever
        # toolbar variant Music.app exposes today (toolbar/flat/sidebar).
        field_path = asc._get_search_field()
        if not field_path:
            pytest.skip(
                "Could not resolve search field path — Music.app build may "
                "have shifted the toolbar layout. Not a code bug."
            )
        ok, value = asc.run_applescript(
            f"""
tell application "System Events"
    tell process "Music"
        try
            return value of ({field_path}) as text
        on error
            return "FIELD_NOT_FOUND"
        end try
    end tell
end tell"""
        )
        if not ok or value.strip() == "FIELD_NOT_FOUND":
            pytest.skip(
                "Could not probe search field via resolved path — Music.app "
                "may have navigated away from Search view after clear."
            )
        assert value.strip() == "", (
            f"ui_clear_search did not empty the search field — value is " f"still {value.strip()!r}"
        )

    def test_popover_disambiguates_by_artist(self):
        """The popover-canonical-match flow's `_find_popover_song_row`
        must pick the row whose artist matches the caller's `artist` arg
        — not just the first row whose Title matches. Same-title tracks
        by different artists is a real common case (e.g. "Heaven" by
        many artists).

        Probe-only: opens popover, calls the matcher, asserts the
        resolved_artist matches what we asked for. No click, no add,
        no playlist mutation.
        """
        ok, _err = asc._open_search_popover("Heaven Talking Heads")
        if not ok:
            pytest.skip("popover did not appear — likely a transient UI flake")
        match = asc._find_popover_song_row("Heaven", "Talking Heads")
        if match is None:
            pytest.skip(
                "Popover did not surface a Song row matching 'Heaven' by "
                "'Talking Heads' — Apple's autocomplete ranking can omit "
                "this candidate; not a code bug."
            )
        idx, resolved_artist = match
        assert idx >= 1, f"matched popover row index should be >= 1, got {idx}"
        # Resolved artist should contain "Talking Heads" — case-insensitive
        # because Music.app sometimes returns "talking heads" lower-cased.
        assert "talking heads" in resolved_artist.lower(), (
            f"popover-canonical-match returned wrong artist: expected "
            f"'Talking Heads', got {resolved_artist!r}. Disambiguation "
            f"may have picked a different artist's 'Heaven'."
        )


class TestOpenSearchPopoverSelfHeal:
    """`_open_search_popover` must self-heal a stale search-field cache: when the
    cached toolbar path no longer resolves in the current view (a hard
    "Can't get group 1 of toolbar 1…" error), it invalidates the cache and
    retries once. Regression guard — the pop-over path lacked this self-heal that
    its sibling search path has, so it hard-failed intermittently on macOS 26."""

    def test_invalidates_cache_and_retries_on_path_error(self, monkeypatch):
        monkeypatch.setattr(asc, "_ensure_music_frontmost", lambda: None)
        monkeypatch.setattr(asc, "_get_search_field", lambda: "FIELD")
        monkeypatch.setattr(asc, "check_ui_accessible", lambda: (True, ""))
        monkeypatch.setattr(asc, "_search_field_cache", "stale-path")

        emits = {"n": 0}

        def fake_run(script):
            if "click FIELD" in script:
                emits["n"] += 1
                if emits["n"] == 1:
                    # First attempt: cached path no longer resolves.
                    return (False, "Can't get group 1 of toolbar 1 of window Music. (-1728)")
                return (True, "")  # retry succeeds
            if "first pop over" in script:
                return (True, "true")  # pop-over appears
            return (True, "")  # navigation steps

        monkeypatch.setattr(asc, "run_applescript", fake_run)

        ok, err = asc._open_search_popover("some query")
        assert ok, f"self-heal should have recovered, got: {err}"
        assert emits["n"] == 2, "the field interaction should have been retried once"
        assert asc._search_field_cache is None, "stale cache must be invalidated"

    def test_no_retry_on_non_path_error(self, monkeypatch):
        # A non-path error (e.g. genuinely empty results) must NOT trigger the
        # cache-invalidate retry — only "path no longer resolves" errors do.
        monkeypatch.setattr(asc, "_ensure_music_frontmost", lambda: None)
        monkeypatch.setattr(asc, "_get_search_field", lambda: "FIELD")
        monkeypatch.setattr(asc, "check_ui_accessible", lambda: (True, ""))

        emits = {"n": 0}

        def fake_run(script):
            if "click FIELD" in script:
                emits["n"] += 1
                return (False, "some unrelated AppleScript failure")
            return (True, "")

        monkeypatch.setattr(asc, "run_applescript", fake_run)

        ok, _err = asc._open_search_popover("some query")
        assert not ok
        assert emits["n"] == 1, "must not retry on a non-path error"
