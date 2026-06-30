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
