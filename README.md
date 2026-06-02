# applemusic-mcp

[![Release](https://img.shields.io/github/v/release/epheterson/applemusic-mcp.svg?label=release)](https://github.com/epheterson/applemusic-mcp/releases)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![Downloads](https://static.pepy.tech/badge/applemusic-mcp)](https://pepy.tech/project/applemusic-mcp)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![macOS](https://img.shields.io/badge/macOS-15%20%7C%2026-blue.svg)]()
[![MCP](https://img.shields.io/badge/MCP-server-purple.svg)](https://modelcontextprotocol.io/)

[MCP](https://modelcontextprotocol.io/) server for Apple Music — lets your AI assistant (Claude, Cursor, Cline, Windsurf, or any [MCP client](https://modelcontextprotocol.io/clients)) manage playlists, control playback, and browse your library.

## Features

| Feature | macOS | API |
|---------|:-----:|:---:|
| List playlists | ✓ | ✓ |
| Browse library songs | ✓ | ✓ |
| Create playlists | ✓ | ✓ |
| Search library | ✓ | ✓ |
| Love/dislike tracks | ✓ | ✓ |
| CSV/JSON export | ✓ | ✓ |
| Add tracks to playlists | ✓ | API-created |
| Search catalog | UI* | ✓ |
| Add songs to library | UI* | ✓ |
| Recommendations, charts, radio |   | ✓ |
| Play tracks | ✓ / UI* |   |
| Play by URL (album, playlist, song) | UI* |   |
| Playback control (pause/skip/seek) | ✓ |   |
| Volume, shuffle, repeat | ✓ |   |
| Star ratings (1-5) | ✓ |   |
| Remove tracks from playlists | ✓ |   |
| Delete playlists/folders | ✓ |   |
| Create folders | ✓ | top-level |
| Rename playlists/folders | ✓ |   |
| Move playlists/folders | ✓ |   |
| Folder hierarchy/paths | ✓ |   |

**macOS** uses AppleScript for full local control. **API** mode enables catalog features and works cross-platform. **UI*** = UI automation fallback (requires the screen to be unlocked, display attached, and Accessibility permissions; Top Results only for search).

---

## Quick Start (macOS)

**Requirements:** Python 3.10+, Apple Music app with subscription.

**No Apple Developer account needed on macOS!** Most features work instantly via AppleScript. Catalog features use the API when available, with UI automation fallback on macOS (requires display + Accessibility permissions). On other platforms and for more features a $99/yr Apple Developer Program membership is required for the API.

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

> **Windows/Linux users:** Skip to [API Setup](#api-setup-optional-on-macos-required-on-windowslinux) - AppleScript features require macOS, but API mode works cross-platform.

---

## API Setup (Optional on macOS, Required on Windows/Linux)

**Requirements:** Python 3.10+, Apple Music app with subscription, active [Apple Developer Program](https://developer.apple.com/programs/) membership ($99/yr USD).

Want catalog search, recommendations, and Windows/Linux support? Set up API access.

### 1. Get MusicKit Key

1. [Apple Developer Portal → Keys](https://developer.apple.com/account/resources/authkeys/list) → Click **+**
2. Name it anything, check **MusicKit**, click Continue → Register
3. **Download the .p8 file** (one-time download!)
4. Note your **Key ID** (10 chars) and **Team ID** (from [Membership](https://developer.apple.com/account/#!/membership))

### 2. Configure

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

### 3. Generate Tokens

```bash
applemusic-mcp generate-token   # Creates developer token (180 days)
applemusic-mcp authorize        # Opens browser for Apple Music auth
applemusic-mcp status           # Verify everything works
```

### 4. Add to Your MCP Client (Windows/Linux)

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
| Only API-created playlists editable | `copy_playlist` makes editable copy |
| Can't delete playlists or remove tracks | Create new playlist instead |
| No playback control | Use Music app directly |

### Both Platforms
- **Tokens expire:** Developer token lasts 180 days. You'll see warnings starting 30 days before expiration. Run `applemusic-mcp generate-token` to renew.
- **Screen must be unlocked for UI flows:** The catalog search / hover-to-add / play UI paths drive Music.app via System Events; a locked screen blocks them. The MCP detects this and returns a clear error.
- **A few playlists silently revert AppleScript edits** ([known Music.app/AppleScript bug](https://www.macscripter.net/t/add-current-track-from-apple-music-to-playlist/72058)). The MCP detects the rollback automatically and returns an actionable error suggesting Music.app's right-click → Add to Playlist as a workaround.

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

## License

MIT · *Unofficial community project, not affiliated with Apple.*

## Credits

[FastMCP](https://github.com/jlowin/fastmcp) · [Apple MusicKit](https://developer.apple.com/documentation/applemusicapi) · [Model Context Protocol](https://modelcontextprotocol.io/)

---

Built with ❤️ in California by [@epheterson](https://github.com/epheterson) and [Claude Code](https://claude.com/claude-code).
