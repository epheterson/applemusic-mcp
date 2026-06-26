# applemusic-mcp

[![Release](https://img.shields.io/github/v/release/epheterson/applemusic-mcp.svg?label=release)](https://github.com/epheterson/applemusic-mcp/releases)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![Downloads](https://static.pepy.tech/badge/applemusic-mcp)](https://pepy.tech/project/applemusic-mcp)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![macOS](https://img.shields.io/badge/macOS-15%20%7C%2026-blue.svg)]()
[![MCP](https://img.shields.io/badge/MCP-server-purple.svg)](https://modelcontextprotocol.io/)

[MCP](https://modelcontextprotocol.io/) server for Apple Music — lets your AI assistant (Claude, Cursor, Cline, Windsurf, or any [MCP client](https://modelcontextprotocol.io/clients)) manage playlists, add music, control playback, and browse your library.

**Works on macOS, Windows, and Linux.** Sign in once with `applemusic-mcp signin` and it all runs over the Apple Music API — the same everywhere.

## What it does

- **Library** — browse, search, and add catalog songs to your library
- **Playlists** — create, rename, delete · add and remove tracks · organize into folders (create, nest, and move playlists between them)
- **Playback** — play, play-by-URL, pause, skip, seek, volume, shuffle, repeat, now-playing
- **Up Next queue** — view, play-next, play-last, remove, reorder-jump, clear, autoplay toggle (browser web player)
- **Discover** — catalog search, recommendations, charts
- **Rate** — love / dislike tracks
- **Export** — CSV / JSON, readable as MCP resources

### Access

| Access | What you get | Platforms |
|---|---|---|
| **Developer token** — recommended | The full Apple Music API — library, playlists, catalog, ratings | macOS · Windows · Linux |
| Web token | Same | macOS · Windows · Linux |
| No token | Control the local Music app — play, browse, edit local playlists | macOS |

<sub>**Developer token** — included with Apple Developer membership (free for App Store developers), valid 6 months — [setup](#appendix-developer-token-setup). **Web token** — a free fallback captured by `applemusic-mcp signin`. The underlying web-player token is valid 35 days and is re-fetched automatically, so you don't re-authenticate — your sign-in persists. It uses Apple's web-player API the same way established open-source projects do — the [Cider](https://github.com/ciderapp/Cider-2) desktop player and the [Music Assistant](https://www.music-assistant.io/music-providers/apple-music/) Home Assistant server among them.</sub>

### Modes & what runs where

One `mode` preference picks the engine for both data and playback. Just ask ("use web mode") or set it: `config(action="set-pref", preference="mode", string_value="web")`.

- **`auto`** *(default)*: native Music.app on macOS, the cross-platform web API + Chrome web player everywhere else
- **`native`**: all-in on the local Music.app (macOS, works with no account)
- **`web`**: all-in on the Apple Music web API + web player, any OS, even on a Mac that isn't signed into the Music app

Playback always follows the engine, so there is no separate playback knob. (`api` is still accepted as an alias for `web`.)

| What you can do | Where it runs |
|---|---|
| Search & browse library and catalog · recommendations · charts | **API** — any OS, no app |
| Add to library · love / dislike · remove | **API** — any OS, no app |
| Create & edit playlists · folders · move playlists between folders | **API** — any OS, no app |
| **Playback** — play, pause, skip, seek, volume, shuffle, repeat | **Music app** on macOS, **or Chrome** on any OS |
| **Up Next queue** — view, play-next/last, remove, jump, clear, autoplay | **Chrome** — any OS |
| 1–5 star ratings · favorites · snapshots · AirPlay · nested folder paths | **macOS only** (local Music app) |

**Browser playback and the queue need Google Chrome** (for DRM audio) and a real desktop session — they open a local Chrome window and won't run on a headless server. Everything in the **API** rows runs anywhere with no browser and no Music app.

<sub>Full per-capability breakdown of what runs on each engine and why: **[docs/CAPABILITIES.md](docs/CAPABILITIES.md)**.</sub>

---

## Quick Start

**Requirements:** Python 3.10+ and an Apple Music subscription. For playback and the Up Next queue, also install **Google Chrome** (macOS can use the Music app instead).

**Claude Code — one line:**

```bash
claude mcp add applemusic -- uvx applemusic-mcp serve
```

**Claude Desktop / Cursor / Cline / Windsurf** — install once, then add the config block:

```bash
pipx install applemusic-mcp        # or: pip install applemusic-mcp
playwright install chromium        # browser engine for sign-in, playback & queue
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

Config file locations — **Claude Desktop:** `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS), `%APPDATA%\Claude\claude_desktop_config.json` (Windows). Cursor / Cline / Windsurf use the same `mcpServers` shape — see your client's docs.

**That's it!** Restart your client and try: *"List my Apple Music playlists"* or *"Play my favorites playlist."* No Apple Developer account needed — on macOS the local library and playback work immediately; to add catalog music or play on any OS, [sign in once](#enable-catalog-features-sign-in-once).

> **Browser features (playback, queue, sign-in)** need Google Chrome + the one-time `playwright install chromium` (the bundled Chromium can't decode Apple's DRM). They open a local Chrome window — not for headless servers. macOS-native-only setups can skip both. With `uvx`, run the browser install as `uvx --from applemusic-mcp playwright install chromium`.
>
> **From source (development):** `git clone … && pip install -e .`, then point the config `command` at `<repo>/venv/bin/applemusic-mcp` or use `python -m applemusic_mcp`.

---

## Enable Catalog Features (sign in once)

Adding catalog music to your library and playlists runs over the Apple Music API. Pick one:

- **Recommended — browser sign-in (no Apple Developer account).** One command, any OS:
  ```bash
  applemusic-mcp signin     # opens Chrome to music.apple.com; sign in once
  applemusic-mcp status     # verify
  ```
  Captures your `media-user-token` from a local signed-in Chrome profile (your password
  never touches this tool); the developer token comes from Apple's public web player. Uses
  your installed Chrome when present. One-time sign-in persists, so you won't repeat it.
- **Power users — Apple Developer token.** If you have an [Apple Developer Program](https://developer.apple.com/programs/)
  membership ($99/yr), generate a sanctioned 6-month token instead — see [Appendix: Developer token setup](#appendix-developer-token-setup).

> Note: browser sign-in uses Apple's web-player API the same way many open-source Apple Music
> clients do — an unofficial path; your own [generated token](#appendix-developer-token-setup)
> is the sanctioned route.

### Optional Preferences

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

Or set any of these conversationally with `config(action="set-pref", preference="mode", string_value="web")` (booleans use `value=true/false`).

- `mode`: Engine for data and playback, `auto` (default) / `native` (local Music.app) / `web` (Apple Music web API + Chrome web player, any OS). Playback follows the engine; `api` is accepted as an alias for `web`.
- `secure_storage`: Where tokens live — `file` (default, `0600` files; reliable everywhere) or `keychain` (OS keychain; opt-in, may prompt once for access).
- `auto_search`: let `playlist(action="add")` pull catalog songs you don't own yet into your library (default false, to avoid unintended writes — set true for "fill this playlist").
- `clean_only` / `fetch_explicit`: filter or fetch explicit status on searches/browse (default false).

---

## Usage Examples

Just talk to your assistant:

- *"Create a playlist called Road Trip and fill it with upbeat 90s alternative."*
- *"Add Hey Jude to my Road Trip playlist; remove the last 3 tracks from my workout one."*
- *"Organize my playlists into Rock, Jazz, and Electronic folders."*
- *"Play my workout playlist on shuffle"* / *"what's playing?"* / *"queue up Bohemian Rhapsody next."*
- *"Find songs similar to Bohemian Rhapsody and add them to my library."*
- *"What have I been listening to lately, and what are the top charts?"*
- *"Export my library to CSV."*

---

## Tools

### `playlist(action=...)`
Playlist and folder operations. Most work on **any OS** over the API; a few folder niceties are macOS-only (see the "where it runs" table above).

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

**Folders:** `/` nests paths (`create(folder="Music/Genres/Jazz")`). Single-level folders and moving a playlist in/out of one work over the API on any OS; **nested paths and the folder tree/`path` view need macOS**. (Native-macOS quirk: AppleScript can't move a playlist *out* of a folder, so `folder=""` recreates it at root with a new ID — the API path moves it in place.)

**Unified `track` parameter** auto-detects and batches: a single name/ID, a comma- or newline-separated list, or a JSON array (`["A","B"]` or `[{"name":"A","artist":"X"}]`). Whole albums via `album`.

### `library(action=...)`
Library management. Reads, add, remove, and love/dislike work on **any OS** over the API; favorites, snapshots, genre search, and 1–5 star ratings are macOS-only.

| Action | Parameters | Description | Where |
|--------|-----------|-------------|-------|
| `search` | `query`, `types`, `limit`, `format`, `export`, `full`, `fetch_explicit`, `clean_only` | Search your library (genre search: macOS) | Any OS · genre: macOS |
| `add` | `track`, `album`, `artist` | Add tracks/albums from the catalog | Any OS |
| `browse` | `item_type`, `limit`, `offset`, `format`, `export`, `full`, `fetch_explicit`, `clean_only` | List songs/albums/artists/videos | Any OS |
| `favorites` | `limit`, `offset`, `format`, `export`, `full`, `fetch_explicit`, `clean_only` | List songs marked Favorite (loved) | macOS |
| `recently_played` | `limit`, `format`, `export`, `full` | Recent listening history | Any OS |
| `recently_added` | `limit`, `format`, `export`, `full` | Recently added content | Any OS |
| `rate` | `rate_action`, `track`, `artist`, `stars` | Love / dislike / clear (any OS); 1–5 stars get/set (macOS) | Any OS · stars: macOS |
| `remove` | `track`, `artist` | Remove one track from your library (exact match preferred) | Any OS |
| `snapshot` | `query` | Library integrity checking — tracks, playlists, folder hierarchy | macOS |

**Snapshot sub-commands** via `query`:

| Query | Description |
|-------|-------------|
| _(empty)_ | Diff current state from baseline, or take initial baseline |
| `new` | Reset baseline to current state |
| `history` | View recorded changes over time |
| `list` | List all saved snapshot/diff files |
| `delete FILENAME` | Delete a specific diff file |

### `catalog(action=...)`
Catalog search and details - search, albums, songs, artists, genres, stations

`search` accepts fuzzy queries — typos, partial lyrics, vague descriptions ("whistling beatles song"). On macOS it falls back to Music.app's built-in UI search when no API token is available, so you can find a half-remembered song without credentials.

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
Discovery and recommendations - personalized stations, charts, top songs, similar artists

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

### Playback & Queue

**Playback** is cross-platform: on macOS through the Music app, on any OS through the signed-in Chrome web player. It follows the `mode` engine (`auto`/`native`/`web`), so playback goes wherever your data requests go.

| Action | Description | Where |
|--------|-------------|-------|
| `playback(action="play", ...)` | Play a track, playlist, album, or URL | macOS app · or Chrome |
| `playback(action="control", control="play\|pause\|stop\|next\|previous\|seek")` | Transport controls | macOS app · or Chrome |
| `playback(action="now_playing")` | Current track and player state | macOS app · or Chrome |
| `playback(action="settings", volume=, shuffle_mode=, repeat=)` | Volume / shuffle / repeat | macOS app · or Chrome |
| `playback(action="reveal", track=)` | Show a track — opens it in the Music app (macOS) or in the Chrome web player (any OS) | macOS app · or Chrome |
| `playback(action="airplay", device_name=)` | List or switch AirPlay devices | macOS only |

`play` accepts ONE of `track`, `playlist`, `album`, or `url`; `shuffle=True` shuffles. **URL playback** handles albums, playlists (incl. personal `pl.u-`), and songs via `?i=`:

```
playback(action="play", url="https://music.apple.com/us/album/ok-computer/1097861387")
playback(action="play", url="https://music.apple.com/us/playlist/todays-hits/pl.f4d106fed2bd41149aaacabb233eb5eb")
```

Browser playback opens a local Chrome window (needs a desktop session). Native macOS catalog playback uses UI scripting — it brings Music.app to the front and **moves the cursor** to click Play, so it needs **Accessibility permission** (System Settings → Privacy & Security → Accessibility) for your terminal / MCP host. In `auto` mode, if the native click can't start playback, it falls back to the Chrome web player; pin `playback="native"` to keep it Music-app-only.

**Up Next queue** — `queue(action=...)` drives the web player's play queue through Chrome, on any OS.

| Action | Parameters | Description |
|--------|-----------|-------------|
| `list` | — | Show Up Next (▶ marks the current item; indices are 0-based) |
| `play_next` | `track`, `artist` | Insert a track right after the current one |
| `play_last` | `track`, `artist` | Append a track to the end of Up Next |
| `remove` | `index` | Remove the item at `index` |
| `jump` | `index` | Jump playback to the item at `index` |
| `clear` | — | Empty the queue |
| `autoplay` | `enabled` | Turn ∞ Autoplay on/off (keep playing similar music when the queue ends) |

### `config(action=...)`

Settings, cache, and authentication — all in one tool. (AirPlay and reveal-in-app are `playback` actions; see above.)

**Settings & cache:** `info`, `set-pref`, `list-storefronts`, `audit-log`, `clear-tracks`, `clear-exports`, `clear-audit-log`. All modifying operations are logged — view with `config(action="audit-log")`.

**Auth — conversational sign-in, no terminal needed** (Claude Code's native `/mcp` auth UI is for remote servers; this local server exposes auth through this tool):

| `config(action=...)` | Description |
|---|---|
| `status` | Which tokens are active, expiry / auto-renew, what works, and the next step |
| `signin` | Open a browser to sign in (any OS) and capture your session |
| `logout` | Sign out — clears your user token + browser session so you can switch accounts (needs `confirm=True`) |
| `reset` | Wipe all credentials for a clean slate, or to drop a developer token for the web path (needs `confirm=True`) |

Tokens **self-heal**: the developer token auto-renews from your `.p8` when ≤30 days out, and the web token re-fetches itself ≤15 days out — so you rarely re-authenticate. To **switch accounts**, run `logout` then `signin`. The same actions exist on the CLI: `applemusic-mcp signin | logout | reset | status`.

### Output Format

Most list tools support these output options:

| Parameter | Values | Description |
|-----------|--------|-------------|
| `format` | `"text"` (default), `"json"`, `"csv"`, `"none"` | Response format |
| `export` | `"none"` (default), `"csv"`, `"json"` | Write file to disk |
| `full` | `False` (default), `True` | Include all metadata |

Text format auto-selects the tier that fits (Full → Compact → Minimal). Use `export="csv"` with `format="none"` to write a file without spending tokens on the response. Exported files are also readable as MCP resources: `exports://list` and `exports://{filename}`.

---

## Limitations

### Windows/Linux
Library, catalog, add/rate, and the full playlist + folder surface all work over the API after `signin`. Two caveats:

| Limitation | Detail |
|------------|--------|
| Playback & queue need Google Chrome | They open a local Chrome window for DRM audio — install Chrome + run `playwright install chromium`; not for headless servers |
| A few features are macOS-only | 1–5 star ratings, favorites, library snapshots, AirPlay, and nested folder paths have no Apple Music API equivalent |

### Both Platforms
- **Brand-new playlists take a moment to be addable** over the API (cloud propagation); existing ones are immediate.
- **Sign-in persists but can expire** — if catalog actions start failing, re-run `applemusic-mcp signin`.
- **macOS playback/play-from-URL needs an unlocked screen and Accessibility permission** (it drives Music.app via System Events and moves the cursor to click); grant it under System Settings → Privacy & Security → Accessibility. The MCP returns a clear error if locked or if the click can't start playback.
- **A few playlists silently revert AppleScript edits** ([known Music.app bug](https://www.macscripter.net/t/add-current-track-from-apple-music-to-playlist/72058)); the MCP detects the rollback and surfaces it.

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| 401 Unauthorized | `applemusic-mcp signin` (web path) or `applemusic-mcp authorize` (dev-token path) |
| macOS "couldn't auto-play it" | Grant Accessibility (System Settings → Privacy & Security → Accessibility), or set `playback="browser"` to play in Chrome |
| "Cannot edit playlist" | Use `copy_playlist` for editable copy |
| Token expiring | `applemusic-mcp generate-token` |
| Check everything | `applemusic-mcp status` |

---

## CLI Reference

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

The preferred path if you have an [Apple Developer Program](https://developer.apple.com/programs/) membership — a sanctioned, 6-month token. (Browser sign-in above needs none of this.)

**1. Get a MusicKit key** — [Apple Developer Portal → Keys](https://developer.apple.com/account/resources/authkeys/list) → **+** → name it, check **MusicKit**, Register → **download the .p8** (one-time). Note your **Key ID** and **Team ID** (from [Membership](https://developer.apple.com/account/#!/membership)).

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

**3. Generate + authorize:**
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
