"""Tests for the Safari media-user-token harvest (macOS opt-in onboarding).

The AppleScript boundary (run_applescript) is mocked — the live read is validated
by hand on a signed-in Safari. These lock the parsing, the two recoverable-error
classifications, and the CLI wiring.
"""

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
    monkeypatch.setattr(safari_mod, "media_user_token", lambda: (True, "TOK"))
    monkeypatch.setattr(auth, "save_user_token", lambda t: saved.setdefault("t", t))
    import types

    rc = cli.cmd_login(types.SimpleNamespace(dev=False, safari=True))
    assert rc == 0 and saved["t"] == "TOK"


def test_cli_login_safari_failure_returns_1(monkeypatch):
    from applemusic_mcp import cli, safari as safari_mod
    import types

    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr(safari_mod, "media_user_token", lambda: (False, "not signed in"))
    rc = cli.cmd_login(types.SimpleNamespace(dev=False, safari=True))
    assert rc == 1


def test_cli_login_safari_non_macos(monkeypatch):
    from applemusic_mcp import cli
    import types

    monkeypatch.setattr("platform.system", lambda: "Linux")
    rc = cli.cmd_login(types.SimpleNamespace(dev=False, safari=True))
    assert rc == 1
