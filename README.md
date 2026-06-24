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

Pick how it runs with the `mode` preference:

- **`auto`** *(default)* — native Music.app on macOS, the cross-platform API everywhere else
- **`native`** — all-in on the local Music.app (macOS; works with no account)
- **`api`** — all-in on the Apple Music API + web player; runs on any OS, even on a Mac that isn't signed into the Music app

| What you can do | Where it runs |
|---|---|
| Search & browse library and catalog · recommendations · charts | **API** — any OS, no app |
| Add to library · love / dislike · remove | **API** — any OS, no app |
| Create & edit playlists · folders · move playlists between folders | **API** — any OS, no app |
| **Playback** — play, pause, skip, seek, volume, shuffle, repeat | **Music app** on macOS, **or Chrome** on any OS |
| **Up Next queue** — view, play-next/last, remove, jump, clear, autoplay | **Chrome** — any OS |
| 1–5 star ratings · favorites · snapshots · AirPlay · nested folder paths | **macOS only** (local Music app) |

**Browser playback and the queue need Google Chrome** (for DRM audio) and a real desktop session — they open a local Chrome window and won't run on a headless server. Everything in the **API** rows runs anywhere with no browser and no Music app.

---

## Quick Start

**Requirements:** Python 3.10+ and an Apple Music subscription. For playback and the Up Next queue, install **Google Chrome** (macOS can use the Music app instead).

