"""Full-coverage unit tests for ``applemusic_mcp.browser`` WITHOUT a real browser.

Strategy
--------
Two layers of fakes:

1. *Real-engine* tests drive the actual ``_BrowserEngine`` owner thread, but with
   ``playwright.sync_api.sync_playwright`` monkeypatched to a fake. This exercises
   ``_run`` / ``_launch_context`` / ``_serve`` / ``submit`` / ``shutdown`` /
   ``_ensure_thread`` end-to-end against fakes — the dedicated thread really runs.

2. *Module-function* tests swap ``browser._engine`` for a tiny ``FakeEngine`` whose
   ``submit`` runs the supplied callable against a programmable ``FakePage``. This
   lets every public helper's success / ``BrowserUnavailable`` / generic-error
   branch execute and assert on the *real* descriptors and messages produced.
"""

from __future__ import annotations

import os

import pytest

import applemusic_mcp.browser as browser
from applemusic_mcp.browser import BrowserUnavailable

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _route(js: str):
    """Map a MusicKit JS snippet to a stable key so FakePage can return canned
    values keyed by *which* page.evaluate call is being made."""
    if "ids[songs]" in js:
        return "add"
    if "developerToken" in js and "musicUserToken" in js:
        return "ready"  # _MUSICKIT_READY
    if "isAuthorized" in js:
        return "authorized"
    if "__shuffle" in js:
        return "play_queue"
    if "setQueue({ song: songId" in js:
        return "play_song"
    if "skipToNextItem" in js:
        return "control"
    if "shuffleMode" in js and "repeatMode" in js:
        return "settings"
    if "isPlaying" in js:
        return "now"
    if "playNext" in js:
        return "qnext"
    if "playLater" in js:
        return "qlater"
    if "queue.remove" in js:
        return "qremove"
    if "clearQueue" in js:
        return "qclear"
    if "changeToMediaAtIndex" in js:
        return "qjump"
    if "autoplayEnabled = !!on" in js:
        return "qautoplay"
    if "q.position" in js:
        return "queue_list"
    return None


class FakeContext:
    def __init__(self, cookies=None, raise_=False):
        self._cookies = cookies or []
        self._raise = raise_

    def cookies(self, url):
        if self._raise:
            raise RuntimeError("cookie read blew up")
        return self._cookies


class FakePage:
    def __init__(self, url="", cookies=None, cookies_raise=False):
        self.url = url
        self.context = FakeContext(cookies=cookies, raise_=cookies_raise)
        self.goto_calls = []
        self.eval_calls = []
        self.wait_for_function_raises = False
        self.evaluate_raises_on = set()  # set of route keys that should raise
        self.returns = {
            "ready": True,
            "add": 202,
            "authorized": True,
            "play_song": "Strobe",
            "play_queue": "Abbey Road",
            "control": "ok",
            "settings": {"volume": 50, "shuffle": 1, "repeat": 2},
            "now": {"name": "N", "artist": "A", "album": "Al", "playing": True},
            "qnext": 3,
            "qlater": 4,
            "qremove": 2,
            "qclear": 0,
            "qjump": 1,
            "qautoplay": True,
            "queue_list": {
                "position": 0,
                "autoplay": True,
                "items": [{"index": 0, "name": "x", "artist": "y"}],
            },
        }

    def goto(self, url, wait_until=None):
        self.goto_calls.append((url, wait_until))
        self.url = url

    def wait_for_timeout(self, ms):
        pass

    def wait_for_function(self, js, timeout=None):
        if self.wait_for_function_raises:
            raise RuntimeError("musickit never became ready")

    def evaluate(self, js, arg=None):
        key = _route(js)
        self.eval_calls.append((key, arg))
        if key in self.evaluate_raises_on:
            raise RuntimeError(f"evaluate failed for {key}")
        val = self.returns.get(key)
        if isinstance(val, BaseException):
            raise val
        return val


class FakeEngine:
    """Stand-in for ``_BrowserEngine`` — runs callables inline against a FakePage."""

    def __init__(self, page=None, error=None, raise_exc=None, using_chrome=True):
        self.page = page or FakePage()
        self.error = error
        self.raise_exc = raise_exc
        self._using_chrome = using_chrome
        self.shutdown_called = False

    def submit(self, fn, timeout=120.0):
        if self.error is not None:
            raise self.error
        if self.raise_exc is not None:
            raise self.raise_exc
        return fn(self.page)

    def shutdown(self, timeout=10.0):
        self.shutdown_called = True


def set_engine(monkeypatch, **kwargs):
    eng = FakeEngine(**kwargs)
    monkeypatch.setattr(browser, "_engine", eng)
    return eng


# -- Playwright-level fakes for real-engine tests ---------------------------


