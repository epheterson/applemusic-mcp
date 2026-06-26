# applemusic-mcp

[![Release](https://img.shields.io/github/v/release/epheterson/applemusic-mcp.svg?label=release)](https://github.com/epheterson/applemusic-mcp/releases)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![Downloads](https://static.pepy.tech/badge/applemusic-mcp)](https://pepy.tech/project/applemusic-mcp)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![macOS](https://img.shields.io/badge/macOS-15%20%7C%2026-blue.svg)]()
[![MCP](https://img.shields.io/badge/MCP-server-purple.svg)](https://modelcontextprotocol.io/)

An [MCP](https://modelcontextprotocol.io/) server for Apple Music: playlists, library, catalog, discovery, playback, and the Up Next queue, exposed to any [MCP client](https://modelcontextprotocol.io/clients) (Claude, Cursor, Cline, Windsurf). Runs on macOS, Windows, and Linux.

## Capability matrix

Three engines back the server. Native drives the local Music.app via AppleScript and UI scripting (macOS only, no account required). API talks to Apple Music's web API at `amp-api.music.apple.com` (any OS, after sign-in). Browser runs a local Google Chrome window with MusicKit JS (any OS, required for DRM audio). The `mode` and `playback` preferences decide which engine handles a given call.

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

Legend: ✓ works, ✗ not possible on that engine, — not applicable or not exposed there. Per-cell reasoning lives in [docs/CAPABILITIES.md](docs/CAPABILITIES.md).

## Quickstart

Requirements: Python 3.10+ and an Apple Music subscription. Playback and the Up Next queue also need Google Chrome (on macOS the Music app can substitute).

### Claude Code (one line)

```bash
claude mcp add applemusic -- uvx applemusic-mcp serve
```

### Claude Desktop / Cursor / Cline / Windsurf

```bash
pipx install applemusic-mcp        # or: pip install applemusic-mcp
playwright install chromium        # browser engine for sign-in, playback, queue
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

Config file locations:

|Client|Path|
|---|---|
|Claude Desktop (macOS)|`~/Library/Application Support/Claude/claude_desktop_config.json`|
|Claude Desktop (Windows)|`%APPDATA%\Claude\claude_desktop_config.json`|
|Cursor / Cline / Windsurf|Same `mcpServers` shape, see the client's docs|

Restart the client, then try "List my Apple Music playlists" or "Play my favorites playlist." On macOS the local library and playback work with no account. To add catalog music or run on any OS, [sign in once](#sign-in).

Notes:

- Browser features (playback, queue, sign-in) need Google Chrome plus the one-time `playwright install chromium` (the bundled Chromium cannot decode Apple's DRM). They open a local Chrome window and will not run headless. With `uvx`, install the browser as `uvx --from applemusic-mcp playwright install chromium`.
- From source: `git clone … && pip install -e .`, then point the config `command` at `<repo>/venv/bin/applemusic-mcp` or use `python -m applemusic_mcp`.

## Modes

The `mode` preference picks the engine. The `playback` preference overrides the engine for audio only (handy for "API everything, native audio on macOS").

|`mode`|Behavior|
|---|---|
|`auto` (default)|Native Music.app on macOS, the cross-platform API everywhere else|
|`native`|All local Music.app (macOS, works with no account)|
|`api`|All Apple Music API plus web player, any OS, even a Mac not signed into the Music app|

|`playback`|Behavior|
|---|---|
|`auto` (default)|Follows `mode`|
|`native`|macOS Music.app|
|`browser`|Chrome web player|

In `auto` mode, if a native playback click cannot start (for example Accessibility not granted), playback falls back to the Chrome web player. Pin `playback="native"` to keep audio in the Music app only.

Set any preference conversationally ("use API mode") or directly:

```
config(action="set-pref", preference="mode", string_value="api")
```

## Sign in

Adding catalog music to your library and playlists runs over the Apple Music API. Pick one path.

Browser sign-in (recommended, no Apple Developer account, any OS):

```bash
applemusic-mcp signin     # opens Chrome to music.apple.com, sign in once
applemusic-mcp status     # verify
```

This captures your `media-user-token` from a local signed-in Chrome profile (your password never touches the tool). The developer token comes from Apple's public web player. The web-player token is valid 35 days and re-fetches automatically, so the sign-in persists and you do not re-authenticate. This uses Apple's web-player API the same way established open-source clients do, including the [Cider](https://github.com/ciderapp/Cider-2) desktop player and the [Music Assistant](https://www.music-assistant.io/music-providers/apple-music/) Home Assistant server.

Apple Developer token (sanctioned, 6-month, for [Apple Developer Program](https://developer.apple.com/programs/) members): see [Appendix: Developer token setup](#appendix-developer-token-setup).

Tokens self-heal: the developer token auto-renews from your `.p8` when 30 days or less from expiry, the web token re-fetches itself when 15 days or less out. To switch accounts, run `logout` then `signin`.

### Preferences

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

|Preference|Values|Meaning|
|---|---|---|
|`mode`|`auto` / `native` / `api`|Engine selection (see above)|
|`playback`|`auto` / `native` / `browser`|Playback engine override|
|`secure_storage`|`file` (default) / `keychain`|Token storage: `0600` files everywhere, or the OS keychain (opt-in, may prompt once)|
|`auto_search`|`false` (default) / `true`|Let `playlist(action="add")` pull catalog songs you do not own into your library|
|`clean_only`|`false` (default) / `true`|Filter explicit content on search and browse|
|`fetch_explicit`|`false` (default) / `true`|Fetch explicit status on search and browse|

Set any of these conversationally too: `config(action="set-pref", preference="mode", string_value="api")` (booleans use `value=true/false`).

## Tools

Seven tools cover the surface: `playlist`, `library`, `catalog`, `discover`, `playback`, `queue`, `config`. The "Where" column reflects the capability matrix above.

### `playlist(action=...)`

Playlist and folder operations. Most run on any OS over the API. A few folder niceties are macOS-only.

|Action|Parameters|Description|Where|
|---|---|---|---|
|`list`|`format`, `export`, `full`|List all playlists|Any OS|
|`tracks`|`playlist`, `filter`, `limit`, `offset`, `format`, `export`, `full`, `fetch_explicit`|Get playlist tracks with filter and pagination|Any OS|
|`search`|`query`, `playlist`|Search tracks within a playlist|Any OS|
|`create`|`name`, `description`, `folder`|Create a playlist and/or folder|Any OS, nested paths macOS|
|`add`|`playlist`, `track`, `album`, `artist`, `allow_duplicates`, `verify`, `auto_search`|Smart add: auto-search catalog, skip duplicates|Any OS|
|`copy`|`source`, `new_name`|Copy a playlist to an editable version|macOS|
|`move`|`playlist`, `folder`|Move a playlist into a folder, or to top level (`folder=""`)|Any OS, nested paths macOS|
|`remove`|`playlist`, `track`, `artist`|Remove a track from a playlist|Any OS|
|`delete`|`playlist` or `folder`|Delete a playlist or folder|Any OS, nested paths macOS|
|`rename`|`playlist` or `folder`, `new_name`|Rename a playlist (any OS) or folder (macOS)|Any OS, folder macOS|
|`path`|`playlist` or `folder`|Get full path or show hierarchy|macOS|

Folders: `/` nests paths (`create(folder="Music/Genres/Jazz")`). Single-level folders and moving a playlist in or out work over the API on any OS. Nested paths and the folder tree or `path` view need macOS. (Native quirk: AppleScript cannot move a playlist out of a folder, so `folder=""` recreates it at root with a new ID. The API path moves it in place.)

The unified `track` parameter auto-detects and batches: a single name or ID, a comma- or newline-separated list, or a JSON array (`["A","B"]` or `[{"name":"A","artist":"X"}]`). Whole albums via `album`.

### `library(action=...)`

Reads, add, remove, and love/dislike work on any OS over the API. Favorites, snapshots, genre search, and 1-5 star ratings are macOS-only.

|Action|Parameters|Description|Where|
|---|---|---|---|
|`search`|`query`, `types`, `limit`, `format`, `export`, `full`, `fetch_explicit`, `clean_only`|Search your library (genre search macOS)|Any OS, genre macOS|
|`add`|`track`, `album`, `artist`|Add tracks or albums from the catalog|Any OS|
|`browse`|`item_type`, `limit`, `offset`, `format`, `export`, `full`, `fetch_explicit`, `clean_only`|List songs, albums, artists, videos|Any OS|
|`favorites`|`limit`, `offset`, `format`, `export`, `full`, `fetch_explicit`, `clean_only`|List songs marked Favorite (loved)|macOS|
|`recently_played`|`limit`, `format`, `export`, `full`|Recent listening history|Any OS|
|`recently_added`|`limit`, `format`, `export`, `full`|Recently added content|Any OS|
|`rate`|`rate_action`, `track`, `artist`, `stars`|Love, dislike, clear (any OS), 1-5 stars get/set (macOS)|Any OS, stars macOS|
|`remove`|`track`, `artist`|Remove one track from your library (exact match preferred)|Any OS|
|`snapshot`|`query`|Library integrity checking: tracks, playlists, folder hierarchy|macOS|

Snapshot sub-commands via `query`:

|Query|Description|
|---|---|
|_(empty)_|Diff current state from baseline, or take initial baseline|
|`new`|Reset baseline to current state|
|`history`|View recorded changes over time|
|`list`|List all saved snapshot/diff files|
|`delete FILENAME`|Delete a specific diff file|

### `catalog(action=...)`

Catalog search and details. `search` accepts fuzzy queries (typos, partial lyrics, vague descriptions like "whistling beatles song"). On macOS it falls back to Music.app's built-in UI search when no API token is available, so you can find a half-remembered song without credentials.

|Action|Parameters|Description|Platform|
|---|---|---|---|
|`search`|`query`, `types`, `limit`, `format`, `export`, `full`, `clean_only`|Search the catalog (fuzzy, UI fallback on macOS when tokenless)|All|
|`album_tracks`|`album`, `artist`, `limit`, `offset`, `format`, `export`, `full`|Album tracks (by name or ID)|All|
|`album_details`|`album`, `artist`, `format`, `export`, `full`|Full album metadata plus track listing|All|
|`song_details`|`song_id`|Full song metadata|All|
|`artist_details`|`artist`|Artist info and discography|All|
|`song_station`|`song_id`|Radio station for a song|All|
|`genres`|-|List all available genres|All|

### `discover(action=...)`

Discovery and recommendations: personalized stations, charts, top songs, similar artists. API only, no UI fallback.

|Action|Parameters|Description|Platform|
|---|---|---|---|
|`recommendations`|`format`, `export`, `full`|Personalized recommendations|All|
|`heavy_rotation`|`format`, `export`, `full`|Your frequently played|All|
|`charts`|`chart_type`, `format`, `export`, `full`|Apple Music charts|All|
|`top_songs`|`artist`|An artist's popular songs|All|
|`similar_artists`|`artist`|Find similar artists|All|
|`search_suggestions`|`term`|Autocomplete suggestions|All|
|`personal_station`|-|Your personal radio station|All|

Catalog-based actions (`charts`, `top_songs`, `similar_artists`, `song_station`) take an optional `storefront` to query other regions (for example `storefront="it"`) without changing your default.

### `playback(action=...)`

Cross-platform: on macOS through the Music app, on any OS through the signed-in Chrome web player. The `playback` preference picks `auto`/`native`/`browser`.

|Action|Description|Where|
|---|---|---|
|`play` (`track`/`playlist`/`album`/`url`, `shuffle`)|Play a track, playlist, album, or URL|macOS app or Chrome|
|`control` (`control="play\|pause\|stop\|next\|previous\|seek"`)|Transport controls|macOS app or Chrome|
|`now_playing`|Current track and player state|macOS app or Chrome|
|`settings` (`volume`, `shuffle_mode`, `repeat`)|Volume, shuffle, repeat|macOS app or Chrome|
|`reveal` (`track`)|Show a track: opens in Music.app (macOS) or the Chrome web player (any OS)|macOS app or Chrome|
|`airplay` (`device_name`)|List or switch AirPlay devices|macOS only|

`play` accepts exactly one of `track`, `playlist`, `album`, or `url`. `shuffle=True` shuffles. URL playback handles albums, playlists (including personal `pl.u-`), and songs via `?i=`:

```
playback(action="play", url="https://music.apple.com/us/album/ok-computer/1097861387")
playback(action="play", url="https://music.apple.com/us/playlist/todays-hits/pl.f4d106fed2bd41149aaacabb233eb5eb")
```

Native macOS catalog playback uses UI scripting: it brings Music.app to the front and moves the cursor to click Play, so it needs Accessibility permission (System Settings, Privacy and Security, Accessibility) for your terminal or MCP host, and an unlocked screen.

### `queue(action=...)`

The Up Next queue. Drives the web player's play queue through Chrome, on any OS. Browser-only (no REST endpoint exists and AppleScript cannot reach the queue).

|Action|Parameters|Description|
|---|---|---|
|`list`|—|Show Up Next (▶ marks the current item, indices are 0-based)|
|`play_next`|`track`, `artist`|Insert a track right after the current one|
|`play_last`|`track`, `artist`|Append a track to the end of Up Next|
|`remove`|`index`|Remove the item at `index`|
|`jump`|`index`|Jump playback to the item at `index`|
|`clear`|—|Empty the queue|
|`autoplay`|`enabled`|Turn ∞ Autoplay on/off (keep playing similar music when the queue ends)|

### `config(action=...)`

Settings, cache, and authentication in one tool. (AirPlay and reveal-in-app live on `playback`, see above.)

Settings and cache: `info`, `set-pref`, `list-storefronts`, `audit-log`, `clear-tracks`, `clear-exports`, `clear-audit-log`. All modifying operations are logged, viewable with `config(action="audit-log")`.

Auth (conversational, no terminal needed; Claude Code's native `/mcp` auth UI is for remote servers, this local server exposes auth through this tool):

|`config(action=...)`|Description|
|---|---|
|`status`|Which tokens are active, expiry and auto-renew, what works, and the next step|
|`signin`|Open a browser to sign in (any OS) and capture your session|
|`logout`|Sign out, clears your user token and browser session so you can switch accounts (needs `confirm=True`)|
|`reset`|Wipe all credentials for a clean slate, or drop a developer token for the web path (needs `confirm=True`)|

The same actions exist on the CLI: `applemusic-mcp signin | logout | reset | status`.

### Output format

Most list tools support these output options:

|Parameter|Values|Description|
|---|---|---|
|`format`|`"text"` (default), `"json"`, `"csv"`, `"none"`|Response format|
|`export`|`"none"` (default), `"csv"`, `"json"`|Write a file to disk|
|`full`|`False` (default), `True`|Include all metadata|

Text format auto-selects the tier that fits (Full, Compact, Minimal). Use `export="csv"` with `format="none"` to write a file without spending tokens on the response. Exported files are also readable as MCP resources: `exports://list` and `exports://{filename}`.

## Library-first rule

You cannot add catalog songs directly to playlists. Catalog IDs (`1234567890`) are not playlist-addable; songs must enter the library first, where they get a library ID (`i.abc123`). The server handles this for you: `playlist(action="add", auto_search=True)` searches the catalog, adds to the library, resolves the library ID, and inserts into the playlist. This applies to both the native and API paths.

## Limitations

### Windows and Linux

Library, catalog, add and rate, and the full playlist plus folder surface all work over the API after `signin`. Two caveats:

|Limitation|Detail|
|---|---|
|Playback and queue need Google Chrome|They open a local Chrome window for DRM audio. Install Chrome and run `playwright install chromium`. Not for headless servers.|
|A few features are macOS-only|1-5 star ratings, favorites, library snapshots, AirPlay, and nested folder paths have no Apple Music API equivalent.|

### Both platforms

- Brand-new playlists take a moment to be addable over the API (cloud propagation). Existing ones are immediate.
- Sign-in persists but can expire. If catalog actions start failing, re-run `applemusic-mcp signin`.
- macOS playback and play-from-URL need an unlocked screen and Accessibility permission (the server drives Music.app via System Events and moves the cursor to click). Grant it under System Settings, Privacy and Security, Accessibility. The MCP returns a clear error if the screen is locked or the click cannot start playback.
- A few playlists silently revert AppleScript edits ([known Music.app bug](https://www.macscripter.net/t/add-current-track-from-apple-music-to-playlist/72058)). The MCP detects the rollback and surfaces it.
- Browser playback and queue need an Apple Music subscription for full-track DRM audio. Without one, MusicKit serves roughly 30-second previews.

## Troubleshooting

|Problem|Solution|
|---|---|
|401 Unauthorized|`applemusic-mcp signin` (web path) or `applemusic-mcp authorize` (dev-token path)|
|macOS "couldn't auto-play it"|Grant Accessibility (System Settings, Privacy and Security, Accessibility), or set `playback="browser"` to play in Chrome|
|"Cannot edit playlist"|Use `copy_playlist` for an editable copy|
|Token expiring|`applemusic-mcp generate-token`|
|Check everything|`applemusic-mcp status`|

## CLI reference

```bash
applemusic-mcp signin          # Browser sign-in (no Apple Developer account)
applemusic-mcp status          # Check tokens and connection
applemusic-mcp logout          # Sign out (switch accounts: logout then signin)
applemusic-mcp reset --force   # Wipe all credentials (keeps your .p8 key file)
applemusic-mcp init            # Scaffold config.json for the developer-token path
applemusic-mcp generate-token  # New developer token (180 days, auto-renews on use)
applemusic-mcp authorize       # Browser auth for a user token (developer-token path)
applemusic-mcp serve           # Run MCP server (auto-launched by your MCP client)
```

Config directory: `~/.config/applemusic-mcp/` (config.json, .p8 key, tokens).

## Appendix: Developer token setup

The preferred path if you have an [Apple Developer Program](https://developer.apple.com/programs/) membership: a sanctioned, 6-month token. (Browser sign-in needs none of this.)

1. Get a MusicKit key. [Apple Developer Portal, Keys](https://developer.apple.com/account/resources/authkeys/list), +, name it, check MusicKit, Register, download the .p8 (one-time). Note your Key ID and Team ID (from [Membership](https://developer.apple.com/account/#!/membership)).

2. Configure:

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

3. Generate and authorize:

```bash
applemusic-mcp generate-token   # developer token (180 days)
applemusic-mcp authorize        # capture your user token
applemusic-mcp status           # verify
```

## License

MIT. Unofficial community project, not affiliated with Apple.

<!-- Identifier for the official MCP Registry (PyPI ownership check). -->
mcp-name: io.github.epheterson/applemusic-mcp

## Credits

[FastMCP](https://github.com/jlowin/fastmcp) · [Apple MusicKit](https://developer.apple.com/documentation/applemusicapi) · [Model Context Protocol](https://modelcontextprotocol.io/)

---

Built with care in California by [@epheterson](https://github.com/epheterson) and [Claude Code](https://claude.com/claude-code).
