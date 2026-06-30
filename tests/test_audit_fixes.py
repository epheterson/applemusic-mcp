"""Regression tests for the pre-1.0 release-audit findings."""

from __future__ import annotations

import applemusic_mcp.server as server

# -- HIGH: destructive web ops must not destroy the wrong playlist ----------


def test_playlist_delete_ambiguous_refuses(monkeypatch):
    monkeypatch.setattr(
        server.amp_api,
        "list_playlists",
        lambda: [{"id": "p.1", "name": "Jazz Favorites"}, {"id": "p.2", "name": "Jazz Vibes"}],
    )
    monkeypatch.setattr(server.amp_api, "delete_playlist", lambda pid: (True, ""))
    out = server._playlist_delete_api("Jazz")  # no exact match → 2 substring matches
    assert out.startswith("Error") and "multiple" in out.lower()
    assert "Jazz Favorites" in out and "Jazz Vibes" in out


def test_playlist_delete_echoes_resolved_name(monkeypatch):
    deleted = {}
    monkeypatch.setattr(
        server.amp_api, "list_playlists", lambda: [{"id": "p.1", "name": "Jazz Favorites"}]
    )
    monkeypatch.setattr(
        server.amp_api, "delete_playlist", lambda pid: deleted.update(pid=pid) or (True, "")
    )
    out = server._playlist_delete_api("Jazz")  # single substring match
    assert deleted["pid"] == "p.1"
    assert "Deleted playlist: Jazz Favorites" in out  # the RESOLVED name, not "Jazz"


def test_playlist_delete_exact_wins(monkeypatch):
    monkeypatch.setattr(
        server.amp_api,
        "list_playlists",
        lambda: [{"id": "p.1", "name": "Jazz"}, {"id": "p.2", "name": "Jazz Favorites"}],
    )
    monkeypatch.setattr(server.amp_api, "delete_playlist", lambda pid: (True, ""))
    out = server._playlist_delete_api("Jazz")
    assert "Deleted playlist: Jazz" in out and "Favorites" not in out


def test_playlist_rename_ambiguous_refuses(monkeypatch):
    monkeypatch.setattr(
        server.amp_api,
        "list_playlists",
        lambda: [{"id": "p.1", "name": "Workout A"}, {"id": "p.2", "name": "Workout B"}],
    )
    monkeypatch.setattr(server.amp_api, "rename_playlist", lambda pid, n: (True, ""))
    out = server._playlist_rename_api("Workout", "New")
    assert out.startswith("Error") and "multiple" in out.lower()


# -- HIGH: in-MCP signin must offer the macOS Safari path -------------------


def test_signin_prefers_safari_on_macos(monkeypatch):
    monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
    from applemusic_mcp import auth, safari

    saved = {}
    monkeypatch.setattr(safari, "media_user_token", lambda: (True, "TOK"))
    monkeypatch.setattr(auth, "save_user_token", lambda t: saved.setdefault("t", t))
    out = server._auth_action("signin")
    assert saved["t"] == "TOK"
    assert "Safari" in out and out.startswith("✓")  # signed in via Safari, no Chrome flow


def test_signin_safari_fail_no_playwright_guides_to_safari(monkeypatch):
    monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
    from applemusic_mcp import browser, safari

    monkeypatch.setattr(safari, "media_user_token", lambda: (False, "Safari blocked."))
    monkeypatch.setattr(browser, "is_available", lambda: False)  # no Playwright
    out = server._auth_action("signin")
    assert "Apple Events" in out and "browser" in out.lower()  # Safari fix + [browser] option


# -- MEDIUM: off-macOS playlist add must de-dup + honor auto_add -------------

import types  # noqa: E402


def _ri(input_type, value, artist="", error=None):
    return types.SimpleNamespace(input_type=input_type, value=value, artist=artist, error=error)


def test_playlist_add_api_dedup(monkeypatch):
    monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda n: ("p.1", None))
    monkeypatch.setattr(
        server.amp_api,
        "get_tracks",
        lambda pid: [{"catalog_id": "123", "name": "Africa", "artist": "Toto"}],
    )
    monkeypatch.setattr(
        server, "_resolve_track", lambda t, a="": [_ri(server.InputType.CATALOG_ID, "123")]
    )
    calls = {}
    monkeypatch.setattr(
        server.amp_api, "add_tracks", lambda pid, items: calls.update(items=items) or (True, "")
    )
    out = server._playlist_add_api("Mix", "123")  # already present
    assert "items" not in calls  # add_tracks NOT called — de-duped
    assert "already in the playlist" in out.lower()