class FakeCtx:
    def __init__(self, pages=None, close_raises=False):
        self._pages = list(pages) if pages is not None else []
        self.close_raises = close_raises
        self.closed = False

    @property
    def pages(self):
        return self._pages

    def new_page(self):
        p = FakePage()
        self._pages.append(p)
        return p

    def close(self):
        if self.close_raises:
            raise RuntimeError("ctx close failed")
        self.closed = True


class FakeChromium:
    def __init__(self, ctx, chrome_ok=True, chromium_ok=True):
        self._ctx = ctx
        self.chrome_ok = chrome_ok
        self.chromium_ok = chromium_ok

    def launch_persistent_context(
        self,
        user_data_dir,
        channel=None,
        headless=None,
        args=None,
        ignore_default_args=None,
        chromium_sandbox=None,
    ):
        if channel == "chrome":
            if not self.chrome_ok:
                raise RuntimeError("system chrome not installed")
            return self._ctx
        if not self.chromium_ok:
            raise RuntimeError("bundled chromium failed")
        return self._ctx


class FakePW:
    def __init__(self, chromium):
        self.chromium = chromium
        self.stopped = False

    def stop(self):
        self.stopped = True


class FakeSyncPW:
    def __init__(self, pw):
        self._pw = pw

    def start(self):
        return self._pw


def install_pw(monkeypatch, pw, tmp_path):
    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: FakeSyncPW(pw))
    monkeypatch.setattr(browser, "PROFILE_DIR", tmp_path / "chrome")


# ---------------------------------------------------------------------------
# _parse_music_url — pure logic, real assertions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://music.apple.com/us/album/x/9?i=5", {"song": "5"}),
        ("https://music.apple.com/us/song/strobe/55", {"song": "55"}),
        ("https://music.apple.com/us/album/abbey-road/401", {"album": "401"}),
        ("https://music.apple.com/us/playlist/rock/pl.123", {"playlist": "pl.123"}),
        ("https://music.apple.com/us/artist/foo/1", None),
        ("https://example.com/nope", None),
    ],
)
def test_parse_music_url(url, expected):
    assert browser._parse_music_url(url) == expected


# ---------------------------------------------------------------------------
# _check_signed_in / signed_in / open_and_check_signin
# ---------------------------------------------------------------------------


def test_signed_in_cookie_present(monkeypatch):
    page = FakePage(cookies=[{"name": "media-user-token", "value": "x"}])
    set_engine(monkeypatch, page=page)
    assert browser.open_and_check_signin() is True
    # cookie is already in the jar → DON'T navigate (navigating would clobber any
    # active playback/queue). The non-navigating cookie read is the whole point.
    assert page.goto_calls == []


def test_signed_in_navigates_only_when_cookie_absent(monkeypatch):
    # No cookie up front → it must navigate to load the session, then re-check.
    page = FakePage(cookies=[])
    page.returns["authorized"] = True
    set_engine(monkeypatch, page=page)
    assert browser.signed_in() is True
    assert page.goto_calls and "browse" in page.goto_calls[0][0]


def test_signed_in_falls_back_to_musickit_authorized(monkeypatch):
    page = FakePage(cookies=[])  # no media-user-token cookie
    page.returns["authorized"] = True
    set_engine(monkeypatch, page=page)
    assert browser.signed_in() is True


def test_signed_in_evaluate_raises_returns_false(monkeypatch):
    page = FakePage(cookies=[])
    page.evaluate_raises_on.add("authorized")
    set_engine(monkeypatch, page=page)
    assert browser.signed_in() is False


def test_signed_in_cookie_read_raises_then_authorized(monkeypatch):
    page = FakePage(cookies_raise=True)
    page.returns["authorized"] = False
    set_engine(monkeypatch, page=page)
    assert browser.signed_in() is False


# ---------------------------------------------------------------------------
# add_catalog_track_to_library / _add_via_api
# ---------------------------------------------------------------------------


def test_add_empty_id(monkeypatch):
    set_engine(monkeypatch)
    assert browser.add_catalog_track_to_library("  ") == (False, "Empty song id")


def test_add_success_202(monkeypatch):
    page = FakePage()
    page.returns["add"] = 202
    set_engine(monkeypatch, page=page)
    ok, msg = browser.add_catalog_track_to_library("123", name="My Song")
    assert ok and "My Song" in msg and "202" in msg
    # _add_via_api navigated + waited for tokens then POSTed
    assert page.goto_calls and any(k == "add" for k, _ in page.eval_calls)


def test_add_success_default_label(monkeypatch):
    page = FakePage()
    page.returns["add"] = 200
    set_engine(monkeypatch, page=page)
    ok, msg = browser.add_catalog_track_to_library("777")
    assert ok and "song 777" in msg


