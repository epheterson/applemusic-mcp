# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.16.0] - 2026-07-02

Cross-platform playback and a broad hardening pass: library, playlists, *and playback* on macOS, Windows, and Linux.

### Added

- **Four playback engines, one `mode` knob** (`auto` / `native` / `safari` / `chrome` / `api`) plus a per-call `engine=` override. `auto` mixes the best per capability — Music.app playback + Safari queue + API data on macOS, Chrome elsewhere.
- **Safari playback + queue engine (macOS)** — drives your signed-in Safari's MusicKit to play songs/albums/playlists/URLs and manage Up Next, DRM-native and install-free. One macOS `login` covers data, native playback, and the Safari player.
- **Cross-platform playback** via a local Chrome web player (MusicKit + DRM): play, play-by-URL, pause/skip/seek, volume, shuffle, repeat, now_playing — any OS.
- **Up Next queue tool** — list / play-next / play-last / remove / jump / clear / autoplay, with drift-proof jump-by-track and a live `▶` marker.
- **Transactional playlist swaps** — `playlist(action="add", …, replace="<old>")` confirms the new track landed before removing the old one, so a swap never silently loses a track.
- **Sanctioned-first write routing** — writes prefer the official Apple Music API (developer token), use the web session only for what the public API can't do, and use local Music.app on macOS; each write reports the path it took.
- **Full API routing of library/playlist edits on any OS** — create/rename/delete playlists, add/remove tracks, single-level folders, love/dislike, catalog→library add, remove-from-library.
- **`now_playing`** shows position/progress and surfaces both engines when they disagree; **system-aware guidance** names only the engines available on your OS.
- **Conversational auth** via `config` (status / login / logout / reset), **self-renewing tokens**, and MCP tool annotations (read-only / destructive hints).

### Fixed

- Cross-platform hardening: the Chrome web player stays signed in across launches; graceful shutdown; the first-run browser download fails fast with guidance instead of hanging; honest off-mac messaging (preview-only labeling, correct capability labels, real error causes).
- Full-length DRM playback in the managed Chrome (was preview-only); the managed Chrome uses your real Keychain (Touch ID / passkeys / password managers) and keeps its sandbox on.
- Native catalog playback actually plays now (real mouse events, not no-op AXPress); clear errors for a stuck cloud/shared track (`-10006`), removing the currently-playing queue item, and catalog adds to Music.app-made playlists (now one call, with an active sync nudge).
- A multi-reviewer pass fixed several honest-error cases (auth status reflects the real write path; stale preference help text; defensive playlist listing; `repeat` accepts `none`/`off`).

### Security

- Token storage is auto-decided by platform: `0600` files on macOS/Linux, Credential Locker on Windows; config dir and browser profile are `0700`.

### Notes

- Browser playback needs a desktop session and Google Chrome (bundled Chromium has no DRM); audio plays on the machine running the server. A few features stay macOS-only (1–5★ ratings, favorites, AirPlay, nested folders, deletion).

## [0.15.1] - 2026-06-24

### Fixed

- Playlist delete works off-macOS / without Music.app — routes through the web session (the public API's `DELETE` 401s even with a paid token). Folders stay macOS-only.

### Changed

- GitHub Release titles are prefixed "Apple Music MCP vX.Y.Z".

## [0.15.0] - 2026-06-23

### Added

- **Catalog add over the unified Apple Music API on every platform** — works with a developer token (preferred, sanctioned) or a free web sign-in (`applemusic-mcp login`) that captures your session once and persists it.

### Changed

- Catalog add-to-library and add-to-playlist run over the API on every platform; the version-fragile UI automation (#37) was removed. Add-to-playlist resolves the library id and posts directly, replacing the AppleScript insert that raced iCloud sync.

### Removed

- The UI add helpers (`ui_add_to_library*`, `ui_add_to_playlist`); playback and play-from-URL are unaffected.

## [0.14.0] - 2026-06-17

### Added

- Genre search and paging for `library(action="search")` (macOS). Thanks @Tosd0 (#35).

### Fixed

- `library(action="search")` honors `limit`; a zero-match genre search returns "No tracks found" instead of a wrong "not available." Thanks @Tosd0 (#35).

## Earlier releases

- **0.13.0** (2026-06-10) — Favorites listing (`library(action="favorites")`).
- **0.12.2** (2026-06-09) — Hardened the local auth server against forged-token injection.
- **0.12.1** (2026-06-03) — Fixed an intermittent macOS 26 pop-over search failure.
- **0.12.0** (2026-06-02) — Library-add for obscure tracks on macOS 26 / new Music.
- **0.11.0** (2026-05-30) — Fixed playback for titles with typographic apostrophes.
- **0.10.5** (2026-05-13) — Fixed an `authorize` crash on Windows with a non-UTF-8 locale.
- **0.10.4** (2026-05-09) — Renamed the package/repo `mcp-applemusic` → `applemusic-mcp`.
- **0.10.3** (2026-05-05) — Adds complete in ~5s instead of ~19s.
- **0.10.2** (2026-05-04) — Rewrote the UI add with a canonical-match flow.
- **0.10.1** (2026-05-04) — Fixed `library(browse)` timing out on large libraries.
- **0.10.0** (2026-05-04) — Verify-after-modify on every write path.
- **0.9.6** (2026-05-01) — Dual-path search supporting macOS 15 and 26.
- **0.9.5** (2026-04-29) — Cleaner search messaging on macOS without a token.
- **0.9.4** (2026-04-29) — Playlist create no longer mis-cascades on an AppleScript error.
- **0.9.3** (2026-04-27) — One broken track no longer aborts library iteration.
- **0.9.2** (2026-04-12) — Clearer playlist-add messaging on macOS.
- **0.9.1** (2026-04-05) — Nested folder paths (`create(folder="A/B/C")`).
- **0.9.0** (2026-04-04) — Playlist folder management (create / delete / rename / move).
- **0.8.1** (2026-04-03) — Server starts without developer credentials.
- **0.8.0** (2026-03-27) — Catalog search / add / play from search results without a token.
- **0.7.0** (2026-03-26) — Play any Apple Music URL (album / playlist / song).
- **0.6.1** (2026-03-06) — Fixed the album param dumping a whole album into a playlist.
- **0.6.0** (2026-01-06) — Storefront parameter for discover actions.
- **0.4.3** (2026-01-05) — Unified fuzzy matching across playlists / tracks / albums.
- **0.4.2** (2026-01-02) — Fixed a library-lookup regression from 0.4.1.
- **0.4.1** (2026-01-01) — Pagination via an `offset` parameter.
- **0.4.0** (2025-12-30) — Unified `play` tool.
- **0.3.0** (2025-12-29) — Universal input-format auto-detection.
- **0.2.10** (2025-12-23) — `auto_search` works with batch add.
- **0.2.9** (2025-12-23) — Audit logging for destructive operations.
- **0.2.8** (2025-12-23) — Configurable storefront / region.
- **0.2.7** (2025-12-22) — `check_playlist` → `search_playlist`.
- **0.2.6** (2025-12-22) — Opt-in auto-search from the catalog.
- **0.2.5** (2025-12-22) — Track metadata caching.
- **0.2.4** (2025-12-21) — No-credentials mode on macOS.
- **0.2.3** (2025-12-21) — Inline CSV output.
- **0.2.2** (2025-12-20) — MCP Resources for exports.
- **0.2.1** (2025-12-20) — `remove_from_library` (macOS).
- **0.2.0** (2024-12-20) — AppleScript integration for macOS (16 tools).
- **0.1.0** (2024-12-15) — Initial release (REST API integration).
