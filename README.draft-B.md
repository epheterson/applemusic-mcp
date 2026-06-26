# applemusic-mcp

[![Release](https://img.shields.io/github/v/release/epheterson/applemusic-mcp.svg?label=release)](https://github.com/epheterson/applemusic-mcp/releases)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![Downloads](https://static.pepy.tech/badge/applemusic-mcp)](https://pepy.tech/project/applemusic-mcp)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![macOS](https://img.shields.io/badge/macOS-15%20%7C%2026-blue.svg)]()
[![MCP](https://img.shields.io/badge/MCP-server-purple.svg)](https://modelcontextprotocol.io/)

**Run your entire Apple Music life through your AI assistant.** Build playlists, add music, control playback, organize your library, and discover what to play next, all by just asking. Works on macOS, Windows, and Linux, and you do not need a $99 Apple Developer account.

This is an [MCP](https://modelcontextprotocol.io/) server. Drop it into Claude, Cursor, Cline, Windsurf, or any [MCP client](https://modelcontextprotocol.io/clients), sign in once, and your assistant can actually touch your music instead of just talking about it.

## Why you want this

You already tell your assistant what you want to listen to. Now it can make it happen.

- *"Create a Road Trip playlist and fill it with upbeat 90s alternative."*
- *"Add Hey Jude to my Road Trip playlist, and drop the last 3 tracks from my workout one."*
- *"Organize my playlists into Rock, Jazz, and Electronic folders."*
- *"Play my workout playlist on shuffle."* / *"What's playing?"* / *"Queue up Bohemian Rhapsody next."*
- *"Find songs similar to Bohemian Rhapsody and add them to my library."*
- *"What have I been listening to lately, and what's topping the charts?"*
- *"Export my library to CSV."*

No copy-pasting URLs into the Music app. No four-step API dances. You ask, it happens.

## What it can do

- **Playlists** that build themselves: create, rename, delete, add and remove tracks, and tidy everything into folders (including nesting them and moving playlists between them).
- **Your library**: browse, search, add catalog songs, love and dislike, see what you played recently.
- **Playback**: play, play by URL, pause, skip, seek, volume, shuffle, repeat, and a live "now playing."
- **The Up Next queue**: view it, play next, play last, remove, jump, clear, and toggle autoplay.
- **Discovery**: catalog search (typos and half-remembered lyrics welcome), recommendations, and charts.
- **Export**: dump anything to CSV or JSON, readable straight back as MCP resources.

---

## Get started in about a minute

**You need:** Python 3.10+ and an Apple Music subscription. For playback and the Up Next queue, also install **Google Chrome** (on macOS the Music app handles playback instead, no Chrome required).

**Claude Code (one line):**

```bash
claude mcp add applemusic -- uvx applemusic-mcp serve
```

**Claude Desktop / Cursor / Cline / Windsurf:** install once, then add the config block.

```bash
pipx install applemusic-mcp        # or: pip install applemusic-mcp
playwright install chromium        # browser engine for sign-in, playback, and queue
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

Config file locations. **Claude Desktop:** `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS), `%APPDATA%\Claude\claude_desktop_config.json` (Windows). Cursor, Cline, and Windsurf use the same `mcpServers` shape, so check your client's docs for where the file lives.

**That's it.** Restart your client and try *"List my Apple Music playlists"* or *"Play my favorites playlist."* On macOS, browsing your local library and controlling playback work immediately with no account at all. To add catalog music or to run on any OS, [sign in once](#sign-in-once-to-unlock-catalog-features).

> **Heads up on browser features.** Playback, the Up Next queue, and sign-in use Google Chrome plus the one-time `playwright install chromium` (the bundled Chromium cannot decode Apple's DRM audio). They open a real local Chrome window, so they are not for headless servers. macOS-native-only setups can skip both. With `uvx`, run the browser install as `uvx --from applemusic-mcp playwright install chromium`.
>
> **From source (development):** `git clone ... && pip install -e .`, then point the config `command` at `<repo>/venv/bin/applemusic-mcp` or use `python -m applemusic_mcp`.

---

## Sign in once to unlock catalog features

Adding catalog music to your library and playlists runs over the Apple Music API. Pick one path:

- **Recommended: browser sign-in (no Apple Developer account).** One command, any OS:
  ```bash
  applemusic-mcp signin     # opens Chrome to music.apple.com; sign in once
  applemusic-mcp status     # verify
  ```
  This captures your `media-user-token` from a local signed-in Chrome profile (your password never touches this tool), and the developer token comes from Apple's public web player. It uses your installed Chrome when present. The sign-in persists, so you will not be doing this again.

- **Power users: an Apple Developer token.** If you already have an [Apple Developer Program](https://developer.apple.com/programs/) membership ($99/yr), you can generate a sanctioned 6-month token instead. See [Appendix: Developer token setup](#appendix-developer-token-setup).

> Note: browser sign-in uses Apple's web-player API the same way many open-source Apple Music clients do (the [Cider](https://github.com/ciderapp/Cider-2) desktop player and the [Music Assistant](https://www.music-assistant.io/music-providers/apple-music/) Home Assistant server among them). It is an unofficial path. Your own [generated token](#appendix-developer-token-setup) is the sanctioned route.

**Your sign-in heals itself.** The web-player token underneath is valid 35 days and re-fetches automatically; a generated developer token auto-renews from your `.p8` when it gets within 30 days of expiry. You rarely re-authenticate. To switch accounts, run `applemusic-mcp logout` then `applemusic-mcp signin`.

### Modes: choose how it runs

Three engines power everything: the native **Music.app** on macOS, the cross-platform **Apple Music API**, and a local **Chrome** web player for DRM audio and the queue. The `mode` preference decides which it reaches for. Just ask ("use API mode") or set it directly: `config(action="set-pref", preference="mode", string_value="api")`.

- **`auto`** *(default)*: native Music.app on macOS, the cross-platform API everywhere else.
- **`native`**: all-in on the local Music.app (macOS, and works with no account).
- **`api`**: all-in on the Apple Music API plus web player, on any OS, even on a Mac that is not signed into the Music app.

A separate `playback` preference (`auto` / `native` / `browser`) overrides just the playback engine, in case you want, say, API everything but native audio on macOS.

### Optional preferences

Add to config.json:
```json
{
  "preferences": {
    "mode": "auto",
    "playback": "auto",
    "auto_search": true,
    "clean_only": false,
    "fetch_explicit": false
  }
}
```

Or set any of these conversationally: `config(action="set-pref", preference="mode", string_value="api")` (booleans use `value=true/false`).

- `mode`: engine, `auto` (default) / `native` (local Music.app) / `api` (Apple Music API plus web player, any OS).
- `playback`: playback engine override, `auto` (default, follows `mode`) / `native` (macOS Music.app) / `browser` (Chrome web player).
- `secure_storage`: where tokens live, `file` (default, `0600` files, reliable everywhere) or `keychain` (OS keychain, opt-in, may prompt once for access).
- `auto_search`: lets `playlist(action="add")` pull in catalog songs you do not own yet (default false to avoid surprise writes; set true for "fill this playlist" requests).
- `clean_only` / `fetch_explicit`: filter or fetch explicit status on searches and browse (default false).

---

## Exactly what works where

Three engines, three platforms, and an honest map of which capability runs on each. Anything in the **API** column runs on any OS with no browser and no Music app. The **Native** column is the macOS Music.app (and the only path that needs no Apple account at all). The **Browser** column is a local Chrome window, needed for DRM audio playback and the Up Next queue.

|Capability|Native (Music.app) macOS|API (amp-api) any OS|Browser (Chrome) any OS|
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
|Folders — single level + move in/out|✓|✓|—|
|Folders — nested paths / tree / `path`|✓|✗|✗|
|**Playback** — play song / album / playlist / URL|✓|—|✓|
|Controls — pause / stop / next / prev / seek|✓|—|✓|
|Settings — volume / shuffle / repeat|✓|—|✓|
|now_playing|✓|—|✓|
|**Up Next queue** — view / next / last / remove / jump / clear / autoplay|✗|—|✓|
|Reveal in app|✓|—|✓ (navigates page)|
|AirPlay device select|✓|✗|✗|
|Library snapshot / integrity|✓|✗|✗|
|**Works with no Apple account**|✓|✗|✗|
|**Cross-platform (Win/Linux)**|✗|✓|✓|

<sub>The full per-capability breakdown of what runs on each engine and *why* lives in **[docs/CAPABILITIES.md](docs/CAPABILITIES.md)**.</sub>

The short version: library, catalog, playlists, folders, ratings (love/dislike), and add/remove all run **anywhere** over the API after `signin`. Playback and the queue need **Chrome** (or the Music app on macOS). A handful of niceties (1 to 5 star ratings, favorites, library snapshots, AirPlay, nested folder paths) are **macOS only** because Apple's API has no equivalent.

---

## The tools

Five action-based tools plus playback and queue. Each one takes an `action` and routes to the right engine automatically.

### `playlist(action=...)`
Playlist and folder operations. Most work on **any OS** over the API; a few folder niceties are macOS-only (see the matrix above).

| Action | Parameters | Description | Where |
|--------|-----------|-------------|-------|
| `list` | `format`, `export`, `full` | List all playlists | Any OS |
| `tracks` | `playlist`, `filter`, `limit`, `offset`, `format`, `export`, `full`, `fetch_explicit` | Get playlist tracks with filter/pagination | Any OS |
| `search` | `query`, `playlist` | Search tracks in playlist | Any OS |
| `create` | `name`, `description`, `folder` | Create a playlist and/or folder | Any OS · nested paths: macOS |
| `add` | `playlist`, `track`, `album`, `artist`, `allow_duplicates`, `verify`, `auto_search` | Smart add: auto-search catalog, skip duplicates | Any OS |
| `copy` | `source`, `new_name` | Copy a playlist to an editable version | macOS |
| `move` | `playlist`, `folder` | Move a playlist into a folder, or to the top level (`folder=""`) | Any OS · nested paths: macOS |
| `remove` | `playlist`, `track`, `artist` | Remove a track from a playlist | Any OS |
| `delete` | `playlist` or `folder` | Delete a playlist or folder | Any OS · nested paths: macOS |
| `rename` | `playlist` or `folder`, `new_name` | Rename a playlist (any OS) or folder (macOS) | Any OS · folder: macOS |
| `path` | `playlist` or `folder` | Get full path / show hierarchy | macOS |

**Folders:** `/` nests paths (`create(folder="Music/Genres/Jazz")`). Single-level folders and moving a playlist in or out of one work over the API on any OS; **nested paths and the folder tree/`path` view need macOS**. (Native-macOS quirk: AppleScript cannot move a playlist *out* of a folder, so `folder=""` recreates it at root with a new ID. The API path moves it in place.)

**Unified `track` parameter** auto-detects and batches: a single name/ID, a comma- or newline-separated list, or a JSON array (`["A","B"]` or `[{"name":"A","artist":"X"}]`). Whole albums via `album`.

### `library(action=...)`
Library management. Reads, add, remove, and love/dislike work on **any OS** over the API; favorites, snapshots, genre search, and 1 to 5 star ratings are macOS-only.

| Action | Parameters | Description | Where |
|--------|-----------|-------------|-------|
| `search` | `query`, `types`, `limit`, `format`, `export`, `full`, `fetch_explicit`, `clean_only` | Search your library (genre search: macOS) | Any OS · genre: macOS |
| `add` | `track`, `album`, `artist` | Add tracks/albums from the catalog | Any OS |
| `browse` | `item_type`, `limit`, `offset`, `format`, `export`, `full`, `fetch_explicit`, `clean_only` | List songs/albums/artists/videos | Any OS |
| `favorites` | `limit`, `offset`, `format`, `export`, `full`, `fetch_explicit`, `clean_only` | List songs marked Favorite (loved) | macOS |
| `recently_played` | `limit`, `format`, `export`, `full` | Recent listening history | Any OS |
| `recently_added` | `limit`, `format`, `export`, `full` | Recently added content | Any OS |
| `rate` | `rate_action`, `track`, `artist`, `stars` | Love / dislike / clear (any OS); 1 to 5 stars get/set (macOS) | Any OS · stars: macOS |
| `remove` | `track`, `artist` | Remove one track from your library (exact match preferred) | Any OS |
| `snapshot` | `query` | Library integrity checking: tracks, playlists, folder hierarchy | macOS |

**Snapshot sub-commands** via `query`:

| Query | Description |
|-------|-------------|
| _(empty)_ | Diff current state from baseline, or take initial baseline |
| `new` | Reset baseline to current state |
| `history` | View recorded changes over time |
| `list` | List all saved snapshot/diff files |
| `delete FILENAME` | Delete a specific diff file |

### `catalog(action=...)`
Catalog search and details: search, albums, songs, artists, genres, stations.

`search` accepts fuzzy queries: typos, partial lyrics, vague descriptions ("whistling beatles song"). On macOS it falls back to Music.app's built-in UI search when no API token is available, so you can find a half-remembered song without credentials.

| Action | Parameters | Description | Platform |
|--------|-----------|-------------|----------|
| `search` | `query`, `types`, `limit`, `format`, `export`, `full`, `clean_only` | Search Apple Music catalog (fuzzy; UI fallback on macOS when no API token) | All |
| `album_tracks` | `album`, `artist`, `limit`, `offset`, `format`, `export`, `full` | Get album tracks (by name or ID) | All |
| `album_details` | `album`, `artist`, `format`, `export`, `full` | Full album metadata + track listing | All |
| `song_details` | `song_id` | Full song metadata | All |
| `artist_details` | `artist` | Artist info and discography | All |
| `song_station` | `song_id` | Get radio station for song | All |
| `genres` | - | List all available genres | All |

### `discover(action=...)`
Discovery and recommendations: personalized stations, charts, top songs, similar artists.

| Action | Parameters | Description | Platform |
|--------|-----------|-------------|----------|
| `recommendations` | `format`, `export`, `full` | Personalized recommendations | All |
| `heavy_rotation` | `format`, `export`, `full` | Your frequently played | All |
| `charts` | `chart_type`, `format`, `export`, `full` | Apple Music charts | All |
| `top_songs` | `artist` | Artist's popular songs | All |
| `similar_artists` | `artist` | Find similar artists | All |
| `search_suggestions` | `term` | Autocomplete suggestions | All |
| `personal_station` | - | Your personal radio station | All |

Catalog-based actions (`charts`, `top_songs`, `similar_artists`, `song_station`) take an optional `storefront` to query other regions (e.g. `storefront="it"`) without changing your default.

### Playback and Queue

**Playback** is cross-platform: on macOS through the Music app, on any OS through the signed-in Chrome web player (the `playback` preference picks `auto` / `native` / `browser`).

| Action | Description | Where |
|--------|-------------|-------|
| `playback(action="play", ...)` | Play a track, playlist, album, or URL | macOS app · or Chrome |
| `playback(action="control", control="play\|pause\|stop\|next\|previous\|seek")` | Transport controls | macOS app · or Chrome |
| `playback(action="now_playing")` | Current track and player state | macOS app · or Chrome |
| `playback(action="settings", volume=, shuffle_mode=, repeat=)` | Volume / shuffle / repeat | macOS app · or Chrome |
| `playback(action="reveal", track=)` | Show a track: opens it in the Music app (macOS) or in the Chrome web player (any OS) | macOS app · or Chrome |
| `playback(action="airplay", device_name=)` | List or switch AirPlay devices | macOS only |

`play` accepts ONE of `track`, `playlist`, `album`, or `url`; `shuffle=True` shuffles. **URL playback** handles albums, playlists (including personal `pl.u-`), and songs via `?i=`:

```
playback(action="play", url="https://music.apple.com/us/album/ok-computer/1097861387")
playback(action="play", url="https://music.apple.com/us/playlist/todays-hits/pl.f4d106fed2bd41149aaacabb233eb5eb")
```

Browser playback opens a local Chrome window (needs a desktop session). Native macOS catalog playback uses UI scripting: it brings Music.app to the front and **moves the cursor** to click Play, so it needs **Accessibility permission** (System Settings, Privacy & Security, Accessibility) for your terminal or MCP host. In `auto` mode, if the native click cannot start playback, it falls back to the Chrome web player; pin `playback="native"` to keep it Music-app-only.

**Up Next queue** via `queue(action=...)` drives the web player's play queue through Chrome, on any OS.

| Action | Parameters | Description |
|--------|-----------|-------------|
| `list` | (none) | Show Up Next (▶ marks the current item; indices are 0-based) |
| `play_next` | `track`, `artist` | Insert a track right after the current one |
| `play_last` | `track`, `artist` | Append a track to the end of Up Next |
| `remove` | `index` | Remove the item at `index` |
| `jump` | `index` | Jump playback to the item at `index` |
| `clear` | (none) | Empty the queue |
| `autoplay` | `enabled` | Turn ∞ Autoplay on/off (keep playing similar music when the queue ends) |

### `config(action=...)`

Settings, cache, and authentication, all in one tool. (AirPlay and reveal-in-app are `playback` actions; see above.)

**Settings and cache:** `info`, `set-pref`, `list-storefronts`, `audit-log`, `clear-tracks`, `clear-exports`, `clear-audit-log`. All modifying operations are logged; view with `config(action="audit-log")`.

**Auth (conversational sign-in, no terminal needed).** Claude Code's native `/mcp` auth UI is for remote servers; this local server exposes auth through this tool instead:

| `config(action=...)` | Description |
|---|---|
| `status` | Which tokens are active, expiry / auto-renew, what works, and the next step |
| `signin` | Open a browser to sign in (any OS) and capture your session |
| `logout` | Sign out: clears your user token + browser session so you can switch accounts (needs `confirm=True`) |
| `reset` | Wipe all credentials for a clean slate, or to drop a developer token for the web path (needs `confirm=True`) |

Tokens **self-heal**: the developer token auto-renews from your `.p8` when 30 days or fewer from expiry, and the web token re-fetches itself when 15 days or fewer out, so you rarely re-authenticate. To **switch accounts**, run `logout` then `signin`. The same actions exist on the CLI: `applemusic-mcp signin | logout | reset | status`.

### Output format

Most list tools support these output options:

| Parameter | Values | Description |
|-----------|--------|-------------|
| `format` | `"text"` (default), `"json"`, `"csv"`, `"none"` | Response format |
| `export` | `"none"` (default), `"csv"`, `"json"` | Write file to disk |
| `full` | `False` (default), `True` | Include all metadata |

Text format auto-selects the tier that fits (Full, then Compact, then Minimal). Use `export="csv"` with `format="none"` to write a file without spending tokens on the response. Exported files are also readable as MCP resources: `exports://list` and `exports://{filename}`.

---

## Good to know

**Requirements:** an Apple Music subscription and Python 3.10+. For playback and the Up Next queue, also Google Chrome plus the one-time `playwright install chromium` (the bundled Chromium has no DRM and stays silent). Those browser features open a real Chrome window, so they need a desktop session and do not run headless; audio plays on the machine running the server.

**A few honest limits:**

- **Windows and Linux:** library, catalog, add/rate, and the full playlist plus folder surface all work over the API after `signin`. The only catches are that playback and the queue need Chrome, and a few features (1 to 5 star ratings, favorites, library snapshots, AirPlay, nested folder paths) are macOS-only because Apple's API has no equivalent.
- **Brand-new playlists** take a moment to become addable over the API (cloud propagation); existing ones are immediate.
- **Sign-in persists but can expire.** If catalog actions start failing, re-run `applemusic-mcp signin`.
- **macOS playback and play-from-URL need an unlocked screen and Accessibility permission** (it drives Music.app via System Events and moves the cursor to click). Grant it under System Settings, Privacy & Security, Accessibility. The MCP returns a clear error if the screen is locked or the click cannot start playback.
- **A few playlists silently revert AppleScript edits** ([known Music.app bug](https://www.macscripter.net/t/add-current-track-from-apple-music-to-playlist/72058)); the MCP detects the rollback and surfaces it.

### Troubleshooting

| Problem | Solution |
|---------|----------|
| 401 Unauthorized | `applemusic-mcp signin` (web path) or `applemusic-mcp authorize` (dev-token path) |
| macOS "couldn't auto-play it" | Grant Accessibility (System Settings, Privacy & Security, Accessibility), or set `playback="browser"` to play in Chrome |
| "Cannot edit playlist" | Use `copy_playlist` for an editable copy |
| Token expiring | `applemusic-mcp generate-token` |
| Check everything | `applemusic-mcp status` |

### CLI reference

```bash
applemusic-mcp signin          # Browser sign-in (no Apple Developer account)
applemusic-mcp status          # Check tokens and connection
applemusic-mcp logout          # Sign out (switch accounts: logout then signin)
applemusic-mcp reset --force   # Wipe all credentials (keeps your .p8 key file)
applemusic-mcp init            # Scaffold config.json for the developer-token path
applemusic-mcp generate-token  # New developer token (180 days; auto-renews on use)
applemusic-mcp authorize       # Browser auth for a user token (developer-token path)
applemusic-mcp serve           # Run MCP server (auto-launched by your MCP client)
```

**Config:** `~/.config/applemusic-mcp/` (config.json, .p8 key, tokens)

---

## Appendix: Developer token setup

The preferred path if you have an [Apple Developer Program](https://developer.apple.com/programs/) membership: a sanctioned, 6-month token. (Browser sign-in above needs none of this.)

**1. Get a MusicKit key.** [Apple Developer Portal, Keys](https://developer.apple.com/account/resources/authkeys/list), then **+**, name it, check **MusicKit**, Register, then **download the .p8** (one-time). Note your **Key ID** and **Team ID** (from [Membership](https://developer.apple.com/account/#!/membership)).

**2. Configure:**
```bash
mkdir -p ~/.config/applemusic-mcp
cp ~/Downloads/AuthKey_XXXXXXXXXX.p8 ~/.config/applemusic-mcp/
```
Create `~/.config/applemusic-mcp/config.json`:
```json
{
  "team_id": "YOUR_TEAM_ID",
  "key_id": "YOUR_KEY_ID",
  "private_key_path": "~/.config/applemusic-mcp/AuthKey_XXXXXXXXXX.p8"
}
```

**3. Generate and authorize:**
```bash
applemusic-mcp generate-token   # developer token (180 days)
applemusic-mcp authorize        # capture your user token
applemusic-mcp status           # verify
```

---

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=epheterson/applemusic-mcp&type=Date)](https://star-history.com/#epheterson/applemusic-mcp&Date)

---

## License

MIT · *Unofficial community project, not affiliated with Apple.*

<!-- Identifier for the official MCP Registry (PyPI ownership check). -->
mcp-name: io.github.epheterson/applemusic-mcp

## Credits

[FastMCP](https://github.com/jlowin/fastmcp) · [Apple MusicKit](https://developer.apple.com/documentation/applemusicapi) · [Model Context Protocol](https://modelcontextprotocol.io/)

---

Built with ❤️ in California by [@epheterson](https://github.com/epheterson) and [Claude Code](https://claude.com/claude-code).
