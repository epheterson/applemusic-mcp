"""Tests for auth module."""

import json
import os
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from applemusic_mcp import auth

# Captured before conftest's autouse stub replaces it, so these tests can
# exercise the REAL resolve_web_token logic.
_REAL_RESOLVE_WEB = auth.resolve_web_token


class TestGetConfigDir:
    """Tests for get_config_dir function."""

    def test_creates_directory_if_not_exists(self, mock_config_dir):
        """Should create config directory if it doesn't exist."""
        # Remove the directory first
        import shutil

        shutil.rmtree(mock_config_dir)
        assert not mock_config_dir.exists()

        # Call get_config_dir
        result = auth.get_config_dir()

        assert result.exists()
        assert result.is_dir()

    def test_returns_existing_directory(self, mock_config_dir):
        """Should return existing directory without error."""
        result = auth.get_config_dir()
        assert result == mock_config_dir
        assert result.exists()


class TestLoadConfig:
    """Tests for load_config function."""

    def test_loads_valid_config(self, mock_config_dir, sample_config):
        """Should load valid config file."""
        config_file = mock_config_dir / "config.json"
        with open(config_file, "w") as f:
            json.dump(sample_config, f)

        result = auth.load_config()

        assert result["team_id"] == "TEST_TEAM_ID"
        assert result["key_id"] == "TEST_KEY_ID"

    def test_returns_empty_dict_when_config_missing(self, mock_config_dir):
        """Should return empty dict when config doesn't exist."""
        result = auth.load_config()
        assert result == {}

    def test_raises_on_invalid_json(self, mock_config_dir):
        """Should raise error on invalid JSON."""
        config_file = mock_config_dir / "config.json"
        with open(config_file, "w") as f:
            f.write("not valid json {{{")

        with pytest.raises(json.JSONDecodeError):
            auth.load_config()


class TestGetPrivateKeyPath:
    """Tests for get_private_key_path function."""

    def test_resolves_path_with_tilde(self, mock_config_dir):
        """Should expand ~ in path."""
        config = {"private_key_path": "~/test/key.p8"}

        with patch.object(Path, "exists", return_value=True):
            result = auth.get_private_key_path(config)

        assert "~" not in str(result)
        assert result.is_absolute()

    def test_raises_when_key_missing(self, mock_config_dir):
        """Should raise FileNotFoundError when key doesn't exist."""
        config = {"private_key_path": str(mock_config_dir / "nonexistent.p8")}

        with pytest.raises(FileNotFoundError) as exc_info:
            auth.get_private_key_path(config)

        assert "Private key not found" in str(exc_info.value)


class TestGetDeveloperToken:
    """Tests for get_developer_token function."""

    def test_returns_valid_token(self, mock_config_dir, mock_developer_token):
        """Should return token when valid and not expired."""
        token_file = mock_config_dir / "developer_token.json"
        token_data = {
            "token": mock_developer_token,
            "expires": time.time() + 86400 * 30,  # 30 days from now
        }
        with open(token_file, "w") as f:
            json.dump(token_data, f)

        result = auth.get_developer_token()

        assert result == mock_developer_token

    def test_raises_when_token_missing(self, mock_config_dir):
        """Should raise FileNotFoundError when token file doesn't exist."""
        with pytest.raises(FileNotFoundError) as exc_info:
            auth.get_developer_token()

        assert "Developer token not found" in str(exc_info.value)

    def test_raises_when_token_expired(self, mock_config_dir, mock_developer_token):
        """Should raise ValueError when token is expired."""
        token_file = mock_config_dir / "developer_token.json"
        token_data = {
            "token": mock_developer_token,
            "expires": time.time() - 86400,  # Expired yesterday
        }
        with open(token_file, "w") as f:
            json.dump(token_data, f)

        with pytest.raises(ValueError) as exc_info:
            auth.get_developer_token()

        assert "expired" in str(exc_info.value).lower()

    def test_raises_when_token_expiring_soon(self, mock_config_dir, mock_developer_token):
        """Should raise ValueError when token expires within 1 day."""
        token_file = mock_config_dir / "developer_token.json"
        token_data = {
            "token": mock_developer_token,
            "expires": time.time() + 3600,  # 1 hour from now
        }
        with open(token_file, "w") as f:
            json.dump(token_data, f)

        with pytest.raises(ValueError) as exc_info:
            auth.get_developer_token()

        assert "expired" in str(exc_info.value).lower()