**No Apple Developer account needed.** On macOS, local library and playback work instantly via the Music app. To add catalog music — and to play or use the queue on any OS — sign in once; see [Enable catalog features](#enable-catalog-features-sign-in-once) below.

```bash
git clone https://github.com/epheterson/applemusic-mcp.git
cd applemusic-mcp
python3 -m venv venv && source venv/bin/activate
pip install -e .
playwright install chromium     # browser engine for sign-in, playback & queue
```

> Install **Google Chrome** too if you want audio playback — the bundled Chromium can't decode Apple's DRM. On macOS-only/native setups you can skip both Chrome and the `playwright install` step.

Add to your MCP client config. **Claude Desktop** (`~/Library/Application Support/Claude/claude_desktop_config.json`); **Cursor / Cline / Windsurf** use the same `mcpServers` shape — see your client's docs for the file location.

```json
{
  "mcpServers": {
    "Apple Music": {
      "command": "/full/path/to/applemusic-mcp/venv/bin/python",
      "args": ["-m", "applemusic_mcp"]
    }
  }
}
```

**That's it!** Restart your client and try: "List my Apple Music playlists" or "Play my favorites playlist"

> **Windows/Linux users:** library, playlists, catalog, and add/rate all work over the API after you
> [sign in](#enable-catalog-features-sign-in-once). Playback and the queue additionally need Google
> Chrome (they open a local Chrome window — not for headless servers).

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

### Add to Your MCP Client (Windows/Linux)

Same `mcpServers` shape works across clients (Claude Desktop, Cursor, Cline, Windsurf, etc.) — only the config file path differs.

**Claude Desktop:**
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- Linux: `~/.config/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "Apple Music": {
      "command": "/full/path/to/applemusic-mcp/venv/bin/python",
      "args": ["-m", "applemusic_mcp"]
    }
  }
}
```

### Optional Preferences

Add to config.json:
```json
{
  "preferences": {
    "auto_search": true,
    "clean_only": false,
    "fetch_explicit": false,
    "reveal_on_library_miss": false
  }
}
```

- `auto_search`: For `playlist(action="add")`, search the catalog (and add to your library) when a track isn't already in your library — required to add catalog songs you don't own yet. Default false to avoid unintended library writes; set true for "fill this playlist" workflows.
- `clean_only`: Filter explicit content, for `search_catalog`, `search_library`, `browse_library` (default: false)
- `fetch_explicit`: Fetch explicit status (cached), for `get_playlist_tracks`, `search_library`, `browse_library` (default: false)
- `reveal_on_library_miss`: Open catalog tracks in Music app, for `play` (default: false)

---

## Usage Examples

**Playlist management:**
- "List my Apple Music playlists"
- "Create a playlist called 'Road Trip' and add some upbeat songs"
- "Add Hey Jude by The Beatles to my Road Trip playlist"
- "Remove the last 3 tracks from my workout playlist"
- "Export my library to CSV"

**Folder organization (macOS):**
- "Create a folder called Genres and put subfolders for Rock, Jazz, and Electronic in it"
- "Move my Road Trip playlist into the Summer folder"
- "Show me my folder hierarchy"
- "Where is my workout playlist?"

**Discovery & playback (macOS):**
- "What have I been listening to recently?"
- "Play my workout playlist on shuffle"
- "Skip to the next track"
- "What's playing right now?"

**With API enabled:**
- "Search Apple Music for 90s alternative rock"
- "Find songs similar to Bohemian Rhapsody and add them to my library"
- "What are the top charts right now?"
- "Get me personalized recommendations"

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

**Folder paths:** Use `/` for nesting: `create(folder="Music/Genres/Jazz")`. Single-level folders and moving a playlist in/out of a folder work over the API on any OS; **nested paths and the folder tree/`path` view need macOS** (AppleScript).

*Native macOS note: the AppleScript interface can't move a playlist out of a folder, so on a tokenless/native Mac `folder=""` recreates the playlist at root (its persistent ID changes). The API path (any OS, incl. Mac in api mode) moves it directly with no recreate.*

**Examples:**
```python
playlist(action="list")
playlist(action="create", name="Road Trip", description="Summer vibes")
playlist(action="create", folder="Summer/Chill")                           # nested folders
playlist(action="create", name="Road Trip", folder="Summer/Chill")         # playlist in nested folder
playlist(action="move", playlist="Road Trip", folder="Summer/Chill")       # into nested folder
playlist(action="move", playlist="Road Trip", folder="")                   # back to root
playlist(action="move", playlist="Chill", folder="Archive")                # folder into folder
playlist(action="path")                                                    # show full hierarchy
playlist(action="path", playlist="Road Trip")                              # "Summer/Chill/Road Trip"
playlist(action="path", folder="Chill")                                    # "Summer/Chill"
playlist(action="delete", folder="Summer/Chill")                           # delete nested folder
playlist(action="rename", folder="Summer", new_name="Summer 2026")
playlist(action="add", playlist="Road Trip", track="Hey Jude", artist="Beatles")
```

**Unified `track` parameter** auto-detects and batches: a single name/ID, a comma-separated CSV, a newline-separated list (one per line — safe for titles containing commas), or a JSON array (`["A","B"]` or `[{"name":"A","artist":"X"}]`). Add entire albums with `album` parameter.

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

**Examples:**
```python
library(action="search", query="Beatles", types="songs", limit=25)
library(action="add", album="Abbey Road", artist="Beatles")
library(action="recently_played", limit=30)
library(action="rate", rate_action="love", track="Hey Jude")
```

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

**Examples:**
```python
catalog(action="search", query="90s alternative", types="songs", limit=50)
catalog(action="album_tracks", album="Abbey Road", artist="Beatles")
catalog(action="album_details", album="GNX", artist="Kendrick Lamar")
catalog(action="artist_details", artist="The Beatles")
```

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

**Optional:** All catalog-based discover actions (`charts`, `top_songs`, `similar_artists`, `song_station`) accept an optional `storefront` parameter to query other regions without changing your default storefront.

**Examples:**
```python
discover(action="recommendations")
discover(action="charts", chart_type="songs", storefront="it")  # Italy charts
discover(action="top_songs", artist="The Beatles")
```

### Playback & Queue

**Playback** is cross-platform: on macOS through the Music app, on any OS through the signed-in Chrome web player (the `playback` preference picks `auto`/`native`/`browser`).

| Action | Description | Where |
|--------|-------------|-------|
| `playback(action="play", ...)` | Play a track, playlist, album, or URL | macOS app · or Chrome |
| `playback(action="control", control="play\|pause\|stop\|next\|previous\|seek")` | Transport controls | macOS app · or Chrome |
| `playback(action="now_playing")` | Current track and player state | macOS app · or Chrome |
| `playback(action="settings", volume=, shuffle_mode=, repeat=)` | Volume / shuffle / repeat | macOS app · or Chrome |
| `playback(action="reveal", track=)` | Show a track in the Music app | macOS only |
| `playback(action="airplay", device_name=)` | List or switch AirPlay devices | macOS only |

`play` accepts ONE of `track`, `playlist`, `album`, or `url`; `shuffle=True` shuffles. **URL playback** handles albums, playlists (incl. personal `pl.u-`), and songs via `?i=`:

```
playback(action="play", url="https://music.apple.com/us/album/ok-computer/1097861387")
playback(action="play", url="https://music.apple.com/us/playlist/todays-hits/pl.f4d106fed2bd41149aaacabb233eb5eb")
```

Browser playback opens a local Chrome window (needs a desktop session). Native macOS URL playback uses UI scripting (display + Accessibility); the cursor may briefly move for `?i=` selection.

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

### Utilities

| Tool | Description | Platform |
|------|-------------|----------|
| `config(action=...)` | Preferences, storefronts, cache, audit log | All |
| `check_auth_status()` | Verify tokens and API connection | All |
| `airplay(device_name=...)` | List or switch AirPlay devices | macOS |
| `reveal_in_music(track, artist)` | Show track in Music app | macOS |

**Config actions:** `info`, `set-pref`, `list-storefronts`, `audit-log`, `clear-tracks`, `clear-exports`, `clear-audit-log`

All modifying operations are logged — view with `config(action="audit-log")`.

### Output Format

Most list tools support these output options:

| Parameter | Values | Description |
|-----------|--------|-------------|
| `format` | `"text"` (default), `"json"`, `"csv"`, `"none"` | Response format |
| `export` | `"none"` (default), `"csv"`, `"json"` | Write file to disk |
| `full` | `False` (default), `True` | Include all metadata |

**Text format** auto-selects the best tier that fits:
- **Full**: Name - Artist (duration) Album [Year] Genre id
- **Compact**: Name - Artist (duration) id
- **Minimal**: Name - Artist id

**Examples:**
```
library(action="search", query="beatles", format="json")                      # JSON response
library(action="browse", item_type="songs", export="csv")                     # Text + CSV file
library(action="browse", item_type="songs", format="none", export="csv")      # CSV only (saves tokens)
playlist(action="tracks", playlist="p.123", export="json", full=True)         # JSON file with all metadata
```

### MCP Resources

Exported files are accessible via MCP resources (any MCP client that supports resource reads):

| Resource | Description |
|----------|-------------|
| `exports://list` | List all exported files |
| `exports://{filename}` | Read a specific export file |

---

## Limitations

### Windows/Linux
Library, catalog, add/rate, and the full playlist + folder surface all work over the API after `signin`. Two caveats:

| Limitation | Detail |
|------------|--------|
| Playback & queue need Google Chrome | They open a local Chrome window for DRM audio — install Chrome + run `playwright install chromium`; not for headless servers |
| A few features are macOS-only | 1–5 star ratings, favorites, library snapshots, AirPlay, and nested folder paths have no Apple Music API equivalent |

### Both Platforms
- **Brand-new playlists take a moment to be addable:** a just-created playlist needs to
  propagate to the cloud library before tracks can be added over the API; existing playlists
  are immediate.
- **Sign-in persists, but can expire:** if catalog actions start failing, re-run
  `applemusic-mcp signin` (or `applemusic-mcp generate-token` for the developer-token path).
- **Screen must be unlocked for macOS playback/play-from-URL:** those drive Music.app via
  System Events; a locked screen blocks them. The MCP detects this and returns a clear error.
- **A few playlists silently revert AppleScript edits** ([known Music.app/AppleScript bug](https://www.macscripter.net/t/add-current-track-from-apple-music-to-playlist/72058)). The MCP detects the rollback automatically and returns an actionable error.

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| 401 Unauthorized | `applemusic-mcp authorize` |
| "Cannot edit playlist" | Use `copy_playlist` for editable copy |
| Token expiring | `applemusic-mcp generate-token` |
| Check everything | `applemusic-mcp status` |

---

## CLI Reference

```bash
applemusic-mcp status          # Check tokens and connection
applemusic-mcp generate-token  # New developer token (180 days)
applemusic-mcp authorize       # Browser auth for user token
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

## Credits

[FastMCP](https://github.com/jlowin/fastmcp) · [Apple MusicKit](https://developer.apple.com/documentation/applemusicapi) · [Model Context Protocol](https://modelcontextprotocol.io/)

---

Built with ❤️ in California by [@epheterson](https://github.com/epheterson) and [Claude Code](https://claude.com/claude-code).
