# Security Policy

## Reporting a vulnerability

Please report security issues privately via [GitHub Security Advisories](https://github.com/epheterson/applemusic-mcp/security/advisories/new) (or open a minimal issue asking for a private channel — don't include details publicly). You'll get an acknowledgement as soon as possible.

## What this server stores, and where

This is a **local** stdio MCP server. It talks to Apple Music on your behalf and holds the credentials needed to do so. Nothing is sent anywhere except Apple's own endpoints (`amp-api.music.apple.com`, `api.music.apple.com`, `music.apple.com`) and, for catalog resolution, the public iTunes Search API.

- **Tokens** (the Apple Developer token, your `media-user-token`, and the harvested web-player token) are stored by default in **`0600` files** under `~/.config/applemusic-mcp/` — readable only by you, and reliable across the separate CLI and server processes. Set the `secure_storage` preference to `keychain` to use the **OS keychain** instead (macOS Keychain / Windows Credential Locker / Linux Secret Service); it's opt-in because the keychain's per-process access policy can prompt or fail when the CLI writes a token and the server reads it. Either way a secret lives in exactly one place at a time.
- **Token files and the generated `auth.html`** are created `0600` atomically (no world-readable window). The config directory and the browser profile directory (`~/.applemusic-mcp/chrome`, which holds your signed-in session) are `0700`.
- **Your `.p8` signing key** (Apple Developer path only) stays where you put it; it is never copied into the keychain. `reset` does **not** delete it.
- The **audit log** (`~/.cache/applemusic-mcp/audit_log.jsonl`) records actions (e.g. "deleted playlist X") and library content — never tokens, cookies, or headers.

To wipe everything: `applemusic-mcp reset --force` (or `config(action="reset", confirm=True)`), which clears the tokens and the browser session but keeps your `.p8`.

## The web-player token path (unofficial)

Without an Apple Developer account, the server can use Apple's **public** `AMPWebPlay` developer token (the same token every browser receives when loading music.apple.com) plus a `media-user-token` you capture by signing in to a local Chrome window. This is an **unofficial** path — the same web-player API that established open-source Apple Music clients use — and it is the user's choice. A generated (paid) Apple Developer token is the sanctioned route and always takes precedence when present. No credentials are transmitted anywhere other than Apple.

## Hardening notes

- Browser features (sign-in, playback, queue) drive a **local** Chrome via Playwright. The server never opens a network-listening port except the short-lived `localhost`-only callback used by the optional `authorize` flow, which is gated by an exact Host/Origin check (anti-DNS-rebinding).
- User input routed into AppleScript is escaped; values passed to the browser are sent as `page.evaluate` arguments, never string-interpolated into a script body.

## Supported versions

Fixes land on the latest released version. Please update before reporting.
