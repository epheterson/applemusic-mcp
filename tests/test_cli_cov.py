"""Full-coverage unit tests for applemusic_mcp.cli (the slim 5-verb CLI).

Everything is mocked at the boundary (auth/browser/server/requests/input), so
no real tokens, network, or browser are touched.
"""

import runpy
import sys
import types

import pytest

from applemusic_mcp import cli


def _args(**kw):
    """Build an argparse-like namespace with CLI defaults."""
    base = dict(
        dev=False, team_id=None, key_id=None, key_path=None, days=180, port=8765, force=False
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


# --- login (web) -----------------------------------------------------------


def test_login_web_delegates_to_browser(monkeypatch):
    """Off macOS, plain `login` uses the Chrome web flow."""
    called = {}

    def fake_signin():
        called["signin"] = True
        return 0

    monkeypatch.setattr("platform.system", lambda: "Linux")
    monkeypatch.setitem(
        sys.modules,
        "applemusic_mcp.browser",
        types.SimpleNamespace(
            _cli_signin=fake_signin, _profile_in_use=lambda: False, is_available=lambda: True
        ),
    )
    assert cli.cmd_login(_args(dev=False)) == 0
    assert called["signin"] is True


def test_login_macos_defaults_to_safari(monkeypatch):
    """On macOS, plain `login` (no --chrome) harvests from Safari — no Chrome."""
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    from applemusic_mcp import auth, safari as safari_mod

    saved = {}
    monkeypatch.setattr(safari_mod, "media_user_token", lambda: (True, "TOK"))
    monkeypatch.setattr(auth, "save_user_token", lambda t: saved.setdefault("t", t))
    assert cli.cmd_login(_args(dev=False)) == 0
    assert saved["t"] == "TOK"


def test_login_macos_chrome_needs_browser_extra(monkeypatch, capsys):
    """macOS --chrome with Playwright absent guides to the extra / Safari, no crash."""
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setitem(
        sys.modules,
        "applemusic_mcp.browser",
        types.SimpleNamespace(
            _cli_signin=lambda: 0, _profile_in_use=lambda: False, is_available=lambda: False
        ),
    )
    assert cli.cmd_login(_args(dev=False, chrome=True)) == 1
    out = capsys.readouterr().out.lower()
    assert "browser" in out and ("safari" in out or "playwright" in out)


# --- login --dev -----------------------------------------------------------


def test_login_dev_config_exists(monkeypatch, tmp_path, capsys):
    cfg = tmp_path
    (cfg / "config.json").write_text("{}")
    monkeypatch.setattr(cli, "get_config_dir", lambda: cfg)
    monkeypatch.setattr(cli, "generate_developer_token", lambda expiry_days=180: "DEVTOK")
    monkeypatch.setattr(cli, "run_auth_server", lambda port=8765: "USERTOK")
    assert cli.cmd_login(_args(dev=True)) == 0
    assert "Developer token generated" in capsys.readouterr().out


def test_login_dev_writes_config_from_flags(monkeypatch, tmp_path):
    cfg = tmp_path / "cfgdir"
    monkeypatch.setattr(cli, "get_config_dir", lambda: cfg)
    monkeypatch.setattr(cli, "generate_developer_token", lambda expiry_days=180: "DEVTOK")
    monkeypatch.setattr(cli, "run_auth_server", lambda port=8765: "USERTOK")
    rc = cli.cmd_login(_args(dev=True, team_id="T1", key_id="K1", key_path="/k.p8"))
    assert rc == 0
    written = (cfg / "config.json").read_text()
    assert "T1" in written and "K1" in written and "/k.p8" in written


def test_login_dev_prompts_for_missing(monkeypatch, tmp_path):
    cfg = tmp_path / "cfgdir"
    monkeypatch.setattr(cli, "get_config_dir", lambda: cfg)
    answers = iter(["TEAM", "KEY", "/path.p8"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    monkeypatch.setattr(cli, "generate_developer_token", lambda expiry_days=180: "DEVTOK")
    monkeypatch.setattr(cli, "run_auth_server", lambda port=8765: "USERTOK")
    assert cli.cmd_login(_args(dev=True)) == 0
    assert "TEAM" in (cfg / "config.json").read_text()


def test_login_dev_missing_required_aborts(monkeypatch, tmp_path, capsys):
    cfg = tmp_path / "cfgdir"
    monkeypatch.setattr(cli, "get_config_dir", lambda: cfg)
    monkeypatch.setattr("builtins.input", lambda *a: "")  # all blank
    assert cli.cmd_login(_args(dev=True)) == 1
    assert "required" in capsys.readouterr().out.lower()


def test_login_dev_generate_filenotfound(monkeypatch, tmp_path, capsys):
    cfg = tmp_path
    (cfg / "config.json").write_text("{}")
    monkeypatch.setattr(cli, "get_config_dir", lambda: cfg)

    def boom(expiry_days=180):
        raise FileNotFoundError("no key")

    monkeypatch.setattr(cli, "generate_developer_token", boom)
    assert cli.cmd_login(_args(dev=True)) == 1
    assert "Error" in capsys.readouterr().out


def test_login_dev_generate_generic_error(monkeypatch, tmp_path, capsys):
    cfg = tmp_path
    (cfg / "config.json").write_text("{}")
    monkeypatch.setattr(cli, "get_config_dir", lambda: cfg)

    def boom(expiry_days=180):
        raise ValueError("bad")

    monkeypatch.setattr(cli, "generate_developer_token", boom)
    assert cli.cmd_login(_args(dev=True)) == 1
    assert "Error generating token" in capsys.readouterr().out


def test_login_dev_authorize_fails(monkeypatch, tmp_path):
    cfg = tmp_path
    (cfg / "config.json").write_text("{}")
    monkeypatch.setattr(cli, "get_config_dir", lambda: cfg)
    monkeypatch.setattr(cli, "generate_developer_token", lambda expiry_days=180: "DEVTOK")
    monkeypatch.setattr(cli, "run_auth_server", lambda port=8765: None)
    assert cli.cmd_login(_args(dev=True)) == 1


# --- logout ----------------------------------------------------------------


def test_logout(monkeypatch, capsys):
    # `from . import browser` reads the package attribute, so patch the real
    # module's clear_session (not a sys.modules fake). `from .auth import
    # secret_delete` reads from the module, so patch that on the real module too.
    import applemusic_mcp.auth as real_auth
    import applemusic_mcp.browser as real_browser

    deleted = []
    cleared = {}
    monkeypatch.setattr(real_browser, "clear_session", lambda: cleared.setdefault("y", True))
    monkeypatch.setattr(real_auth, "secret_delete", lambda k: deleted.append(k))
    assert cli.cmd_logout(_args()) == 0
    assert "music_user_token" in deleted and "harvested_token" in deleted
    assert cleared["y"] and "Signed out" in capsys.readouterr().out


# --- reset -----------------------------------------------------------------


def test_reset_no_force(capsys):
    assert cli.cmd_reset(_args(force=False)) == 1
    assert "force" in capsys.readouterr().out.lower()


def test_reset_force_with_config(monkeypatch, tmp_path, capsys):
    cfg = tmp_path
    (cfg / "config.json").write_text("{}")
    deleted = []
    fake_browser = types.SimpleNamespace(clear_session=lambda: None)
    fake_auth = types.SimpleNamespace(secret_delete=lambda k: deleted.append(k))
    monkeypatch.setitem(sys.modules, "applemusic_mcp.browser", fake_browser)
    monkeypatch.setitem(sys.modules, "applemusic_mcp.auth", fake_auth)
    monkeypatch.setattr(cli, "get_config_dir", lambda: cfg)
    assert cli.cmd_reset(_args(force=True)) == 0
    assert not (cfg / "config.json").exists()
    assert "developer_token" in deleted
    assert "Reset complete" in capsys.readouterr().out


def test_reset_force_no_config(monkeypatch, tmp_path):
    cfg = tmp_path / "empty"
    cfg.mkdir()
    fake_browser = types.SimpleNamespace(clear_session=lambda: None)
    fake_auth = types.SimpleNamespace(secret_delete=lambda k: None)
    monkeypatch.setitem(sys.modules, "applemusic_mcp.browser", fake_browser)
    monkeypatch.setitem(sys.modules, "applemusic_mcp.auth", fake_auth)
    monkeypatch.setattr(cli, "get_config_dir", lambda: cfg)
    assert cli.cmd_reset(_args(force=True)) == 0  # no unlink branch


# --- status ----------------------------------------------------------------


def _status_auth(dev_info, user):
    return types.SimpleNamespace(developer_token_info=lambda: dev_info, has_user_token=lambda: user)


def test_status_valid_and_api_ok(monkeypatch, tmp_path, capsys):
    import time

    monkeypatch.setattr(cli, "get_config_dir", lambda: tmp_path)
    monkeypatch.setitem(
        sys.modules, "applemusic_mcp.auth", _status_auth({"expires": time.time() + 99999}, True)
    )
    monkeypatch.setattr(cli, "get_developer_token", lambda: "D")
    monkeypatch.setattr(cli, "get_user_token", lambda: "U")
    fake_requests = types.SimpleNamespace(
        get=lambda *a, **k: types.SimpleNamespace(status_code=200)
    )
    monkeypatch.setitem(sys.modules, "requests", fake_requests)
    assert cli.cmd_status(_args()) == 0
    out = capsys.readouterr().out
    assert "valid" in out and "present" in out and "API: ok" in out


def test_status_expired_nonok_api(monkeypatch, tmp_path, capsys):
    import time

    monkeypatch.setattr(cli, "get_config_dir", lambda: tmp_path)
    monkeypatch.setitem(
        sys.modules, "applemusic_mcp.auth", _status_auth({"expires": time.time() - 10}, False)
    )
    monkeypatch.setattr(cli, "get_developer_token", lambda: "D")
    monkeypatch.setattr(cli, "get_user_token", lambda: "U")
    fake_requests = types.SimpleNamespace(
        get=lambda *a, **k: types.SimpleNamespace(status_code=403)
    )
    monkeypatch.setitem(sys.modules, "requests", fake_requests)
    assert cli.cmd_status(_args()) == 0
    out = capsys.readouterr().out
    assert "expired" in out and "none" in out and "status 403" in out


def test_status_no_token_filenotfound(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "get_config_dir", lambda: tmp_path)
    monkeypatch.setitem(sys.modules, "applemusic_mcp.auth", _status_auth(None, False))

    def boom():
        raise FileNotFoundError()

    monkeypatch.setattr(cli, "get_developer_token", boom)
    monkeypatch.setattr(cli, "get_user_token", lambda: "U")
    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(get=lambda *a, **k: None))
    assert cli.cmd_status(_args()) == 0
    assert "not configured" in capsys.readouterr().out


def test_status_api_generic_error(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "get_config_dir", lambda: tmp_path)
    monkeypatch.setitem(sys.modules, "applemusic_mcp.auth", _status_auth(None, True))
    monkeypatch.setattr(cli, "get_developer_token", lambda: "D")
    monkeypatch.setattr(cli, "get_user_token", lambda: "U")

    def boom(*a, **k):
        raise RuntimeError("net down")

    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(get=boom))
    assert cli.cmd_status(_args()) == 0
    assert "error" in capsys.readouterr().out.lower()


# --- serve -----------------------------------------------------------------


def test_serve(monkeypatch):
    called = {}
    fake_server = types.SimpleNamespace(main=lambda: called.setdefault("ran", True))
    monkeypatch.setitem(sys.modules, "applemusic_mcp.server", fake_server)
    cli.cmd_serve(_args())
    assert called["ran"]


# --- main() dispatch -------------------------------------------------------


@pytest.mark.parametrize(
    "argv,fn",
    [
        (["login"], "cmd_login"),
        (["signin"], "cmd_login"),  # hidden alias
        (["logout"], "cmd_logout"),
        (["status"], "cmd_status"),
        (["reset"], "cmd_reset"),
    ],
)
def test_main_dispatch_exits(monkeypatch, argv, fn):
    monkeypatch.setattr(sys, "argv", ["applemusic-mcp"] + argv)
    monkeypatch.setattr(cli, fn, lambda args: 0)
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 0


def test_main_serve_no_exit(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["applemusic-mcp", "serve"])
    ran = {}
    monkeypatch.setattr(cli, "cmd_serve", lambda args: ran.setdefault("y", True))
    cli.main()  # serve does not sys.exit
    assert ran["y"]


def test_main_no_command_prints_help(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["applemusic-mcp"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 0
    assert "usage" in capsys.readouterr().out.lower()


def test_dunder_main_entrypoint(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["applemusic-mcp", "status"])
    monkeypatch.setattr(cli, "cmd_status", lambda args: 0)
    with pytest.raises(SystemExit):
        runpy.run_module("applemusic_mcp.cli", run_name="__main__")


def test_reset_all_wipes_all_state_dirs(tmp_path, monkeypatch):
    """reset --all --force removes every state root (full uninstall)."""
    import types

    monkeypatch.setenv("APPLEMUSIC_MCP_HOME", str(tmp_path))
    from applemusic_mcp import auth, browser, paths

    monkeypatch.setattr(browser, "clear_session", lambda: None)
    monkeypatch.setattr(auth, "secret_delete", lambda k: None)
    for d in paths.all_state_dirs():
        d.mkdir(parents=True, exist_ok=True)
        (d / "marker").write_text("x")
    rc = cli.cmd_reset(types.SimpleNamespace(force=True, all=True))
    assert rc == 0
    assert all(not d.exists() for d in paths.all_state_dirs())


def test_status_web_session_not_misreported(monkeypatch, tmp_path, capsys):
    """A signed-in web/Safari user (no generated dev token) must read as OK, not
    'not configured' — the regression that told a just-signed-in user to sign in.

    Patches the real modules' attributes (not sys.modules): cmd_status uses
    `from . import amp_api` / `from .auth import ...`, which read the already-bound
    package attributes regardless of a sys.modules swap (order-dependent otherwise)."""
    import applemusic_mcp.amp_api as real_amp
    import applemusic_mcp.auth as real_auth

    monkeypatch.setattr(cli, "get_config_dir", lambda: tmp_path)
    monkeypatch.setattr(real_auth, "developer_token_info", lambda: None)
    monkeypatch.setattr(real_auth, "has_user_token", lambda: True)
    monkeypatch.setattr(real_amp, "session_status", lambda: "ok")

    def _no_dev():
        raise FileNotFoundError("no generated developer token")

    monkeypatch.setattr(cli, "get_developer_token", _no_dev)
    monkeypatch.setattr(cli, "get_user_token", lambda: "U")
    assert cli.cmd_status(_args()) == 0
    out = capsys.readouterr().out
    assert "API: ok (web session)" in out
    assert "not configured" not in out
    assert "web-player token" in out  # dev-token line clarifies the web path
