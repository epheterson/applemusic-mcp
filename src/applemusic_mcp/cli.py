"""Command-line interface for the Apple Music MCP server.

Five verbs, no ceremony:

    applemusic-mcp serve            run the MCP server (your client calls this)
    applemusic-mcp login            sign in (web flow, no developer account)
    applemusic-mcp login --dev      sign in with an Apple Developer token (.p8)
    applemusic-mcp logout           sign out (switch accounts)
    applemusic-mcp status           show auth status
    applemusic-mcp reset --force    wipe all credentials

The developer-token flow folds the old init + generate-token + authorize steps
into one guided `login --dev`.
"""

import argparse
import json
import sys
import time

from .auth import (
    get_config_dir,
    generate_developer_token,
    run_auth_server,
    get_developer_token,
    get_user_token,
)


def cmd_login(args):
    """Sign in. Web flow by default (captures a media-user-token, no developer
    account); `--dev` runs the Apple Developer token flow (.p8 → token →
    authorize)."""
    if args.dev:
        return _login_dev(args)
    from .browser import _cli_signin

    return _cli_signin()


def _login_dev(args):
    """Guided Apple Developer token setup: ensure config.json, generate the
    developer token, then authorize for a user token. Prompts for anything
    missing."""
    config_dir = get_config_dir()
    config_file = config_dir / "config.json"

    if not config_file.exists():
        print("Apple Developer setup (one time). From your MusicKit key:")
        team_id = args.team_id or input("  Team ID: ").strip()
        key_id = args.key_id or input("  Key ID: ").strip()
        key_path = args.key_path or input("  Path to .p8 key: ").strip()
        if not (team_id and key_id and key_path):
            print("Error: team ID, key ID, and .p8 path are all required.")
            return 1
        config_dir.mkdir(parents=True, exist_ok=True)
        with open(config_file, "w") as f:
            json.dump(
                {"team_id": team_id, "key_id": key_id, "private_key_path": key_path}, f, indent=2
            )
        print(f"Wrote {config_file}")

    try:
        generate_developer_token(expiry_days=args.days)
        print("Developer token generated.")
    except FileNotFoundError as e:
        print(f"Error: {e}")
        return 1
    except Exception as e:
        print(f"Error generating token: {e}")
        return 1

    token = run_auth_server(port=args.port)
    return 0 if token else 1


def cmd_logout(args):
    """Sign out: clear the media-user-token and browser session so you can sign in
    with a different account. Leaves any developer token in place."""
    from . import browser
    from .auth import secret_delete

    for key in ("music_user_token", "harvested_token"):
        secret_delete(key)
    browser.clear_session()
    print("Signed out. Run `applemusic-mcp login` to sign in (you can switch accounts now).")
    return 0


def cmd_reset(args):
    """Wipe ALL credentials (developer token, config.json, user/web tokens,
    browser session). The downloaded .p8 key file is left in place."""
    from . import browser
    from .auth import secret_delete

    if not args.force:
        print("This removes the developer token, config.json, the user/web tokens, and the")
        print("browser session (your .p8 key file is kept). Re-run with --force to proceed.")
        return 1
    for key in ("developer_token", "music_user_token", "harvested_token"):
        secret_delete(key)
    cfg_file = get_config_dir() / "config.json"
    if cfg_file.exists():
        cfg_file.unlink()
    browser.clear_session()
    print("Reset complete. Run `applemusic-mcp login` (web) or `login --dev` (developer token).")
    return 0


def cmd_status(args):
    """Show auth status: developer token, user token, and a live API check."""
    from .auth import developer_token_info, has_user_token

    config_dir = get_config_dir()
    print("Apple Music MCP status")
    print("=" * 40)
    print(f"Config: {config_dir}")

    data = developer_token_info()
    if data is not None and data.get("expires", 0) > time.time():
        days_left = (data["expires"] - time.time()) / 86400
        print(f"Developer token: valid ({days_left:.0f} days left)")
    elif data is not None:
        print("Developer token: expired")
    else:
        print("Developer token: none")

    print(f"User token: {'present' if has_user_token() else 'none'}")

    try:
        import requests

        headers = {
            "Authorization": f"Bearer {get_developer_token()}",
            "Music-User-Token": get_user_token(),
        }
        r = requests.get(
            "https://api.music.apple.com/v1/me/library/playlists",
            headers=headers,
            params={"limit": 1},
        )
        print("API: ok" if r.status_code == 200 else f"API: status {r.status_code}")
    except FileNotFoundError:
        print("API: not configured (run `applemusic-mcp login`)")
    except Exception as e:
        print(f"API: error ({e})")
    return 0


def cmd_serve(args):
    """Start the MCP server."""
    from .server import main

    main()


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="MCP server for Apple Music")
    sub = parser.add_subparsers(dest="command", help="Commands")

    sub.add_parser("serve", help="Run the MCP server (your client calls this)")

    login = sub.add_parser("login", help="Sign in (web flow; --dev for an Apple Developer token)")
    login.add_argument("--dev", action="store_true", help="Apple Developer token flow (.p8)")
    login.add_argument("--team-id", dest="team_id", help="Apple Developer Team ID (with --dev)")
    login.add_argument("--key-id", dest="key_id", help="MusicKit Key ID (with --dev)")
    login.add_argument("--key-path", dest="key_path", help="Path to the .p8 key (with --dev)")
    login.add_argument("--days", type=int, default=180, help="Token validity in days (max 180)")
    login.add_argument("--port", type=int, default=8765, help="Local authorize port (with --dev)")

    # `signin` stays as a hidden alias for the heavily-documented old name.
    signin = sub.add_parser("signin")
    signin.add_argument("--dev", action="store_true")
    signin.add_argument("--team-id", dest="team_id")
    signin.add_argument("--key-id", dest="key_id")
    signin.add_argument("--key-path", dest="key_path")
    signin.add_argument("--days", type=int, default=180)
    signin.add_argument("--port", type=int, default=8765)

    sub.add_parser("logout", help="Sign out (switch accounts)")
    sub.add_parser("status", help="Show auth status")
    reset = sub.add_parser("reset", help="Wipe all credentials (keeps your .p8 key file)")
    reset.add_argument("--force", action="store_true", help="Confirm the wipe")

    args = parser.parse_args()

    if args.command in ("login", "signin"):
        sys.exit(cmd_login(args))
    elif args.command == "logout":
        sys.exit(cmd_logout(args))
    elif args.command == "status":
        sys.exit(cmd_status(args))
    elif args.command == "reset":
        sys.exit(cmd_reset(args))
    elif args.command == "serve":
        cmd_serve(args)
    else:
        parser.print_help()
        sys.exit(0)


if __name__ == "__main__":
    main()
