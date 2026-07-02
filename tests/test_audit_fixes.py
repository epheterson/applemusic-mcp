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
    assert not ok and ("did not persist" in msg.lower() or "reopen music" in msg.lower())


def test_native_attach_adds_once_then_verify_lag_succeeds(monkeypatch):
    add_calls = []
    seen = []

    def verify(*a, **k):
        seen.append(1)
        return len(seen) >= 2  # confirms on the 2nd poll (propagation lag)

    _stub_attach(monkeypatch, add_calls, verify)
    ok, msg, _steps = server._auto_search_and_add_to_playlist("Africa", "Toto", "My User PL")
    assert len(add_calls) == 1 and ok  # added once, then verified on retry


# -- task #6: native control confirms real state (no false "paused") --------


def test_pause_that_doesnt_stick_is_honest(monkeypatch):
    """If Music is still 'playing' after a pause, say so + point at the all-engines
    view — don't claim 'Playback: pause' (the old false-success / 'won't stay paused')."""
    monkeypatch.setattr(server.asc, "pause", lambda: (True, ""))
    monkeypatch.setattr(server.asc, "get_current_track", lambda: (True, {"state": "playing"}))
    out = server._playback_control("pause")
    assert "still playing" in out.lower() and "now_playing" in out


def test_pause_that_sticks_reports_paused(monkeypatch):
    monkeypatch.setattr(server.asc, "pause", lambda: (True, ""))
    monkeypatch.setattr(server.asc, "get_current_track", lambda: (True, {"state": "paused"}))
    out = server._playback_control("pause")
    assert "Playback: pause" in out and "paused" in out


# -- dev-session: transactional swap never loses the old track --------------


def test_swap_aborts_when_add_not_confirmed(monkeypatch):
    """If the new track's add can't be confirmed (Music.app revert), the OLD track is
    NOT removed — the Coltrane data-loss case."""
    monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
    monkeypatch.setattr(server, "_playlist_add", lambda *a, **k: "Error: did not persist (revert)")
    removed = []
    monkeypatch.setattr(server, "_playlist_remove", lambda p, t, ar: removed.append(t) or "removed")
    out = server.playlist(action="add", playlist="Jazz", track="Coltrane", replace="Old Song")
    assert "aborted" in out.lower() and removed == []  # old track kept


def test_swap_removes_old_only_after_add_confirms(monkeypatch):
    monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
    monkeypatch.setattr(
        server, "_playlist_add", lambda *a, **k: "Added Coltrane to Jazz (via Music.app)"
    )
    monkeypatch.setattr(server, "_resolve_playlist", lambda p: _fake_resolved())
    monkeypatch.setattr(server, "_confirm_swap_track", lambda *a, **k: True)
    removed = []
    monkeypatch.setattr(server, "_playlist_remove", lambda p, t, ar: removed.append(t) or "Removed")
    out = server.playlist(action="add", playlist="Jazz", track="Coltrane", replace="Old Song")
    assert "Swapped" in out and removed == ["Old Song"]


def test_swap_aborts_when_strict_confirm_fails(monkeypatch):
    """The add reports success but the STRICT confirm can't find the exact track (a
    reverted add hiding behind a similar title) → old track kept. The substring-verify
    data-loss fix."""
    monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", True)
    monkeypatch.setattr(
        server, "_playlist_add", lambda *a, **k: "Added One to Jazz (via Music.app)"
    )
    monkeypatch.setattr(server, "_resolve_playlist", lambda p: _fake_resolved())
    monkeypatch.setattr(server, "_confirm_swap_track", lambda *a, **k: False)
    removed = []
    monkeypatch.setattr(server, "_playlist_remove", lambda p, t, ar: removed.append(t) or "Removed")
    out = server.playlist(action="add", playlist="Jazz", track="One", replace="Old Song")
    assert "aborted" in out.lower() and removed == []


