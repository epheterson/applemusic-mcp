"""Full line-coverage tests for applemusic_mcp.cli.

Every command and the argparse/main dispatch are exercised, including the
error/edge branches. Boundaries (auth/server/browser/requests) are mocked so
nothing touches the network, the keychain, or a real Music.app.
"""

import argparse
import json
import time
from unittest.mock import MagicMock

import pytest

from applemusic_mcp import cli


def _ns(**kwargs):
    return argparse.Namespace(**kwargs)


# --------------------------------------------------------------------------- #
# cmd_init
# --------------------------------------------------------------------------- #
class TestCmdInit:
    def test_creates_config(self, mock_config_dir, capsys):
        rc = cli.cmd_init(_ns(force=False))
        assert rc == 0
        out = capsys.readouterr().out
        assert "Created config file" in out
        assert "generate-token" in out
        # File written with the sample scaffold.
        data = json.loads((mock_config_dir / "config.json").read_text())
        assert data["team_id"] == "YOUR_TEAM_ID"
        assert data["key_id"] == "YOUR_KEY_ID"
        assert data["private_key_path"].endswith("AuthKey_XXXXXXXX.p8")

    def test_exists_without_force(self, mock_config_dir, capsys):
        (mock_config_dir / "config.json").write_text("{}")
        rc = cli.cmd_init(_ns(force=False))
        assert rc == 1
        out = capsys.readouterr().out
        assert "Config already exists" in out
        assert "--force" in out

    def test_exists_with_force_overwrites(self, mock_config_dir, capsys):
        (mock_config_dir / "config.json").write_text('{"team_id": "OLD"}')
        rc = cli.cmd_init(_ns(force=True))
        assert rc == 0
        assert "Created config file" in capsys.readouterr().out
        data = json.loads((mock_config_dir / "config.json").read_text())
        assert data["team_id"] == "YOUR_TEAM_ID"


# --------------------------------------------------------------------------- #
# cmd_generate_token
# --------------------------------------------------------------------------- #
class TestCmdGenerateToken:
    def test_success(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "generate_developer_token", lambda expiry_days: "x" * 100)
        rc = cli.cmd_generate_token(_ns(days=30))
        assert rc == 0
        out = capsys.readouterr().out
        assert "Developer token generated!" in out
        assert "Expires:" in out
        assert ("x" * 50) in out

    def test_missing_config(self, monkeypatch, capsys):
        def _boom(expiry_days):
            raise FileNotFoundError("no key")

        monkeypatch.setattr(cli, "generate_developer_token", _boom)
        rc = cli.cmd_generate_token(_ns(days=180))
        assert rc == 1
        out = capsys.readouterr().out
        assert "Error: no key" in out
        assert "applemusic-mcp init" in out

    def test_generic_error(self, monkeypatch, capsys):
        def _boom(expiry_days):
            raise RuntimeError("bad key")

        monkeypatch.setattr(cli, "generate_developer_token", _boom)
        rc = cli.cmd_generate_token(_ns(days=180))
        assert rc == 1
        assert "Error generating token: bad key" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# cmd_authorize
# --------------------------------------------------------------------------- #
class TestCmdAuthorize:
    def test_missing_developer_token(self, monkeypatch, capsys):
        def _boom():
            raise FileNotFoundError("no dev token")

        monkeypatch.setattr(cli, "get_developer_token", _boom)
        rc = cli.cmd_authorize(_ns(port=8765))
        assert rc == 1
        assert "Error: no dev token" in capsys.readouterr().out

    def test_success(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "get_developer_token", lambda: "DEV")
        called = {}

        def _run(port):
            called["port"] = port
            return "USER_TOKEN"

        monkeypatch.setattr(cli, "run_auth_server", _run)
        rc = cli.cmd_authorize(_ns(port=9999))
        assert rc == 0
        assert called["port"] == 9999
        assert "Starting authorization flow..." in capsys.readouterr().out

    def test_no_token_returned(self, monkeypatch):
        monkeypatch.setattr(cli, "get_developer_token", lambda: "DEV")
        monkeypatch.setattr(cli, "run_auth_server", lambda port: None)
        rc = cli.cmd_authorize(_ns(port=8765))
        assert rc == 1


