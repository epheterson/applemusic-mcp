"""Coverage-completion tests for applemusic_mcp.auth.

Drives auth.py to 100% line coverage. The existing tests/test_auth.py stays
untouched; this file fills the gaps: every error/except branch, the harvested
token load/refresh/save logic, generated-token generation + renewal, the secret
store's keychain precedence corners, and the full local auth HTTP server.

Boundaries are mocked, never reached for real: filesystem via tmp_path + the
``mock_config_dir`` fixture (monkeypatches ``DEFAULT_CONFIG_DIR``), ``keyring``
via monkeypatch, network (``requests``/harvest) via monkeypatch. JWT/crypto is
REAL — ``_write_signing_config`` mints a throwaway EC P-256 .p8 so
``generate_developer_token`` actually signs an ES256 JWT we then assert on.
"""

import json
import socket
import threading
import time
from pathlib import Path

import jwt
import pytest

from applemusic_mcp import auth

# Captured before conftest's autouse stub replaces it, so these tests can drive
# the REAL resolve_web_token / harvest logic.
_REAL_RESOLVE_WEB = auth.resolve_web_token
_REAL_HARVEST = auth.harvest_developer_token


def _write_signing_config(config_dir):
    """Write a REAL ES256 .p8 + config.json so generate_developer_token signs."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    key_path = config_dir / "AuthKey_TEST.p8"
    key_path.write_text(pem)
    (config_dir / "config.json").write_text(
        json.dumps(
            {"team_id": "TEAM123456", "key_id": "KEY1234567", "private_key_path": str(key_path)}
        )
    )


class _FakeKeyring:
    """Minimal in-memory stand-in for the keyring module."""

    def __init__(self):
        self.d = {}

    def get_keyring(self):
        class _Backend:  # name isn't fail/null → treated as usable
            pass

        return _Backend()

    def set_password(self, service, key, value):
        self.d[(service, key)] = value

    def get_password(self, service, key):
        return self.d.get((service, key))

    def delete_password(self, service, key):
        self.d.pop((service, key), None)


def _enable_keychain(monkeypatch, fake=None):
    """Flip the secret store onto an in-memory keychain backend. Keychain is
    auto-selected on Windows only, so simulate win32."""
    monkeypatch.delenv("APPLEMUSIC_NO_KEYRING", raising=False)
    monkeypatch.setattr(auth.sys, "platform", "win32")
    fake = fake or _FakeKeyring()
    monkeypatch.setattr(auth, "_keyring", fake)
    return fake


# --------------------------------------------------------------------------- #
# _write_private — secret files are created 0600 from birth                    #
# --------------------------------------------------------------------------- #
class TestWritePrivate:
    @pytest.mark.skipif(__import__("os").name == "nt", reason="POSIX file modes only")
    def test_creates_file_0600_with_content(self, mock_config_dir):
        p = mock_config_dir / "secret.txt"
        auth._write_private(p, "hush")
        assert p.read_text() == "hush"
        assert (p.stat().st_mode & 0o777) == 0o600

    @pytest.mark.skipif(__import__("os").name == "nt", reason="POSIX file modes only")
    def test_truncates_existing_file(self, mock_config_dir):
        p = mock_config_dir / "secret.txt"
        auth._write_private(p, "a much longer original value")
        auth._write_private(p, "x")
        assert p.read_text() == "x"
        assert (p.stat().st_mode & 0o777) == 0o600


# --------------------------------------------------------------------------- #
# _keyring_ok — the opt-in gate and its except branch                          #
# --------------------------------------------------------------------------- #
class TestKeyringOk:
    def test_false_when_env_guard_set(self, mock_config_dir, monkeypatch):
        monkeypatch.setenv("APPLEMUSIC_NO_KEYRING", "1")
        assert auth._keyring_ok() is False

    def test_false_when_keyring_module_missing(self, mock_config_dir, monkeypatch):
        monkeypatch.delenv("APPLEMUSIC_NO_KEYRING", raising=False)
        monkeypatch.setattr(auth, "_keyring", None)
        assert auth._keyring_ok() is False

    def test_false_on_posix_even_with_backend(self, mock_config_dir, monkeypatch):
        # macOS/Linux always use files (per-process ACL is unreliable), even with
        # a working backend present.
        monkeypatch.delenv("APPLEMUSIC_NO_KEYRING", raising=False)
        monkeypatch.setattr(auth.sys, "platform", "darwin")
        monkeypatch.setattr(auth, "_keyring", _FakeKeyring())
        assert auth._keyring_ok() is False

    def test_false_when_backend_is_fail_or_null(self, mock_config_dir, monkeypatch):
        monkeypatch.delenv("APPLEMUSIC_NO_KEYRING", raising=False)
        monkeypatch.setattr(auth.sys, "platform", "win32")

        class _NullKeyring(_FakeKeyring):
            def get_keyring(self):
                class fail_Backend:  # name contains "fail" → treated as unusable
                    pass

                return fail_Backend()

        monkeypatch.setattr(auth, "_keyring", _NullKeyring())
        assert auth._keyring_ok() is False

    def test_false_when_get_keyring_raises(self, mock_config_dir, monkeypatch):
        """The except branch: a backend probe that blows up → file."""
        monkeypatch.delenv("APPLEMUSIC_NO_KEYRING", raising=False)
        monkeypatch.setattr(auth.sys, "platform", "win32")

        class _BoomKeyring:
            def get_keyring(self):
                raise RuntimeError("backend exploded")

        monkeypatch.setattr(auth, "_keyring", _BoomKeyring())
        assert auth._keyring_ok() is False

    def test_true_with_usable_backend(self, mock_config_dir, monkeypatch):
        _enable_keychain(monkeypatch)
        assert auth._keyring_ok() is True


# --------------------------------------------------------------------------- #
# secret_set / secret_get / secret_delete                                      #
# --------------------------------------------------------------------------- #
class TestSecretStore:
    def test_file_roundtrip_default(self, mock_config_dir):
        auth.secret_set("k", "v")
        assert auth._secret_file("k").exists()
        assert auth.secret_get("k") == "v"

    def test_get_returns_none_when_absent(self, mock_config_dir):
        assert auth.secret_get("nope") is None

    def test_get_handles_unreadable_file(self, mock_config_dir, monkeypatch):
        """File exists but read_text raises OSError → None (lines 100-101, 115)."""
        f = auth._secret_file("k")
        auth._write_private(f, "v")

        real_read = Path.read_text

        def boom(self, *a, **k):
            if self == f:
                raise OSError("unreadable")
            return real_read(self, *a, **k)

        monkeypatch.setattr(Path, "read_text", boom)
        assert auth.secret_get("k") is None

    def test_set_drops_stale_keychain_copy_on_file_write(self, mock_config_dir, monkeypatch):
        """Keychain not used for write (POSIX) but a keyring exists: the
        now-stale keychain copy is best-effort deleted."""
        monkeypatch.delenv("APPLEMUSIC_NO_KEYRING", raising=False)
        monkeypatch.setattr(auth.sys, "platform", "darwin")
        fake = _FakeKeyring()
        fake.d[("applemusic-mcp", "k")] = "stale"
        monkeypatch.setattr(auth, "_keyring", fake)

        auth.secret_set("k", "fresh")

        assert auth._secret_file("k").read_text() == "fresh"
        assert ("applemusic-mcp", "k") not in fake.d  # stale keychain copy purged

    def test_delete_swallows_backend_error(self, mock_config_dir, monkeypatch):
        """delete_password raising is caught (lines 125-126); deletion still
        verified by the post-read, which returns None → True."""

        class _RaiseOnDelete(_FakeKeyring):
            def delete_password(self, service, key):
                raise RuntimeError("locked")

            def get_password(self, service, key):
                return None  # nothing there once we look

        fake = _enable_keychain(monkeypatch, _RaiseOnDelete())
        assert auth.secret_delete("k") is True


class TestHasUserToken:
    def test_true_when_present(self, mock_config_dir):
        auth.save_user_token("tok")
        assert auth.has_user_token() is True

    def test_false_when_absent(self, mock_config_dir):
        assert auth.has_user_token() is False


class TestDeveloperTokenInfo:
    def test_none_when_absent(self, mock_config_dir):
        assert auth.developer_token_info() is None

    def test_parses_json(self, mock_config_dir):
        auth.secret_set("developer_token", json.dumps({"token": "t", "expires": 1}))
        assert auth.developer_token_info() == {"token": "t", "expires": 1}

    def test_none_on_malformed_json(self, mock_config_dir):
        auth.secret_set("developer_token", "{not json")
        assert auth.developer_token_info() is None


# --------------------------------------------------------------------------- #
# Harvested (fallback) developer token                                         #
# --------------------------------------------------------------------------- #
_VALID_AMP = "eyJ0eXAiOiJKV1QabcDEF123_-.eyJpc3MiOiJBTVBXZWJQbGF5xyz789_-.SiGnAtUrE_-0"


class TestExtractDeveloperToken:
    def test_extracts_amp_token(self):
        bundle = f"var x=1; const t='{_VALID_AMP}'; doStuff();"
        assert auth.extract_developer_token(bundle) == _VALID_AMP

    def test_raises_when_absent(self):
        with pytest.raises(RuntimeError, match="not found"):
            auth.extract_developer_token("no token in here")


class _FakeResp:
    def __init__(self, text):
        self.text = text


class TestHarvestDeveloperToken:
    def test_full_harvest_flow(self, monkeypatch):
        monkeypatch.setattr(auth, "harvest_developer_token", _REAL_HARVEST)
        calls = []

        def fake_get(url, headers=None, timeout=None):
            calls.append(url)
            if url == auth._HARVEST_BROWSE_URL:
                return _FakeResp("<script src='/assets/index~deadbeef99.js'></script>")
            return _FakeResp(f"chunk; tok='{_VALID_AMP}';")

        monkeypatch.setattr(auth.requests, "get", fake_get)
        assert auth.harvest_developer_token() == _VALID_AMP
        assert calls == [
            auth._HARVEST_BROWSE_URL,
            auth._HARVEST_ORIGIN + "/assets/index~deadbeef99.js",
        ]

    def test_raises_when_bundle_not_found(self, monkeypatch):
        monkeypatch.setattr(auth, "harvest_developer_token", _REAL_HARVEST)
        monkeypatch.setattr(
            auth.requests, "get", lambda *a, **k: _FakeResp("<html>no bundle</html>")
        )
        with pytest.raises(RuntimeError, match="web-player JS bundle"):
            auth.harvest_developer_token()


class TestHarvestedTokenFile:
    def test_path_name(self, mock_config_dir):
        assert auth._harvested_token_file() == mock_config_dir / "harvested_token.json"


class TestLoadHarvestedToken:
    def test_none_when_absent(self, mock_config_dir):
        assert auth._load_harvested_token() is None

    def test_returns_token_with_runway(self, mock_config_dir):
        exp = time.time() + 60 * 86400  # 60 days out — well past the 15-day floor
        auth.secret_set("harvested_token", json.dumps({"token": "good", "expires": exp}))
        assert auth._load_harvested_token() == "good"

    def test_none_when_near_expiry(self, mock_config_dir):
        exp = time.time() + 5 * 86400  # inside the 15-day refresh window
        auth.secret_set("harvested_token", json.dumps({"token": "stale", "expires": exp}))
        assert auth._load_harvested_token() is None

    def test_none_on_malformed_json(self, mock_config_dir):
        auth.secret_set("harvested_token", "{bad json")
        assert auth._load_harvested_token() is None

    def test_none_on_missing_token_key(self, mock_config_dir):
        # expires far out, but no "token" key → KeyError → None (line 208-210).
        auth.secret_set("harvested_token", json.dumps({"expires": time.time() + 99 * 86400}))
        assert auth._load_harvested_token() is None


class TestSaveHarvestedToken:
    def test_uses_jwt_exp_claim(self, mock_config_dir):
        exp = int(time.time() + 100 * 86400)
        token = jwt.encode({"iss": "AMPWebPlay", "exp": exp}, "secret", algorithm="HS256")
        auth._save_harvested_token(token)
        stored = json.loads(auth.secret_get("harvested_token"))
        assert stored["token"] == token
        assert stored["expires"] == exp
        assert stored["source"] == "harvested"

    def test_falls_back_to_30_days_on_bad_token(self, mock_config_dir):
        """jwt.decode blows up on garbage → 30-day fallback exp (lines 216-218)."""
        before = time.time()
        auth._save_harvested_token("not-a-jwt")
        stored = json.loads(auth.secret_get("harvested_token"))
        days = (stored["expires"] - before) / 86400
        assert 29 < days < 31


# --------------------------------------------------------------------------- #
# resolve_developer_token / resolve_web_token / has_any_developer_token        #
# --------------------------------------------------------------------------- #
class TestResolveTokens:
    def test_resolve_dev_prefers_generated(self, mock_config_dir, monkeypatch):
        _write_signing_config(mock_config_dir)
        monkeypatch.setattr(auth, "resolve_web_token", lambda: pytest.fail("should not fall back"))
        tok = auth.resolve_developer_token()
        # Real ES256 JWT minted from our throwaway key.
        hdr = jwt.get_unverified_header(tok)
        assert hdr["alg"] == "ES256" and hdr["kid"] == "KEY1234567"

    def test_resolve_dev_falls_back_to_web(self, mock_config_dir, monkeypatch):
        # No generated token, no signing key → FileNotFoundError → web token.
        monkeypatch.setattr(auth, "resolve_web_token", _REAL_RESOLVE_WEB)
        monkeypatch.setattr(auth, "harvest_developer_token", lambda: "WEBTOK")
        assert auth.resolve_developer_token() == "WEBTOK"

    def test_resolve_web_returns_cached(self, mock_config_dir, monkeypatch):
        monkeypatch.setattr(auth, "resolve_web_token", _REAL_RESOLVE_WEB)
        auth._save_harvested_token(
            jwt.encode({"exp": int(time.time() + 99 * 86400)}, "s", algorithm="HS256")
        )
        monkeypatch.setattr(auth, "harvest_developer_token", lambda: pytest.fail("no harvest"))
        assert auth.resolve_web_token()  # served from cache

    def test_resolve_web_harvests_when_empty(self, mock_config_dir, monkeypatch):
        monkeypatch.setattr(auth, "resolve_web_token", _REAL_RESOLVE_WEB)
        monkeypatch.setattr(auth, "harvest_developer_token", lambda: "FRESH")
        assert auth.resolve_web_token() == "FRESH"
        # Saved for next time.
        assert json.loads(auth.secret_get("harvested_token"))["token"] == "FRESH"

    def test_has_any_true(self, mock_config_dir, monkeypatch):
        _write_signing_config(mock_config_dir)
        assert auth.has_any_developer_token() is True

    def test_has_any_false_when_all_sources_fail(self, mock_config_dir, monkeypatch):
        monkeypatch.setattr(auth, "resolve_web_token", _REAL_RESOLVE_WEB)

        def boom():
            raise RuntimeError("offline")

        monkeypatch.setattr(auth, "harvest_developer_token", boom)
        assert auth.has_any_developer_token() is False


# --------------------------------------------------------------------------- #
# get_config_dir / load_config caching                                         #
# --------------------------------------------------------------------------- #
class TestGetConfigDir:
    def test_chmod_oserror_is_swallowed(self, mock_config_dir, monkeypatch):
        """A platform/FS that rejects chmod must not break dir creation (268-269)."""

        def boom(*a, **k):
            raise OSError("no chmod here")

        monkeypatch.setattr(auth.os, "chmod", boom)
        assert auth.get_config_dir() == mock_config_dir


class TestLoadConfigCaching:
    def test_caches_by_mtime(self, mock_config_dir, monkeypatch):
        monkeypatch.setattr(auth, "_config_cache", None)
        (mock_config_dir / "config.json").write_text(json.dumps({"a": 1}))
        first = auth.load_config()
        # Same mtime → identical cached object returned (line 290).
        assert auth.load_config() is first

    def test_missing_config_resets_cache(self, mock_config_dir, monkeypatch):
        monkeypatch.setattr(auth, "_config_cache", ("x", {"stale": True}))
        assert auth.load_config() == {}
        assert auth._config_cache is None

    def test_stat_oserror_uses_zero_mtime(self, mock_config_dir, monkeypatch):
        """exists() True but stat() raises → mtime 0.0, still parses (287-288)."""
        cfg = mock_config_dir / "config.json"
        cfg.write_text(json.dumps({"ok": 1}))
        monkeypatch.setattr(auth, "_config_cache", None)
        monkeypatch.setattr(auth, "get_config_dir", lambda: mock_config_dir)
        monkeypatch.setattr(Path, "exists", lambda self: True)

        def boom_stat(self, *a, **k):
            raise OSError("no stat")

        monkeypatch.setattr(Path, "stat", boom_stat)
        assert auth.load_config() == {"ok": 1}


# --------------------------------------------------------------------------- #
# get_user_preferences — defaults, platform, error branch                      #
# --------------------------------------------------------------------------- #
class TestGetUserPreferences:
    def test_all_defaults(self, mock_config_dir):
        prefs = auth.get_user_preferences()
        assert prefs["fetch_explicit"] is False
        assert prefs["clean_only"] is False
        assert prefs["auto_add"] is False
        assert prefs["storefront"] == "us"
        assert prefs["mode"] == "auto"
        # Single mode now; playback follows it and token storage is auto by platform.
        assert "playback" not in prefs
        assert "secure_storage" not in prefs

    def test_error_in_load_config_yields_defaults(self, mock_config_dir, monkeypatch):
        """load_config raising JSONDecodeError → defaults (lines 310-311)."""
        (mock_config_dir / "config.json").write_text("{invalid json")
        monkeypatch.setattr(auth, "_config_cache", None)
        prefs = auth.get_user_preferences()
        assert prefs["storefront"] == "us"  # fell through to defaults


# --------------------------------------------------------------------------- #
# generate_developer_token / can_generate / get_developer_token corners        #
# --------------------------------------------------------------------------- #
class TestGenerateDeveloperToken:
    def test_signs_real_es256_jwt(self, mock_config_dir):
        _write_signing_config(mock_config_dir)
        token = auth.generate_developer_token(expiry_days=180)
        hdr = jwt.get_unverified_header(token)
        assert hdr == {"alg": "ES256", "typ": "JWT", "kid": "KEY1234567"}
        claims = jwt.decode(token, options={"verify_signature": False})
        assert claims["iss"] == "TEAM123456"
        assert 179 < (claims["exp"] - claims["iat"]) / 86400 < 181
        # Persisted record matches.
        stored = json.loads(auth.secret_get("developer_token"))
        assert stored["token"] == token
        assert stored["team_id"] == "TEAM123456"
        assert stored["key_id"] == "KEY1234567"

    def test_raises_without_config(self, mock_config_dir):
        with pytest.raises(FileNotFoundError, match="No config.json"):
            auth.generate_developer_token()


class TestCanGenerate:
    def test_false_when_team_or_key_missing(self, mock_config_dir):
        (mock_config_dir / "config.json").write_text(json.dumps({"team_id": "T"}))  # no key_id
        assert auth.can_generate_developer_token() is False

    def test_false_on_load_config_error(self, mock_config_dir, monkeypatch):
        """A blowup anywhere inside → False (lines 399-400)."""
        (mock_config_dir / "config.json").write_text("{bad json")
        monkeypatch.setattr(auth, "_config_cache", None)
        assert auth.can_generate_developer_token() is False

    def test_true_with_full_signing_config(self, mock_config_dir):
        _write_signing_config(mock_config_dir)
        assert auth.can_generate_developer_token() is True


class TestGetDeveloperTokenCorners:
    def test_missing_expires_and_no_key_raises(self, mock_config_dir):
        """data lacks 'expires' → KeyError → days_left=-1 (414-415), no key → raise."""
        auth.secret_set("developer_token", json.dumps({"token": "x"}))
        with pytest.raises(ValueError, match="expired or expiring soon"):
            auth.get_developer_token()

    def test_in_window_no_key_keeps_valid_token(self, mock_config_dir):
        """Inside renewal window, still >1 day valid, no .p8 → keep it (421-422)."""
        auth.secret_set(
            "developer_token",
            json.dumps({"token": "keepme", "expires": time.time() + 10 * 86400}),
        )
        assert auth.get_developer_token() == "keepme"


# --------------------------------------------------------------------------- #
# create_success_html                                                          #
# --------------------------------------------------------------------------- #
class TestCreateSuccessHtml:
    def test_renders_complete_page(self):
        html = auth.create_success_html()
        assert "Authorization Complete" in html
        assert "<!DOCTYPE html>" in html


# --------------------------------------------------------------------------- #
# run_auth_server — the full local HTTP auth server                            #
# --------------------------------------------------------------------------- #
def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _raw(port, raw_bytes):
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall(raw_bytes)
    chunks = []
    try:
        while True:
            b = s.recv(4096)
            if not b:
                break
            chunks.append(b)
    except socket.timeout:
        pass
    s.close()
    return b"".join(chunks)


def _status(resp):
    return int(resp.split(b" ", 2)[1])


def _wait_up(port):
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("server never came up")


class TestRunAuthServer:
    def test_full_server_lifecycle(self, mock_config_dir, monkeypatch):
        """Drive every handler branch against a real running server, then post a
        valid token to stop it and assert the captured return value."""
        _write_signing_config(mock_config_dir)  # so get_developer_token() mints one
        monkeypatch.setattr(auth.webbrowser, "open", lambda *a, **k: None)

        port = _free_port()
        host = f"localhost:{port}".encode()
        result = {}

        def run():
            result["value"] = auth.run_auth_server(port=port)

        t = threading.Thread(target=run)
        t.start()
        try:
            _wait_up(port)

            # do_GET: foreign Host → 403.
            r = _raw(
                port,
                b"GET /auth.html HTTP/1.1\r\nHost: evil.example:1\r\nConnection: close\r\n\r\n",
            )
            assert _status(r) == 403

            # do_GET: /auth.html → 200, embeds the developer token's HTML.
            r = _raw(
                port, b"GET /auth.html HTTP/1.1\r\nHost: " + host + b"\r\nConnection: close\r\n\r\n"
            )
            assert _status(r) == 200
            assert b"Apple Music Authorization" in r

            # do_GET: unknown path → 404.
            r = _raw(
                port, b"GET /nope HTTP/1.1\r\nHost: " + host + b"\r\nConnection: close\r\n\r\n"
            )
            assert _status(r) == 404

            # do_POST: foreign Host → 403.
            r = _raw(
                port,
                b"POST /save-token HTTP/1.1\r\nHost: evil:1\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
            )
            assert _status(r) == 403

            # do_POST: non-integer Content-Length → 400.
            r = _raw(
                port,
                b"POST /save-token HTTP/1.1\r\nHost: "
                + host
                + b"\r\nContent-Length: abc\r\nConnection: close\r\n\r\n",
            )
            assert _status(r) == 400

            # do_POST: oversized Content-Length → 413 (checked before body read).
            big = str(auth._MAX_POST_BYTES + 1).encode()
            r = _raw(
                port,
                b"POST /save-token HTTP/1.1\r\nHost: "
                + host
                + b"\r\nContent-Length: "
                + big
                + b"\r\nConnection: close\r\n\r\n",
            )
            assert _status(r) == 413

            # do_POST: unknown path → 404.
            r = _raw(
                port,
                b"POST /other HTTP/1.1\r\nHost: "
                + host
                + b"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
            )
            assert _status(r) == 404

            # do_POST: /save-token with no token param → 400.
            body = b"foo=bar"
            r = _raw(
                port,
                b"POST /save-token HTTP/1.1\r\nHost: "
                + host
                + b"\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + body,
            )
            assert _status(r) == 400

            # do_OPTIONS: CORS preflight → 200 with allow-origin.
            r = _raw(
                port,
                b"OPTIONS /save-token HTTP/1.1\r\nHost: " + host + b"\r\nConnection: close\r\n\r\n",
            )
            assert _status(r) == 200
            assert b"Access-Control-Allow-Origin" in r

            # do_POST: valid token → 200 success, captures token, stops server.
            body = b"token=CAPTURED123"
            r = _raw(
                port,
                b"POST /save-token HTTP/1.1\r\nHost: "
                + host
                + b"\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + body,
            )
            assert _status(r) == 200
            assert b"Authorization Complete" in r
        finally:
            t.join(timeout=10)

        assert result["value"] == "CAPTURED123"
        # Token persisted, auth.html cleaned up (it embedded the dev token).
        assert auth.get_user_token() == "CAPTURED123"
        assert not (mock_config_dir / "auth.html").exists()

    def test_keyboard_interrupt_returns_none(self, mock_config_dir, monkeypatch):
        """Ctrl-C during the serve loop → None, and auth.html is still cleaned up."""
        _write_signing_config(mock_config_dir)
        monkeypatch.setattr(auth.webbrowser, "open", lambda *a, **k: None)

        class _KIServer:
            def __init__(self, addr, handler):
                self.timeout = None

            def handle_request(self):
                raise KeyboardInterrupt

            def server_close(self):
                pass

        monkeypatch.setattr(auth, "HTTPServer", _KIServer)
        assert auth.run_auth_server(port=_free_port()) is None
        assert not (mock_config_dir / "auth.html").exists()

    def test_auth_html_unlink_oserror_is_swallowed(self, mock_config_dir, monkeypatch):
        """A wedged FS that can't remove auth.html must not crash teardown (727-728)."""
        _write_signing_config(mock_config_dir)
        monkeypatch.setattr(auth.webbrowser, "open", lambda *a, **k: None)

        class _KIServer:
            def __init__(self, addr, handler):
                self.timeout = None

            def handle_request(self):
                raise KeyboardInterrupt

            def server_close(self):
                pass

        monkeypatch.setattr(auth, "HTTPServer", _KIServer)

        def boom_unlink(self, *a, **k):
            raise OSError("wedged")

        monkeypatch.setattr(Path, "unlink", boom_unlink)
        assert auth.run_auth_server(port=_free_port()) is None  # OSError swallowed