def test_add_not_authorized(monkeypatch):
    page = FakePage()
    page.returns["add"] = 401
    set_engine(monkeypatch, page=page)
    ok, msg = browser.add_catalog_track_to_library("9", name="T")
    assert not ok and "Not authorized" in msg and "login" in msg


def test_add_other_status(monkeypatch):
    page = FakePage()
    page.returns["add"] = 500
    set_engine(monkeypatch, page=page)
    ok, msg = browser.add_catalog_track_to_library("9", name="T")
    assert not ok and "HTTP 500" in msg


def test_add_browser_unavailable(monkeypatch):
    set_engine(monkeypatch, error=BrowserUnavailable("no chrome"))
    assert browser.add_catalog_track_to_library("9") == (False, "no chrome")


def test_add_generic_error(monkeypatch):
    set_engine(monkeypatch, raise_exc=RuntimeError("js boom"))
    ok, msg = browser.add_catalog_track_to_library("9", name="T")
    assert not ok and "In-browser add failed for T" in msg and "js boom" in msg


# ---------------------------------------------------------------------------
# _ensure_player_ready (covered through play_catalog_track + crafted pages)
# ---------------------------------------------------------------------------


def test_play_catalog_empty(monkeypatch):
    set_engine(monkeypatch)
    assert browser.play_catalog_track("") == (False, "Empty catalog id")


def test_play_catalog_success_drm(monkeypatch):
    page = FakePage(url="https://music.apple.com/us/browse")  # already ready
    set_engine(monkeypatch, page=page, using_chrome=True)
    ok, msg = browser.play_catalog_track("55")
    assert ok and msg == "Playing: Strobe"


def test_play_catalog_success_no_drm_note(monkeypatch):
    page = FakePage(url="https://music.apple.com/us/browse")
    set_engine(monkeypatch, page=page, using_chrome=False)
    ok, msg = browser.play_catalog_track("55")
    assert ok and msg.startswith("Playing: Strobe") and "preview only" in msg


def test_player_ready_navigates_when_ready_check_raises(monkeypatch):
    # On a music.apple.com page but the MusicKit-ready probe raises -> must
    # fall through to goto + token check + wait_for_function.
    page = FakePage(
        url="https://music.apple.com/us/browse",
        cookies=[{"name": "media-user-token", "value": "tok"}],
    )
    page.evaluate_raises_on.add("ready")
    set_engine(monkeypatch, page=page)
    ok, _ = browser.play_catalog_track("55")
    assert ok
    assert page.goto_calls  # had to re-navigate


def test_player_ready_not_signed_in(monkeypatch):
    page = FakePage(url="", cookies=[])  # no token after navigation
    set_engine(monkeypatch, page=page)
    ok, msg = browser.play_catalog_track("55")
    assert not ok and "Not signed in" in msg


def test_player_ready_musickit_timeout(monkeypatch):
    page = FakePage(url="", cookies=[{"name": "media-user-token", "value": "tok"}])
    page.wait_for_function_raises = True
    set_engine(monkeypatch, page=page)
    ok, msg = browser.play_catalog_track("55")
    assert not ok and "didn't initialize" in msg


def test_play_catalog_generic_error(monkeypatch):
    page = FakePage(url="https://music.apple.com/us/browse")
    page.evaluate_raises_on.add("play_song")
    set_engine(monkeypatch, page=page)
    ok, msg = browser.play_catalog_track("55")
    assert not ok and "Browser play failed" in msg


# ---------------------------------------------------------------------------
# play_descriptor / play_url
# ---------------------------------------------------------------------------


def _ready_page():
    return FakePage(url="https://music.apple.com/us/browse")


def test_play_descriptor_success_and_shuffle(monkeypatch):
    page = _ready_page()
    set_engine(monkeypatch, page=page, using_chrome=True)
    ok, msg = browser.play_descriptor({"album": "alb1"}, shuffle=True)
    assert ok and msg == "Playing: Abbey Road"
    # shuffle flag threaded into the JS payload
    key, payload = page.eval_calls[-1]
    assert key == "play_queue" and payload["__shuffle"] is True and payload["album"] == "alb1"


def test_play_descriptor_browser_unavailable(monkeypatch):
    set_engine(monkeypatch, error=BrowserUnavailable("down"))
    assert browser.play_descriptor({"song": "1"}) == (False, "down")


def test_play_descriptor_generic_error(monkeypatch):
    set_engine(monkeypatch, raise_exc=RuntimeError("kaput"))
    ok, msg = browser.play_descriptor({"song": "1"})
    assert not ok and "Browser play failed: kaput" in msg


def test_play_url_empty(monkeypatch):
    set_engine(monkeypatch)
    assert browser.play_url("   ") == (False, "Empty URL")


