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
    """Sign in. On macOS the default is the Safari token harvest (no Chrome / no
    Playwright); pass `--chrome` for the Chrome web player instead, or `--dev` for
    the Apple Developer token flow. Off macOS, the Chrome web flow is the default
    (it's the only path there)."""
    if args.dev:
        return _login_dev(args)

    import platform

    is_mac = platform.system() == "Darwin"
    # Explicit --safari always routes to the Safari harvest (which rejects non-macOS
    # with a clear message rather than silently using Chrome).
    if getattr(args, "safari", False):
        return _login_safari()
    want_chrome = getattr(args, "chrome", False)
    # macOS default = Safari (zero-install, no Chrome). --chrome opts into Chrome.
    # Off macOS = Chrome (Playwright ships there; it's the only path).
    if is_mac and not want_chrome:
        return _login_safari()
    return _login_chrome(is_mac=is_mac)


def _login_chrome(is_mac: bool = False):
    """Chrome web-player sign-in (Playwright). The off-mac default; on macOS it's
    opt-in via --chrome and needs the `browser` extra installed."""
    from .browser import _cli_signin, _profile_in_use, is_available

    # On macOS Playwright is an optional extra — guide instead of a cryptic failure.
    if is_mac and not is_available():
        print(
            "Chrome sign-in needs Playwright, which macOS doesn't install by default.\n"
            "  • Zero-install instead:  applemusic-mcp login --safari\n"
            "  • Or add Chrome support:  pip install 'applemusic-mcp[browser]'  then re-run"
        )
        return 1

    # Fail fast rather than fighting a running MCP server for the Chrome profile
    # (which orphans windows and kills the server's browser context).
    if _profile_in_use():
        print(
            "The MCP server appears to be running and using the browser profile.\n"
            "Sign in through the server instead — ask your assistant to run the Apple\n"
            "Music sign-in (it'll open the player window) — or stop the server first,\n"
            "then re-run `applemusic-mcp login --chrome`.",
            flush=True,
        )
        return 1
    return _cli_signin()


def _login_safari():
    """macOS default: harvest the media-user-token from a signed-in Safari (no
    Chrome / no Playwright). The developer token is fetched tokenlessly on demand,
    so this alone is a complete sign-in. On failure, spell out the (security-
    sensitive) one-time Safari setting and the alternatives."""
    import platform

    if platform.system() != "Darwin":
        print("--safari is macOS-only. Use `applemusic-mcp login --chrome` instead.")
        return 1
    from . import safari
    from .auth import save_user_token

    print("Reading your Apple Music session from Safari…")
    ok, res = safari.media_user_token()
    if ok:
        save_user_token(res)
        print("Signed in via Safari — no Chrome needed (the developer token is fetched")
        print("automatically). Playback uses Music.app on macOS. For the cross-platform")
        print("web player, install `pip install 'applemusic-mcp[browser]'` and set mode=web.")
        return 0

    print(res)  # the specific reason (setting off / not signed in)
    print()
    print("To finish signing in, pick whichever you prefer:")
    print('  1) Easiest, no Chrome — in Safari turn on Settings → Advanced → "Show')
    print('     features for web developers", then the Develop menu → "Allow JavaScript')
    print("     from Apple Events\", make sure you're signed into Apple Music at")
    print("     music.apple.com, and re-run `applemusic-mcp login`.")
    print("     (That setting only lets this tool read ONE cookie — your Apple Music")
    print("     token — from your own signed-in Safari. It reads nothing else, and you")
    print("     can turn it back off afterward.)")
    print("  2) No browser at all — Apple Developer token:  applemusic-mcp login --dev")
    print("  3) Use Chrome —  pip install 'applemusic-mcp[browser]'  then")
    print("     applemusic-mcp login --chrome")
    return 1


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
    browser session). The downloaded .p8 key file is left in place — unless
    --all, which is a full uninstall that removes every state directory."""
    import shutil

    from . import browser, paths
    from .auth import secret_delete

    full = getattr(args, "all", False)
    if not args.force:
        if full:
            print("--all is a FULL UNINSTALL: it deletes every applemusic-mcp state directory —")
            print("credentials AND your .p8 key, the Chrome profile, and the cache/exports:")
            for d in paths.all_state_dirs():
                print(f"  {d}")
        else:
            print("This removes the developer token, config.json, the user/web tokens, and the")
            print("browser session (your .p8 key file is kept).")
        print("Re-run with --force to proceed.")
        return 1

    for key in ("developer_token", "music_user_token", "harvested_token"):
        secret_delete(key)

    if full:
        # Full uninstall: nuke all three state roots (keychain secrets cleared above).
        browser.clear_session()  # shut the engine down before removing the profile
        for d in paths.all_state_dirs():
            shutil.rmtree(d, ignore_errors=True)
        print("Full reset complete — all applemusic-mcp state removed (including your .p8).")
        print("Run `applemusic-mcp login` to start fresh.")
        return 0

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
        print("Developer token: expired — re-run `applemusic-mcp login --dev` to renew")
    elif has_user_token():
        # The web/Safari path has no generated token — it's harvested from Apple's
        # web player on demand. Say so rather than implying something's missing.
        print("Developer token: none (web path — uses Apple's web-player token)")
    else:
        print("Developer token: none")

    print(f"User token: {'present' if has_user_token() else 'none'}")

    # Live check. Prefer the generated developer token (api.music.apple.com); for
    # the web/Safari path there's no generated token, so verify the WEB session
    # (amp-api) instead — otherwise a fully signed-in web user is wrongly told
    # "not configured" right after signing in.
    try:
        dev = None
        try:
            dev = get_developer_token()
        except FileNotFoundError:
            dev = None
        if dev:
            import requests

            r = requests.get(
                "https://api.music.apple.com/v1/me/library/playlists",
                headers={"Authorization": f"Bearer {dev}", "Music-User-Token": get_user_token()},
                params={"limit": 1},
                timeout=30,
            )
            print("API: ok" if r.status_code == 200 else f"API: status {r.status_code}")
        elif has_user_token():
            from . import amp_api

            st = amp_api.session_status()
            print(
                {
                    "ok": "API: ok (web session)",
                    "expired": "API: session expired (run `applemusic-mcp login`)",
                    "throttled": "API: rate-limited (try again shortly)",
                }.get(st, "API: not configured (run `applemusic-mcp login`)")
            )
        else:
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
    login.add_argument(
        "--safari",
        action="store_true",
        help="macOS: read the session from a signed-in Safari (default on macOS)",
    )
    login.add_argument(
        "--chrome",
        action="store_true",
        help="Use the Chrome web player to sign in (default off macOS; needs the browser extra)",
    )
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
    reset.add_argument(
        "--all",
        action="store_true",
        dest="all",
        help="Full uninstall: remove ALL state dirs (config incl. .p8, Chrome profile, cache)",
    )

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
