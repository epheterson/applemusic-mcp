# applemusic-mcp

[![Release](https://img.shields.io/github/v/release/epheterson/applemusic-mcp.svg?label=release)](https://github.com/epheterson/applemusic-mcp/releases)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![Downloads](https://static.pepy.tech/badge/applemusic-mcp)](https://pepy.tech/project/applemusic-mcp)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![macOS](https://img.shields.io/badge/macOS-15%20%7C%2026-blue.svg)]()
[![MCP](https://img.shields.io/badge/MCP-server-purple.svg)](https://modelcontextprotocol.io/)

[MCP](https://modelcontextprotocol.io/) server for Apple Music — lets your AI assistant (Claude, Cursor, Cline, Windsurf, or any [MCP client](https://modelcontextprotocol.io/clients)) manage playlists, add music, control playback, and browse your library.

**Works on macOS, Windows, and Linux.**

### Access

| Access | What it does | Platforms |
|---|---|---|
| **Developer token** — recommended | Full Apple Music API: library, playlists, catalog add, recommendations | macOS · Windows · Linux |
| Web token | Same | macOS · Windows · Linux |
| No token | Control the local Music app — play, browse, edit local playlists | macOS |

<sub>**Developer token** is included with Apple Developer membership (free for App Store developers), valid 6 months — [setup](#appendix-developer-token-setup). **Web token** is a free fallback captured by `applemusic-mcp signin`; it uses Apple's web-player API the same way open-source clients such as [Cider](https://github.com/ciderapp/Cider-2) and [Sidra](https://github.com/wimpysworld/sidra) do.</sub>

## Features

| Feature | macOS | Cross-platform |
|---------|:-----:|:--------------:|
| List playlists | ✓ | ✓ |
| Browse / search library | ✓ | ✓ |
| Create playlists | ✓ | ✓ |
| Love/dislike tracks | ✓ | ✓ |
| CSV/JSON export | ✓ | ✓ |
| Search catalog | ✓ | ✓ |
| **Add songs to library** | ✓ | ✓ |
| **Add tracks to playlists** | ✓ | ✓ |
| Recommendations, charts, radio | ✓ | ✓ |
| Play tracks | ✓ |   |
| Play by URL (album, playlist, song) | ✓ |   |
| Playback control (pause/skip/seek/volume) | ✓ |   |
| Star ratings (1-5) | ✓ |   |
| Remove tracks / delete playlists / folders | ✓ |   |
| Rename / move playlists & folders | ✓ |   |

**Catalog, library, and playlist features run over the Apple Music API** — same on every
platform (sign in once). **Playback** is macOS-only for now (native AppleScript control of
Music.app); cross-platform browser playback is in progress. Play-from-URL on macOS uses a
small AppleScript UI step and needs the screen unlocked + Accessibility permission.

---

## Quick Start (macOS)

**Requirements:** Python 3.10+, Apple Music app with subscription.

**No Apple Developer account needed.** On macOS, playback and local library features work instantly via AppleScript. To add catalog music to your library/playlists (any platform), sign in once — see [Enable catalog features](#enable-catalog-features-sign-in-once) below.

```bash
git clone https://github.com/epheterson/applemusic-mcp.git
cd applemusic-mcp
python3 -m venv venv && source venv/bin/activate
pip install -e .
```

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

> **Windows/Linux users:** AppleScript playback requires macOS, but catalog/library/playlist
> features work cross-platform — just [sign in](#enable-catalog-features-sign-in-once).

---

## Enable Catalog Features (sign in once)

Adding catalog music to your library and playlists runs over the Apple Music API. Sign in once:

```bash
applemusic-mcp signin     # opens Chrome to music.apple.com; sign in once
applemusic-mcp status     # verify
```

Captures your `media-user-token` from a local, signed-in Chrome profile (your password never
touches this tool); uses your installed Chrome when present. The sign-in persists.

Have an Apple Developer membership? A generated token (6-month, sanctioned) is preferred — see
[Appendix: Developer token setup](#appendix-developer-token-setup).

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

- `auto_search`: Enables catalog search + library-add fallback for `playlist(action="add")` when a track isn't already in the user's library (default: false to avoid unintended library writes — set to true for "fill this playlist" workflows). On macOS works without an API token by falling back to Music.app UI automation (requires display + Accessibility permissions for the host process).
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
Playlist and folder operations - list, manage tracks, create, copy, remove (macOS), delete (macOS), rename (macOS), folder management (macOS)

| Action | Parameters | Description | Platform |
|--------|-----------|-------------|----------|
| `list` | `format`, `export`, `full` | List all playlists | All |
| `tracks` | `playlist`, `filter`, `limit`, `offset`, `format`, `export`, `full`, `fetch_explicit` | Get playlist tracks with filter/pagination | All (by-name: macOS) |
| `search` | `query`, `playlist` | Search tracks in playlist | All |
| `create` | `name`, `description`, `folder` | Create playlist and/or folder. `folder` supports slash paths (e.g. `Summer/Chill`). | All |
| `add` | `playlist`, `track`, `album`, `artist`, `allow_duplicates`, `verify`, `auto_search` | Smart add: auto-search catalog, UI fallback when no API token, skip duplicates | All (by-name: macOS) |
| `copy` | `source`, `new_name` | Copy playlist to editable version | All (by-name: macOS) |
| `move` | `playlist`, `folder` | Move playlist/folder into a folder path. `folder=""` moves to root (recreates playlist*). | macOS |
| `remove` | `playlist`, `track`, `artist` | Remove track(s) from playlist | macOS |
| `delete` | `playlist` or `folder` | Delete a playlist or folder (supports slash paths) | macOS |
| `rename` | `playlist` or `folder`, `new_name` | Rename a playlist or folder | macOS |
| `path` | `playlist` or `folder` | Get full path of a playlist/folder. No args = show full hierarchy. | macOS |

**Folder paths:** Use `/` for nesting: `create(folder="Music/Genres/Jazz")` creates all levels. Works with `move`, `delete`, and `create`.

**Note:** The Apple Music API only supports creating single-level folders. Nested paths, move, delete, rename, tree, and path operations require macOS (AppleScript). Snapshots on macOS capture full folder hierarchy.

*\*Move-to-root limitation: Music.app's AppleScript interface cannot move playlists out of folders. `folder=""` recreates the playlist at root with the same tracks — the playlist's persistent ID will change. Moving INTO folders preserves the ID.*

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
Library management - search, add, browse, favorites (macOS), rate, recently played/added, remove (macOS), snapshot (macOS)

| Action | Parameters | Description | Platform |
|--------|-----------|-------------|----------|
| `search` | `query`, `types`, `limit`, `format`, `export`, `full`, `fetch_explicit`, `clean_only` | Search your library (fast local on macOS) | All |
| `add` | `track`, `album`, `artist` | Add tracks/albums from catalog | All |
| `browse` | `item_type`, `limit`, `offset`, `format`, `export`, `full`, `fetch_explicit`, `clean_only` | List songs/albums/artists/videos | All |
| `favorites` | `limit`, `offset`, `format`, `export`, `full`, `fetch_explicit`, `clean_only` | List songs marked Favorite (loved) | macOS |
| `recently_played` | `limit`, `format`, `export`, `full` | Recent listening history | All |
| `recently_added` | `limit`, `format`, `export`, `full` | Recently added content | All |
| `rate` | `rate_action`, `track`, `artist`, `stars` | Love/dislike/clear/get/set ratings | All (stars/clear: macOS) |
| `remove` | `track`, `artist` | Remove track(s) from library | macOS |
| `snapshot` | `query` | Library integrity checking — captures tracks, playlists, and folder hierarchy | macOS |

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

### Playback (macOS only)
| Action | Description | Method |
|--------|-------------|--------|
| `playback(action="play", ...)` | Play track, playlist, album, or URL | API + AS |
| `playback(action="control", ...)` | Play, pause, stop, next, previous, seek | AppleScript |
| `playback(action="now_playing")` | Current track info and player state | AppleScript |
| `playback(action="settings", ...)` | Get/set volume, shuffle, repeat | AppleScript |
| `playback(action="airplay", ...)` | List or switch AirPlay devices | AppleScript |

`play` accepts ONE of: `track`, `playlist`, `album`, or `url`. Use `shuffle=True` for shuffled playback. Response shows source: `[Library]`, `[Catalog]`, `[Catalog→Library]`, `[UI Catalog]` (UI played a track API resolved), or `[UI Search]` (UI played a track only the UI search found). Catalog items can be added first (`add_to_library=True`) or opened in Music (`reveal=True`).

**URL playback** — albums, playlists (including personal `pl.u-`), and songs via `?i=`:
```
playback(action="play", url="https://music.apple.com/us/album/ok-computer/1097861387")
playback(action="play", url="https://music.apple.com/us/album/cowboy-carter/1738363766?i=1738363961")
playback(action="play", url="https://music.apple.com/us/playlist/todays-hits/pl.f4d106fed2bd41149aaacabb233eb5eb")
```
Uses UI scripting (requires display + Accessibility permissions). The mouse cursor may briefly move for `?i=` track selection.

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
| Limitation | Workaround |
|------------|------------|
| No playback control | Use Music app/web directly (cross-platform browser playback is in progress) |
| Deleting playlists / removing tracks is macOS-only | Done via AppleScript; not yet exposed cross-platform |

Adding songs to your library and to existing playlists works cross-platform (after `signin`).

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

The preferred path if you have an [Apple Developer Program](https://developer.apple.com/programs/) membership — a sanctioned, 6-month token.

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