def test_play_url_not_apple(monkeypatch):
    set_engine(monkeypatch)
    ok, msg = browser.play_url("https://example.com/x")
    assert not ok and "Not an Apple Music URL" in msg


def test_play_url_unrecognized_shape(monkeypatch):
    set_engine(monkeypatch)
    ok, msg = browser.play_url("https://music.apple.com/us/artist/foo/1")
    assert not ok and "Unrecognized" in msg


def test_play_url_success(monkeypatch):
    page = _ready_page()
    set_engine(monkeypatch, page=page, using_chrome=True)
    ok, msg = browser.play_url("https://music.apple.com/us/album/x/9?i=5")
    assert ok and msg == "Playing: Abbey Road"


# ---------------------------------------------------------------------------
# reveal_url
# ---------------------------------------------------------------------------


def test_reveal_not_apple(monkeypatch):
    set_engine(monkeypatch)
    ok, msg = browser.reveal_url("https://example.com/x")
    assert not ok and "Not an Apple Music URL" in msg


def test_reveal_success(monkeypatch):
    page = FakePage()
    set_engine(monkeypatch, page=page)
    url = "https://music.apple.com/us/song/x/1"
    ok, msg = browser.reveal_url(url)
    assert ok and msg == f"Showing in browser: {url}"
    assert page.goto_calls[0][0] == url


def test_reveal_subdomain_host(monkeypatch):
    page = FakePage()
    set_engine(monkeypatch, page=page)
    ok, _ = browser.reveal_url("https://beta.music.apple.com/us/song/x/1")
    assert ok


def test_reveal_browser_unavailable(monkeypatch):
    set_engine(monkeypatch, error=BrowserUnavailable("nope"))
    assert browser.reveal_url("https://music.apple.com/us/song/x/1") == (False, "nope")


def test_reveal_generic_error(monkeypatch):
    set_engine(monkeypatch, raise_exc=RuntimeError("boom"))
    ok, msg = browser.reveal_url("https://music.apple.com/us/song/x/1")
    assert not ok and "Browser reveal failed: boom" in msg


# ---------------------------------------------------------------------------
# playback_control
# ---------------------------------------------------------------------------


def test_playback_control_ok(monkeypatch):
    page = _ready_page()
    page.returns["control"] = "ok"
    set_engine(monkeypatch, page=page)
    assert browser.playback_control("pause") == (True, "ok")


def test_playback_control_unknown(monkeypatch):
    page = _ready_page()
    page.returns["control"] = "unknown action: bogus"
    set_engine(monkeypatch, page=page)
    ok, msg = browser.playback_control("bogus")
    assert not ok and msg == "unknown action: bogus"


def test_playback_control_error(monkeypatch):
    set_engine(monkeypatch, raise_exc=RuntimeError("x"))
    ok, msg = browser.playback_control("play")
    assert not ok and "Browser control failed: x" in msg


# ---------------------------------------------------------------------------
# browser_settings
# ---------------------------------------------------------------------------


def test_browser_settings_success(monkeypatch):
    page = _ready_page()
    page.returns["settings"] = {"volume": 80, "shuffle": 1, "repeat": 2}
    set_engine(monkeypatch, page=page)
    ok, msg = browser.browser_settings(volume=80, shuffle=True, repeat="all")
    assert ok and msg == "volume=80 shuffle=on repeat=all"


def test_browser_settings_shuffle_off(monkeypatch):
    page = _ready_page()
    page.returns["settings"] = {"volume": 0, "shuffle": 0, "repeat": 0}
    set_engine(monkeypatch, page=page)
    ok, msg = browser.browser_settings()
    assert ok and msg == "volume=0 shuffle=off repeat=none"


def test_browser_settings_error(monkeypatch):
    set_engine(monkeypatch, raise_exc=RuntimeError("nope"))
    ok, msg = browser.browser_settings()
    assert not ok and "Browser settings failed: nope" in msg


# ---------------------------------------------------------------------------
# now_playing
# ---------------------------------------------------------------------------


def test_now_playing_success(monkeypatch):
    page = FakePage()
    page.returns["now"] = {"name": "N", "artist": "A", "album": "Al", "playing": True}
    set_engine(monkeypatch, page=page)
    assert browser.now_playing()["name"] == "N"


def test_now_playing_error_returns_none(monkeypatch):
    set_engine(monkeypatch, raise_exc=RuntimeError("x"))
    assert browser.now_playing() is None


# ---------------------------------------------------------------------------
# queue functions
# ---------------------------------------------------------------------------


def test_queue_list_success(monkeypatch):
    page = _ready_page()
    set_engine(monkeypatch, page=page)
    ok, data = browser.queue_list()
    assert ok and data["items"][0]["name"] == "x"


