"""Authentication and token management for Apple Music API."""

import json
import logging
import os
import re
import sys
import time
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs

import jwt
import requests

from . import paths

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_DIR = paths.config_dir()


def _write_private(path, text: str) -> None:
    """Write ``text`` to ``path`` created 0600 from the start — no world-readable
    window on POSIX. The old ``open(w)`` + ``chmod`` pattern left the secret
    readable between create and chmod; ``os.open`` with the mode closes that
    TOCTOU gap. NOTE: POSIX mode bits don't restrict access on Windows — that's
    why Windows defaults ``secure_storage`` to the keychain (see
    ``get_user_preferences``)."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


# --- Secret store: OS keychain with a 0600-file fallback --------------------
# Tokens live in the OS keychain (macOS Keychain / Windows Credential Locker /
# Linux Secret Service) when a backend is available, else a 0600 JSON file (the
# same filenames/format as before, so existing installs and headless servers
# keep working). The stored VALUE is always the JSON blob the file used to hold,
# so callers parse it identically regardless of backend.

try:  # keyring is a dependency, but import defensively
    import keyring as _keyring
except Exception:  # pragma: no cover - keyring import failure
    _keyring = None  # type: ignore

_KEYRING_SERVICE = "applemusic-mcp"


def _keyring_ok() -> bool:
    """True when the OS keychain should be used. Auto-decided by platform, no
    user knob: Windows uses the Credential Locker (POSIX 0600 file bits are a
    no-op there, so a token file would be readable by other local accounts);
    macOS and Linux use 0600 files, because the keychain's per-process ACL is
    unreliable across this tool's separate CLI and server processes. The
    APPLEMUSIC_NO_KEYRING=1 test guard forces files everywhere."""
    if os.environ.get("APPLEMUSIC_NO_KEYRING") == "1" or _keyring is None:
        return False
    if sys.platform != "win32":
        return False
    try:
        kr = _keyring.get_keyring()
        name = f"{type(kr).__module__}.{type(kr).__name__}".lower()
        return "fail" not in name and "null" not in name
    except Exception:
        return False


def _secret_file(key: str) -> Path:
    # Back-compat: same filenames the tokens have always used.
    return get_config_dir() / f"{key}.json"


def secret_set(key: str, value: str) -> None:
    """Persist a secret blob under ``key``. Invariant: the secret lives in exactly
    ONE place. Keychain write deletes the file; file write (keychain unavailable)
    best-effort deletes any keychain copy. Combined with file-precedence in
    ``secret_get``, this prevents a stale keychain value from shadowing a newer
    file token (the SSH-writes-file / GUI-writes-keychain flap)."""
    if _keyring_ok():
        try:
            _keyring.set_password(_KEYRING_SERVICE, key, value)
            _secret_file(key).unlink(missing_ok=True)
            return
        except Exception:  # pragma: no cover - backend write failure → fall back
            pass
    # File path: drop any now-stale keychain copy so reads don't shadow this write.
    if _keyring is not None:
        try:
            _keyring.delete_password(_KEYRING_SERVICE, key)
        except Exception:  # pragma: no cover
            pass
    # On Windows the secret SHOULD live in the Credential Locker; if we're here, that
    # backend was unavailable and we're writing a plain file — say so, don't hide it.
    if sys.platform == "win32":
        logger.warning(
            "Windows Credential Locker unavailable — storing %r in a local file "
            "(%s) instead of the OS keychain.",
            key,
            _secret_file(key),
        )
    _write_private(_secret_file(key), value)


def secret_get(key: str) -> Optional[str]:
    """Read a secret blob. A file's EXISTENCE means it was the most recent write
    (keychain writes always delete the file), so the file wins and is migrated
    back into the keychain when one is available; otherwise read the keychain."""
    f = _secret_file(key)
    if f.exists():
        try:
            data = f.read_text()
        except OSError:
            data = None
        if data is not None:
            if _keyring_ok():  # migrate the newer file value into the keychain
                try:
                    _keyring.set_password(_KEYRING_SERVICE, key, data)
                    f.unlink(missing_ok=True)
                except Exception:  # pragma: no cover
                    pass
            return data
    if _keyring_ok():
        try:
            return _keyring.get_password(_KEYRING_SERVICE, key)
        except Exception:  # pragma: no cover
            pass
    return None


def secret_delete(key: str) -> bool:
    """Forget a secret from both the keychain and the file. Returns True only if
    the secret is actually gone afterward (so logout/reset can't report success
    while a locked keychain still holds it)."""
    if _keyring_ok():
        try:
            _keyring.delete_password(_KEYRING_SERVICE, key)
        except Exception:  # not present, or a real backend error — verify below
            pass
    _secret_file(key).unlink(missing_ok=True)
    return secret_get(key) is None


def has_user_token() -> bool:
    return (
        bool(os.environ.get("APPLEMUSIC_USER_TOKEN")) or secret_get("music_user_token") is not None
    )


def developer_token_info() -> Optional[dict]:
    """Parsed generated-developer-token record ({token, expires, ...}) or None."""
    raw = secret_get("developer_token")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None


# --- Harvested (fallback) developer token -----------------------------------
# Apple ships a public developer token (issuer "AMPWebPlay") to every browser
# that loads music.apple.com, embedded in the web player's JS bundle. It's the
# fallback dev-token source for users WITHOUT a generated (paid) token — the
# generated token always takes precedence (legit, 6-month).
_HARVEST_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
_HARVEST_BROWSE_URL = "https://music.apple.com/us/browse"
_HARVEST_ORIGIN = "https://music.apple.com"
_HARVEST_TIMEOUT = 15
# The web player's main JS chunk, e.g. /assets/index~6da982354d.js
_BUNDLE_RE = re.compile(r"/assets/index~[A-Za-z0-9]+\.js")
# A complete JWT whose payload segment begins with the AMPWebPlay issuer claim.
_AMP_TOKEN_RE = re.compile(
    r"eyJ0eXAiOiJKV1Q[A-Za-z0-9_-]+\.eyJpc3MiOiJBTVBXZWJQbGF5[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
)


def extract_developer_token(bundle_js: str) -> str:
    """Pure extraction step (no network) — split out for testability. Given the
    web-player bundle's JS source, return the embedded AMPWebPlay JWT. Raises
    RuntimeError if not found."""
    m = _AMP_TOKEN_RE.search(bundle_js)
    if not m:
        raise RuntimeError("AMPWebPlay developer token not found in web-player bundle")
    return m.group(0)


def harvest_developer_token() -> str:
    """Fetch music.apple.com's web-player bundle and extract its public
    developer token. Network call; raises on failure."""
    browse = requests.get(_HARVEST_BROWSE_URL, headers=_HARVEST_UA, timeout=_HARVEST_TIMEOUT)
    m = _BUNDLE_RE.search(browse.text)
    if not m:
        raise RuntimeError("Could not locate the web-player JS bundle on music.apple.com")
    bundle = requests.get(
        _HARVEST_ORIGIN + m.group(0), headers=_HARVEST_UA, timeout=_HARVEST_TIMEOUT
    )
    return extract_developer_token(bundle.text)


def _harvested_token_file() -> Path:
    return get_config_dir() / "harvested_token.json"


# Re-harvest the web token once it's within this many days of expiry. Sparse
# usage means we want plenty of runway, so a fresh token is fetched well before
# the old one dies rather than at the last minute.
_HARVEST_REFRESH_DAYS = 15


def _load_harvested_token() -> Optional[str]:
    """Return the cached harvested token only if it has more than
    ``_HARVEST_REFRESH_DAYS`` of life left; otherwise return None so
    ``resolve_developer_token`` fetches a fresh one."""
    raw = secret_get("harvested_token")
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if data.get("expires", 0) > time.time() + _HARVEST_REFRESH_DAYS * 86400:
            return data["token"]
    except (json.JSONDecodeError, ValueError, KeyError):
        return None
    return None


def _save_harvested_token(token: str) -> None:
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
        exp = int(claims.get("exp", time.time() + 30 * 86400))
    except Exception:
        exp = int(time.time() + 30 * 86400)
    secret_set(
        "harvested_token", json.dumps({"token": token, "expires": exp, "source": "harvested"})
    )


def resolve_developer_token() -> str:
    """Return a usable developer token, **preferring the generated (paid) one**.

    Order: valid generated token → valid cached harvested token → harvest fresh.
    The generated token is the legit, recommended path; harvesting is the
    fallback so users without an Apple Developer account still work.
    """
    try:
        return get_developer_token()  # generated (paid) — preferred
    except (FileNotFoundError, ValueError):
        pass
    return resolve_web_token()


def resolve_web_token() -> str:
    """A developer token accepted by ``amp-api.music.apple.com`` (the web player's
    host). This is ALWAYS the harvested ``AMPWebPlay`` token — amp-api rejects a
    generated (Apple Developer) token with 401, even though it works on the public
    ``api.music.apple.com``. So every amp-api call uses this, not
    ``resolve_developer_token`` (which prefers the generated token)."""
    cached = _load_harvested_token()
    if cached:
        return cached
    token = harvest_developer_token()
    _save_harvested_token(token)
    return token


def has_any_developer_token() -> bool:
    """True if a developer token is obtainable from either source (generated or
    harvestable). Used for API-vs-fallback feature detection."""
    try:
        resolve_developer_token()
        return True
    except Exception as exc:
        # Don't let a network blip during harvest look identical to "no credentials":
        # log it so a silently-downgraded write rail is at least visible in the logs.
        logger.warning("has_any_developer_token: couldn't obtain a developer token: %s", exc)
        return False


def get_config_dir() -> Path:
    """Get or create the config directory (0700 — it holds tokens and the .p8)."""
    config_dir = DEFAULT_CONFIG_DIR
    try:
        config_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # A clear, actionable message beats a raw traceback (e.g. an unwritable
        # APPLEMUSIC_MCP_HOME, or a root-owned mount under a non-root user).
        raise RuntimeError(
            f"Can't create the config directory at {config_dir}: {exc}. "
            "Check that the path is writable (or set APPLEMUSIC_MCP_HOME to one that is)."
        ) from exc
    try:
        os.chmod(config_dir, 0o700)
    except OSError:
        pass
    return config_dir


_config_cache: "Optional[tuple[float, dict]]" = None


def load_config() -> dict:
    """Load config.json (empty dict if absent). Cached by file mtime — `_engine()`
    reads prefs many times per tool call, so this avoids re-parsing on every read;
    a `set-pref` write changes the mtime and invalidates the cache automatically."""
    global _config_cache
    config_file = get_config_dir() / "config.json"
    if not config_file.exists():
        _config_cache = None
        return {}
    try:
        mtime = config_file.stat().st_mtime
    except OSError:
        mtime = 0.0
    if _config_cache is not None and _config_cache[0] == mtime:
        return _config_cache[1]
    with open(config_file) as f:
        data = json.load(f)
    _config_cache = (mtime, data)
    return data


def get_user_preferences() -> dict:
    """Get user preferences with defaults.

    Returns:
        dict with keys:
        - fetch_explicit: bool (default False)
        - clean_only: bool (default False)
        - auto_add: bool (default False)
        - storefront: str (default "us")
    """
    try:
        config = load_config()
        prefs = config.get("preferences", {})
    except (FileNotFoundError, json.JSONDecodeError):
        prefs = {}

    # Return with defaults
    return {
        "fetch_explicit": prefs.get("fetch_explicit", False),
        "clean_only": prefs.get("clean_only", False),
        # Default FALSE (don't modify the library without permission). `auto_search`
        # is the old name for this key and is still honored from existing configs.
        "auto_add": prefs.get("auto_add", prefs.get("auto_search", False)),
        "storefront": prefs.get("storefront", "us"),  # Apple Music region (default: US)
        # Single engine mode, governs BOTH data ops and playback: "auto"
        # (native Music.app on macOS, web API + Chrome web player elsewhere),
        # "native" (all-in on the local Music.app, macOS, no token), or "web"
        # (all-in on the cross-platform Apple Music web API + web player, so a Mac
        # not signed into Music.app, or on a different account, stays fully web).
        # "api" is accepted as a back-compat alias for "web". Playback always
        # follows the engine, so there is no separate playback preference.
        "mode": prefs.get("mode", "auto"),
        # Token storage location is auto-decided by platform (see _keyring_ok),
        # not a user preference: Windows uses the Credential Locker, macOS/Linux
        # use 0600 files.
    }


def get_private_key_path(config: dict) -> Path:
    """Resolve the private key path from config."""
    path = Path(config["private_key_path"]).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"Private key not found: {path}")
    return path


def generate_developer_token(expiry_days: int = 180) -> str:
    """Generate a developer token (JWT) valid for up to 180 days."""
    config = load_config()
    if not config:
        raise FileNotFoundError("No config.json found. Run: applemusic-mcp login --dev")
    key_path = get_private_key_path(config)

    with open(key_path) as f:
        private_key = f.read()

    now = int(time.time())
    exp = now + (expiry_days * 24 * 60 * 60)

    headers = {"alg": "ES256", "kid": config["key_id"]}
    payload = {"iss": config["team_id"], "iat": now, "exp": exp}

    token = jwt.encode(payload, private_key, algorithm="ES256", headers=headers)

    # Save token
    token_data = {
        "token": token,
        "created": now,
        "expires": exp,
        "team_id": config["team_id"],
        "key_id": config["key_id"],
    }
    secret_set("developer_token", json.dumps(token_data))

    return token


# The MCP may be used only occasionally, so a passive "expiring soon" warning
# can arrive after the token has already died. Whenever the generated token is
# within this many days of expiry AND the signing key (.p8) is present, mint a
# fresh one on use — the same self-healing the harvested web token already has.
_DEV_TOKEN_RENEW_DAYS = 30


def can_generate_developer_token() -> bool:
    """True if we have everything needed to mint a fresh generated token (config
    + a readable .p8). When True, the generated token self-renews and no manual
    `generate-token` is needed."""
    try:
        config = load_config()
        if not config.get("team_id") or not config.get("key_id"):
            return False
        get_private_key_path(config)  # raises if the .p8 is missing
        return True
    except Exception:
        return False


def get_developer_token() -> str:
    """Get the generated developer token, auto-renewing it when it's within
    ``_DEV_TOKEN_RENEW_DAYS`` of expiry and the signing key is available.

    Raises FileNotFoundError/ValueError when there's no usable token and none can
    be minted — callers (``resolve_developer_token``) then fall back to the
    harvested web token."""
    data = developer_token_info()
    if data is not None:
        try:
            days_left = (data["expires"] - time.time()) / 86400
        except (KeyError, TypeError):
            days_left = -1.0
        if days_left > _DEV_TOKEN_RENEW_DAYS:
            return data["token"]
        # In the renewal window (or already expired): mint a fresh one if we can.
        if can_generate_developer_token():
            return generate_developer_token()
        if days_left > 1:
            return data["token"]  # still valid; no key to renew with, so keep using it
        raise ValueError(
            "Developer token expired or expiring soon and no signing key is available "
            "to renew it. Run: applemusic-mcp login --dev (or `login` for the web path)."
        )
    # No stored token yet — generate if we have the key, else signal "not found".
    if can_generate_developer_token():
        return generate_developer_token()
    raise FileNotFoundError("Developer token not found. Run: applemusic-mcp login --dev")


def get_user_token() -> str:
    """Get the music user token or raise if not found.

    Honors APPLEMUSIC_USER_TOKEN if set (for headless / CI runs), else the stored token."""
    env_tok = os.environ.get("APPLEMUSIC_USER_TOKEN")
    if env_tok:
        return env_tok
    raw = secret_get("music_user_token")
    if not raw:
        raise FileNotFoundError("Music user token not found. Run: applemusic-mcp login")
    return json.loads(raw)["music_user_token"]


def save_user_token(token: str) -> None:
    """Save the music user token."""
    data = {
        "music_user_token": token,
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    secret_set("music_user_token", json.dumps(data))


def create_auth_html(developer_token: str, port: int) -> str:
    """Generate the HTML for browser-based authorization with auto-submit."""
    return f"""<!DOCTYPE html>
