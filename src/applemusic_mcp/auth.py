"""Authentication and token management for Apple Music API."""

import json
import os
import time
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs

import jwt

DEFAULT_CONFIG_DIR = Path.home() / ".config" / "applemusic-mcp"


def get_config_dir() -> Path:
    """Get or create the config directory."""
    config_dir = DEFAULT_CONFIG_DIR
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


def load_config() -> dict:
    """Load configuration from config.json, returning empty dict if not found."""
    config_file = get_config_dir() / "config.json"
    if not config_file.exists():
        return {}
    with open(config_file) as f:
        return json.load(f)


def get_user_preferences() -> dict:
    """Get user preferences with defaults.

    Returns:
        dict with keys:
        - fetch_explicit: bool (default False)
        - reveal_on_library_miss: bool (default False)
        - clean_only: bool (default False)
        - auto_search: bool (default False)
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
        "reveal_on_library_miss": prefs.get("reveal_on_library_miss", False),
        "clean_only": prefs.get("clean_only", False),
        "auto_search": prefs.get(
            "auto_search", False
        ),  # Default FALSE (don't modify library without permission)
        "storefront": prefs.get("storefront", "us"),  # Apple Music region (default: US)
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
        raise FileNotFoundError("No config.json found. Run: applemusic-mcp init")
    key_path = get_private_key_path(config)

    with open(key_path) as f:
        private_key = f.read()

    now = int(time.time())
    exp = now + (expiry_days * 24 * 60 * 60)

    headers = {"alg": "ES256", "kid": config["key_id"]}
    payload = {"iss": config["team_id"], "iat": now, "exp": exp}

    token = jwt.encode(payload, private_key, algorithm="ES256", headers=headers)

    # Save token
    token_file = get_config_dir() / "developer_token.json"
    token_data = {
        "token": token,
        "created": now,
        "expires": exp,
        "team_id": config["team_id"],
        "key_id": config["key_id"],
    }
    with open(token_file, "w") as f:
        json.dump(token_data, f, indent=2)
    os.chmod(token_file, 0o600)

    return token


def get_developer_token() -> str:
    """Get existing developer token or raise if not found/expired."""
    token_file = get_config_dir() / "developer_token.json"
    if not token_file.exists():
        raise FileNotFoundError("Developer token not found. Run: applemusic-mcp generate-token")

    with open(token_file) as f:
        data = json.load(f)

    # Check if expired (with 1 day buffer)
    if data["expires"] < time.time() + 86400:
        raise ValueError(
            "Developer token expired or expiring soon. Run: applemusic-mcp generate-token"
        )

    return data["token"]


def get_user_token() -> str:
    """Get the music user token or raise if not found."""
    token_file = get_config_dir() / "music_user_token.json"
    if not token_file.exists():
        raise FileNotFoundError("Music user token not found. Run: applemusic-mcp authorize")

    with open(token_file) as f:
        data = json.load(f)

    return data["music_user_token"]


def save_user_token(token: str) -> None:
    """Save the music user token."""
    token_file = get_config_dir() / "music_user_token.json"
    data = {
        "music_user_token": token,
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    with open(token_file, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(token_file, 0o600)


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
                    app: {{ name: 'Apple Music MCP Server', build: '1.0.0' }}
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

    # Write auth HTML with restricted permissions (contains developer token)
    auth_html = create_auth_html(developer_token, port)
    auth_file = config_dir / "auth.html"
    with open(auth_file, "w", encoding="utf-8") as f:
        f.write(auth_html)
    os.chmod(auth_file, 0o600)

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
    else:
        print("No token received.")
        return None
