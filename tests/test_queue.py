"""Tests for the `queue` tool (Up Next) — routing + formatting.

The queue lives in the browser web player's MusicKit instance, so these mock the
browser module and assert the server tool resolves/dispatches correctly. The
live MusicKit calls themselves are exercised by hand (and the method names were
verified live against MusicKit v3 before wiring).
"""

from unittest.mock import MagicMock

import pytest

from applemusic_mcp import browser, server


@pytest.fixture(autouse=True)
def _hermetic_queue(monkeypatch):
    """After a mutation, queue() reads the queue back via _queue_after → the engine's
    queue_list. Stub it to an empty default so tests never launch a real browser
    (individual tests override it where they assert on the read-back)."""
    monkeypatch.setattr(
        browser,
        "queue_list",
        lambda: (True, {"position": -1, "autoplay": False, "items": []}),
        raising=False,
    )
    yield


def test_format_queue_marks_current():
    out = server._format_queue(
        {
            "position": 1,
            "autoplay": True,
            "items": [
                {"index": 0, "name": "A", "artist": "X"},
                {"index": 1, "name": "B", "artist": "Y"},
            ],
        }
    )
    assert "autoplay on" in out
    assert "▶ 1. B — Y" in out
    assert "  0. A — X" in out


def test_format_queue_empty():
    assert "empty" in server._format_queue({"position": -1, "autoplay": False, "items": []})


def test_resolve_catalog_id_passthrough_for_digits(monkeypatch):
    # A bare catalog id should not hit catalog search.
    called = False

    def boom(*a, **k):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(server.amp_api, "search_catalog_songs", boom)
    assert server._queue_resolve_catalog_id("123456") == "123456"
    assert called is False


def test_resolve_catalog_id_searches_for_name(monkeypatch):
    monkeypatch.setattr(
        server.amp_api,
        "search_catalog_songs",
        lambda q, n=1: [{"id": "999", "name": "Some Song", "artist": "Some Artist"}],
    )
    assert server._queue_resolve_catalog_id("Some Song", "Some Artist") == "999"


def test_queue_list_routes_to_browser(monkeypatch):
    monkeypatch.setattr(
        browser,
        "queue_list",
        lambda: (
            True,
            {"position": 0, "autoplay": False, "items": [{"index": 0, "name": "N", "artist": "A"}]},
        ),
    )
    out = server.queue(action="list", engine="chrome")
    assert "N — A" in out


def test_queue_play_next_resolves_and_calls(monkeypatch):
    monkeypatch.setattr(
        server.amp_api,
        "search_catalog_songs",
        lambda q, n=1: [{"id": "555", "name": "Strobe", "artist": "deadmau5"}],
    )
    spy = MagicMock(return_value=(True, "Queued to Up Next (2 in queue)"))
    monkeypatch.setattr(browser, "queue_play_next", spy)
    out = server.queue(action="play_next", track="Strobe", artist="deadmau5", engine="chrome")
    spy.assert_called_once_with("555")
    assert "Queued" in out


def test_queue_play_last_uses_play_later(monkeypatch):
    monkeypatch.setattr(
        server.amp_api,
        "search_catalog_songs",
        lambda q, n=1: [{"id": "777", "name": "Ghosts", "artist": "A"}],
    )
    spy = MagicMock(return_value=(True, "Queued to end of Up Next (3 in queue)"))
    monkeypatch.setattr(browser, "queue_play_later", spy)
    out = server.queue(action="play_last", track="Ghosts", engine="chrome")
    spy.assert_called_once_with("777")
    assert "end of Up Next" in out


def test_queue_play_next_not_found(monkeypatch):
    monkeypatch.setattr(server.amp_api, "search_catalog_songs", lambda q, n=1: [])
    out = server.queue(action="play_next", track="zzznope")
    assert "not found" in out.lower()


def test_queue_remove_requires_index():
    assert "index required" in server.queue(action="remove")


def test_queue_remove_routes(monkeypatch):
    spy = MagicMock(return_value=(True, "Removed item 2 (3 left in queue)"))
    monkeypatch.setattr(browser, "queue_remove", spy)
    out = server.queue(action="remove", index=2, engine="chrome")
    spy.assert_called_once_with(2)
    assert "Removed" in out


def test_queue_clear_routes(monkeypatch):
    monkeypatch.setattr(browser, "queue_clear", lambda: (True, "Cleared the queue"))
    assert "Cleared" in server.queue(action="clear", engine="chrome")


def test_queue_jump_requires_index():
    out = server.queue(action="jump")
    assert "index" in out and "track" in out  # accepts index OR track now


def test_queue_jump_routes(monkeypatch):
    spy = MagicMock(return_value=(True, "Jumped to item 3"))
    monkeypatch.setattr(browser, "queue_jump", spy)
    out = server.queue(action="jump", index=3, engine="chrome")
    spy.assert_called_once_with(3)
    assert "Jumped" in out


def test_queue_autoplay_routes(monkeypatch):
    spy = MagicMock(return_value=(True, "Autoplay on"))
    monkeypatch.setattr(browser, "queue_autoplay", spy)
    out = server.queue(action="autoplay", enabled=True, engine="chrome")
    spy.assert_called_once_with(True)
    assert "Autoplay on" in out


def test_queue_unknown_action():
    assert "Unknown action" in server.queue(action="bogus")


def test_queue_set_resolves_all_and_replaces(monkeypatch):
    monkeypatch.setattr(server, "_engine", lambda: "api")
    monkeypatch.setattr(
        server, "_queue_resolve_catalog_id", lambda t, a="": {"a": "1", "b": "2", "c": "3"}.get(t)
    )
    spy = MagicMock(return_value=(True, "Queue set (3 track(s))"))
    monkeypatch.setattr(browser, "queue_set", spy)
    out = server.queue(action="set", track="a, b, c", engine="chrome")
    spy.assert_called_once_with(["1", "2", "3"])
    assert "Queue set" in out


