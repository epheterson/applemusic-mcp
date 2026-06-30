# Chrome web-player session persistence — research + the right flow

**Problem:** the Chrome web player launches a fresh/signed-out session each time
instead of reusing the prior sign-in. Eric flagged 2026-06-30. Not a blocker
(Safari is the primary macOS path), but Chrome should work well. Tracked: task #11.

## Root cause (it's upstream, not us)

`launchPersistentContext(user_data_dir=...)` — what we use — has two known issues:

1. **Session cookies aren't reliably persisted.** Cookies with no expiry (session
   cookies) often don't survive between runs.
   <https://github.com/microsoft/playwright/issues/36139>
2. **Ungraceful exit corrupts the profile / leaves `SingletonLock`.** When the MCP
   server is killed without `ctx.close()`, the profile can be left locked or the
   cookie DB corrupted → next launch looks signed-out.
   <https://github.com/microsoft/playwright/issues/35466>

Compounding it on macOS: we drop Playwright's `--password-store=basic` /
`--use-mock-keychain` (so Touch ID / passkeys work at sign-in). That makes Chrome
encrypt cookies with the macOS keychain; if that key isn't consistently granted to
the automated Chrome, it can't *decrypt* the stored `media-user-token` → effectively
signed out. Tradeoff: passkeys-at-signin vs portable cookie persistence.

## What people do (2025–26)

- **Persistent profile + dedicated dir** — official default; hits the bugs above.
- **Inject `storageState` / the auth cookie on launch** — the standard workaround:
  save the cookie once, re-apply every launch. Deterministic, immune to profile
  corruption. <https://playwright.dev/mcp/configuration/user-profile>
- **`connectOverCDP` to a user-launched Chrome (`--remote-debugging-port`)** — reuse
  a live session. **DEAD for "my daily Chrome" since Chrome 136 (Apr 2025):** remote
  debugging is REFUSED on the default profile; you must pass a non-default
  `--user-data-dir` → a separate profile → its own sign-in. So CDP buys nothing over
  our persistent profile. <https://developer.chrome.com/blog/remote-debugging-port>
  · <https://github.com/ChromeDevTools/chrome-devtools-mcp/issues/1830>
- **Browser-extension bridge** — Microsoft's Playwright MCP extension and Anthropic's
  Claude-in-Chrome attach to your real, already-logged-in browser. The modern "use my
  actual browser" answer. <https://playwright.dev/mcp/configuration/browser-extension>

## The right flow for us

We already have the storageState pattern: `_ensure_session_cookie` injects the saved
`media-user-token`. Make it authoritative and stop trusting the profile:

1. **Token-injection = source of truth.** `_ensure_session_cookie` currently bails if
   *any* `media-user-token` cookie exists (including a stale/undecryptable one). Change
   it to REPLACE the cookie from our saved token whenever we have one → every launch is
   signed in regardless of profile state, and we no longer depend on Chrome decrypting
   its own store (sidesteps the keychain tradeoff).
2. **Graceful shutdown.** Register atexit + a SIGTERM handler → `engine.shutdown()` →
   `ctx.close()`, so we stop leaving locks / corrupting the profile.
3. **"Use my real browser" = Safari** on macOS (the extension-bridge equivalent, already
   shipped). Chrome stays the cross-platform fallback. No CDP / no extension needed.

Not needed: CDP attach, a custom extension — they solve "drive my daily Chrome," which
Safari already covers for us.

## Status — fixes 1 & 2 IMPLEMENTED 2026-06-30

- `_ensure_session_cookie` is now authoritative: if a token is saved, it sets/REPLACES
  the web-player cookie every readiness check (no longer bails on a stale cookie).
- Graceful shutdown wired in `server.main()`: `atexit` + a SIGTERM→`sys.exit` handler
  → `browser._engine.shutdown()` → `ctx.close()`.
- "Drive my actual daily Chrome" is intentionally NOT pursued: Chrome 136 killed CDP on
  the default profile, and an extension is a separate product. Safari is our real-browser
  path on macOS; Chrome is the reliable dedicated-profile fallback (esp. off-mac).