<html>
<head>
    <title>Apple Music Authorization</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, sans-serif;
            max-width: 600px;
            margin: 50px auto;
            padding: 20px;
            background: #1a1a1a;
            color: #fff;
        }}
        h1 {{ color: #fa586a; }}
        button {{
            background: #fa586a;
            color: white;
            border: none;
            padding: 15px 30px;
            font-size: 18px;
            border-radius: 8px;
            cursor: pointer;
            margin: 10px 0;
        }}
        button:hover {{ background: #ff6b7a; }}
        button:disabled {{ background: #666; cursor: not-allowed; }}
        .success {{ color: #4ade80; }}
        .error {{ color: #f87171; }}
        #status {{ margin: 20px 0; }}
        .spinner {{
            display: inline-block;
            width: 20px;
            height: 20px;
            border: 3px solid #666;
            border-radius: 50%;
            border-top-color: #fa586a;
            animation: spin 1s ease-in-out infinite;
            margin-right: 10px;
            vertical-align: middle;
        }}
        @keyframes spin {{
            to {{ transform: rotate(360deg); }}
        }}
    </style>
</head>
<body>
    <h1>Apple Music Authorization</h1>
    <p>Click the button below to authorize access to your Apple Music library.</p>
    <button id="authButton" onclick="authorize()">Authorize with Apple Music</button>
    <div id="status"></div>
    <script src="https://js-cdn.music.apple.com/musickit/v3/musickit.js" data-web-components async></script>
    <script>
        const developerToken = "{developer_token}";
        const serverPort = {port};

        document.addEventListener('musickitloaded', async () => {{
            try {{
                await MusicKit.configure({{
                    developerToken: developerToken,
                    app: {{ name: 'Apple Music MCP Server', build: '0.16.0' }}
                }});
                document.getElementById('status').innerHTML = '<p class="success">MusicKit loaded. Click the button to authorize.</p>';
            }} catch (err) {{
                document.getElementById('status').innerHTML = '<p class="error">Error loading MusicKit: ' + err.message + '</p>';
            }}
        }});

        async function authorize() {{
            const button = document.getElementById('authButton');
            const status = document.getElementById('status');
            button.disabled = true;
            status.innerHTML = '<p><span class="spinner"></span>Waiting for Apple authorization...</p>';

            try {{
                const music = MusicKit.getInstance();
                const musicUserToken = await music.authorize();

                status.innerHTML = '<p><span class="spinner"></span>Saving token...</p>';

                // POST token back to local server
                const response = await fetch('http://localhost:' + serverPort + '/save-token', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/x-www-form-urlencoded' }},
                    body: 'token=' + encodeURIComponent(musicUserToken)
                }});

                if (response.ok) {{
                    status.innerHTML = '<p class="success">✓ Authorization successful! Token saved.</p><p>You can close this window and return to the terminal.</p>';
                }} else {{
                    throw new Error('Failed to save token to server');
                }}
            }} catch (err) {{
                status.innerHTML = '<p class="error">Failed: ' + err.message + '</p>';
                button.disabled = false;
            }}
        }}
    </script>
</body>
</html>"""


def create_success_html() -> str:
    """Generate success page HTML."""
    return """<!DOCTYPE html>
<html>
<head>
    <title>Authorization Complete</title>
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, sans-serif;
            max-width: 600px;
            margin: 50px auto;
            padding: 20px;
            background: #1a1a1a;
            color: #fff;
            text-align: center;
        }
        h1 { color: #4ade80; }
        p { font-size: 18px; }
    </style>
</head>
<body>
    <h1>✓ Authorization Complete</h1>
    <p>Your Music User Token has been saved.</p>
    <p>You can close this window.</p>
</body>
</html>"""


_MAX_POST_BYTES = 65_536  # 64 KB — Apple Music tokens are well under 1 KB


def _is_trusted_local_request(headers, port: int) -> bool:
    """Server-side trust check for requests to the local auth server.

    The CORS ``Access-Control-Allow-Origin`` header alone does NOT protect the
    ``/save-token`` endpoint: a form-encoded POST is a CORS "simple request" —
    browsers send it without a preflight and the server processes it regardless
    of that header (CORS only gates whether cross-origin JS can read the
    response). So a malicious webpage could inject a forged Music User Token
    during the auth window. This closes that hole server-side:

    - **Host check** (always present): must be our exact localhost host:port.
      Also defeats DNS rebinding (evil.com resolving to 127.0.0.1 would carry
      ``Host: evil.com:{port}``).
    - **Origin check** (browsers send it on all cross-origin POSTs): when
      present, must be our exact localhost origin. Absent is allowed —
      same-origin GET navigations omit it.
    """
    allowed_hosts = (f"localhost:{port}", f"127.0.0.1:{port}")
    if headers.get("Host", "") not in allowed_hosts:
        return False
    origin = headers.get("Origin")
    allowed_origins = tuple(f"http://{h}" for h in allowed_hosts)
    if origin is not None and origin not in allowed_origins:
        return False
    return True


def run_auth_server(port: int = 8765) -> Optional[str]:
    """Run a local server for browser-based authorization with automatic token capture."""
    config_dir = get_config_dir()
    developer_token = get_developer_token()
    cors_origin = f"http://localhost:{port}"

    # Write auth HTML private from creation (it embeds the developer token).
    auth_html = create_auth_html(developer_token, port)
    auth_file = config_dir / "auth.html"
    _write_private(auth_file, auth_html)

    # Token storage for callback
    captured_token = {"value": None}
    server_should_stop = {"value": False}

    class AuthHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass  # Suppress logs

        def do_GET(self):
            # Host/Origin gate: the auth page embeds the developer token, so a
            # DNS-rebinding page (same-origin from the browser's view, but with
            # a foreign Host header) must not be able to read it.
            if not _is_trusted_local_request(self.headers, port):
                self.send_response(403)
                self.end_headers()
                return
            if self.path == "/auth.html" or self.path == "/":
                self.send_response(200)
                self.send_header("Content-type", "text/html; charset=utf-8")
                self.end_headers()
                with open(auth_file, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):
            # Server-side trust gate — the CORS header below does NOT stop a
            # cross-origin simple POST from being processed; this does. Closes
            # forged-token injection from a malicious webpage during the auth
            # window (see _is_trusted_local_request).
            if not _is_trusted_local_request(self.headers, port):
                self.send_response(403)
                self.end_headers()
                return
            if self.path == "/save-token":
                raw_length = self.headers.get("Content-Length", "0")
                try:
                    content_length = int(raw_length)
                except ValueError:
                    self.send_response(400)
                    self.end_headers()
                    return
                if content_length > _MAX_POST_BYTES:
                    self.send_response(413)
                    self.end_headers()
                    return

                post_data = self.rfile.read(content_length).decode("utf-8")
                params = parse_qs(post_data)

                token = params.get("token", [None])[0]
                if token:
                    save_user_token(token)
                    captured_token["value"] = token
                    server_should_stop["value"] = True

                    self.send_response(200)
                    self.send_header("Content-type", "text/html")
                    self.send_header("Access-Control-Allow-Origin", cors_origin)
                    self.end_headers()
                    self.wfile.write(create_success_html().encode())
                else:
                    self.send_response(400)
                    self.end_headers()
            else:
                self.send_response(404)
                self.end_headers()

        def do_OPTIONS(self):
            # CORS preflight — restricted to localhost origin only
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", cors_origin)
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

    print(f"Starting authorization server on http://localhost:{port}")
    print("Opening browser for Apple Music authorization...")
    print()
    print("1. Click 'Authorize with Apple Music' in the browser")
    print("2. Sign in with your Apple ID if prompted")
    print("3. The token will be saved automatically")
    print()

    server = HTTPServer(("localhost", port), AuthHandler)
    server.timeout = 1  # 1 second timeout for checking stop flag

    # Open browser
    webbrowser.open(f"http://localhost:{port}/auth.html")

    print("Waiting for authorization... (Ctrl+C to cancel)")
    print()

    try:
        while not server_should_stop["value"]:
            server.handle_request()
    except KeyboardInterrupt:
        print("\nCancelled.")
        return None
    finally:
        server.server_close()
        # Remove auth.html — it contains the developer token
        try:
            auth_file.unlink(missing_ok=True)
        except OSError:
            pass

    if captured_token["value"]:
        print("✓ Token saved successfully!")
        return captured_token["value"]
    else:  # pragma: no cover - unreachable: the serve loop only exits via
        # server_should_stop, which is set ONLY alongside capturing the token
        # (do_POST), so reaching here would require a captured-but-falsy token.
        print("No token received.")
        return None
