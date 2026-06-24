"""Tests for the `queue` tool (Up Next) — routing + formatting.

The queue lives in the browser web player's MusicKit instance, so these mock the
browser module and assert the server tool resolves/dispatches correctly. The
live MusicKit calls themselves are exercised by hand (and the method names were
verified live against MusicKit v3 before wiring).
"""

from unittest.mock import MagicMock

from applemusic_mcp import browser, server


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
        server.amp_api, "search_catalog_songs", lambda q, n=1: [{"id": "999", "name": "x"}]
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
    out = server.queue(action="list")
    assert "N — A" in out


def test_queue_play_next_resolves_and_calls(monkeypatch):
    monkeypatch.setattr(server.amp_api, "search_catalog_songs", lambda q, n=1: [{"id": "555"}])
    spy = MagicMock(return_value=(True, "Queued to Up Next (2 in queue)"))
    monkeypatch.setattr(browser, "queue_play_next", spy)
    out = server.queue(action="play_next", track="Strobe", artist="deadmau5")
    spy.assert_called_once_with("555")
    assert "Queued" in out


def test_queue_play_last_uses_play_later(monkeypatch):
    monkeypatch.setattr(server.amp_api, "search_catalog_songs", lambda q, n=1: [{"id": "777"}])
    spy = MagicMock(return_value=(True, "Queued to end of Up Next (3 in queue)"))
    monkeypatch.setattr(browser, "queue_play_later", spy)
    out = server.queue(action="play_last", track="Ghosts")
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
    out = server.queue(action="remove", index=2)
    spy.assert_called_once_with(2)
    assert "Removed" in out


def test_queue_clear_routes(monkeypatch):
    monkeypatch.setattr(browser, "queue_clear", lambda: (True, "Cleared the queue"))
    assert "Cleared" in server.queue(action="clear")


def test_queue_jump_requires_index():
    assert "index required" in server.queue(action="jump")


def test_queue_jump_routes(monkeypatch):
    spy = MagicMock(return_value=(True, "Jumped to item 3"))
    monkeypatch.setattr(browser, "queue_jump", spy)
    out = server.queue(action="jump", index=3)
    spy.assert_called_once_with(3)
    assert "Jumped" in out


def test_queue_autoplay_routes(monkeypatch):
    spy = MagicMock(return_value=(True, "Autoplay on"))
    monkeypatch.setattr(browser, "queue_autoplay", spy)
    out = server.queue(action="autoplay", enabled=True)
    spy.assert_called_once_with(True)
    assert "Autoplay on" in out


def test_queue_unknown_action():
    assert "Unknown action" in server.queue(action="bogus")


def test_resolve_failure_msg_expired(monkeypatch):
    """A resolve miss while the session is expired must say so, not 'not found'."""
    monkeypatch.setattr(server.amp_api, "session_status", lambda: "expired")
    msg = server._resolve_failure_msg("playlist 'X' not found in your library")
    assert "expired" in msg.lower() and "signin" in msg.lower()
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
    out = server.queue(action="list")
    assert out.startswith("Error:") and "signin" in out
