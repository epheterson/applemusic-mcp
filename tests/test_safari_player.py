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
