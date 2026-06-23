"""Command-line interface for Apple Music MCP server setup."""

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


def cmd_init(args):
    """Initialize configuration directory and create sample config."""
    config_dir = get_config_dir()
    config_file = config_dir / "config.json"

    if config_file.exists() and not args.force:
        print(f"Config already exists: {config_file}")
        print("Use --force to overwrite")
        return 1

    sample_config = {
        "team_id": "YOUR_TEAM_ID",
        "key_id": "YOUR_KEY_ID",
        "private_key_path": str(config_dir / "AuthKey_XXXXXXXX.p8"),
    }

    with open(config_file, "w") as f:
        json.dump(sample_config, f, indent=2)

    print(f"Created config file: {config_file}")
    print()
    print("Next steps:")
    print("1. Edit the config file with your Apple Developer credentials")
    print("2. Copy your .p8 private key to the config directory")
    print("3. Run: applemusic-mcp generate-token")
    print("4. Run: applemusic-mcp authorize")
    return 0


def cmd_generate_token(args):
    """Generate a new developer token."""
    try:
        token = generate_developer_token(expiry_days=args.days)
        exp_time = time.time() + (args.days * 24 * 60 * 60)
        print(f"Developer token generated!")
        print(f"Expires: {time.ctime(exp_time)}")
        print(f"Token (first 50 chars): {token[:50]}...")
        return 0
    except FileNotFoundError as e:
        print(f"Error: {e}")
        print("Run 'applemusic-mcp init' first to create config")
        return 1
    except Exception as e:
        print(f"Error generating token: {e}")
        return 1


def cmd_authorize(args):
    """Run browser-based authorization to get music user token."""
    try:
        # Check developer token exists
        get_developer_token()
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}")
        return 1

    print("Starting authorization flow...")
    print()
    token = run_auth_server(port=args.port)
    if token:
        return 0
    return 1


def cmd_signin(args):
    """Browser sign-in to capture the media-user-token — the no-Apple-Developer-
    account path. Opens Chrome to music.apple.com; you sign in once and the token
    is saved for the unified API."""
    from .browser import _cli_signin

    return _cli_signin()


def cmd_status(args):
    """Check authentication status."""
    config_dir = get_config_dir()

    print("Apple Music MCP Status")
    print("=" * 40)
    print(f"Config directory: {config_dir}")
    print()

    # Check config
    config_file = config_dir / "config.json"
    if config_file.exists():
        print("✓ Config file exists")
        try:
            with open(config_file) as f:
                config = json.load(f)
            print(f"  Team ID: {config.get('team_id', 'NOT SET')}")
            print(f"  Key ID: {config.get('key_id', 'NOT SET')}")
        except Exception as e:
            print(f"  Error reading config: {e}")
    else:
        print("✗ Config file missing")

    print()

    # Check developer token
    dev_token_file = config_dir / "developer_token.json"
    if dev_token_file.exists():
        try:
            with open(dev_token_file) as f:
                data = json.load(f)
            exp = data.get("expires", 0)
            if exp > time.time():
                days_left = (exp - time.time()) / 86400
                print(f"✓ Developer token valid ({days_left:.0f} days remaining)")
            else:
                print("✗ Developer token expired")
        except Exception as e:
            print(f"✗ Developer token error: {e}")
    else:
        print("✗ Developer token missing")

    # Check user token
    user_token_file = config_dir / "music_user_token.json"
    if user_token_file.exists():
        print("✓ Music user token exists")
    else:
        print("✗ Music user token missing")

    print()

    # Test API connection
    try:
        import requests

        headers = {
            "Authorization": f"Bearer {get_developer_token()}",
            "Music-User-Token": get_user_token(),
        }
        response = requests.get(
            "https://api.music.apple.com/v1/me/library/playlists",
            headers=headers,
            params={"limit": 1},
        )
        if response.status_code == 200:
            print("✓ API connection successful")
        else:
            print(f"✗ API returned status {response.status_code}")
    except FileNotFoundError:
        print("✗ Cannot test API (missing tokens)")
    except Exception as e:
        print(f"✗ API error: {e}")

    return 0


def cmd_serve(args):
    """Start the MCP server."""
    from .server import main

    main()


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Apple Music MCP Server - Playlist management via API"
    )
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # init
    init_parser = subparsers.add_parser("init", help="Initialize configuration")
    init_parser.add_argument("--force", action="store_true", help="Overwrite existing config")

    # generate-token
    token_parser = subparsers.add_parser("generate-token", help="Generate developer token")
    token_parser.add_argument(
        "--days", type=int, default=180, help="Token validity in days (max 180)"
    )

    # authorize
    auth_parser = subparsers.add_parser("authorize", help="Authorize with Apple Music")
    auth_parser.add_argument("--port", type=int, default=8765, help="Local server port")

    # signin (browser-based media-user-token capture; no Apple Developer account)
    subparsers.add_parser(
        "signin",
        help="Browser sign-in to capture your media-user-token (no Apple Developer account)",
    )

    # status
    subparsers.add_parser("status", help="Check authentication status")

    # serve
    subparsers.add_parser("serve", help="Start MCP server")

    args = parser.parse_args()

    if args.command == "init":
        sys.exit(cmd_init(args))
    elif args.command == "generate-token":
        sys.exit(cmd_generate_token(args))
    elif args.command == "authorize":
        sys.exit(cmd_authorize(args))
    elif args.command == "signin":
        sys.exit(cmd_signin(args))
    elif args.command == "status":
        sys.exit(cmd_status(args))
    elif args.command == "serve":
        cmd_serve(args)
    else:
        parser.print_help()
        sys.exit(0)


if __name__ == "__main__":
    main()