def _write_signing_config(config_dir):
    """Write a real ES256 .p8 + config.json so generate_developer_token works."""
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


class TestDeveloperTokenAutoRenew:
    """The generated token self-renews when within 30 days AND the .p8 is present."""

    def test_can_generate_true_with_key(self, mock_config_dir):
        _write_signing_config(mock_config_dir)
        assert auth.can_generate_developer_token() is True

    def test_can_generate_false_without_key(self, mock_config_dir):
        assert auth.can_generate_developer_token() is False

    def test_auto_renews_when_within_30_days(self, mock_config_dir):
        _write_signing_config(mock_config_dir)
        token_file = mock_config_dir / "developer_token.json"
        token_file.write_text(
            json.dumps({"token": "old.stale.token", "expires": time.time() + 86400 * 10})
        )

        result = auth.get_developer_token()

        assert result != "old.stale.token"  # minted fresh
        data = json.loads(token_file.read_text())
        assert (data["expires"] - time.time()) / 86400 > 150  # ~180-day token

    def test_keeps_token_when_far_from_expiry(self, mock_config_dir):
        _write_signing_config(mock_config_dir)
        token_file = mock_config_dir / "developer_token.json"
        token_file.write_text(
            json.dumps({"token": "still.good.token", "expires": time.time() + 86400 * 90})
        )

        assert auth.get_developer_token() == "still.good.token"  # no needless renew

    def test_generates_when_no_token_file_but_key_present(self, mock_config_dir):
        _write_signing_config(mock_config_dir)
        assert auth.get_developer_token()  # mints one instead of raising


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


class TestSecretStoreKeychain:
    """The keychain path of the secret store (file path is covered everywhere else)."""

    def _enable(self, monkeypatch):
        # Keychain is auto-selected on Windows only (0600 file bits are a no-op
        # there). Simulate Windows + a working backend.
        monkeypatch.delenv("APPLEMUSIC_NO_KEYRING", raising=False)
        monkeypatch.setattr(auth.sys, "platform", "win32")
        fake = _FakeKeyring()
        monkeypatch.setattr(auth, "_keyring", fake)
        return fake

    def test_keychain_off_on_posix_even_with_backend(self, mock_config_dir, monkeypatch):
        """On macOS/Linux a working backend is still NOT used (per-process ACL is
        unreliable): the token must be written to a FILE, not the keychain."""
        monkeypatch.delenv("APPLEMUSIC_NO_KEYRING", raising=False)
        monkeypatch.setattr(auth.sys, "platform", "darwin")
        fake = _FakeKeyring()
        monkeypatch.setattr(auth, "_keyring", fake)
        auth.secret_set("music_user_token", '{"music_user_token": "x"}')
        assert auth._secret_file("music_user_token").exists()
        assert ("applemusic-mcp", "music_user_token") not in fake.d

    def test_round_trip_uses_keychain_not_file(self, mock_config_dir, monkeypatch):
        fake = self._enable(monkeypatch)
        auth.secret_set("music_user_token", '{"music_user_token": "tok"}')
        assert ("applemusic-mcp", "music_user_token") in fake.d
        # no plaintext file written when the keychain holds it
        assert not auth._secret_file("music_user_token").exists()
        assert auth.get_user_token() == "tok"
        auth.secret_delete("music_user_token")
        assert auth.secret_get("music_user_token") is None

    def test_migrates_existing_file_into_keychain(self, mock_config_dir, monkeypatch):
        # Pre-existing plaintext file (an upgrade from the old file-based storage).
        auth._write_private(auth._secret_file("harvested_token"), '{"token": "h", "expires": 0}')
        self._enable(monkeypatch)
        # First read migrates it off disk and into the keychain.
        assert auth.secret_get("harvested_token") is not None
        assert not auth._secret_file("harvested_token").exists()

    def test_disabled_by_env_uses_file(self, mock_config_dir, monkeypatch):
        monkeypatch.setenv("APPLEMUSIC_NO_KEYRING", "1")
        auth.secret_set("music_user_token", '{"music_user_token": "f"}')
        assert auth._secret_file("music_user_token").exists()
        assert auth.get_user_token() == "f"

    def test_file_wins_over_stale_keychain(self, mock_config_dir, monkeypatch):
        """The token-loss bug: keychain holds an OLD value (A) and a newer FILE
        value (B) exists from a keychain-unavailable write. A file's existence
        means it was the most recent write, so secret_get must return B, not A —
        and migrate B back into the keychain."""
        fake = self._enable(monkeypatch)
        fake.d[("applemusic-mcp", "k")] = "A-stale"
        auth._write_private(auth._secret_file("k"), "B-newer")
        assert auth.secret_get("k") == "B-newer"
        # B was migrated into the keychain and the file removed (single copy).
        assert fake.d[("applemusic-mcp", "k")] == "B-newer"
        assert not auth._secret_file("k").exists()

    def test_secret_delete_reports_failure_when_backend_errors(self, mock_config_dir, monkeypatch):
        """A locked keychain that can't delete must NOT report success."""

        class _StuckKeyring(_FakeKeyring):
            def delete_password(self, service, key):
                pass  # pretend it succeeded but the value stays

            def get_password(self, service, key):
                return self.d.get((service, key))

        monkeypatch.delenv("APPLEMUSIC_NO_KEYRING", raising=False)
        monkeypatch.setattr(auth.sys, "platform", "win32")  # keychain path (Windows)
        stuck = _StuckKeyring()
        stuck.d[("applemusic-mcp", "k")] = "stuck"
        monkeypatch.setattr(auth, "_keyring", stuck)
        assert auth.secret_delete("k") is False  # still present → reports failure