def test_queue_list_browser_unavailable(monkeypatch):
    set_engine(monkeypatch, error=BrowserUnavailable("bu"))
    assert browser.queue_list() == (False, "bu")


def test_queue_list_error(monkeypatch):
    set_engine(monkeypatch, raise_exc=RuntimeError("x"))
    ok, msg = browser.queue_list()
    assert not ok and "Queue read failed: x" in msg


def test_queue_play_next_success(monkeypatch):
    page = _ready_page()
    page.returns["qnext"] = 3
    set_engine(monkeypatch, page=page)
    ok, msg = browser.queue_play_next("abc")
    assert ok and "Queued to Up Next (3 in queue)" == msg


def test_queue_play_later_success(monkeypatch):
    page = _ready_page()
    page.returns["qlater"] = 4
    set_engine(monkeypatch, page=page)
    ok, msg = browser.queue_play_later("abc")
    assert ok and "Queued to end of Up Next (4 in queue)" == msg


def test_queue_insert_browser_unavailable(monkeypatch):
    set_engine(monkeypatch, error=BrowserUnavailable("bu"))
    assert browser.queue_play_next("x") == (False, "bu")


def test_queue_insert_error(monkeypatch):
    set_engine(monkeypatch, raise_exc=RuntimeError("y"))
    ok, msg = browser.queue_play_later("x")
    assert not ok and "Queue insert failed: y" in msg


def test_queue_remove_success(monkeypatch):
    page = _ready_page()
    page.returns["qremove"] = 2
    set_engine(monkeypatch, page=page)
    ok, msg = browser.queue_remove(1)
    assert ok and "Removed item 1 (2 left in queue)" == msg


def test_queue_remove_out_of_range(monkeypatch):
    page = _ready_page()
    page.returns["qremove"] = -1
    set_engine(monkeypatch, page=page)
    ok, msg = browser.queue_remove(99)
    assert not ok and "No queue item at index 99" == msg


def test_queue_remove_browser_unavailable(monkeypatch):
    set_engine(monkeypatch, error=BrowserUnavailable("bu"))
    assert browser.queue_remove(0) == (False, "bu")


def test_queue_remove_error(monkeypatch):
    set_engine(monkeypatch, raise_exc=RuntimeError("z"))
    ok, msg = browser.queue_remove(0)
    assert not ok and "Queue remove failed: z" in msg


def test_queue_clear_success(monkeypatch):
    page = _ready_page()
    set_engine(monkeypatch, page=page)
    assert browser.queue_clear() == (True, "Cleared the queue")


def test_queue_clear_browser_unavailable(monkeypatch):
    set_engine(monkeypatch, error=BrowserUnavailable("bu"))
    assert browser.queue_clear() == (False, "bu")


def test_queue_clear_error(monkeypatch):
    set_engine(monkeypatch, raise_exc=RuntimeError("z"))
    ok, msg = browser.queue_clear()
    assert not ok and "Queue clear failed: z" in msg


def test_queue_jump_success(monkeypatch):
    page = _ready_page()
    page.returns["qjump"] = 2
    set_engine(monkeypatch, page=page)
    assert browser.queue_jump(2) == (True, "Jumped to item 2")


def test_queue_jump_out_of_range(monkeypatch):
    page = _ready_page()
    page.returns["qjump"] = -1
    set_engine(monkeypatch, page=page)
    ok, msg = browser.queue_jump(99)
    assert not ok and "No queue item at index 99" == msg


def test_queue_jump_browser_unavailable(monkeypatch):
    set_engine(monkeypatch, error=BrowserUnavailable("bu"))
    assert browser.queue_jump(0) == (False, "bu")


def test_queue_jump_error(monkeypatch):
    set_engine(monkeypatch, raise_exc=RuntimeError("z"))
    ok, msg = browser.queue_jump(0)
    assert not ok and "Queue jump failed: z" in msg


def test_queue_autoplay_on(monkeypatch):
    page = _ready_page()
    page.returns["qautoplay"] = True
    set_engine(monkeypatch, page=page)
    assert browser.queue_autoplay(True) == (True, "Autoplay on")


def test_queue_autoplay_off(monkeypatch):
    page = _ready_page()
    page.returns["qautoplay"] = False
    set_engine(monkeypatch, page=page)
    assert browser.queue_autoplay(False) == (True, "Autoplay off")


def test_queue_autoplay_browser_unavailable(monkeypatch):
    set_engine(monkeypatch, error=BrowserUnavailable("bu"))
    assert browser.queue_autoplay(True) == (False, "bu")


def test_queue_autoplay_error(monkeypatch):
    set_engine(monkeypatch, raise_exc=RuntimeError("z"))
    ok, msg = browser.queue_autoplay(True)
    assert not ok and "Queue autoplay failed: z" in msg


