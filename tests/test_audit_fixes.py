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