class TestResolveWebToken:
    """amp-api needs the harvested web token, never the generated one (which it
    401s). resolve_web_token must return harvested even when a generated token
    exists."""

    def test_returns_cached_harvested(self, mock_config_dir, monkeypatch):
        monkeypatch.setattr(auth, "resolve_web_token", _REAL_RESOLVE_WEB)
        auth._save_harvested_token("h.token")  # plenty of life left
        monkeypatch.setattr(
            auth, "harvest_developer_token", lambda: pytest.fail("should not harvest")
        )
        assert auth.resolve_web_token() == "h.token"

    def test_harvests_when_no_cache(self, mock_config_dir, monkeypatch):
        monkeypatch.setattr(auth, "resolve_web_token", _REAL_RESOLVE_WEB)
        monkeypatch.setattr(auth, "harvest_developer_token", lambda: "fresh.harvest")
        assert auth.resolve_web_token() == "fresh.harvest"


class TestGetUserToken:
    """Tests for get_user_token function."""

    def test_returns_valid_token(self, mock_config_dir, mock_user_token):
        """Should return user token when present."""
        token_file = mock_config_dir / "music_user_token.json"
        token_data = {"music_user_token": mock_user_token}
        with open(token_file, "w") as f:
            json.dump(token_data, f)

        result = auth.get_user_token()

        assert result == mock_user_token

    def test_raises_when_token_missing(self, mock_config_dir):
        """Should raise FileNotFoundError when token doesn't exist."""
        with pytest.raises(FileNotFoundError) as exc_info:
            auth.get_user_token()

        assert "Music user token not found" in str(exc_info.value)


class TestSaveUserToken:
    """Tests for save_user_token function."""

    def test_saves_token_to_file(self, mock_config_dir, mock_user_token):
        """Should save token to JSON file."""
        auth.save_user_token(mock_user_token)

        token_file = mock_config_dir / "music_user_token.json"
        assert token_file.exists()

        with open(token_file) as f:
            data = json.load(f)

        assert data["music_user_token"] == mock_user_token
        assert "created" in data

    @pytest.mark.skipif(os.name == "nt", reason="POSIX file modes only")
    def test_token_file_is_owner_only_readable(self, mock_config_dir, mock_user_token):
        """The Music User Token file must be chmod 600 — on a multi-user box other
        users could otherwise read the token (security issue #32)."""
        auth.save_user_token(mock_user_token)
        token_file = mock_config_dir / "music_user_token.json"
        mode = token_file.stat().st_mode & 0o777
        assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