# ---------------------------------------------------------------------------
# token capture / availability / drm
# ---------------------------------------------------------------------------


def test_capture_user_token_present(monkeypatch):
    page = FakePage(cookies=[{"name": "media-user-token", "value": "tok123"}])
    set_engine(monkeypatch, page=page)
    assert browser.capture_user_token() == "tok123"


def test_capture_user_token_absent(monkeypatch):
    set_engine(monkeypatch, page=FakePage(cookies=[]))
    assert browser.capture_user_token() is None


def test_read_user_token_cookie_error(monkeypatch):
    # exercise _read_user_token's except path directly
    page = FakePage(cookies_raise=True)
    assert browser._read_user_token(page) is None


def test_cookie_signed_in(monkeypatch):
    page = FakePage(cookies=[{"name": "media-user-token", "value": "t"}])
    assert browser._cookie_signed_in(page) is True
    assert browser._cookie_signed_in(FakePage(cookies=[])) is False


def test_capture_and_save_no_token(monkeypatch):
    monkeypatch.setattr(browser, "capture_user_token", lambda: None)
    assert browser.capture_and_save_user_token() is False


def test_capture_and_save_with_token(monkeypatch):
    monkeypatch.setattr(browser, "capture_user_token", lambda: "tok")
    saved = {}
    import applemusic_mcp.auth as auth

    monkeypatch.setattr(auth, "save_user_token", lambda t: saved.update(t=t))
    assert browser.capture_and_save_user_token() is True
    assert saved["t"] == "tok"


def test_is_available_true():
    assert browser.is_available() is True


def test_is_available_false(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "playwright", None)
    assert browser.is_available() is False


def test_drm_capable(monkeypatch):
    set_engine(monkeypatch, using_chrome=True)
    assert browser.drm_capable() is True
    set_engine(monkeypatch, using_chrome=False)
    assert browser.drm_capable() is False


def test_goto_music(monkeypatch):
    page = FakePage()
    browser._goto_music(page)
    assert "browse" in page.goto_calls[0][0]


# ---------------------------------------------------------------------------
# signin_interactive
# ---------------------------------------------------------------------------


def _patch_time(monkeypatch, monotonic_values):
    import time

    vals = list(monotonic_values)

    def fake_monotonic():
        return vals.pop(0) if vals else 10_000.0

    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(time, "monotonic", fake_monotonic)


def test_signin_interactive_already_signed_in(monkeypatch):
    set_engine(monkeypatch)
    monkeypatch.setattr(browser, "capture_and_save_user_token", lambda: True)
    ok, msg = browser.signin_interactive()
    assert ok and "Already signed in" in msg


def test_signin_interactive_browser_unavailable(monkeypatch):
    set_engine(monkeypatch)

    def boom():
        raise BrowserUnavailable("no chrome")

    monkeypatch.setattr(browser, "capture_and_save_user_token", boom)
    ok, msg = browser.signin_interactive()
    assert not ok and msg == "no chrome"


def test_signin_interactive_poll_success(monkeypatch):
    set_engine(monkeypatch)
    seq = iter([False, True])
    monkeypatch.setattr(browser, "capture_and_save_user_token", lambda: next(seq))
    _patch_time(monkeypatch, [0, 1])
    ok, msg = browser.signin_interactive(timeout_s=90)
    assert ok and "session captured" in msg


def test_signin_interactive_timeout(monkeypatch):
    set_engine(monkeypatch)
    monkeypatch.setattr(browser, "capture_and_save_user_token", lambda: False)
    _patch_time(monkeypatch, [0, 91])
    ok, msg = browser.signin_interactive(timeout_s=90)
    assert not ok and msg == "still-waiting"


def test_signin_interactive_poll_exception_then_timeout(monkeypatch):
    set_engine(monkeypatch)
    calls = {"n": 0}

    def cap():
        calls["n"] += 1
        if calls["n"] == 1:
            return False
        raise RuntimeError("transient")

    monkeypatch.setattr(browser, "capture_and_save_user_token", cap)
    _patch_time(monkeypatch, [0, 1, 91])
    ok, msg = browser.signin_interactive(timeout_s=90)
    assert not ok and msg == "still-waiting"


# ---------------------------------------------------------------------------
# clear_session
# ---------------------------------------------------------------------------


def test_clear_session_removes_profile(monkeypatch, tmp_path):
    eng = set_engine(monkeypatch)
    prof = tmp_path / "chrome"
    prof.mkdir()
    (prof / "marker").write_text("x")
    monkeypatch.setattr(browser, "PROFILE_DIR", prof)
    browser.clear_session()
    assert eng.shutdown_called and not prof.exists()


