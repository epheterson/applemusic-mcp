"""Unit tests for the Safari MusicKit transport — no real Safari / osascript.

The transport (`_run_musickit`) builds an AppleScript that drives a signed-in
Safari's MusicKit via `do JavaScript`, kicks an async wrapper, and polls a result
sentinel. We mock `run_applescript` to return canned osascript output and assert
on both the AppleScript that gets built and the parsed result.
"""

from __future__ import annotations

import json

import applemusic_mcp.musickit_js as mk
import applemusic_mcp.safari_player as sp


def _mock(monkeypatch, ok, out, cap=None):
    def fake(script, *a, **k):
        if cap is not None:
            cap["script"] = script
        return (ok, out)

    monkeypatch.setattr(sp, "run_applescript", fake)


# -- transport --------------------------------------------------------------


def test_run_musickit_success_and_builds_script(monkeypatch):
    cap = {}
    _mock(monkeypatch, True, '{"ok":1,"v":"playing"}', cap)
    ok, v = sp._run_musickit(mk._PLAY_SONG_JS, "123")
    assert ok and v == "playing"
    s = cap["script"]
    assert "do JavaScript" in s and "music.apple.com" in s and "__amR" in s
    assert "123" in s  # the arg is embedded


def test_run_musickit_js_blocked(monkeypatch):
    _mock(monkeypatch, False, "Error: JavaScript through Apple Events is turned off (-1)")
    ok, msg = sp._run_musickit(mk._CONTROL_JS, {"action": "pause", "seconds": 0})
    assert not ok and "Apple Events" in msg


def test_run_musickit_not_authorized(monkeypatch):
    _mock(monkeypatch, True, '{"ok":0,"e":"not-authorized"}')
    ok, msg = sp._run_musickit(mk._NOW_PLAYING_JS)
    assert not ok and "sign" in msg.lower()


def test_run_musickit_not_ready(monkeypatch):
    _mock(monkeypatch, True, '{"ok":0,"e":"musickit-not-ready"}')
    ok, msg = sp._run_musickit(mk._QUEUE_LIST_JS)
    assert not ok and ("music.apple.com" in msg.lower() or "ready" in msg.lower())


def test_run_musickit_timeout(monkeypatch):
    _mock(monkeypatch, True, "pending")
    ok, msg = sp._run_musickit(mk._QUEUE_LIST_JS)
    assert not ok and "tim" in msg.lower()


def test_run_musickit_bad_json(monkeypatch):
    _mock(monkeypatch, True, "<not json>")
    ok, msg = sp._run_musickit(mk._QUEUE_LIST_JS)
    assert not ok


# -- public surface (drop-in parity with browser.py) ------------------------


def test_play_catalog_track(monkeypatch):
    _mock(monkeypatch, True, '{"ok":1,"v":"Africa"}')
    ok, msg = sp.play_catalog_track("123")
    assert ok and "Africa" in msg and "Playing" in msg
    # Safari decodes DRM natively — no Chrome "preview only" caveat.
    assert "preview" not in msg.lower()


def test_play_catalog_track_empty():
    ok, msg = sp.play_catalog_track("")
    assert not ok


def test_queue_set(monkeypatch):
    _mock(monkeypatch, True, '{"ok":1,"v":3}')
    ok, msg = sp.queue_set(["1", "2", "3"])
    assert ok and "3 track" in msg


def test_queue_set_partial(monkeypatch):
    _mock(monkeypatch, True, '{"ok":1,"v":2}')
    ok, msg = sp.queue_set(["1", "2", "3"])
    assert ok and "not queued" in msg


def test_queue_list(monkeypatch):
    data = {
        "position": 0,
        "autoplay": False,
        "items": [{"index": 0, "id": "1", "name": "A", "artist": "B"}],
    }
    _mock(monkeypatch, True, json.dumps({"ok": 1, "v": data}))
    ok, v = sp.queue_list()
    assert ok and v["items"][0]["name"] == "A"


def test_playback_control(monkeypatch):
    _mock(monkeypatch, True, '{"ok":1,"v":"ok"}')
    ok, msg = sp.playback_control("pause")
    assert ok and msg == "ok"


def test_is_available_non_darwin(monkeypatch):
    monkeypatch.setattr(sp.platform, "system", lambda: "Linux")
    assert sp.is_available() is False


def test_is_available_darwin_probe_ok(monkeypatch):
    monkeypatch.setattr(sp.platform, "system", lambda: "Darwin")
    _mock(monkeypatch, True, '{"ok":1,"v":true}'.replace("true", '"object"'))
    assert sp.is_available() is True


# -- completed surface ------------------------------------------------------


def test_browser_settings(monkeypatch):
    _mock(monkeypatch, True, json.dumps({"ok": 1, "v": {"volume": 50, "shuffle": 1, "repeat": 0}}))
    ok, msg = sp.browser_settings(volume=50)
    assert ok and "volume=50" in msg and "shuffle=on" in msg and "repeat=none" in msg