class TestIsTrustedLocalRequest:
    """Server-side Host/Origin gate for the local auth server (issue #32).

    The CORS response header alone cannot stop a cross-origin form POST from
    being processed (simple requests skip preflight) — this server-side check
    is what actually blocks forged-token injection and DNS rebinding."""

    PORT = 8765

    def _headers(self, host=None, origin=None):
        h = {}
        if host is not None:
            h["Host"] = host
        if origin is not None:
            h["Origin"] = origin
        return h

    def test_same_origin_post_allowed(self):
        h = self._headers(host="localhost:8765", origin="http://localhost:8765")
        assert auth._is_trusted_local_request(h, self.PORT) is True

    def test_same_origin_navigation_without_origin_allowed(self):
        # Same-origin GET navigations omit the Origin header.
        h = self._headers(host="localhost:8765")
        assert auth._is_trusted_local_request(h, self.PORT) is True

    def test_loopback_ip_variant_allowed(self):
        h = self._headers(host="127.0.0.1:8765", origin="http://127.0.0.1:8765")
        assert auth._is_trusted_local_request(h, self.PORT) is True

    def test_cross_origin_post_rejected(self):
        # A malicious page POSTing a forged token: browser sends its Origin.
        h = self._headers(host="localhost:8765", origin="https://evil.example")
        assert auth._is_trusted_local_request(h, self.PORT) is False

    def test_dns_rebinding_rejected(self):
        # evil.example resolving to 127.0.0.1 — browser sends the FOREIGN host.
        h = self._headers(host="evil.example:8765", origin=None)
        assert auth._is_trusted_local_request(h, self.PORT) is False

    def test_missing_host_rejected(self):
        assert auth._is_trusted_local_request(self._headers(), self.PORT) is False

    def test_wrong_port_rejected(self):
        h = self._headers(host="localhost:9999", origin="http://localhost:9999")
        assert auth._is_trusted_local_request(h, self.PORT) is False

    def test_null_origin_rejected(self):
        # Sandboxed iframes / data: URLs send the literal string "null".
        h = self._headers(host="localhost:8765", origin="null")
        assert auth._is_trusted_local_request(h, self.PORT) is False


class TestConfigExample:
    """The shipped config.example.json must not enable surprising behavior when
    copied verbatim (security issue #32)."""

    def test_auto_add_defaults_off(self):
        example = Path(__file__).resolve().parent.parent / "config.example.json"
        data = json.loads(example.read_text())
        assert data.get("preferences", {}).get("auto_add") is False, (
            "config.example.json must ship auto_add=false to match the safe " "code default"
        )


class TestCreateAuthHtml:
    """Tests for create_auth_html function."""

    def test_contains_developer_token(self, mock_developer_token):
        """Should embed developer token in HTML."""
        html = auth.create_auth_html(mock_developer_token, 8765)

        assert mock_developer_token in html

    def test_contains_port(self):
        """Should embed port number in HTML."""
        html = auth.create_auth_html("test_token", 9999)

        assert "9999" in html

    def test_contains_musickit_script(self, mock_developer_token):
        """Should include MusicKit JS library."""
        html = auth.create_auth_html(mock_developer_token, 8765)

        assert "musickit" in html.lower()
        assert "js-cdn.music.apple.com" in html


class TestGetUserPreferences:
    """Tests for get_user_preferences function."""

    def test_returns_defaults_when_no_config(self, mock_config_dir):
        """Should return default preferences when config doesn't exist."""
        prefs = auth.get_user_preferences()

        assert prefs["fetch_explicit"] is False
        assert prefs["clean_only"] is False

    def test_returns_defaults_when_no_preferences_section(self, mock_config_dir, sample_config):
        """Should return defaults when preferences section missing."""
        config_file = mock_config_dir / "config.json"
        with open(config_file, "w") as f:
            json.dump(sample_config, f)

        prefs = auth.get_user_preferences()

        assert prefs["fetch_explicit"] is False
        assert prefs["clean_only"] is False

    def test_returns_configured_preferences(self, mock_config_dir, sample_config):
        """Should return configured preferences when set."""
        config = sample_config.copy()
        config["preferences"] = {
            "fetch_explicit": True,
            "clean_only": False,
        }
        config_file = mock_config_dir / "config.json"
        with open(config_file, "w") as f:
            json.dump(config, f)

        prefs = auth.get_user_preferences()

        assert prefs["fetch_explicit"] is True
        assert prefs["clean_only"] is False

    def test_handles_partial_preferences(self, mock_config_dir, sample_config):
        """Should use defaults for missing preference keys."""
        config = sample_config.copy()
        config["preferences"] = {
            "fetch_explicit": True,
            # clean_only not set
        }
        config_file = mock_config_dir / "config.json"
        with open(config_file, "w") as f:
            json.dump(config, f)

        prefs = auth.get_user_preferences()

        assert prefs["fetch_explicit"] is True
        assert prefs["clean_only"] is False  # default
