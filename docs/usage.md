# Usage patterns

Just talk to your assistant:

- *"Create a playlist called Road Trip and fill it with upbeat 90s alternative."*
- *"Add Hey Jude to my Road Trip playlist, and drop the last 3 tracks from my workout one."*
- *"Organize my playlists into Rock, Jazz, and Electronic folders."*
- *"Play my workout playlist on shuffle, and queue up Bohemian Rhapsody next."*
- *"Find songs similar to Bohemian Rhapsody and add them to my library."*
- *"What have I been listening to lately, and what's topping the charts?"*
- *"Export my library to CSV."*

## Patterns

- **`track` is one parameter that batches.** Pass a single name or ID, a comma- or newline-separated list, or a JSON array (`["A","B"]` or `[{"name":"A","artist":"X"}]`). Whole albums via `album`.
- **Importing a playlist from elsewhere? Resolve first, then add.** `catalog(action="resolve", …)` turns a track list into catalog IDs and writes nothing. Pass `isrcs=` when you have them — Spotify, Rekordbox, and Plex exports all carry ISRCs — and it matches *exactly*, 25 per request, instead of one fuzzy search per track (which is what puts a large import into `429` territory). Pass `tracks=` for titles/artists and it reports how confident each match is, so a wrong edition gets caught before it's written — that path costs one request per track, so it stops at 25 unless you raise `max_tracks`. Either way the IDs go straight to `playlist(action="add", track=…)`.
- **Previewing an add? `playlist(action="add", …, dry_run=True)`.** Same resolution the real add would do, plus a diff against what's already in the playlist, writing nothing. Ask for `Dont Let Me Down` without an artist and you get The Chainsmokers, not The Beatles — this is where you see that before it lands. It previews matching and duplicates only; it doesn't claim to predict whether the write itself succeeds.
- **Adding to a playlist** auto-searches the catalog and skips duplicates. Set the `auto_add` preference to `true` for "fill this playlist" workflows (default `false`). `track` also accepts a **catalog song id** (e.g. `1440857781`) to pin an exact edition when a name would be version-ambiguous.
- **Adding a not-yet-owned catalog track to a *Music.app-made* playlist is two-step.** The dev-token API can't write those playlists, so the tool adds the track to your library over the API, then attaches it locally once iCloud syncs it down (usually seconds). If the sync is slow you'll get "added to your library — re-run to attach"; just re-run the same add. Rarely, if the sync stalls past ~20s, Music.app briefly flashes as a last-resort sync nudge (focus returns to your previous app) — expected, not a glitch.
- **Output format** on list tools: `format` (`text` / `json` / `csv` / `none`), `export` (writes a file readable as an MCP resource via `exports://`), `full` (all metadata).
- **URL playback** handles albums, playlists, and songs: `playback(action="play", url="https://music.apple.com/...")`.
- **Storefronts:** catalog actions take an optional `storefront` (for example `storefront="it"`) to query other regions without changing your default.