def test_clear_session_shutdown_raises(monkeypatch, tmp_path):
    eng = FakeEngine()

    def boom(timeout=10.0):
        raise RuntimeError("shutdown failed")

    eng.shutdown = boom
    monkeypatch.setattr(browser, "_engine", eng)
    prof = tmp_path / "chrome"  # does not exist -> skips rmtree branch
    monkeypatch.setattr(browser, "PROFILE_DIR", prof)
    browser.clear_session()  # must not raise


def test_clear_session_exists_raises(monkeypatch):
    set_engine(monkeypatch)

    class BadPath:
        def exists(self):
            raise RuntimeError("stat failed")

    monkeypatch.setattr(browser, "PROFILE_DIR", BadPath())
    browser.clear_session()  # outer try/except swallows it


# ---------------------------------------------------------------------------
# CLI helpers: _cli_signin / _cli_add
# ---------------------------------------------------------------------------


def test_cli_signin_already_signed_in(monkeypatch, capsys):
    set_engine(monkeypatch)
    monkeypatch.setattr(browser, "_cookie_signed_in", lambda page: True)
    monkeypatch.setattr(browser, "capture_and_save_user_token", lambda: True)
    assert browser._cli_signin() == 0
    assert "Already signed in" in capsys.readouterr().out


def test_cli_signin_browser_unavailable(monkeypatch, capsys):
    set_engine(monkeypatch, error=BrowserUnavailable("no chrome"))
    assert browser._cli_signin() == 2
    assert "unavailable" in capsys.readouterr().out


def test_cli_signin_poll_success(monkeypatch, capsys):
    set_engine(monkeypatch)
    seq = iter([False, True])
    monkeypatch.setattr(browser, "_cookie_signed_in", lambda page: next(seq))
    monkeypatch.setattr(browser, "capture_and_save_user_token", lambda: True)
    _patch_time(monkeypatch, [0, 1])
    assert browser._cli_signin(timeout_s=600) == 0
    assert "Signed in" in capsys.readouterr().out


def test_cli_signin_timeout(monkeypatch, capsys):
    set_engine(monkeypatch)
    monkeypatch.setattr(browser, "_cookie_signed_in", lambda page: False)
    _patch_time(monkeypatch, [0, 601])
    assert browser._cli_signin(timeout_s=600) == 1
    assert "Timed out" in capsys.readouterr().out


def test_cli_signin_poll_exception(monkeypatch):
    set_engine(monkeypatch)
    calls = {"n": 0}

    def cookie(page):
        calls["n"] += 1
        if calls["n"] == 1:
            return False
        raise RuntimeError("flaky")

    monkeypatch.setattr(browser, "_cookie_signed_in", cookie)
    _patch_time(monkeypatch, [0, 1, 601])
    assert browser._cli_signin(timeout_s=600) == 1


def test_cli_add_not_signed_in(monkeypatch, capsys):
    set_engine(monkeypatch)
    monkeypatch.setattr(browser, "_cookie_signed_in", lambda page: False)
    assert browser._cli_add("9") == 1
    assert "Not signed in" in capsys.readouterr().out


def test_cli_add_browser_unavailable(monkeypatch, capsys):
    set_engine(monkeypatch, error=BrowserUnavailable("no chrome"))
    assert browser._cli_add("9") == 2
    assert "unavailable" in capsys.readouterr().out


def test_cli_add_ok(monkeypatch, capsys):
    set_engine(monkeypatch)
    monkeypatch.setattr(browser, "_cookie_signed_in", lambda page: True)
    monkeypatch.setattr(browser, "add_catalog_track_to_library", lambda sid: (True, "Added"))
    assert browser._cli_add("9") == 0
    assert "OK:" in capsys.readouterr().out


def test_cli_add_fail(monkeypatch, capsys):
    set_engine(monkeypatch)
    monkeypatch.setattr(browser, "_cookie_signed_in", lambda page: True)
    monkeypatch.setattr(browser, "add_catalog_track_to_library", lambda sid: (False, "nope"))
    assert browser._cli_add("9") == 1
    assert "FAIL:" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Real _BrowserEngine on its dedicated thread, driven by a fake Playwright
# ---------------------------------------------------------------------------


def test_engine_import_error(monkeypatch):
    eng = browser._BrowserEngine()
    monkeypatch.delattr("playwright.sync_api.sync_playwright", raising=True)
    with pytest.raises(BrowserUnavailable) as e:
        eng._ensure_thread()
    # Platform-aware message: macOS points at the [browser] extra / Safari; off-mac
    # at reinstalling Playwright. Both name Playwright.
    assert "Playwright" in str(e.value)


def test_engine_launch_failure(monkeypatch, tmp_path):
    ctx = FakeCtx(pages=[])
    pw = FakePW(FakeChromium(ctx, chrome_ok=False, chromium_ok=False))
    install_pw(monkeypatch, pw, tmp_path)
    eng = browser._BrowserEngine()
    with pytest.raises(BrowserUnavailable) as e:
        eng._ensure_thread()
    assert "Could not launch browser" in str(e.value)