def test_playlist_add_api_auto_add_off_skips_catalog(monkeypatch):
    monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda n: ("p.1", None))
    monkeypatch.setattr(server.amp_api, "get_tracks", lambda pid: [])
    monkeypatch.setattr(
        server, "_resolve_track", lambda t, a="": [_ri(server.InputType.NAME, "New Song")]
    )
    monkeypatch.setattr(server.amp_api, "search_library_songs", lambda q, n=1: [])  # not in library
    searched = {}
    monkeypatch.setattr(
        server.amp_api,
        "search_catalog_songs",
        lambda q, n=1: searched.update(hit=1) or [{"id": "999", "name": "New Song", "artist": "X"}],
    )
    monkeypatch.setattr(server.amp_api, "add_tracks", lambda pid, items: (True, ""))
    out = server._playlist_add_api("Mix", "New Song", auto_add=False)
    assert "hit" not in searched  # did NOT catalog-search when opted out
    assert "auto_add=True" in out


def test_playlist_add_api_auto_add_on_searches_catalog(monkeypatch):
    monkeypatch.setattr(server, "_find_api_playlist_by_name", lambda n: ("p.1", None))
    monkeypatch.setattr(server.amp_api, "get_tracks", lambda pid: [])
    monkeypatch.setattr(
        server, "_resolve_track", lambda t, a="": [_ri(server.InputType.NAME, "New Song")]
    )
    monkeypatch.setattr(
        server.amp_api,
        "search_catalog_songs",
        lambda q, n=1: [{"id": "999", "name": "New Song", "artist": "X"}],
    )
    added = {}
    monkeypatch.setattr(
        server.amp_api, "add_tracks", lambda pid, items: added.update(items=items) or (True, "")
    )
    out = server._playlist_add_api("Mix", "New Song", auto_add=True)
    assert added["items"] == ["999"] and "New Song" in out


# -- MEDIUM: native catalog->playlist attach must `duplicate` AT MOST ONCE ---


class _Resp:
    def __init__(self, code, js):
        self.status_code = code
        self._js = js

    def json(self):
        return self._js


def _stub_attach(monkeypatch, add_calls, verify):
    monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
    monkeypatch.setattr(server, "get_headers", lambda: {})
    monkeypatch.setattr(server, "get_storefront", lambda: "us")
    monkeypatch.setattr(server, "_VERIFY_DELAY_S", 0)
    monkeypatch.setattr(server, "_SYNC_POLL_BUDGET_S", 5)
    monkeypatch.setattr(server, "_SYNC_POLL_INTERVAL_S", 0)
    monkeypatch.setattr(server, "_SYNC_NUDGE_AFTER_S", 999)
    monkeypatch.setattr(
        server.requests,
        "get",
        lambda *a, **k: _Resp(
            200,
            {
                "results": {
                    "songs": {
                        "data": [
                            {"id": "123", "attributes": {"name": "Africa", "artistName": "Toto"}}
                        ]
                    }
                }
            },
        ),
    )
    monkeypatch.setattr(server.requests, "post", lambda *a, **k: _Resp(202, {}))
    monkeypatch.setattr(server.asc, "find_library_track", lambda n, a: (True, {}))  # synced
    monkeypatch.setattr(
        server.amp_api, "resolve_playlist", lambda n, **k: {"id": "p.user", "canEdit": True}
    )
    monkeypatch.setattr(server.amp_api, "playlist_kind", lambda pl: "user")

    def fake_add(pl, nm, ar, al):
        add_calls.append(1)
        return True, "added", None

    monkeypatch.setattr(server, "_smart_as_add_track_to_playlist", fake_add)
    monkeypatch.setattr(server, "_verify_track_in_playlist", verify)


def test_native_attach_adds_once_when_verify_never_confirms(monkeypatch):
    add_calls = []
    _stub_attach(monkeypatch, add_calls, lambda *a, **k: False)  # verify never confirms
    ok, msg, _steps = server._auto_search_and_add_to_playlist("Africa", "Toto", "My User PL")
    assert len(add_calls) == 1  # added EXACTLY once (old loop added up to 4x)
    assert not ok and "duplicate" in msg.lower()


def test_native_attach_adds_once_then_verify_lag_succeeds(monkeypatch):
    add_calls = []
    seen = []

    def verify(*a, **k):
        seen.append(1)
        return len(seen) >= 2  # confirms on the 2nd poll (propagation lag)

    _stub_attach(monkeypatch, add_calls, verify)
    ok, msg, _steps = server._auto_search_and_add_to_playlist("Africa", "Toto", "My User PL")
    assert len(add_calls) == 1 and ok  # added once, then verified on retry
