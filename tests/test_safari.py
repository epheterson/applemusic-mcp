"""Tests for the Safari media-user-token harvest (macOS opt-in onboarding).

The AppleScript boundary (run_applescript) is mocked — the live read is validated
by hand on a signed-in Safari. These lock the parsing, the two recoverable-error
classifications, and the CLI wiring.
"""

import pytest

from applemusic_mcp import safari


def test_parse_media_user_token_extracts():
    cookies = "POD=us~en; media-user-token=AbCd123signedtoken==; geo=US"
    assert safari._parse_media_user_token(cookies) == "AbCd123signedtoken=="


def test_parse_media_user_token_absent():
    assert safari._parse_media_user_token("POD=us~en; geo=US") == ""
    assert safari._parse_media_user_token("") == ""


def test_parse_media_user_token_not_a_prefix_substring():
    # must match the real cookie, not a lookalike substring of another cookie
    assert safari._parse_media_user_token("xmedia-user-token=NOPE; geo=US") == ""


def test_looks_like_js_blocked():
    assert safari._looks_like_js_blocked(
        "Safari got an error: Allowing JavaScript from Apple Events is turned off."
    )
    assert not safari._looks_like_js_blocked("Safari got an error: no document.")


def test_media_user_token_success(monkeypatch):
    monkeypatch.setattr(
        safari, "run_applescript", lambda s: (True, "POD=us; media-user-token=TOK123; geo=US")
    )
    ok, res = safari.media_user_token()
    assert ok is True and res == "TOK123"


def test_media_user_token_not_signed_in(monkeypatch):
    monkeypatch.setattr(safari, "run_applescript", lambda s: (True, "POD=us; geo=US"))
    ok, res = safari.media_user_token()
    assert ok is False and "sign in" in res.lower()


def test_media_user_token_setting_off(monkeypatch):
    monkeypatch.setattr(
        safari,
        "run_applescript",
        lambda s: (False, "Allowing JavaScript from Apple Events is turned off"),
    )
    ok, res = safari.media_user_token()
    assert ok is False and "Allow JavaScript from Apple Events" in res


def test_media_user_token_other_error(monkeypatch):
    monkeypatch.setattr(safari, "run_applescript", lambda s: (False, "Safari isn't running"))
    ok, res = safari.media_user_token()
    assert ok is False and "Couldn't read" in res


def test_cli_login_safari_success(monkeypatch):
    from applemusic_mcp import auth, cli, safari as safari_mod

    saved = {}
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr(safari_mod, "media_user_token", lambda **k: (True, "TOK"))
    monkeypatch.setattr(auth, "save_user_token", lambda t: saved.setdefault("t", t))
    import types

    rc = cli.cmd_login(types.SimpleNamespace(dev=False, safari=True))
    assert rc == 0 and saved["t"] == "TOK"


def test_cli_login_safari_failure_returns_1(monkeypatch):
    from applemusic_mcp import cli, safari as safari_mod
    import types

    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr(safari_mod, "media_user_token", lambda **k: (False, "not signed in"))
    rc = cli.cmd_login(types.SimpleNamespace(dev=False, safari=True))
    assert rc == 1


def test_cli_login_safari_non_macos(monkeypatch):
    from applemusic_mcp import cli
    import types

    monkeypatch.setattr("platform.system", lambda: "Linux")
    rc = cli.cmd_login(types.SimpleNamespace(dev=False, safari=True))
    assert rc == 1


class _Clock:
    """A fake monotonic clock that sleep() advances."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.sleeps.append(s)
        self.now += s


def test_wait_for_sign_in_returns_the_token_once_it_appears(monkeypatch):
    replies = [(False, safari.NOT_SIGNED_IN)] * 2 + [(True, "TOK")]
    monkeypatch.setattr(safari, "media_user_token", lambda **k: replies.pop(0))
    clock = _Clock()

    assert safari.wait_for_sign_in(300, 3, sleep=clock.sleep, clock=clock) == (True, "TOK")
    assert clock.sleeps == [3, 3]


def test_wait_for_sign_in_gives_up_at_the_timeout(monkeypatch):
    monkeypatch.setattr(safari, "media_user_token", lambda **k: (False, safari.NOT_SIGNED_IN))
    clock = _Clock()

    assert safari.wait_for_sign_in(9, 3, sleep=clock.sleep, clock=clock) == (
        False,
        safari.NOT_SIGNED_IN,
    )
    assert clock.now == 6  # the last poll is at 6s; one at 9s would start past the deadline


def test_wait_for_sign_in_stops_on_a_different_error(monkeypatch):
    """Waiting only helps someone who still has to sign in; a blocked setting won't fix itself."""
    monkeypatch.setattr(safari, "media_user_token", lambda **k: (False, safari._SETTING_OFF_MSG))
    clock = _Clock()

    assert safari.wait_for_sign_in(300, 3, sleep=clock.sleep, clock=clock)[0] is False
    assert clock.sleeps == []


def test_show_sign_in_brings_the_apple_music_tab_forward(monkeypatch):
    calls = []
    monkeypatch.setattr(safari, "run_applescript", lambda s: calls.append(s) or (True, "shown"))
    assert safari.show_sign_in() == (True, "shown")

    assert "activate" in calls[0] and "https://music.apple.com" in calls[0]
    # Selects the tab and raises its window, not just Safari.
    assert "set current tab of w to t" in calls[0]
    assert "set index of w to 1" in calls[0]