# --------------------------------------------------------------------------- #
# cmd_signin
# --------------------------------------------------------------------------- #
def test_cmd_signin(monkeypatch):
    from applemusic_mcp import browser

    monkeypatch.setattr(browser, "_cli_signin", lambda: 0)
    assert cli.cmd_signin(_ns()) == 0


# --------------------------------------------------------------------------- #
# cmd_status
# --------------------------------------------------------------------------- #
class TestCmdStatus:
    def _patch_api(self, monkeypatch, status_code=None, exc=None):
        """Patch requests.get used inside cmd_status."""
        import requests

        if exc is not None:

            def _get(*a, **k):
                raise exc

        else:
            resp = MagicMock()
            resp.status_code = status_code

            def _get(*a, **k):
                return resp

        monkeypatch.setattr(requests, "get", _get)

    def test_full_valid(self, monkeypatch, mock_config_dir, capsys):
        # Config present and readable.
        (mock_config_dir / "config.json").write_text(json.dumps({"team_id": "T", "key_id": "K"}))
        from applemusic_mcp import auth

        monkeypatch.setattr(auth, "developer_token_info", lambda: {"expires": time.time() + 100000})
        monkeypatch.setattr(auth, "has_user_token", lambda: True)
        monkeypatch.setattr(cli, "get_developer_token", lambda: "DEV")
        monkeypatch.setattr(cli, "get_user_token", lambda: "USER")
        self._patch_api(monkeypatch, status_code=200)

        rc = cli.cmd_status(_ns())
        assert rc == 0
        out = capsys.readouterr().out
        assert "✓ Config file exists" in out
        assert "Team ID: T" in out
        assert "Key ID: K" in out
        assert "Developer token valid" in out
        assert "✓ Music user token exists" in out
        assert "✓ API connection successful" in out

    def test_config_read_error(self, monkeypatch, mock_config_dir, capsys):
        (mock_config_dir / "config.json").write_text("{ not json")
        from applemusic_mcp import auth

        monkeypatch.setattr(auth, "developer_token_info", lambda: None)
        monkeypatch.setattr(auth, "has_user_token", lambda: False)
        # API path raises FileNotFoundError (missing tokens branch).
        monkeypatch.setattr(cli, "get_developer_token", lambda: "DEV")

        def _no_user():
            raise FileNotFoundError("no user token")

        monkeypatch.setattr(cli, "get_user_token", _no_user)

        rc = cli.cmd_status(_ns())
        assert rc == 0
        out = capsys.readouterr().out
        assert "Error reading config" in out
        assert "✗ Developer token missing" in out
        assert "✗ Music user token missing" in out
        assert "✗ Cannot test API (missing tokens)" in out

    def test_config_missing_token_expired_api_non200(self, monkeypatch, mock_config_dir, capsys):
        # No config.json at all.
        from applemusic_mcp import auth

        monkeypatch.setattr(auth, "developer_token_info", lambda: {"expires": time.time() - 100})
        monkeypatch.setattr(auth, "has_user_token", lambda: True)
        monkeypatch.setattr(cli, "get_developer_token", lambda: "DEV")
        monkeypatch.setattr(cli, "get_user_token", lambda: "USER")
        self._patch_api(monkeypatch, status_code=403)

        rc = cli.cmd_status(_ns())
        assert rc == 0
        out = capsys.readouterr().out
        assert "✗ Config file missing" in out
        assert "✗ Developer token expired" in out
        assert "✗ API returned status 403" in out

    def test_api_generic_exception(self, monkeypatch, mock_config_dir, capsys):
        from applemusic_mcp import auth

        monkeypatch.setattr(auth, "developer_token_info", lambda: None)
        monkeypatch.setattr(auth, "has_user_token", lambda: False)
        monkeypatch.setattr(cli, "get_developer_token", lambda: "DEV")
        monkeypatch.setattr(cli, "get_user_token", lambda: "USER")
        self._patch_api(monkeypatch, exc=RuntimeError("boom"))

        rc = cli.cmd_status(_ns())
        assert rc == 0
        assert "✗ API error: boom" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# cmd_logout
# --------------------------------------------------------------------------- #
def test_cmd_logout(monkeypatch, capsys):
    from applemusic_mcp import auth, browser

    deleted = []
    monkeypatch.setattr(auth, "secret_delete", lambda k: deleted.append(k))
    cleared = []
    monkeypatch.setattr(browser, "clear_session", lambda: cleared.append(True))

    rc = cli.cmd_logout(_ns())
    assert rc == 0
    assert deleted == ["music_user_token", "harvested_token"]
    assert cleared == [True]
    assert "✓ Signed out" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# cmd_reset
