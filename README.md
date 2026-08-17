# applemusic-mcp

[![Release](https://img.shields.io/github/v/release/epheterson/applemusic-mcp.svg?label=release)](https://github.com/epheterson/applemusic-mcp/releases)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![macOS](https://img.shields.io/badge/macOS-15%20%7C%2026-blue.svg)]()
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![Downloads](https://static.pepy.tech/badge/applemusic-mcp)](https://pepy.tech/project/applemusic-mcp)
[![MCP](https://img.shields.io/badge/MCP-server-purple.svg)](https://modelcontextprotocol.io/)

MCP server for Apple Music. It gives any [MCP client](https://modelcontextprotocol.io/clients) (Claude, Cursor, Cline, Windsurf) control of your playlists, library, catalog, discovery, playback, and the Up Next queue. Runs on macOS, Windows, and Linux.

**A Mac or an Apple Music subscription is all you need.** Four engines, chosen per call: Music.app and Safari on a Mac, the Apple Music API and Chrome on any OS.

## Features

Four engines back the server. **Native** drives the local Music.app on macOS via AppleScript. **API** uses Apple Music's web API (`amp-api.music.apple.com`) on any OS. **Safari** drives your signed-in Safari's MusicKit on macOS (DRM-native, zero install). **Chrome** runs a local Google Chrome window with MusicKit for DRM audio on any OS. One `mode` preference picks the engine — `auto` (default) mixes the best of each: native Music.app for playback on macOS, Safari for the Up Next queue, the API for data, Chrome off-mac. Pin one with `native` / `safari` / `chrome` / `api`, or override a single playback/queue call with `engine=`. In the table below, the **Browser** column covers both the Safari and Chrome web players. `✓` supported, `✗` not possible on that engine, `—` not applicable there.

|Capability|Native (Music.app) macOS|API (amp-api) any OS|Browser (Safari macOS / Chrome any OS)|
|---|:---:|:---:|:---:|
|**Catalog search / browse**|✓|✓ (+ tokenless resolve)|—|
|Recommendations / charts / suggestions|✗|✓|—|
|**Library search / browse**|✓|✓|—|
|Genre search|✓|✗|—|
|Recently played / added|✓|✓|—|
|**Add catalog → library**|✓|✓|✓ (in-page POST)|
|Remove from library|✓|✓|—|
|Love / dislike|✓|✓|—|
|**1–5 star ratings**|✓|✗|✗|
|Favorites list|✓|✗|✗|
|**Playlist** create / add / remove / rename|✓|✓|—|
|Playlist copy|✓|✗|—|
|Playlist delete|✓|✓ (web token)|—|
|Folders: single level + move in/out|✓|✓|—|
|Folders: nested paths / tree / `path`|✓|✗|✗|
|**Playback**: play song / album / playlist / URL|✓|—|✓|
|Controls: pause / stop / next / prev / seek|✓|—|✓|
|Settings: volume / shuffle / repeat|✓|—|✓|
|now_playing|✓|—|✓|
|**Up Next queue**: view / next / last / remove / jump / clear / autoplay|✗|—|✓|
|Reveal in app|✓|—|✓ (navigates page)|
|AirPlay device select|✓|✗|✗|
|Library snapshot / integrity|✓|✗|✗|
|**Works with no Apple account**|✓|✗|✗|
|**Cross-platform (Win/Linux)**|✗|✓|✓|

Everything in the **API** column runs anywhere, no browser and no Music app. Browser playback and the queue need a desktop session and a web player — **Safari on macOS** (no install) or **Google Chrome** elsewhere.

## Setup

**Requirements:** Python 3.10+, plus either a Mac or an Apple Music subscription. The Chrome web player (cross-platform playback + Up Next queue) needs [Google Chrome](https://www.google.com/chrome/) + Playwright. **On macOS you can skip both** — sign in via Safari and play through the Music app — so the default macOS install is lightweight (no ~500 MB Playwright). Windows/Linux include Playwright automatically (it's the only path there).

**Claude Code**, one line:

```bash
claude mcp add "Apple Music" -- uvx applemusic-mcp serve
```

**Claude Desktop / Cursor / Cline / Windsurf**, install once, then add the config block:

```bash
pipx install applemusic-mcp        # or: pip install applemusic-mcp
# Off macOS, also fetch the browser engine:  playwright install chromium
# macOS needs neither (Safari sign-in + Music.app). For the Chrome web player on a
# Mac:  pipx install 'applemusic-mcp[browser]'  then  playwright install chromium
```

```json
{
  "mcpServers": {
    "Apple Music": {
      "command": "applemusic-mcp",
      "args": ["serve"]
    }
  }
}
```

Restart your client and try *"List my Apple Music playlists"* or *"Play my favorites."* On macOS the local library and playback work immediately. To add catalog music or run on any OS, sign in.

### Sign in

Two paths capture the credentials for the cross-platform API.

**Apple Developer token (preferred).** The sanctioned route, an [Apple Developer Program](https://developer.apple.com/programs/) membership with a MusicKit key. One guided command writes the config, mints a 6-month token, and authorizes:

```bash
applemusic-mcp login --dev      # prompts for Team ID, Key ID, and .p8 path
```

See the [appendix](#appendix-developer-token) for getting the MusicKit key.

**Web sign-in.** The quick path, and what plain `applemusic-mcp login` does. Your password never touches this tool, sign-in persists, and tokens re-fetch before they expire. You can also sign in conversationally — just ask your assistant. (Web sign-in uses Apple's web-player API, the same path as open-source clients like [Cider](https://github.com/ciderapp/Cider-2) and [Music Assistant](https://www.music-assistant.io/music-providers/apple-music/).)

- **macOS — reads from a signed-in Safari (no Chrome, no ~500 MB Playwright):**

  ```bash
  applemusic-mcp login            # macOS default: harvests from Safari
  applemusic-mcp status           # verify
  ```

  One-time Safari setting (a security toggle — *you* enable it, the tool never flips it): **Safari → Settings → Advanced → "Show features for web developers"**, then the **Develop** menu → **"Allow JavaScript from Apple Events."** Sign into Apple Music at [music.apple.com](https://music.apple.com) in Safari first. That setting only lets the tool read **one cookie** — your Apple Music token — from your own signed-in Safari, and you can switch it back off afterward. If it's off or you're not signed in, `login` prints exactly how to fix it (or use `--dev`, or `--chrome`). Prefer Chrome on a Mac? `pip install 'applemusic-mcp[browser]'`, then `applemusic-mcp login --chrome`. Combined with native Music.app playback, a Mac needs no Chrome at all.

- **Windows / Linux — opens a local Chrome** (Playwright ships by default there; it's the only path):

  ```bash
  applemusic-mcp login            # opens Chrome to music.apple.com; sign in once
  ```

**Bulk work wants `--dev`.** Web sign-in uses Apple's *public* web-player token, and its request quota is shared rather than yours alone. Interactive use never gets near it, but a few hundred catalog searches in an hour — a playlist import, a library migration — will hit `HTTP 429`. Apple sends no `Retry-After` on this path and the window is **rolling and ~60 minutes long**, so a short cooldown doesn't clear it and retrying extends it. `applemusic-mcp login --dev` uses your own MusicKit key, which gets its own much larger quota. When you are throttled the tool says so explicitly, rather than letting the empty results read as "song not found."

<details>
<summary>Config locations, mode, and source install</summary>

**Config file:** Claude Desktop uses `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows). Cursor, Cline, and Windsurf use the same `mcpServers` shape, see your client's docs.

**`mode` preference** picks the engine: `auto` (default — best of each: native Music.app for playback on macOS, **Safari** for the Up Next queue, the API for data; **Chrome** off-mac), `native` (Music.app only, no account), `safari` (drive your signed-in Safari — macOS, no Chrome/Playwright), `chrome` (Chrome web player, any OS), or `api` (REST only — data + writes, no playback). Set it conversationally: `config(action="set-pref", preference="mode", string_value="safari")`. A per-call `engine=` (`native` / `safari` / `chrome` / `web`) overrides one playback or queue call (e.g. queue in Safari, then `playback(action="play", engine="safari")`). Using the queue makes its engine the active one, so transport controls reach it. The Safari engine drives the actual Safari you're browsing in (only ever a `music.apple.com` tab); Chrome uses an isolated window.

**Browser (Chrome web player) features** — cross-platform playback + the Up Next queue — need Google Chrome plus Playwright. Off macOS, Playwright is installed by default; **on macOS it's an opt-in extra** (`pip install 'applemusic-mcp[browser]'`) since Safari sign-in + native Music.app playback already cover the Mac. After installing, run the one-time browser download (`playwright install chromium`, or `uvx --from applemusic-mcp playwright install chromium`). The bundled Chromium can't decode Apple's DRM, so a real Chrome install is required, and these features open a local Chrome window (not for headless servers). You don't sign in twice: the web player authorizes off your existing sign-in (the Safari-harvested or developer token is bridged into its profile automatically), so on macOS a single `login` covers the API, native playback, *and* the Chrome web player/queue.

**From source:** `git clone … && pip install -e .`, then point the config `command` at `<repo>/venv/bin/applemusic-mcp` or use `python -m applemusic_mcp`.
</details>

## What's sanctioned vs web

This server reaches Apple Music three ways and prefers the most official one available:

- **Apple Music API (sanctioned).** Your own developer token (from `login --dev`) against `api.music.apple.com`, Apple's documented API.
- **Web player (community path).** A token from a signed-in `music.apple.com` session against the web player's backend — the same approach as [Cider](https://github.com/ciderapp/Cider-2) and [Music Assistant](https://www.music-assistant.io/music-providers/apple-music/). It fills the few gaps the public API doesn't expose.
- **Music.app (macOS).** Local AppleScript automation of your own app. No tokens, no network.

With a developer token, writes go through the sanctioned API; the web path is used only for the operations Apple's public API can't do. With web sign-in alone, everything runs on the web path. On macOS, library and playlist edits can also run locally through Music.app. Each write tells you which path it took.

| Write | Apple Music API | Web player | Music.app (macOS) |
|---|:---:|:---:|:---:|
| Add to library | ✓ | ✓ | ✓ |
| Create playlist | ✓ | ✓ | ✓ |
| Add tracks (API-made playlist) | ✓ | ✓ | ✓ |
| Add tracks (Music.app-made playlist) | ✗ | ✗ | ✓ |
| Rate 1 to 5 | ✗ | ✗ | ✓ |
| Love / dislike | ✓ | ✓ | ✓ |
| Delete playlist | ✗ | ✓ | ✓ |
| Rename / move into folder | partial | ✓ | ✓ |

The `✗` cells are operations the column's path can't do, so they route elsewhere. One Apple constraint to know: **only the client that created a playlist can edit it**, so a playlist made in Music.app can't be written by *either* the dev-token API or the web player — on macOS those adds go through Music.app locally; off macOS, add to an API/web-created playlist instead.

## Usage

Just talk to your assistant:

- *"Create a playlist called Road Trip and fill it with upbeat 90s alternative."*
- *"Add Hey Jude to my Road Trip playlist, and drop the last 3 tracks from my workout one."*
- *"Organize my playlists into Rock, Jazz, and Electronic folders."*
- *"Play my workout playlist on shuffle, and queue up Bohemian Rhapsody next."*
- *"Find songs similar to Bohemian Rhapsody and add them to my library."*
- *"What have I been listening to lately, and what's topping the charts?"*
- *"Export my library to CSV."*

## Tools

Seven action-based tools keep the MCP context small. Each takes an `action` and routes to the right engine.

| Tool | Actions |
|---|---|
| `playlist` | list, folders, tracks, search, create, add, copy, move, remove, delete, rename, path (playlists and folders) |
| `library` | search, add, browse, favorites, recently_played, recently_added, rate, remove, snapshot |
| `catalog` | search, resolve, album_tracks, album_details, song_details, artist_details, genres, suggestions |
| `discover` | recommendations, heavy_rotation, charts, top_songs, similar_artists, personal_station, song_station |
| `playback` | play (track / album / playlist / URL), control, now_playing, settings, reveal, airplay |
| `queue` | list, set, play_next, play_last, remove, jump, clear, autoplay (Up Next — Safari on macOS, Chrome elsewhere; `engine=` to pick) |
| `config` | status, signin, logout, reset, set-pref, audit-log, clear-audit-log, list-storefronts |

<details>
<summary>Common patterns</summary>

- **`track` is one parameter that batches.** Pass a single name or ID, a comma- or newline-separated list, or a JSON array (`["A","B"]` or `[{"name":"A","artist":"X"}]`). Whole albums via `album`.
- **Importing a playlist from elsewhere? Resolve first, then add.** `catalog(action="resolve", …)` turns a track list into catalog IDs and writes nothing. Pass `isrcs=` when you have them — Spotify, Rekordbox, and Plex exports all carry ISRCs — and it matches *exactly*, 25 per request, instead of one fuzzy search per track (which is what puts a large import into `429` territory). Pass `tracks=` for titles/artists and it reports how confident each match is, so a wrong edition gets caught before it's written — that path costs one request per track, so it stops at 25 unless you raise `max_tracks`. Either way the IDs go straight to `playlist(action="add", track=…)`.
- **Previewing an add? `playlist(action="add", …, dry_run=True)`.** Same resolution the real add would do, plus a diff against what's already in the playlist, writing nothing. Ask for `Dont Let Me Down` without an artist and you get The Chainsmokers, not The Beatles — this is where you see that before it lands. It previews matching and duplicates only; it doesn't claim to predict whether the write itself succeeds.
- **Adding to a playlist** auto-searches the catalog and skips duplicates. Set the `auto_add` preference to `true` for "fill this playlist" workflows (default `false`). `track` also accepts a **catalog song id** (e.g. `1440857781`) to pin an exact edition when a name would be version-ambiguous.
- **Adding a not-yet-owned catalog track to a *Music.app-made* playlist is two-step.** The dev-token API can't write those playlists, so the tool adds the track to your library over the API, then attaches it locally once iCloud syncs it down (usually seconds). If the sync is slow you'll get "added to your library — re-run to attach"; just re-run the same add. Rarely, if the sync stalls past ~20s, Music.app briefly flashes as a last-resort sync nudge (focus returns to your previous app) — expected, not a glitch.
- **Output format** on list tools: `format` (`text` / `json` / `csv` / `none`), `export` (writes a file readable as an MCP resource via `exports://`), `full` (all metadata).
- **URL playback** handles albums, playlists, and songs: `playback(action="play", url="https://music.apple.com/...")`.
- **Storefronts:** catalog actions take an optional `storefront` (for example `storefront="it"`) to query other regions without changing your default.
</details>

## CLI

```bash
applemusic-mcp serve            # run the MCP server (your client calls this)
applemusic-mcp login            # web sign-in (macOS: Safari; Windows/Linux: Chrome)
applemusic-mcp login --chrome   # force the Chrome web player (macOS opt-in)
applemusic-mcp login --dev      # Apple Developer token flow (.p8)
applemusic-mcp logout           # sign out (switch accounts)
applemusic-mcp status           # show auth status
applemusic-mcp reset --force    # wipe credentials (keeps your .p8 key file)
applemusic-mcp reset --all --force   # full uninstall: also removes the .p8, profile, and cache
```

## Good to know

- **macOS playback needs an unlocked screen and Accessibility permission.** Native catalog playback drives Music.app via System Events and moves the cursor to click Play. Grant it under System Settings → Privacy & Security → Accessibility, or set `mode="safari"` to play in the Safari web player instead (no Accessibility, no Chrome).
- **Safari playback needs one real click to start.** The first time you play in a freshly-opened or reloaded Safari tab, the browser requires a genuine click before it will start audio (a standard autoplay rule — we don't fake it). Click ▶ once in the Apple Music tab, then play/pause/next all work hands-free. If a play command reports the queue is ready but the track sits at 0:00, that's this — give the tab one click.
- **Brand-new playlists take a moment** to be addable over the API (cloud propagation). Existing ones are immediate.
- **A few macOS-only features** have no Apple Music API equivalent: 1 to 5 star ratings, favorites, library snapshots, AirPlay, and nested folder paths.
- **If catalog actions start failing**, re-run `applemusic-mcp login`. A handful of user playlists silently revert AppleScript edits ([known Music.app bug](https://www.macscripter.net/t/add-current-track-from-apple-music-to-playlist/72058)); the server detects and surfaces the rollback.

## Appendix: developer token

The preferred path. With an [Apple Developer Program](https://developer.apple.com/programs/) membership:

1. **Get a MusicKit key.** [Apple Developer Portal → Keys](https://developer.apple.com/account/resources/authkeys/list) → **+** → name it, check **MusicKit**, Register → download the `.p8` (one time). Note your **Key ID** and **Team ID**.
2. **Run the guided flow:**
   ```bash
   applemusic-mcp login --dev
   ```
   It prompts for the Team ID, Key ID, and `.p8` path (or pass `--team-id`, `--key-id`, `--key-path`), writes `~/.config/applemusic-mcp/config.json`, generates the developer token (180 days, auto-renews on use), and authorizes your user token.

---

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=epheterson/applemusic-mcp&type=Date)](https://star-history.com/#epheterson/applemusic-mcp&Date)

---

## License

MIT · *Unofficial community project — not affiliated with or endorsed by Apple. Uses your own Apple Music account for personal use; follow Apple's terms.*

<!-- Identifier for the official MCP Registry (PyPI ownership check). -->
mcp-name: io.github.epheterson/applemusic-mcp

## Credits

[FastMCP](https://github.com/jlowin/fastmcp) · [Apple MusicKit](https://developer.apple.com/documentation/applemusicapi) · [Model Context Protocol](https://modelcontextprotocol.io/)

---

Built with ❤️ in California by [@epheterson](https://github.com/epheterson) and [Claude Code](https://claude.com/claude-code).