def test_cli_login_safari_waits_for_you_to_sign_in(monkeypatch, capsys):
    """Not signed in: open the tab, say so, wait, then save the token. It used to
    print setup steps and quit, so you had to sign in and run login again."""
    from applemusic_mcp import auth, cli, safari as safari_mod
    import types

    saved, shown = {}, []
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr(safari_mod, "media_user_token", lambda **k: (False, safari_mod.NOT_SIGNED_IN))
    monkeypatch.setattr(safari_mod, "show_sign_in", lambda: shown.append(1) or (True, "shown"))
    monkeypatch.setattr(safari_mod, "wait_for_sign_in", lambda: (True, "TOK"))
    monkeypatch.setattr(auth, "save_user_token", lambda t: saved.setdefault("t", t))

    rc = cli.cmd_login(types.SimpleNamespace(dev=False, safari=True))

    assert rc == 0 and saved["t"] == "TOK" and shown == [1]
    assert "Sign in to Apple Music in Safari" in capsys.readouterr().out


def test_cli_login_safari_wait_times_out(monkeypatch, capsys):
    from applemusic_mcp import cli, safari as safari_mod
    import types

    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr(safari_mod, "media_user_token", lambda **k: (False, safari_mod.NOT_SIGNED_IN))
    monkeypatch.setattr(safari_mod, "show_sign_in", lambda: (True, "shown"))
    monkeypatch.setattr(safari_mod, "wait_for_sign_in", lambda: (False, safari_mod.NOT_SIGNED_IN))

    assert cli.cmd_login(types.SimpleNamespace(dev=False, safari=True)) == 1
    assert "login --dev" in capsys.readouterr().out


def test_cli_login_safari_ctrl_c_while_waiting(monkeypatch, capsys):
    from applemusic_mcp import cli, safari as safari_mod
    import types

    def interrupted():
        raise KeyboardInterrupt

    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr(safari_mod, "media_user_token", lambda **k: (False, safari_mod.NOT_SIGNED_IN))
    monkeypatch.setattr(safari_mod, "show_sign_in", lambda: (True, "shown"))
    monkeypatch.setattr(safari_mod, "wait_for_sign_in", interrupted)

    assert cli.cmd_login(types.SimpleNamespace(dev=False, safari=True)) == 1
    assert "Stopped waiting" in capsys.readouterr().out


def test_scripts_only_trust_the_real_apple_music_host():
    """`starts with "https://music.apple.com"` also matched
    https://music.apple.com.example.net, so login could bring that tab forward and
    read its cookies (cubic, PR #81)."""
    for script in (safari._HARVEST_SCRIPT, safari._POLL_SCRIPT, safari._SHOW_SCRIPT):
        assert 'starts with "https://music.apple.com"' not in script
        assert 'starts with "https://music.apple.com/"' in script


def test_show_sign_in_reports_failure(monkeypatch):
    monkeypatch.setattr(safari, "run_applescript", lambda s: (False, "Safari isn't there"))
    assert safari.show_sign_in() == (False, "Safari isn't there")


def test_wait_for_sign_in_keeps_going_through_a_passing_error(monkeypatch):
    """One osascript hiccup shouldn't end a five-minute wait."""
    replies = [(False, "Couldn't read the session from Safari: timed out"), (True, "TOK")]
    monkeypatch.setattr(safari, "media_user_token", lambda **k: replies.pop(0))
    clock = _Clock()

    assert safari.wait_for_sign_in(300, 3, sleep=clock.sleep, clock=clock) == (True, "TOK")


def test_wait_for_sign_in_does_not_poll_past_the_deadline(monkeypatch):
    polls = []
    monkeypatch.setattr(
        safari, "media_user_token", lambda **k: polls.append(1) or (False, safari.NOT_SIGNED_IN)
    )
    clock = _Clock()
    safari.wait_for_sign_in(9, 3, sleep=clock.sleep, clock=clock)

    assert len(polls) == 3  # at t=0, 3 and 6; none starts at the 9s deadline


def test_cli_login_safari_says_when_it_cannot_open_safari(monkeypatch, capsys):
    from applemusic_mcp import cli, safari as safari_mod
    import types

    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr(safari_mod, "media_user_token", lambda **k: (False, safari_mod.NOT_SIGNED_IN))
    monkeypatch.setattr(safari_mod, "show_sign_in", lambda: (False, "Safari isn't there"))
    monkeypatch.setattr(
        safari_mod, "wait_for_sign_in", lambda: pytest.fail("don't wait on a tab that isn't there")
    )

    assert cli.cmd_login(types.SimpleNamespace(dev=False, safari=True)) == 1
    assert "Safari isn't there" in capsys.readouterr().out


def test_polls_while_waiting_never_open_a_tab(monkeypatch):
    """The first read opens music.apple.com if no tab is there. While waiting,
    a closed tab means "not signed in yet": opening one on every 3-second poll
    would fill Safari with tabs (final review, 0.22.1)."""
    scripts = []
    replies = [(True, "geo=US"), (True, "media-user-token=TOK")]
    monkeypatch.setattr(safari, "run_applescript", lambda s: scripts.append(s) or replies.pop(0))
    clock = _Clock()

    assert safari.wait_for_sign_in(300, 3, sleep=clock.sleep, clock=clock) == (True, "TOK")
    assert scripts and all("make new document" not in s for s in scripts)


def test_a_poll_with_no_tab_reads_as_not_signed_in(monkeypatch):
    monkeypatch.setattr(safari, "run_applescript", lambda s: (True, safari._NO_TAB))
    assert safari.media_user_token(open_tab=False) == (False, safari.NOT_SIGNED_IN)
