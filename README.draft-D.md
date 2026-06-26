# applemusic-mcp

[![Release](https://img.shields.io/github/v/release/epheterson/applemusic-mcp.svg?label=release)](https://github.com/epheterson/applemusic-mcp/releases)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![Downloads](https://static.pepy.tech/badge/applemusic-mcp)](https://pepy.tech/project/applemusic-mcp)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![macOS](https://img.shields.io/badge/macOS-15%20%7C%2026-blue.svg)]()
[![MCP](https://img.shields.io/badge/MCP-server-purple.svg)](https://modelcontextprotocol.io/)

**Run your entire Apple Music life through your AI assistant.** Build playlists, add music, control playback, organize your library, and line up what plays next, all by just asking. Works on macOS, Windows, and Linux, and you do not need a $99 Apple Developer account.

It is an [MCP](https://modelcontextprotocol.io/) server: drop it into Claude, Cursor, Cline, Windsurf, or any [MCP client](https://modelcontextprotocol.io/clients), sign in once, and your assistant can actually touch your music instead of just talking about it.

```
"Create a Road Trip playlist and fill it with upbeat 90s alternative."
"Play my workout playlist on shuffle, and queue up Bohemian Rhapsody next."
"Find songs similar to Bohemian Rhapsody and add them to my library."
```

---

## What runs where

Three engines back the server, picked automatically by the `mode` preference (`auto` is the default; `native` pins the macOS Music.app, `api` pins the cross-platform API + web player). `✓` supported, `✗` not possible on that engine, `—` not applicable there.

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

Everything in the **API** column runs anywhere, no browser and no Music app. Browser playback and the queue need Google Chrome and a desktop session. Full per-capability reasoning lives in [docs/CAPABILITIES.md](docs/CAPABILITIES.md).

---

## Quick start

**Requirements:** Python 3.10+ and an Apple Music subscription. Playback and the Up Next queue also need [Google Chrome](https://www.google.com/chrome/) (macOS can use the Music app instead).

**Claude Code**, one line:

```bash
claude mcp add applemusic -- uvx applemusic-mcp serve
```

**Claude Desktop / Cursor / Cline / Windsurf**, install once, then add the config block:

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

Restart your client and try *"List my Apple Music playlists"* or *"Play my favorites."*

On macOS the local library and playback work immediately, no account setup. To add catalog music or run on any OS, [sign in once](#sign-in).

<details>
<summary>Config file locations and source install</summary>

**Config file:** Claude Desktop uses `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows). Cursor, Cline, and Windsurf use the same `mcpServers` shape, see your client's docs.

**Browser features** (playback, queue, sign-in) need Google Chrome plus the one-time `playwright install chromium`. The bundled Chromium can't decode Apple's DRM, so a real Chrome install is required, and these features open a local Chrome window (not for headless servers). With `uvx`, run the browser install as `uvx --from applemusic-mcp playwright install chromium`. macOS-native-only setups can skip Chrome entirely.

**From source:** `git clone … && pip install -e .`, then point the config `command` at `<repo>/venv/bin/applemusic-mcp` or use `python -m applemusic_mcp`.
</details>

---

## Sign in

Adding catalog music to your library and playlists runs over the Apple Music API. One command, any OS:

```bash
applemusic-mcp signin     # opens Chrome to music.apple.com; sign in once
applemusic-mcp status     # verify
```

This captures your session token from a local signed-in Chrome profile (your password never touches this tool). Sign-in persists, and tokens re-fetch themselves before they expire, so you rarely re-authenticate. You can also sign in conversationally: just ask your assistant to sign in.

Browser sign-in uses Apple's web-player API, the same unofficial path used by open-source clients like [Cider](https://github.com/ciderapp/Cider-2) and [Music Assistant](https://www.music-assistant.io/music-providers/apple-music/). If you have an [Apple Developer](https://developer.apple.com/programs/) membership, you can generate a sanctioned 6-month token instead (see the [appendix](#appendix-developer-token)).

---

## Tools

Seven action-based tools keep the MCP context small. Each takes an `action` and routes to the right engine.

| Tool | What it does |
|---|---|
| `playlist` | list, tracks, search, create, add, copy, move, remove, delete, rename, path (playlists and folders) |
| `library` | search, add, browse, favorites, recently_played, recently_added, rate, remove, snapshot |
| `catalog` | search, album_tracks, album_details, song_details, artist_details, song_station, genres |
| `discover` | recommendations, heavy_rotation, charts, top_songs, similar_artists, search_suggestions, personal_station |
| `playback` | play (track / album / playlist / URL), control, now_playing, settings, reveal, airplay |
| `queue` | list, play_next, play_last, remove, jump, clear, autoplay (web player Up Next) |
| `config` | status, signin, logout, reset, set-pref, audit-log, cache, storefronts |

<details>
<summary>Common patterns</summary>

- **`track` is one parameter that batches.** Pass a single name or ID, a comma- or newline-separated list, or a JSON array (`["A","B"]` or `[{"name":"A","artist":"X"}]`). Whole albums via `album`.
- **Adding to a playlist** auto-searches the catalog and skips duplicates. Set the `auto_search` preference to `true` for "fill this playlist" workflows (default `false`, to avoid unintended library writes).
- **Output format** on list tools: `format` (`text` / `json` / `csv` / `none`), `export` (writes a file readable as an MCP resource via `exports://`), `full` (all metadata).
- **URL playback** handles albums, playlists, and songs: `playback(action="play", url="https://music.apple.com/...")`.
- **Storefronts:** catalog actions take an optional `storefront` (for example `storefront="it"`) to query other regions without changing your default.
</details>

<details>
<summary>Preferences</summary>

Set any of these in config.json under `preferences`, or conversationally with `config(action="set-pref", ...)`:

- `mode`: engine, `auto` (default) / `native` (local Music.app) / `api` (Apple Music API + web player, any OS).
- `playback`: playback-engine override, `auto` (default, follows `mode`) / `native` / `browser`.
- `secure_storage`: where tokens live, `file` (default, `0600` files) or `keychain` (OS keychain, opt-in).
- `auto_search`: let `playlist(action="add")` pull catalog songs into your library (default `false`).
- `clean_only` / `fetch_explicit`: filter or fetch explicit status on searches and browse (default `false`).
</details>

---

## Good to know

- **macOS playback needs an unlocked screen and Accessibility permission.** Native catalog playback drives Music.app via System Events and moves the cursor to click Play. Grant it under System Settings → Privacy & Security → Accessibility, or set `playback="browser"` to play in Chrome instead.
- **Brand-new playlists take a moment** to be addable over the API (cloud propagation). Existing ones are immediate.
- **A few macOS-only features** have no Apple Music API equivalent: 1 to 5 star ratings, favorites, library snapshots, AirPlay, and nested folder paths.
- **If catalog actions start failing**, re-run `applemusic-mcp signin`. A handful of user playlists silently revert AppleScript edits ([known Music.app bug](https://www.macscripter.net/t/add-current-track-from-apple-music-to-playlist/72058)); the server detects and surfaces the rollback.

---

## Appendix: developer token

If you have an [Apple Developer Program](https://developer.apple.com/programs/) membership, generate a sanctioned 6-month token instead of using browser sign-in.

1. **Get a MusicKit key.** [Apple Developer Portal → Keys](https://developer.apple.com/account/resources/authkeys/list) → **+** → name it, check **MusicKit**, Register → download the `.p8` (one time). Note your **Key ID** and **Team ID**.
2. **Configure** `~/.config/applemusic-mcp/config.json`:
   ```json
   {
     "team_id": "YOUR_TEAM_ID",
     "key_id": "YOUR_KEY_ID",
     "private_key_path": "~/.config/applemusic-mcp/AuthKey_XXXXXXXXXX.p8"
   }
   ```
3. **Generate and authorize:**
   ```bash
   applemusic-mcp generate-token   # developer token (180 days, auto-renews on use)
   applemusic-mcp authorize        # capture your user token
   applemusic-mcp status           # verify
   ```

---

## License

MIT · *Unofficial community project, not affiliated with Apple.*

<!-- Identifier for the official MCP Registry (PyPI ownership check). -->
mcp-name: io.github.epheterson/applemusic-mcp

## Credits

[FastMCP](https://github.com/jlowin/fastmcp) · [Apple MusicKit](https://developer.apple.com/documentation/applemusicapi) · [Model Context Protocol](https://modelcontextprotocol.io/)

---

Built with ❤️ in California by [@epheterson](https://github.com/epheterson) and [Claude Code](https://claude.com/claude-code).