def test_now_playing(monkeypatch):
    _mock(
        monkeypatch,
        True,
        json.dumps({"ok": 1, "v": {"name": "X", "artist": "Y", "state": "playing"}}),
    )
    np = sp.now_playing()
    assert np and np["name"] == "X"


def test_now_playing_none_on_error(monkeypatch):
    _mock(monkeypatch, True, '{"ok":0,"e":"not-authorized"}')
    assert sp.now_playing() is None


def test_play_url_valid(monkeypatch):
    _mock(monkeypatch, True, '{"ok":1,"v":"Abbey Road"}')
    ok, msg = sp.play_url("https://music.apple.com/us/album/abbey-road/401")
    assert ok and "Abbey Road" in msg


def test_play_url_rejects_foreign_host():
    ok, msg = sp.play_url("https://music.apple.com.evil.tld/x/1")
    assert not ok and "Not an Apple Music URL" in msg


def test_queue_play_next(monkeypatch):
    _mock(monkeypatch, True, '{"ok":1,"v":4}')
    ok, msg = sp.queue_play_next("123")
    assert ok and "Up Next" in msg and "4 in queue" in msg


def test_queue_play_later(monkeypatch):
    _mock(monkeypatch, True, '{"ok":1,"v":5}')
    ok, msg = sp.queue_play_later("123")
    assert ok and "end of Up Next" in msg


def test_queue_remove_playing(monkeypatch):
    _mock(monkeypatch, True, '{"ok":1,"v":-2}')
    ok, msg = sp.queue_remove(0)
    assert not ok and "currently-playing" in msg


def test_queue_remove_ok(monkeypatch):
    _mock(monkeypatch, True, '{"ok":1,"v":2}')
    ok, msg = sp.queue_remove(1)
    assert ok and "Removed item 1" in msg


def test_queue_jump(monkeypatch):
    _mock(monkeypatch, True, '{"ok":1,"v":2}')
    ok, msg = sp.queue_jump(2)
    assert ok and "Jumped to item 2" in msg


def test_queue_jump_id_missing(monkeypatch):
    _mock(monkeypatch, True, '{"ok":1,"v":-1}')
    ok, msg = sp.queue_jump_id("999")
    assert not ok and "999" in msg


def test_queue_clear(monkeypatch):
    _mock(monkeypatch, True, '{"ok":1,"v":0}')
    ok, msg = sp.queue_clear()
    assert ok and "Cleared" in msg


def test_queue_autoplay_on(monkeypatch):
    _mock(monkeypatch, True, '{"ok":1,"v":1}')
    ok, msg = sp.queue_autoplay(True)
    assert ok and "Autoplay on" in msg


def test_reveal_url(monkeypatch):
    cap = {}
    _mock(monkeypatch, True, "ok", cap)
    ok, msg = sp.reveal_url("https://music.apple.com/us/album/x/1")
    assert ok and "Showing in Safari" in msg
    # Reuses the consistent music tab (open one only if none) — no duplicate tabs.
    assert "set URL of theTab" in cap["script"]


def test_music_tab_count(monkeypatch):
    monkeypatch.setattr(sp.platform, "system", lambda: "Darwin")
    _mock(monkeypatch, True, "2")
    assert sp.music_tab_count() == 2


def test_now_playing_if_running_no_tab(monkeypatch):
    # No music tab → peek returns None WITHOUT opening one.
    monkeypatch.setattr(sp, "music_tab_count", lambda: 0)
    assert sp.now_playing_if_running() is None


def test_now_playing_if_running_with_tab(monkeypatch):
    monkeypatch.setattr(sp, "music_tab_count", lambda: 1)
    monkeypatch.setattr(sp, "now_playing", lambda: {"name": "X", "artist": "Y"})
    assert sp.now_playing_if_running()["name"] == "X"


# -- Safari first-play gesture cliff → actionable message, no 30s hang -----------


def test_play_gesture_blocked_returns_actionable(monkeypatch):
    """A gesture-blocked play (queue loaded, audio didn't start) returns the one-click
    guidance, not a bare timeout or a false 'Playing'."""
    monkeypatch.setattr(
        sp, "_run_musickit", lambda *a, **k: (True, "Naima [no audio — the player did not start]")
    )
    ok, msg = sp.play_catalog_track("123")
    assert ok and "one real click" in msg and "0:00" not in msg


def test_play_normal_still_reports_playing(monkeypatch):
    monkeypatch.setattr(sp, "_run_musickit", lambda *a, **k: (True, "Naima"))
    ok, msg = sp.play_catalog_track("123")
    assert ok and msg == "Playing: Naima"


def test_control_play_gesture_blocked_returns_actionable(monkeypatch):
    monkeypatch.setattr(sp, "_run_musickit", lambda *a, **k: (True, "no-audio"))
    ok, msg = sp.playback_control("play")
    assert ok and "one real click" in msg