def test_engine_ensure_thread_reraises_start_error():
    eng = browser._BrowserEngine()

    class _Alive:
        def is_alive(self):
            return True

    eng._thread = _Alive()
    eng._start_error = BrowserUnavailable("prior failure")
    with pytest.raises(BrowserUnavailable):
        eng._ensure_thread()


def test_engine_chrome_path_roundtrip_and_shutdown(monkeypatch, tmp_path):
    page = FakePage(cookies=[{"name": "media-user-token", "value": "tok123"}])
    ctx = FakeCtx(pages=[page], close_raises=True)  # non-empty pages branch + close raises
    pw = FakePW(FakeChromium(ctx, chrome_ok=True))
    install_pw(monkeypatch, pw, tmp_path)
    # chmod failure path (102-104) — must be swallowed
    monkeypatch.setattr(os, "chmod", lambda *a, **k: (_ for _ in ()).throw(OSError("denied")))

    eng = browser._BrowserEngine()
    # real submit round-trips a callable to the owner thread and back
    assert eng.submit(browser._read_user_token) == "tok123"
    # second submit hits the "thread already alive" fast path
    assert eng.submit(lambda p: 42) == 42
    assert eng._using_chrome is True
    # exception marshalling from owner thread to caller
    with pytest.raises(ValueError):
        eng.submit(lambda p: (_ for _ in ()).throw(ValueError("z")))
    eng.shutdown()  # _STOP -> ctx.close raises -> except swallows it (pw.stop skipped)
    assert eng._thread is not None and not eng._thread.is_alive()


def test_engine_chromium_fallback_and_new_page(monkeypatch, tmp_path):
    ctx = FakeCtx(pages=[])  # empty -> new_page() branch
    pw = FakePW(FakeChromium(ctx, chrome_ok=False, chromium_ok=True))
    install_pw(monkeypatch, pw, tmp_path)
    eng = browser._BrowserEngine()
    assert eng.submit(lambda p: "ok") == "ok"
    assert eng._using_chrome is False
    assert len(ctx.pages) == 1  # a page was created
    eng.shutdown()


def test_engine_shutdown_when_not_started():
    eng = browser._BrowserEngine()
    eng.shutdown()  # no thread -> no-op, must not raise


def test_ensure_session_cookie_injects_saved_token(monkeypatch):
    """A saved (e.g. Safari-harvested) token is bridged into the profile so the web
    player authorizes off the same sign-in."""
    import applemusic_mcp.auth as real_auth

    page = FakePage(cookies=[])  # profile has no cookie yet
    added = []
    page.context.add_cookies = lambda c: added.append(c)
    monkeypatch.setattr(real_auth, "get_user_token", lambda: "SAVEDTOK")
    browser._ensure_session_cookie(page)
    assert added and added[0][0]["name"] == "media-user-token"
    assert added[0][0]["value"] == "SAVEDTOK"


def test_ensure_session_cookie_noop_when_cookie_already_current(monkeypatch):
    """If the profile's cookie already equals our saved token, do nothing."""
    import applemusic_mcp.auth as real_auth

    page = FakePage(cookies=[{"name": "media-user-token", "value": "SAVEDTOK"}])
    added = []
    page.context.add_cookies = lambda c: added.append(c)
    monkeypatch.setattr(real_auth, "get_user_token", lambda: "SAVEDTOK")
    browser._ensure_session_cookie(page)
    assert added == []  # already current → don't re-set


def test_ensure_session_cookie_replaces_stale_cookie(monkeypatch):
    """A stale profile cookie (≠ our saved token, e.g. left by a corrupt/ungraceful
    profile) is REPLACED with the saved token — the fix for 'signed-out every launch'."""
    import applemusic_mcp.auth as real_auth

    page = FakePage(cookies=[{"name": "media-user-token", "value": "STALE"}])
    added = []
    page.context.add_cookies = lambda c: added.append(c)
    monkeypatch.setattr(real_auth, "get_user_token", lambda: "FRESHTOK")
    browser._ensure_session_cookie(page)
    assert added and added[0][0]["value"] == "FRESHTOK"


def test_ensure_session_cookie_noop_when_no_saved_token(monkeypatch):
    import applemusic_mcp.auth as real_auth

    page = FakePage(cookies=[])
    added = []
    page.context.add_cookies = lambda c: added.append(c)

    def _none():
        raise FileNotFoundError("no token")

    monkeypatch.setattr(real_auth, "get_user_token", _none)
    browser._ensure_session_cookie(page)
    assert added == []  # nothing to bridge
