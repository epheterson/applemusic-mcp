# Configuration and engines

What the server may reach at all is set by the [permission level](../README.md#choose-how-much-it-can-reach). Within it, these settings pick the engine.

## Engines

Four engines back the server. **Native** drives the local Music.app on macOS via AppleScript. **API** uses Apple Music's web API (`amp-api.music.apple.com`) on any OS. **Safari** drives your signed-in Safari's MusicKit on macOS (DRM-native, zero install). **Chrome** runs a local Google Chrome window with MusicKit for DRM audio on any OS. One `mode` preference picks the engine — `auto` (default) mixes the best of each: native Music.app for playback on macOS, Safari for the Up Next queue, the API for data, Chrome off-mac. Pin one with `native` / `safari` / `chrome` / `api`, or override a single playback/queue call with `engine=`. The [feature table](../README.md#features) shows what each engine can do; its web-player column covers both Safari and Chrome.

## Settings

**Config file:** Claude Desktop uses `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows). Cursor, Cline, and Windsurf use the same `mcpServers` shape, see your client's docs.

**`mode` preference** picks the engine: `auto` (default — best of each: native Music.app for playback on macOS, **Safari** for the Up Next queue, the API for data; **Chrome** off-mac), `native` (Music.app only, no account), `safari` (drive your signed-in Safari — macOS, no Chrome/Playwright), `chrome` (Chrome web player, any OS), or `api` (REST only — data + writes, no playback). Set it conversationally: `config(action="set-pref", preference="mode", string_value="safari")`. `mode` stays inside the [permission level](#choose-how-much-it-can-reach): a mode the level doesn't allow is refused, and one saved earlier is ignored. A per-call `engine=` (`native` / `safari` / `chrome` / `web`) overrides one playback or queue call (e.g. queue in Safari, then `playback(action="play", engine="safari")`). Using the queue makes its engine the active one, so transport controls reach it. The Safari engine drives the actual Safari you're browsing in (only ever a `music.apple.com` tab); Chrome uses an isolated window.

**Browser (Chrome web player) features** — cross-platform playback + the Up Next queue — need Google Chrome plus Playwright. Off macOS, Playwright is installed by default; **on macOS it's an opt-in extra** (`pip install 'applemusic-mcp[browser]'`) since Safari sign-in + native Music.app playback already cover the Mac. After installing, run the one-time browser download (`playwright install chromium`, or `uvx --from applemusic-mcp playwright install chromium`). The bundled Chromium can't decode Apple's DRM, so a real Chrome install is required, and these features open a local Chrome window (not for headless servers). You don't sign in twice: the web player authorizes off your existing sign-in (the Safari-harvested or developer token is bridged into its profile automatically), so on macOS a single `login` covers the API, native playback, *and* the Chrome web player/queue.

**From source:** `git clone … && pip install -e .`, then point the config `command` at `<repo>/venv/bin/applemusic-mcp` or use `python -m applemusic_mcp`.