def test_add_landed_is_conservative():
    assert server._add_landed("Added X to Y (via Music.app)") is True
    for bad in [
        "Error: nope",
        "Added but did not persist after retry",
        "couldn't confirm",
        "re-run this add",
        "relaunch Music.app",
        "Nothing added",
    ]:
        assert server._add_landed(bad) is False, bad


# -- off-mac (Win/Linux) swap: API read-back guards the destructive remove --------


def _fake_resolved(api_id="p.1", name="Jazz"):
    import types as _t

    return _t.SimpleNamespace(api_id=api_id, applescript_name=name, error=None, fuzzy_match=None)


def test_offmac_swap_aborts_when_confirm_fails(monkeypatch):
    """Off-mac there's no native verify; if the strict confirm can't find the exact
    track, the old track is NOT removed."""
    monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
    monkeypatch.setattr(server, "_playlist_add_api", lambda *a, **k: "Added Coltrane to Jazz")
    monkeypatch.setattr(server, "_resolve_playlist", lambda p: _fake_resolved())
    monkeypatch.setattr(server, "_confirm_swap_track", lambda *a, **k: False)
    removed = []
    monkeypatch.setattr(server, "_playlist_remove_api", lambda p, t, ar: removed.append(t) or "rm")
    out = server.playlist(action="add", playlist="Jazz", track="Coltrane", replace="Old Song")
    assert "aborted" in out.lower() and removed == []


def test_offmac_swap_removes_old_after_confirm(monkeypatch):
    monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
    monkeypatch.setattr(server, "_playlist_add_api", lambda *a, **k: "Added Coltrane to Jazz")
    monkeypatch.setattr(server, "_resolve_playlist", lambda p: _fake_resolved())
    monkeypatch.setattr(server, "_confirm_swap_track", lambda *a, **k: True)
    removed = []
    monkeypatch.setattr(server, "_playlist_remove_api", lambda p, t, ar: removed.append(t) or "rm")
    out = server.playlist(action="add", playlist="Jazz", track="Coltrane", replace="Old Song")
    assert "Swapped" in out and removed == ["Old Song"]


def test_confirm_swap_track_requires_exact_name(monkeypatch):
    """The strict confirm must NOT accept a substring collision — adding 'One' while
    'One More Time' is present must return False (the data-loss fix)."""
    monkeypatch.setattr(server, "APPLESCRIPT_AVAILABLE", False)
    listing = (True, [{"name": "One More Time", "artist": "Daft Punk"}])
    monkeypatch.setattr(server, "_get_playlist_track_names", lambda pid: listing)
    assert server._confirm_swap_track("One", "", api_id="p.1") is False
    exact = (True, [{"name": "One", "artist": "Metallica"}])
    monkeypatch.setattr(server, "_get_playlist_track_names", lambda pid: exact)
    assert server._confirm_swap_track("One", "", api_id="p.1") is True


# -- config dir: clean error instead of a raw traceback when unwritable -----------


def test_get_config_dir_raises_clean_error(monkeypatch):
    """An unwritable APPLEMUSIC_MCP_HOME (or root-owned mount) should give an
    actionable message, not a raw OSError traceback."""
    import pytest

    import applemusic_mcp.auth as auth

    def boom(*a, **k):
        raise PermissionError("Permission denied")

    monkeypatch.setattr("pathlib.Path.mkdir", boom)
    with pytest.raises(RuntimeError, match="config directory"):
        auth.get_config_dir()


def test_env_var_user_token_injection(monkeypatch):
    """APPLEMUSIC_USER_TOKEN is the headless/container injection path (for an Apple ID
    that requires a hardware key that can't reach the box)."""
    import applemusic_mcp.auth as auth

    monkeypatch.setenv("APPLEMUSIC_USER_TOKEN", "INJECTED-TOK")
    assert auth.get_user_token() == "INJECTED-TOK"
    assert auth.has_user_token() is True