# --------------------------------------------------------------------------- #
class TestCmdReset:
    def test_without_force(self, monkeypatch, capsys):
        from applemusic_mcp import auth, browser

        # Must not delete anything when not forced.
        monkeypatch.setattr(auth, "secret_delete", lambda k: pytest.fail("should not delete"))
        monkeypatch.setattr(browser, "clear_session", lambda: pytest.fail("should not clear"))
        rc = cli.cmd_reset(_ns(force=False))
        assert rc == 1
        assert "--force to proceed" in capsys.readouterr().out

    def test_with_force(self, monkeypatch, mock_config_dir, capsys):
        from applemusic_mcp import auth, browser

        deleted = []
        monkeypatch.setattr(auth, "secret_delete", lambda k: deleted.append(k))
        cleared = []
        monkeypatch.setattr(browser, "clear_session", lambda: cleared.append(True))
        cfg = mock_config_dir / "config.json"
        cfg.write_text("{}")

        rc = cli.cmd_reset(_ns(force=True))
        assert rc == 0
        assert deleted == ["developer_token", "music_user_token", "harvested_token"]
        assert cleared == [True]
        assert not cfg.exists()  # config.json unlinked
        assert "✓ Reset complete" in capsys.readouterr().out

    def test_with_force_no_config_file(self, monkeypatch, mock_config_dir, capsys):
        # cfg_file.exists() is False -> skip unlink branch.
        from applemusic_mcp import auth, browser

        monkeypatch.setattr(auth, "secret_delete", lambda k: None)
        monkeypatch.setattr(browser, "clear_session", lambda: None)
        rc = cli.cmd_reset(_ns(force=True))
        assert rc == 0
        assert "✓ Reset complete" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# cmd_serve
# --------------------------------------------------------------------------- #
def test_cmd_serve(monkeypatch):
    from applemusic_mcp import server

    called = []
    monkeypatch.setattr(server, "main", lambda: called.append(True))
    cli.cmd_serve(_ns())
    assert called == [True]


# --------------------------------------------------------------------------- #
# main() dispatch
# --------------------------------------------------------------------------- #
class TestMainDispatch:
    @pytest.mark.parametrize(
        "command,cmd_func",
        [
            ("init", "cmd_init"),
            ("generate-token", "cmd_generate_token"),
            ("authorize", "cmd_authorize"),
            ("signin", "cmd_signin"),
            ("status", "cmd_status"),
            ("logout", "cmd_logout"),
            ("reset", "cmd_reset"),
        ],
    )
    def test_dispatch_exits_with_cmd_return(self, monkeypatch, command, cmd_func):
        monkeypatch.setattr(cli, cmd_func, lambda args: 7)
        monkeypatch.setattr("sys.argv", ["applemusic-mcp", command])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 7

    def test_dispatch_serve_no_exit(self, monkeypatch):
        called = []
        monkeypatch.setattr(cli, "cmd_serve", lambda args: called.append(True))
        monkeypatch.setattr("sys.argv", ["applemusic-mcp", "serve"])
        # serve does not sys.exit; main returns normally.
        assert cli.main() is None
        assert called == [True]

    def test_no_command_prints_help(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.argv", ["applemusic-mcp"])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 0
        assert "usage:" in capsys.readouterr().out.lower()


# --------------------------------------------------------------------------- #
# __main__ entry shim
# --------------------------------------------------------------------------- #
def test_main_module_runpy(monkeypatch):
    """Run the package __main__ shim end to end via runpy, with server.main
    stubbed so nothing actually starts."""
    import runpy
    from applemusic_mcp import server

    monkeypatch.setattr(server, "main", lambda: None)
    runpy.run_module("applemusic_mcp", run_name="__main__")


def test_cli_main_guard(monkeypatch):
    """Execute cli.py as __main__ so its `if __name__ == '__main__'` line runs."""
    import runpy

    monkeypatch.setattr("sys.argv", ["applemusic-mcp"])
    with pytest.raises(SystemExit):
        runpy.run_module("applemusic_mcp.cli", run_name="__main__")