def test_queue_set_reports_misses(monkeypatch):
    monkeypatch.setattr(server, "_engine", lambda: "api")
    monkeypatch.setattr(server, "_queue_resolve_catalog_id", lambda t, a="": {"a": "1"}.get(t))
    monkeypatch.setattr(
        browser, "queue_set", lambda ids: (True, f"Queue set ({len(ids)} track(s))")
    )
    out = server.queue(action="set", track="a\nzzz", engine="chrome")
    assert "skipped" in out.lower() and "zzz" in out


def test_queue_set_empty_input():
    assert "Error" in server.queue(action="set", track="   ")


def test_queue_set_none_resolve(monkeypatch):
    monkeypatch.setattr(server, "_queue_resolve_catalog_id", lambda t, a="": None)
    out = server.queue(action="set", track="x,y")
    assert "Error" in out and "none of those" in out.lower()


def test_queue_native_mode_has_no_up_next(monkeypatch):
    """Native (Music.app) mode has no exposed Up Next — queue returns an actionable
    error pointing at safari/chrome, instead of silently building an unreachable queue."""
    monkeypatch.setattr(server, "get_user_preferences", lambda: {"mode": "native"})
    monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
    monkeypatch.setattr(server, "_queue_resolve_catalog_id", lambda t, a="": "555")
    out = server.queue(action="play_last", track="X")
    assert out.startswith("Error") and ("safari" in out.lower() or "chrome" in out.lower())


def test_queue_no_warn_when_engine_web(monkeypatch):
    monkeypatch.setattr(server, "_queue_resolve_catalog_id", lambda t, a="": "555")
    monkeypatch.setattr(browser, "queue_play_later", lambda c: (True, "Queued"))
    monkeypatch.setattr(
        browser, "queue_list", lambda: (True, {"position": -1, "autoplay": False, "items": []})
    )
    out = server.queue(action="play_last", track="X", engine="chrome")
    assert "won't reach it" not in out


def test_resolve_failure_msg_expired(monkeypatch):
    """A resolve miss while the session is expired must say so, not 'not found'."""
    monkeypatch.setattr(server.amp_api, "session_status", lambda: "expired")
    msg = server._resolve_failure_msg("playlist 'X' not found in your library")
    assert "expired" in msg.lower() and "login" in msg.lower()
    assert "not found" not in msg.lower()


def test_resolve_failure_msg_throttled(monkeypatch):
    monkeypatch.setattr(server.amp_api, "session_status", lambda: "throttled")
    msg = server._resolve_failure_msg("playlist 'X' not found in your library")
    assert "429" in msg or "rate" in msg.lower()


def test_resolve_failure_msg_genuinely_not_found(monkeypatch):
    """Session is fine → keep the honest 'not found' message."""
    monkeypatch.setattr(server.amp_api, "session_status", lambda: "ok")
    msg = server._resolve_failure_msg("playlist 'X' not found in your library")
    assert "not found" in msg.lower()


def test_queue_surfaces_browser_error(monkeypatch):
    monkeypatch.setattr(
        browser,
        "queue_list",
        lambda: (False, "Not signed in to Apple Music — run `applemusic-mcp signin`."),
    )
    out = server.queue(action="list", engine="chrome")
    assert out.startswith("Error:") and "signin" in out


# --- resolution: don't queue a song the user didn't ask for -----------------

_LIB = [
    {"id": "1", "name": "Renaissance Fair (Single Version)", "artist": "The Byrds"},
    {"id": "2", "name": "So You Want To Be a Rock 'n' Roll Star", "artist": "The Byrds"},
    {"id": "3", "name": "Yesterday", "artist": "The Beatles"},
    {"id": "4", "name": "Yesterday", "artist": "Atmosphere"},
]


def test_queue_skips_an_unrelated_first_hit(monkeypatch):
    """Apple's search returns loosely-related rows first; taking [0] blindly is how
    the playlist add ended up queueing The Byrds for "Yesterday"."""
    monkeypatch.setattr(server.amp_api, "search_catalog_songs", lambda q, n: _LIB)
    assert server._queue_resolve_catalog_id("Yesterday") == "3"


def test_queue_artist_hint_disambiguates(monkeypatch):
    monkeypatch.setattr(server.amp_api, "search_catalog_songs", lambda q, n: _LIB)
    assert server._queue_resolve_catalog_id("Yesterday", "Atmosphere") == "4"


def test_queue_no_defensible_match_resolves_to_nothing(monkeypatch):
    """Callers turn None into "not found", which beats queueing the wrong track."""
    monkeypatch.setattr(server.amp_api, "search_catalog_songs", lambda q, n: _LIB)
    assert server._queue_resolve_catalog_id("Zzz Nonexistent") is None


def test_queue_bare_catalog_id_costs_no_request(monkeypatch):
    monkeypatch.setattr(
        server.amp_api,
        "search_catalog_songs",
        lambda q, n: pytest.fail("a bare catalog id must not trigger a search"),
    )
    assert server._queue_resolve_catalog_id("1441164805") == "1441164805"


def test_queue_throttle_is_not_reported_as_not_found(monkeypatch):
    """A 429 empties the search, so "not found in catalog" was the #42 bug
    surviving in the queue path."""

    def throttled(q, n):
        server.amp_api.note_status(429, server.amp_api.API)
        return []

    monkeypatch.setattr(server.amp_api, "search_catalog_songs", throttled)
    monkeypatch.setattr(server, "_engine", lambda **k: "chrome")
    result = server.queue(action="play_next", track="Anything")
    assert "429" in result
    assert "not found in catalog" not in result
