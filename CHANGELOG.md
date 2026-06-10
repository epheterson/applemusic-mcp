# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.12.2] - 2026-06-09

### Security

External security audit + fixes contributed by **Timo Prager-Mai (@777Timo)**
([#32]) — thank you. Each fix verified and the pinned Action SHAs checked against
their upstream releases before merge.

- **CORS hardening (`auth.py`).** The temporary local auth server sent
  `Access-Control-Allow-Origin: *`, so during the short authorization window any
  webpage open in the browser could `POST localhost:{port}/save-token` and read
  back the Music User Token. Restricted to the exact `http://localhost:{port}`
  origin. (The same change also fixes a latent crash on a missing/malformed
  `Content-Length` header.)
- **Token files written `0o600` (`auth.py`).** `developer_token.json` and
  `music_user_token.json` are now owner-read/write only, so other users on a
  multi-user machine can't read the tokens.
- **`auth.html` cleanup (`auth.py`).** The auth page embeds the developer token;
  it's now `chmod 600` while it exists and deleted in a `finally` block after the
  flow completes, instead of lingering in `~/.config/applemusic-mcp/`.
- **Content-Length cap (`auth.py`).** The `/save-token` handler now rejects
  bodies over 64 KB (tokens are <1 KB), closing a local memory-DoS vector.
- **Third-party GitHub Actions pinned to commit SHA.** `checkout`, `setup-uv`,
  and `upload/download-artifact` are pinned to verified SHAs (with version
  comments) instead of mutable tags. `pypa/gh-action-pypi-publish` is left on its
  maintained `release/v1` branch on purpose — it's PyPA's first-party action
  using OIDC (no long-lived secret) and `release/v1` is where PyPA ships security
  patches; pinning it would opt out of those for little gain.
- **`config.example.json`:** `auto_search` set to `false` to match the safe code
  default (was `true`, which could surprise users copying the example verbatim).

## [0.12.1] - 2026-06-03

### Fixed

- **Intermittent pop-over search failure on macOS 26** (`"Can't get group 1 of
  toolbar 1 of window Music"`). `_open_search_popover` (the catalog→library add
  path) cached the toolbar search-field path but, unlike the results-page search
  path, never self-healed when that cached path went stale for the current view
  (e.g. after a prior op left Music on a playlist/detail view). It now
  invalidates the cache and re-navigates once on such an error. Surfaced by the
  new live pre-release gate.
- **Add-to-playlist could fail on a slow iCloud sync.** `ui_add_to_playlist`
  waited only 12 s for a freshly-added track to appear in the local library
  before the playlist `duplicate` step; the iCloud index can lag a fresh add by
  tens of seconds. Widened to 30 s so a slow-but-successful add isn't reported
  as a failure.

### Added

- **Local pre-release integration gate** (`make preflight` / `scripts/preflight.sh`
  / `tests/test_live_integration.py` / `RELEASING.md`). GitHub CI can't sign into
  Music.app, so the tokenless add paths were never validated before release —
  this runs them on a real signed-in Mac and refuses to pass if the cross-OS add
  flows didn't actually run. It immediately caught both fixes above.

## [0.12.0] - 2026-06-02

### Fixed

- **Tokenless library-add now works for obscure tracks on macOS 26 / new Music**
  ([#28]). The add flow resolved a track's canonical title via the free iTunes
  Search API but then queried Music's search autocomplete with the *raw* user
  query — and Apple's autocomplete is ranking-sensitive, so a vague query
  ("Lemons" / "Brye") surfaced popular homonyms and never the target. It now
  queries the pop-over with the **canonical title** ("LEMONS (feat. Cavetown)"),
  which makes the exact row appear. Validated end-to-end on macOS 26.5.
- **Wrong-track resolution when an artist was named.** The iTunes resolver
  weighted exact-title over artist, so `library(action="add", track="Lemons",
  artist="Brye")` could silently resolve to "Lemons" by *Hairitage* (exact
  title, wrong artist). Scoring is now **artist-primary**: a named artist is the
  strong signal, and a different artist's identically-titled song is rejected
  outright rather than added silently.
- **False-failure on a successful add.** The post-add verify checked only the
  local `library playlist 1`, which lags the iCloud add by ~3 s, so a real add
  could be reported as failed (and wrongly fall through to the macOS-15
  deep-link path, which can't navigate on macOS 26). The pop-over UI is now
  trusted as the authoritative success signal, with the library lookup as
  confirmation rather than a gate; the verify retry window was widened (3→6
  attempts) to outlast the index lag.
- **"Already in library" misreported as a failure.** When a track is already in
  the library, Music shows a "Download" button where "Add to Library" would be;
  the flow treated the missing Add button as an error. It now recognizes the
  Download button as "already in the library" (success) — on both the pop-over
  and deep-link paths.
- **macOS 15 deep-link add was completely broken by a reserved word.** The
  highlighted-row button finder used `set {bx, by} to position of b`, but `by`
  is an AppleScript reserved word (`repeat … by`), so the script was a silent
  syntax error and the finder returned None every time — the macOS-15 add never
  worked. Fixed (and guarded with a regression test); validated live on
  macOS 15.7.3 (Music 1.5.6). The hover→find and click steps were also made
  resilient (retry the hover-reveal; a second insurance click).

### Changed

- The macOS UI surface for catalog→library add is now **version-aware**: the
  search autocomplete pop-over on macOS 26 / new Music (in the accessibility
  tree there), and the resolved-URL deep-link on macOS 15 / old Music (where the
  pop-over is not in the accessibility tree but deep-links still navigate). Apple
  changed catalog deep-link handling in the new Music app so it no longer
  navigates to a browsable page — hence the split.

## [0.11.0] - 2026-05-30

### Fixed

- **Playback failed for tracks whose title has a typographic apostrophe**
  (U+2019, e.g. "That's a No No") even though search found them ([#26]). Root
  cause was an asymmetry between two Music.app engines: the native `search`
  command folds smart punctuation, but the `whose name contains` object-query
  filter used by playback is glyph-exact, so a straight-apostrophe query missed
  a curly-stored title (and vice versa). `osascript` also normalizes
  U+2019→U+0027 on stdout, so names returned by search re-queried the curly
  database and missed.

### Changed

- **Track resolution for play/love/dislike/rate/reveal is now a two-stage
  matcher.** Stage 1 is the fast glyph-exact `whose` filter (now robust to
  apostrophe/quote/ellipsis variants by matching the quote-free fragments of
  the title). Stage 2, only on a miss, bulk-fetches every track name in one
  Apple Event and folds in-memory under AppleScript's native
  `ignoring punctuation and diacriticals` — matching curly quotes, ellipses,
  and accents (café ≈ cafe) via Apple's Unicode tables. The bulk fetch is
  essential: per-track property access in a loop is ~1 Apple Event each
  (seconds on a large library); the bulk fetch is one event (~instant).
- **One canonical normalized matcher at every point a user string is compared
  to a candidate** (server side). `_normalize_for_match` now folds diacritics
  (NFD) in addition to quotes/`&`/punctuation, and a new `_loose_contains` /
  `_loose_equals` pair replaces the ad-hoc `x.lower() in y.lower()` guards
  across playback, library add, rating, search filters, and playlist
  resolution — so the same smart-quote/accent class of bug can't recur at other
  entry points.

### Notes

- The generic fuzzy matcher (`_fuzzy_match_entity`, used for catalog/playlist
  matching) keeps its own normalization core (`_normalize_with_tracking`),
  now behavior-aligned with `_normalize_for_match`. Consolidating both onto a
  single core remains a follow-up.

[#26]: https://github.com/epheterson/applemusic-mcp/issues/26
[#28]: https://github.com/epheterson/applemusic-mcp/issues/28
[#32]: https://github.com/epheterson/applemusic-mcp/issues/32

## [0.10.5] - 2026-05-13

### Fixed

- **`applemusic-mcp authorize` crash on Windows with non-UTF-8 system locale**
  (e.g. Chinese GBK). The auth HTML page contains a `✓` glyph; writing it to
  disk used Python's locale-default codec, which raised
  `UnicodeEncodeError: 'gbk' codec can't encode character '✓'`. The file
  is now opened with `encoding="utf-8"`, and the local auth server advertises
  `Content-Type: text/html; charset=utf-8` so browsers render the glyph
  correctly.

## [0.10.4] - 2026-05-09

### Renamed

- **PyPI package and GitHub repo renamed** from `mcp-applemusic` to `applemusic-mcp`.
  - Install: `pip install applemusic-mcp`
  - GitHub: https://github.com/epheterson/applemusic-mcp (old URL auto-redirects)
  - The `mcp-applemusic` name on PyPI was registered by an unrelated project
    in February 2025, blocking us from publishing under that name. v0.10.4 is
    the first PyPI release under the new name.
- **CLI command `applemusic-mcp` is unchanged** (was already this name).

## [0.10.3] - 2026-05-05

### Faster

- Adding a track (to a playlist or just to your library) now takes ~5 seconds instead of ~19.

### Better at finding tracks

- Picks the canonical recording (preferring it over remixes, lives,
  and covers), disambiguates by artist when supplied, and fails with
  a clear error when no canonical match exists — no more silent
  wrong-track adds.
- Empty searches return no results (was treating Apple's "No results"
  placeholder as a fake hit).

### More reliable

- Music.app is auto-launched and restored to a usable state if it's quit,
  windowless, or stuck on an album/song page.
- v0.10.2 was tagged but never reached PyPI (CI permissions gap). v0.10.3
  ships v0.10.2's improvements alongside its own.

### Better release notes

- GitHub Releases now show this changelog instead of auto-generated PR lists.

## [0.10.2] - 2026-05-04

### Changed

- **`ui_add_to_library` rewritten with popover-canonical-match flow.** The previous Top Results-based approach was unreliable on macOS 26: Apple's Top Results ranking lottery sometimes surfaced only Album rows for our query, sometimes the Song row had no Add to Library button (only `[play]` + `[More]`), and "Hong Kong" by Gorillaz / Holocene by Bon Iver consistently failed to expose the per-row +. The new flow uses Music.app's autocomplete pop-over (the suggestions that appear as you type) which exposes structured `{Title}` + `Song · {Artist}` rows — canonical (name, artist) match, no ranking ambiguity. CoreGraphics-clicking the matched pop-over row navigates to the song's detail page where the per-row Add to Library button has stable accessibility description `"Add to Library"`. Validate-before-act at every step: pop-over visible, row matched, navigation completed (track group present in album section), button visible after hover with the EXACT expected description before clicking. Refuses to click anything else (Favorite, Download, More, Bon-Iver-artist-link). Signature change: `ui_add_to_library(name, artist="")` — artist is now a meaningful parameter for canonical disambiguation.
- **`ui_add_to_playlist` removed its Top Results pre-search** — now goes straight to popover via the rewritten `ui_add_to_library`. Splits free-form `"Track - Artist"` queries automatically; strips trailing artist tokens when artist is supplied separately.

### Fixed

- **Mid-poll re-hover in `_hover_then_click_subelement`** to catch macOS 26 dropped `mouseMoved` events. The 2-pixel nudge wasn't always sufficient — empirically observed that hover-revealed buttons sometimes failed to appear within the 1.5s poll window. New behavior re-hovers halfway through the poll if the inner element still isn't visible.

### Internal

- **Live UI integration tests** (opt-in via `TEST_UI=1` + `-m ui`) — `TestUIFlowsLive` covers the full search → add-to-library → add-to-playlist chain. Candidate-track fallback (picks the first of several known-indexed tracks not already in your library AND surfacing as Song row in pop-over). Strict per-test cleanup removes the test track from playlist + library; `teardown_class` deletes `_UI_TEST_PLAYLIST_`. Verified 3 passed locally on macOS 26.
- **Auto-release + auto-publish chain validated end-to-end** — pipeline landed in [#18](https://github.com/epheterson/mcp-applemusic/pull/18) and [#19](https://github.com/epheterson/mcp-applemusic/pull/19): pushing a version bump to `main` now auto-creates the matching tag + GitHub Release, then dispatches `publish.yml` which uploads the sdist/wheel to PyPI via Trusted Publishing. v0.10.2 is the proving release for the publish step.

## [0.10.1] - 2026-05-04

### Fixed

- **`library(action="browse")` no longer times out on libraries larger than the page limit** — `_library_browse` was fetching only `limit` songs from the top via AppleScript and then slicing, so any request with `offset > 0` left nothing to slice (and `offset=450` on a >500-track library hit a 30s AppleScript timeout). New `get_library_songs_page(offset, limit)` uses `items N through M of trackList` for O(limit) range access (~0.6s vs ~30s for large offsets). Reported and fixed by [@mik-rzo](https://github.com/mik-rzo) in [#15](https://github.com/epheterson/mcp-applemusic/pull/15).
- **`_apply_pagination` treats `offset == total_count` as a valid "end of list"** rather than an error — required by cursor-style pagination through full library scans. (Pre-existing semantic bug exposed by the new paginated path.)

### Internal

- Extracted `_build_library_track_data` helper to dedupe the track-dict construction + explicit-enrichment + clean_only filter logic that was copy-pasted across two AppleScript branches.
- `clean_only=True` now routes through the full-fetch path so the displayed `X of Z` total reflects the post-filter count rather than the raw library size.
- Defensive guard in `get_library_songs_page` rejects `limit <= 0` before any AppleScript runs (the implicit `items 1 through 0` range was invalid).

### Documentation

- Added neutral first-party badges (PyPI version, supported Python versions, monthly downloads via pepy.tech, License, macOS, MCP) to the top of the README.
- Added `[UI Catalog]` to the documented `playback(action="play")` response prefixes (was inadvertently omitted from the v0.10.0 docs even though the prefix was wired through that release).
- Documented the screen-unlocked requirement for UI flows.
- Brief note in Limitations about the [known Music.app/AppleScript playlist-revert bug](https://www.macscripter.net/t/add-current-track-from-apple-music-to-playlist/72058) and how the MCP detects + reports it.

## [0.10.0] - 2026-05-04

This release consolidates the macOS UI automation paths onto a small set of shared primitives and tightens every data-modifying operation with post-mutation verification. The headline outcome: no more silent false-positive success messages when AppleScript reports OK but the change doesn't actually persist (a real failure mode some user-created playlists exhibit, where iCloud-side reconciliation reverts local AppleScript edits).

### Added

- **Verify-after-modify on every playlist + library data path** — `_playlist_add` (both names path and IDs path), `ui_add_to_playlist`'s final step, `_playlist_remove` (all 4 input-type branches), `_library_remove` (all 4 input-type branches), and `_library_add_track_via_ui` now confirm the change actually persisted before reporting success. On verify miss the call retries once with sync-lag delay; if still missing, the user sees a clear actionable error ("Some user-created playlists silently revert AppleScript edits server-side; adding manually via Music.app's right-click → Add to Playlist usually works") instead of a misleading "Added 1 track(s)" with nothing in the playlist. The previous behavior could return success three times in a row for the same track that never actually landed (caught during live testing — Lionel Richie "Hello" added to a `canEdit:false` user-created playlist).
- **`playlist_tracks` paginated header now shows true total on the API path** — when a `limit` is set on the API-optimized branch (used when both `api_id` and `applescript_name` resolve, or when only `api_id` is available), the header now reads `=== 1-200 of 436 tracks ===` rather than `=== 200 tracks ===`. Captured from `meta.total` on the first /tracks API response — no extra API call. The AppleScript-only branch (used in tokenless mode and for playlists with no API ID) was already correct: it fetches the full list, then `_apply_pagination` records the pre-slice count, so the header has always shown the true total there. This fix closes the API-path gap. Prevents callers (including AI agents) from treating a partial view as authoritative when surveying playlist contents. Same hard-rule lesson Eric called out: "don't define a limit, and even if a limit is defined the true count is returned."
- **`_SEARCH_FIELD_TOOLBAR_FLAT` variant** — third toolbar layout for macOS 26 builds where the search text field sits directly under `toolbar 1` without a wrapping `group 1`. The dual-path probe in `_get_search_field()` now tries grouped → flat → sidebar in order, caches the first hit, and picks up future toolbar variants without code changes if needed.
- **TestUIPrimitives** — 13 new mock-based unit tests covering each new internal primitive (`_focus_search_field`, `_wait_for_top_results`, `_parse_top_results`, `_find_top_result_position`, `_hover_then_click_subelement`, `_verify_track_playing`).

### Changed

- **UI automation refactored onto shared primitives** — `applescript.py` now exposes a small set of internal primitives that compose into every `ui_*` public entrypoint. Each macOS-specific quirk (toolbar layout variants, autocomplete popover dismissal, hover-then-click row mismatches, post-click verification timing) gets fixed in exactly one place. Public API is unchanged. `ui_search_catalog` shrank from ~150 lines to ~12; `ui_play_result` and `ui_add_to_library` are now thin wrappers around `_hover_then_click_subelement` (which polls for the inner sub-element to appear and CoreGraphics-clicks its exact pixel position — solving the play-checkbox-clicks-wrong-row bug along the way).
- **`_try_ui_catalog_play` helper extracted** — three duplicated UI-Catalog play branches in `_play_from_catalog` (server.py) collapsed into one helper with `source_label` + `prefix` parameters. The `(False, msg)` vs `(False, None)` return discriminates "UI tried and failed" (surface the inline `[UI Catalog failed: ... ] Falling back —` reason) from "UI not available" (fall through to next path) — preserving exact previous behavior of all three call sites.
- **Search popover self-heal** — when results don't appear within ~1.2s after Enter, the autocomplete popover may still be covering them; the poll loop now sends one recovery Enter to dismiss it (matches the manual "two Enters" Eric observed). Replaces the previous fixed 4-second sleep, so search returns as soon as results render.

### Fixed

- **Search field path discovery now used everywhere** — `ui_search_catalog`'s consolidated AppleScript previously hardcoded `text field 1 of group 1 of toolbar 1 of window "Music"` instead of calling `_get_search_field()`. On macOS 26 builds where the toolbar lacks `group 1` (the new flat variant above), the search would error immediately with no fallback. Search now resolves the path at call time using the same probe everything else uses.
- **Race-resistant catalog play** — `ui_play_result` previously re-found the result row by description in a second AppleScript and called `click checkbox 1 of e`, which sometimes landed on the wrong row when descriptions were similar (resulting in "Clicked play but got 'Other Track' instead of 'Target Track'"). The new path queries the play checkbox's exact pixel position after hover and CoreGraphics-clicks at that pixel — deterministic, no AppleScript element-resolution ambiguity. Verified: same flow that intermittently played the wrong track now plays the requested one reliably.
- **Result waits are now poll loops** — fixed sleeps of 1.5s (post-hover) and 2.0s (post-click) replaced with poll-for-state loops. Best case 10× faster on a responsive Music.app; worst case identical to the old fixed sleep.

### Internal

- Net code change: ~+961 / −193 across `applescript.py`, `server.py`, and the test suite. `applescript.py` UI-automation section is materially smaller and more readable after the duplication collapse. Test count: 301 → 315 (+13 new + 1 toolbar variant).

## [0.9.6] - 2026-05-01

### Fixed

- **Dual-path search-field probe supports both macOS 15 and macOS 26 (Tahoe)** — on macOS 26, the Music.app search field moved from the sidebar outline row to `text field 1 of group 1 of toolbar 1 of window "Music"`. Rather than hardcoding either path, `_get_search_field()` probes for the toolbar element at runtime using an AppleScript `exists` check and falls back to the sidebar path when not found. Result is cached per server session so the probe runs at most once. Verified on macOS 15 (sidebar path correctly detected); toolbar path validated by horrorshow75 on macOS 26 (Tahoe). Diagnosed and reported by horrorshow75 on Reddit.
- **Catalog result type-line parser handles macOS 26 extra static text node** — macOS 26 prepends an empty static text element inside each result row, shifting the type line (e.g. "Song · Radiohead") from the second to the third static text. The old code used a fixed `item 2` index; the new code iterates and picks the first static text containing the middle-dot separator `·`, making it version-agnostic.
- **Hover dwell increased and nudge added at 3 UI-interaction sites** — on macOS 26, `CGEventMouseMoved` events posted to the same coordinates as the previous position are silently dropped, so hover-dependent buttons (Add to Library, play checkbox) never appeared. A small 2-pixel nudge move before the final position guarantees a real delta and event delivery. Dwell increased from 0.5–1.0 s to 1.5 s at all three sites.

## [0.9.5] - 2026-04-29

### Fixed

- **`library(action="search")` no longer leaks "Developer token not found" on tokenless macOS when the search returns zero hits** — same bug class as v0.9.3 / v0.9.4, missed by both sweeps. The early-return guard in `_library_search` was `if success and results:`, which fails on a clean empty list and falls through to the API path. On a tokenless macOS host, that path raises `FileNotFoundError("Developer token not found...")` for what was actually just "song's not in your library." A Claude session reading the leaked error then (correctly) tells the user to run `applemusic-mcp generate-token` and `applemusic-mcp authorize` — sending tokenless users back down the developer-account rabbit hole the README explicitly says they don't need. Reported on Reddit by horrorshow75 immediately after testing v0.9.4. Fix: empty AS results now return a hint pointing to `catalog(action='search')` instead of cascading to the API. When a token IS configured the cascade still fires (the API may see cloud-synced tracks AS hasn't seen yet).

### Added

- **`library(action="add")` now works tokenlessly on macOS via UI automation** — the actual root cause of horrorshow75's report. His Claude session likely tried `library(action="add", track="<song>")` to add catalog tracks to library *before* adding to playlist, and the API-only `_library_add` path leaked the developer-token error. The README has always promised tokenless macOS works for "most features" — `library(action="add")` was a gap. New helper `_library_add_track_via_ui` orchestrates `asc.ui_search_catalog` + `asc.ui_add_to_library` (the same primitives `ui_add_to_playlist` already uses). For tracks by name on a tokenless macOS host, the function now actually completes the add instead of returning a misleading auth error.
- **Tool-routing hints when error suggests user is in the wrong place** — `library(action="search")` empty-result message now points at `catalog(action='search')`. `playlist(action="add")` "Track not found" without `auto_search` now suggests setting `auto_search=True`. Tool docstrings updated so a Claude session reading the registry alone (before any error) knows when to use which action.
- **SKILL.md "Tool routing" section** — table mapping user goals to the right MCP action, with explicit note that "No songs found in library" is NOT a hint to set up an API token.

### Internal

- 8 new regression tests across 2 new test classes (`TestLibrarySearchEmptyDoesNotLeakToken`, `TestLibraryAddUiOnTokenlessMacos`) covering the empty-AS cascade case, the UI library-add happy path, UI-search-empty and album-input fallbacks, and the catalog-ID-without-token guard.
- Pre-fix sweep covered every `if APPLESCRIPT_AVAILABLE:` block in server.py for similar empty-cascade patterns (an Explore subagent did the first pass; a feature-dev:code-reviewer agent independently re-swept to verify). Only `_library_search` was vulnerable — all other sites either return "no results" directly on empty AS or have no API fallback at all.

## [0.9.4] - 2026-04-29

### Fixed

The v0.9.3 sweep missed several callsites in the same bug class — tokenless macOS paths leaking "Developer token not found" when AppleScript failed or when an API call sat upstream of an AS-only path. A user reported on Reddit that `playlist(action="create")` still surfaced the misleading token error. Each callsite below is now fixed individually:

- **`_playlist_create` (`playlist(action="create")`)** — no longer cascades to the API path on AS failure. AS error surfaces directly via `_format_applescript_error` with actionable guidance.
- **`_playlist_list` (`playlist(action="list")`)** — same fix. The API path returned a strict subset (only API-visible playlists), so cascading wasn't even useful on macOS.
- **`_library_browse` (`library(action="browse")`)** — same shape, same fix. Caught in the second review pass.
- **`_playlist_copy` (`playlist(action="copy")`)** — refactored to defer `get_headers()` to the API-mode (by-ID) branch only. AS-mode (by-name) is now strictly tokenless on macOS.
- **`_playlist_add` track-ID path** — `playlist(action="add", track="<catalog_id>")` requires the API to resolve metadata, but previously called `get_headers()` unconditionally. Now gated on `_has_developer_token()` with a specific "use track name instead" message when no token is configured.
- **`_playlist_add` album path** — `playlist(action="add", album="X")` requires the API to fetch the album's tracklist. Same gate, same shape.
- **`_library_rate` gated fallthrough** — `rate(action="love"|"dislike")` AS failure now classifies before cascading. Environmental errors (Music.app not running, Automation denied, timeout) surface actionable messages; logic-level errors (track not found) still cascade so the catalog API can rate songs not downloaded locally.
- **`_library_rate` get/set + `_playlist_tracks` + `_playlist_search`** — AS failures now run through `_format_applescript_error` instead of surfacing raw osascript stderr. No token leak (these paths don't cascade), but the error message now tells users HOW to fix it (open Music.app, grant Automation permissions, etc.).

### Added

- **`asc.classify_error(text)`** — categorizes AppleScript error strings into stable categories (`music_not_running`, `automation_denied`, `timeout`, `syntax`, `unknown`) by matching numeric error codes (-609, -1743, -10810) and stable phrases (`Connection is invalid`, `not authorized`, `not allowed assistive`, etc.). `-1728` ("can't get") is deliberately left as `unknown` — it's a logic-level error (track/playlist doesn't exist), not environmental, so callers with legitimate API fallbacks can still cascade. Used to decide whether an AS failure should block API cascade (environmental errors do; logic errors don't).
- **`_format_applescript_error(raw, operation)`** — translates a raw AppleScript stderr into a user-facing actionable message. Tells users to open Music.app for `-609`, opens the path to System Settings → Privacy & Security → Automation for `-1743`, etc. Includes the operation context so users know which call surfaced the problem.
- **`_playlist_copy` AS-mode no longer requires a developer token** — refactored to defer `get_headers()` to the API-mode (by-ID) branch only. The AS-mode (by-name) branch is now strictly tokenless on macOS, matching the README's promise.

### Internal

- 27 new unit tests across 9 new test classes (verified by `grep -cE '^\s+def test_' tests/test_server.py` against origin/main: 143 vs 116) covering `classify_error` categorization (codes + phrases + unknown fallback), `_format_applescript_error` per-category messaging, and regression tests confirming none of the fixed callsites (`_playlist_create`, `_playlist_list`, `_library_browse`, `_library_rate` love/dislike, `_playlist_add` track-ID path, `_playlist_add` album path, `_playlist_copy`) leak the developer-token error on AS environmental failures, plus a test for the `"not allowed"` overmatch fix in the classifier. Total suite: 279 passed, 3 skipped (282 collected).
- Existing test `test_fallback_to_api_when_applescript_fails` was capturing the prior bad behavior — rewritten to assert the new contract: AS failure on macOS surfaces actionable error, not silent API cascade with misleading token error.
- SKILL.md updated with a "Common Failures" categorization table aligned with `classify_error` so a Claude session reading the skill teaches its user the same env-vs-logic error categorization the package now produces.

## [0.9.3] - 2026-04-27

### Fixed

- **One broken track no longer aborts library iteration** — `get_library_songs`, `search_library`, `search_playlist`, and the playlist-level loop in `library_snapshot` previously bailed on the first AppleScript error -1728 ("Can't get") thrown by an inaccessible track reference. The loop now wraps the per-track property reads in `try/on error` and skips the offending track instead of returning empty. When the AppleScript path silently bailed, `_library_search` would fall through to the API and surface a misleading "Developer token not found" — completely hiding what was actually wrong. Diagnosis credit: mik-rzo (PR #11, withdrawn before testing).
- **`_library_search` surfaces both failures when both paths fail** — when AppleScript fails AND the API call fails (or returns zero songs), the message now includes the AppleScript error alongside the API error so the real cause isn't hidden behind a misleading token message.
- **`_unified_auto_search_to_playlist` no-paths-available message is platform-aware** — replaced the legacy "need API token or macOS with Accessibility" with a darwin-specific "AppleScript unavailable (Music.app + Accessibility permissions required)" or non-darwin "API token required" so users see what's actually missing for their platform, not both.

### Added

- **`_has_developer_token()` feature-detection helper** — extracts the inline probe pattern from `_unified_auto_search_to_playlist` so future callsites can choose between API and AppleScript paths without raising the misleading token error. Returns False on any exception from `get_developer_token()`.
- **`_smart_as_add_track_to_playlist` verifies split-resolved adds** — when a "Song - Artist" combined input is split into candidates and one matches, `_verify_track_in_playlist` now confirms the right track actually landed before claiming success. On verify-fail returns False with a "may have landed; check manually" message rather than cascading to the next candidate (which could compound a second wrong-track add if iCloud sync is just slow). First-attempt success still skips verification — preserves latency profile and existing behavior for the common path.

### Internal

- Renamed local variable `shits` → `s_hits` in the library-sync poll loop. Cosmetic.
- Black formatter applied to `applescript.py` and `test_applescript.py` (split into separate prep commits so the logic diffs stay reviewable).
- Test `test_create_and_delete_playlist` made hermetic against leftover state — Music.app allows duplicate playlist names, so prior failed runs could leave a stale `_TEST_PLAYLIST_DELETE_ME_` that cascaded into future-run failures. Now drains by name before creating.
- 13 new unit tests covering the probe helper, split-resolved verify behavior, platform-aware error message, AppleScript per-track wrap presence in generated scripts, and AppleScript-error surfacing in `_library_search`.

## [0.9.2] - 2026-04-12

### Fixed

- **`playlist(action="add")` surfaced misleading "Developer token not found" on macOS** — when `auto_search=True` and the track couldn't be found in the local library, the fallback path unconditionally hit the API and bubbled the token error up, even though macOS has a full UI-automation catalog path that needs no API credentials. Now the auto-search fallback prefers API when a token is present, falls back to UI automation via `asc.ui_add_to_playlist` when not, and only returns a token-related error if both paths are genuinely unavailable.
- **API auto-search failed on non-API-created playlists** — `_auto_search_and_add_to_playlist` added the resolved track to the playlist via `POST /me/library/playlists/{id}/tracks`, which returns HTTP 500 for any playlist not originally created by the API (most user playlists). On macOS the playlist-add step now goes through AppleScript after polling `search_library` for iCloud→local sync propagation, so any playlist works. API playlist-add remains as a last-resort fallback for headless / non-macOS setups.
- **Empty-artist false-match in local sync poll** — the library-visibility check used a substring test that matched empty strings against anything; a track added with no artist metadata could select the wrong library hit. Now guards on a minimum-length artist and trusts the first name hit when artist is unusable.

### Added

- **Open-ended `track` input** — `playlist(action="add", track="Silvera - GOJIRA")` now works without a separate `artist` parameter. The new `_split_track_artist_candidates` helper splits on ` - ` and tries forward (Song - Artist) and reverse (Artist - Song) candidate pairs, plus last-dash variants for multi-dash names. Only activates when the caller provided no artist/album filter and the first lookup hit a "Track not found" error — explicit caller intent is always respected.
- **Post-add playlist verification** — both the API and UI auto-search paths now verify via `track_exists_in_playlist` that the intended track actually landed in the target playlist before claiming success. Catches two real failure modes: AppleScript propagation lag (retries once on verify miss) and silent UI misclicks where Music.app's search state led the automation to add the wrong song.
- **Error folding on cascade fail** — when both API and UI auto-search paths fail, the returned message now includes both reasons (`"{ui_error} (API attempt: {api_error})"`) so users can tell what broke where.
- **Tuning constants at module level** — `_LIBRARY_SYNC_DEADLINE_S`, `_LIBRARY_SYNC_TICK_S`, `_VERIFY_ATTEMPTS`, `_VERIFY_DELAY_S` for easy adjustment.
- **7 unit tests** for `_split_track_artist_candidates` covering forward/reverse forms, no-separator, `Jay-Z`-style plain hyphens, multi-dash splits, whitespace trimming, and empty-part rejection.

## [0.9.1] - 2026-04-05

### Added

- **Slash-separated folder paths** — `create(folder="Summer/Chill/Deep")` creates nested folder hierarchies. Works with `move`, `delete`, and `create`. Intermediate folders created automatically.
- **`path` action** — `playlist(action="path", playlist="Road Trip")` returns the full path like `Summer/Chill/Road Trip`. No args shows the full folder hierarchy (macOS).
- **Move-out-of-folder** — `move(playlist="...", folder="")` moves a playlist back to the top level (macOS).
- **API fallback for folder creation** — `create(folder="...")` works on non-macOS via `POST /v1/me/library/playlist-folders` (single-level only).
- **Folder-into-folder nesting** — Moving folders into other folders works via the shared playlist lookup helper.
- Audit log display for `move_to_root` entries.
- `get_playlist_parent` renamed to `get_playlist_path` — returns full slash-separated path instead of just immediate parent.
- Integration tests for nested folder creation, move-to-root, get-playlist-path, folder tree.

### Fixed

- **Security: AppleScript injection via newline characters** — `_escape_for_applescript()` now strips `\n`, `\r`, and `\t` from all user input. Previously, a newline in a playlist/folder name could break out of an AppleScript string literal. Affects all versions prior to 0.9.1.

### Changed

- **Snapshot format** — Playlists in snapshots now include folder path info (`{"folder": "...", "tracks": [...]}`). The `library_diff` function handles both old (list) and new (dict) formats for backward compatibility. Snapshots also track folder additions/removals.

## [0.9.0] - 2026-04-04

### Added

- **Playlist folder management** — Create, delete, rename, and move playlists into folders via AppleScript. Contributed by @jamisonbryant in PR #4.
- **Unified folder parameter** — `create(folder="...")` creates a folder. `create(name="...", folder="...")` creates a playlist inside a folder. `delete(folder="...")` and `rename(folder="...", new_name="...")` target folders explicitly. `move(playlist="...", folder="...")` moves a playlist into a folder.
- **Audit log for preferences** — `config(action="set-pref")` changes are now logged.
- **Audit log for folder operations** — create, delete, rename, and move folder operations logged.
- **Audit log rotation** — Log file automatically rotates at 10 MB to prevent unbounded growth.

### Changed

- `create_folder` action still works for backward compatibility but `create(folder="...")` is the preferred form.
- Playlist lookup (`_find_playlist_applescript`) now falls back from user playlists to folder playlists (exact then partial match).

## [0.8.1] - 2026-04-03

### Fixed

- **No credentials? No crash.** Server now starts without Apple Developer credentials. AppleScript features (playback, library, playlists) work out of the box. API features (catalog search, recommendations) fail gracefully at call time instead of crashing on startup.

## [0.8.0] - 2026-03-27

### Added

- **UI Catalog Free Tier** — catalog search, add-to-library, and play from search results all work without an API token via Music.app UI automation. Falls back automatically when API is unavailable.
- **Library Snapshots** — `library(action="snapshot")` captures full library state (tracks, playlists, playback) as a baseline. Subsequent calls diff against it. Sub-commands via `query`: `new`, `history`, `list`, `delete`.
- **Audit logging for playback** — all play, pause, skip, volume, shuffle, repeat, and AirPlay operations now logged to the audit log.
- **UI add-to-playlist** — composite flow: search catalog via UI, add to library via hover+click, wait for iCloud sync, then add to playlist via existing AppleScript backend.

### Notes

- UI automation features require macOS with a display (not headless), Music.app visible, and Accessibility permissions for System Events.
- Library snapshots store one full baseline + lightweight diffs. Diffs auto-rotate at 50.
- Search results show Top Results only (not full catalog categories).

## [0.7.0] - 2026-03-26

### Added

- **URL playback** — `playback(action="play", url="...")` plays any Apple Music URL directly. Supports albums, editorial playlists, personal playlists, and specific songs via `?i=` parameter. Uses UI scripting to auto-click the Play/Shuffle button across different page layouts.
- **Specific song playback via `?i=`** — Album URLs with `?i=songId` find the highlighted track row, hover via CoreGraphics to reveal the per-track play checkbox, and click it. Auto-scrolls off-screen tracks into view.
- **Song URL conversion** — `/song/` URLs are automatically converted to `/album/?i=` format via the Apple Music API when available. Without API, returns a helpful error with the correct URL format.
- **Shuffle support for URL playback** — `playback(action="play", url="...", shuffle=True)` clicks the Shuffle button instead of Play.
- Zero new dependencies — CoreGraphics mouse events generated via `osascript -l JavaScript` (JXA).

### Notes

- URL playback requires macOS with a display (not headless), Music.app visible (not minimized), and Accessibility permissions for System Events.
- For `?i=` song playback, the mouse cursor will briefly move to click the track row.
- Inspired by [PR #2](https://github.com/epheterson/mcp-applemusic/pull/2) from @hummusonrails.

## [0.6.1] - 2026-03-06

### Fixed

- **Album param dumps entire album into playlist** — When both `track` and `album` are provided to `playlist(action="add")`, the `album` param now acts as a disambiguation filter instead of adding all tracks from the album. Album-only adds (no `track`) still work as before.
- **Artist disambiguation picks wrong version** — `add_track_to_playlist` now uses exact artist match (`artist is`) first, falling back to partial match (`artist contains`) only if exact fails. Prevents "The Wiggles" from matching "Dorothy the Dinosaur & The Wiggles".
- **Library IDs fail with 403 on non-API playlists** — Library IDs (e.g., `i.abc123`) now correctly route through AppleScript for playlists not created via the API, instead of falling through to the API endpoint which rejects them.

### Improved

- `add_track_to_playlist` now accepts an optional `album` parameter for disambiguation when multiple versions of a track exist in the library.
- AppleScript add responses now include album and artist info for better feedback (e.g., "Added Hot Potato (Ready, Steady, Wiggle!) by The Wiggles").

## [0.6.0] - 2026-01-06

### Breaking Changes

**MCP Tool Consolidation** - Reduced from 37 to 5 action-based dispatchers to minimize MCP context footprint (80%+ reduction):

#### Playlist Operations → `playlist(action=...)`

| Old Tool | New Call |
|----------|----------|
| `get_library_playlists()` | `playlist(action="list")` |
| `get_playlist_tracks(playlist, ...)` | `playlist(action="tracks", playlist=playlist, ...)` |
| `search_playlist(query, playlist)` | `playlist(action="search", query=query, playlist=playlist)` |
| `create_playlist(name, description)` | `playlist(action="create", name=name, description=description)` |
| `add_to_playlist(playlist, ...)` | `playlist(action="add", playlist=playlist, ...)` |
| `copy_playlist(source, new_name)` | `playlist(action="copy", source=source, new_name=new_name)` |
| `remove_from_playlist(...)` | `playlist(action="remove", ...)` _(macOS only)_ |
| `delete_playlist(name)` | `playlist(action="delete", name=name)` _(macOS only)_ |

#### Library Operations → `library(action=...)`

| Old Tool | New Call |
|----------|----------|
| `search_library(query, ...)` | `library(action="search", query=query, ...)` |
| `add_to_library(track, album, ...)` | `library(action="add", track=track, album=album, ...)` |
| `get_recently_played(...)` | `library(action="recently_played", ...)` |
| `get_recently_added(...)` | `library(action="recently_added", ...)` |
| `browse_library(item_type, ...)` | `library(action="browse", item_type=item_type, ...)` |
| `rating(rate_action, ...)` | `library(action="rate", rate_action=rate_action, ...)` |
| `remove_from_library(...)` | `library(action="remove", ...)` _(macOS only)_ |

#### Catalog Operations → `catalog(action=...)`

| Old Tool | New Call |
|----------|----------|
| `search_catalog(query, ...)` | `catalog(action="search", query=query, ...)` |
| `get_album_tracks(album, ...)` | `catalog(action="album_tracks", album=album, ...)` |
| `get_song_details(song_id)` | `catalog(action="song_details", song_id=song_id)` |
| `get_artist_details(artist)` | `catalog(action="artist_details", artist=artist)` |
| `get_song_station(song_id)` | `catalog(action="song_station", song_id=song_id)` |
| `get_genres()` | `catalog(action="genres")` |

#### Discovery → `discover(action=...)`

| Old Tool | New Call |
|----------|----------|
| `get_recommendations(...)` | `discover(action="recommendations", ...)` |
| `get_heavy_rotation(...)` | `discover(action="heavy_rotation", ...)` |
| `get_charts(chart_type)` | `discover(action="charts", chart_type=chart_type)` |
| `get_artist_top_songs(artist)` | `discover(action="artist_top_songs", artist=artist)` |
| `get_similar_artists(artist)` | `discover(action="similar_artists", artist=artist)` |
| `get_search_suggestions(term)` | `discover(action="search_suggestions", term=term)` |
| `get_personal_station()` | `discover(action="personal_station")` |

#### Configuration → `config(action=...)` _(unchanged)_

Playback tools (`play`, `playback_control`, `playback_settings`, `get_now_playing`) remain unchanged.

**Rationale:** Reduces MCP context overhead from 37 tools to 5 dispatchers, enabling more efficient token usage for Claude and other LLM clients. Each dispatcher accepts a superset of parameters and routes to internal implementation functions.

**Migration:** All functionality preserved. Update MCP client code to use action-based API.

### Added

- **Storefront parameter for discover actions** - All catalog-based discover actions (`charts`, `top_songs`, `similar_artists`, `song_station`) now accept an optional `storefront` parameter to query other regions without modifying your default storefront setting. No more 3-step workflow for international queries!
  ```python
  discover(action="charts", chart_type="songs", storefront="it")  # Italy charts
  ```

- **Album details action** - New `catalog(action="album_details")` provides complete album information including metadata and full track listing in a single call. Eliminates the need for a 2-step workflow (search → get tracks).
  ```python
  catalog(action="album_details", album="GNX", artist="Kendrick Lamar")
  # Returns: metadata (artist, release date, genre, label) + full track listing
  ```

- **Rename playlist action** - New `playlist(action="rename")` allows renaming playlists in-place without creating a copy (macOS only).
  ```python
  playlist(action="rename", playlist="Old Name", new_name="New Name")
  # Much cleaner than copy + delete workflow
  ```

### Fixed

- **Recommendations limit parameter** - The `discover(action="recommendations", limit=N)` action now correctly respects the `limit` parameter. Previously returned all 77 items regardless of limit.

- **README documentation errors**:
  - Fixed action name: `artist_top_songs` → `top_songs`
  - Fixed parameter name in `reveal_in_music`: `track_name` → `track`

### Changed

- **Trimmed tool docstrings** - Reduced verbose multi-line docstrings to concise 1-2 line summaries to further reduce MCP context footprint (commit 0984338)
- **Action normalization** - All dispatchers normalize action names: `action.lower().strip().replace("-", "_")` allows both "recently-played" and "recently_played"
- **Consistent error messages** - All dispatchers validate required parameters and provide helpful error messages with valid action lists

### Improved

- Enhanced album resolution - All album lookup operations now use fuzzy matching for improved flexibility
- Better album support - User can provide album name, ID, or any identifier; the system resolves to the optimal format for each operation

## [0.4.3] - 2026-01-05

### Added

- **Fuzzy matching for all entity types** - Unified fuzzy matching across playlists, tracks, and albums:
  - 3-pass algorithm: exact match → partial substring → fuzzy (normalized)
  - Transformations: lowercase, diacritics removal, "and" ↔ "&", emoji stripping, apostrophe normalization
  - Partial-after-normalization support: "Sgt Peppers" matches "Sgt. Pepper's Lonely Hearts Club Band"
  - AppleScript fuzzy resolution: emoji playlists resolve correctly ("My Mix" → "🎵 My Mix")
  - Fuzzy match info shown in output when non-exact match used

- **ResolvedPlaylist dataclass** - All playlist operations now use structured resolution:
  - `api_id` - p.XXX ID for API calls (fast, cross-platform)
  - `applescript_name` - Actual playlist name for AppleScript (required for remove operations)
  - `fuzzy_match` - Details about any fuzzy matching performed
  - Eliminates tuple unpacking bugs and provides type safety

- **Search result deduplication** - `search_catalog` and `search_library` now deduplicate by track ID

- **Comprehensive integration tests** - 15 new tests covering:
  - API-only mode (first 2, 5, 10 user actions)
  - macOS-only mode (AppleScript-preferred operations)
  - Combined mode (routing logic, fallback behavior)
  - Fuzzy matching workflows (playlists, tracks, albums)
  - Power user workflows (album operations, copy playlist, deduplication)

### Fixed

- **API/AppleScript routing** - Functions now correctly prefer API when `api_id` is available:
  - `get_playlist_tracks`, `search_playlist`, `copy_playlist`, `add_to_playlist`
  - Previously incorrectly preferred AppleScript even when API ID was available

- **Fallback logic in fuzzy matching** - Fixed condition that checked if filtered list was empty instead of whether a match was found

- **Variable shadowing in `remove_from_playlist`** - Renamed internal variable to avoid shadowing the resolved playlist

### Changed

- **DRY fuzzy matching** - Extracted `_fuzzy_match_entity()` generic function used by:
  - Playlist resolution (`_find_api_playlist_by_name`)
  - Track matching (`_find_matching_catalog_song`)
  - Album matching (`_find_matching_catalog_album`)

- **Performance optimization** - Fuzzy matching uses 3-pass approach:
  - Pass 1: Exact match (O(n), no normalization) - fastest
  - Pass 2: Partial match (O(n), substring only) - fast
  - Pass 3: Fuzzy match (normalization, only if needed) - slower but thorough

## [0.4.2] - 2026-01-02

### Fixed

- **add_to_playlist regression** - v0.4.1's API-first playlist resolution broke library track lookup. "Four Tops" couldn't find "The Four Tops" because AppleScript mode (which does partial matching on library) was skipped in favor of API mode (which only searched catalog).

### Changed

- **add_to_playlist prefers AppleScript on macOS** - When user provides track names, now uses AppleScript mode (searches library directly with partial matching) instead of forcing API mode. API mode only used for explicit playlist IDs or track IDs.

### Added

- **`_find_track_id()` helper** - Canonical way to find a track: searches library first, then catalog. Used as fallback when API mode is needed.
- **`_search_library_songs()` helper** - Matches `_search_catalog_songs()` for consistency.

## [0.4.1] - 2026-01-01

### Added

- **Pagination with `offset` parameter** - Skip first N items in `get_playlist_tracks`, `get_album_tracks`, `browse_library`:
  ```python
  get_playlist_tracks(playlist="Mix", limit=50, offset=100)  # Get tracks 101-150
  ```
- **`_find_api_playlist_by_name()`** - New helper to look up `p.XXX` playlist IDs from names via API (case-insensitive, exact match prioritized)
- **`_apply_pagination()` helper** - DRY pagination logic for all listing tools
- **Catalog/library ID support for removal** - `remove_from_playlist` and `remove_from_library` now accept any ID type via cache lookup

### Changed

- **20x faster playlist queries for shared tracks** - Playlists with Apple Music subscription content now resolve via API instead of slow AppleScript per-track iteration (~29s → ~1.6s for 432-track playlist)
- **API-first playlist name resolution** - `_resolve_playlist()` now looks up API playlist ID by name before falling back to AppleScript
- **Performance stats in output** - `get_playlist_tracks` API path now shows timing and API call count
- **Improved pagination display** - Shows "X-Y of Z tracks" when paginating, simple count otherwise
- **Cache stores track metadata** - Name, artist, album now cached alongside explicit status for ID-to-name lookups
- **Slimmed tool docstrings** - Removed verbose format detection examples, kept essential info

### Fixed

- **Offset shadowing bug** - When `fetch_explicit=True` on AppleScript path, internal API pagination loop overwrote the function's offset parameter, causing wrong headers like "401-432 of 432" when no pagination was requested

## [0.4.0] - 2025-12-30

### Breaking Changes

**Tool consolidation** - Reduced from 40 to 37 tools:

| Removed | Replacement |
|---------|-------------|
| `play_track` | `play(track="...")` |
| `play_playlist` | `play(playlist="...")` |
| `get_music_videos` | `search_catalog(types="music-videos")` |
| `get_storefronts` | `config(action="list-storefronts")` |
| `seek_to_position` | `playback_control(action="seek", seconds=...)` |

### Added

- **Unified `play` tool** - Play tracks, playlists, or albums with one tool:
  ```python
  play(track="Hey Jude")                    # play a track
  play(playlist="Road Trip", shuffle=True)  # shuffle a playlist
  play(album="Abbey Road", artist="Beatles") # play an album
  ```

- **Album playback** - New `album` parameter in `play` tool

- **Music video search in catalog** - `search_catalog(types="music-videos")` or leave query empty for featured videos

### Changed

- **Enhanced `get_now_playing`** - Now includes player state (playing/paused/stopped)

- **Expanded preference scope**:
  - `clean_only` now works in `search_library` and `browse_library` (was only `search_catalog`)
  - `fetch_explicit` now works in `search_library` and `browse_library` (was only `get_playlist_tracks`)

### Fixed

- **Documentation** - Removed ghost tool `get_player_state` that never existed in code

## [0.3.0] - 2025-12-29

### Breaking Changes

This release introduces a **unified parameter architecture** where entity parameters (track, album, artist) accept any format with automatic detection. Old parameter names are replaced:

| Tool | Old Parameters | New Parameters |
|------|----------------|----------------|
| `add_to_playlist` | `ids`, `track_name`, `tracks` | `track`, `album` |
| `add_to_library` | `ids`, `track_name`, `tracks` | `track`, `album` |
| `remove_from_playlist` | `ids`, `track_name`, `tracks` | `track` |
| `remove_from_library` | `ids`, `track_name`, `tracks` | `track` |
| `rating` | `song_id`, `track_name` | `track` |
| `play_track` | `track_name` | `track` |
| `get_album_tracks` | `album_id` | `album` |
| `get_artist_details` | `artist_name` | `artist` |
| `get_artist_top_songs` | `artist_name` | `artist` |
| `get_similar_artists` | `artist_name` | `artist` |

### Added

- **Universal input detection** - All entity parameters auto-detect format:
  - JSON array: `track='[{"name":"Hey Jude","artist":"Beatles"}]'`
  - Prefixed IDs: `track="i.ABC123"` (library), `playlist="p.XYZ789"`
  - CSV names: `track="Hey Jude, Let It Be"`
  - Catalog IDs: `track="1440783617"` (10+ digits)
  - Persistent IDs: `track="ABC123DEF456"` (12+ hex chars)
  - Names: `track="Hey Jude"` (triggers search)

- **Album support for playlists** - Add entire albums to playlists:
  ```python
  add_to_playlist(playlist="Road Trip", album="Abbey Road", artist="Beatles")
  add_to_playlist(playlist="Mix", album="1440783617")  # by catalog ID
  ```

- **Album by name lookup** - `get_album_tracks` now accepts album names:
  ```python
  get_album_tracks(album="Abbey Road", artist="Beatles")  # search by name
  get_album_tracks(album="1440783617")  # catalog ID still works
  get_album_tracks(album="l.ABC123")    # library ID still works
  ```

- **Artist by ID** - `get_artist_details`, `get_artist_top_songs`, `get_similar_artists` now accept catalog IDs:
  ```python
  get_artist_details(artist="136975")       # by catalog ID
  get_artist_details(artist="The Beatles")  # by name still works
  ```

- **Extended cache** - Cache now stores albums and name index:
  - Cache file renamed from `track_cache.json` to `cache.json`
  - Stores album metadata (name, artist, track count, year)
  - Name index for reverse lookups (name+artist → ID)
  - Automatic migration from legacy format

### Changed

- **Simplified API surface** - Each tool now has 1-2 main parameters instead of 3-5 mutually exclusive ones
- **Consistent naming** - All tools use `track`, `album`, `artist` parameter names consistently
- **Detection order priority**: JSON → prefixed ID → CSV → catalog ID → persistent ID → name

### Migration Guide

```python
# Before (0.2.x)
add_to_playlist(playlist_name="Mix", track_name="Hey Jude", artist="Beatles")
add_to_playlist(playlist_name="Mix", ids="1440783617")
add_to_playlist(playlist_name="Mix", tracks='[{"name":"Hey Jude","artist":"Beatles"}]')

# After (0.3.0) - all equivalent, auto-detected
add_to_playlist(playlist="Mix", track="Hey Jude", artist="Beatles")
add_to_playlist(playlist="Mix", track="1440783617")
add_to_playlist(playlist="Mix", track='[{"name":"Hey Jude","artist":"Beatles"}]')

# New: add albums to playlists
add_to_playlist(playlist="Mix", album="Abbey Road", artist="Beatles")

# New: get album tracks by name
get_album_tracks(album="Abbey Road", artist="Beatles")
```

### Documentation

- **Restructured README** - Quick Start (macOS) section now comes first with zero-config setup
- **Clearer platform guidance** - Windows/Linux users directed to API Setup section
- **Better usage examples** - Organized by category (playlist management, discovery, API features)
- **MCP link added** - Links to modelcontextprotocol.io for newcomers
- **Token expiration clarity** - Notes that warnings appear 30 days before expiration

## [0.2.10] - 2025-12-23

### Fixed

- **`auto_search` now works with batch add** - JSON tracks mode (`tracks='[...]'`) now falls back to auto_search when tracks aren't found in library
- **DRY refactoring** - Consolidated auto_search logic into `_auto_search_and_add_to_playlist()` helper (~100 lines reduced to ~15)

### Changed

- **Unified playlist parameters** - All playlist-related tools now accept a single `playlist` parameter:
  - Starts with `p.` → playlist ID (API mode, cross-platform)
  - Otherwise → playlist name (AppleScript, macOS only)
  - Affects: `get_playlist_tracks`, `add_to_playlist`, `remove_from_playlist`, `search_playlist`
  - `copy_playlist` uses `source` parameter with same auto-detection
- **Unified ID parameters** - Simplified ID parameters across tools:
  - `add_to_library`: `catalog_ids` → `ids` (auto-detects catalog/library IDs), added `type` param for albums
  - `add_to_playlist`: `track_ids` → `ids` (auto-detects catalog/library IDs)
  - `remove_from_playlist`: `track_ids` → `ids`
  - `remove_from_library`: `track_ids` → `ids`
- **ID auto-detection** - New `_detect_id_type()` helper identifies ID types:
  - All digits → catalog ID
  - Starts with `i.` → library ID
  - Starts with `p.` → playlist ID
  - Otherwise → persistent ID (hex)
- **README features table sorted** - Double-checkmark features (both macOS and API) now listed first

## [0.2.9] - 2025-12-23

### Added

- **Audit logging for destructive operations** - All library/playlist modifications now logged:
  - Logs to `~/.cache/applemusic-mcp/audit_log.jsonl`
  - Operations: add_to_library, remove_from_library, add_to_playlist, remove_from_playlist, create_playlist, delete_playlist, copy_playlist, rating
  - View via `config(action="audit-log")`, clear via `config(action="clear-audit-log")`
  - Includes undo hints for recovery guidance
- **JSON `tracks` parameter for `add_to_playlist`** - Consistent with `add_to_library`:
  - `add_to_playlist(playlist_name="Mix", tracks='[{"name":"Song","artist":"Artist"},...]')`
  - Supports multiple tracks with different artists in a single call

### Changed

- **Renamed `system` tool to `config`** - Better reflects purpose (configuration, preferences, cache management)
- **Improved limit parameter docs** - Now says "default: all" instead of "0 = all" to discourage explicit 0
- **DRY refactoring of track operations** - Extracted common patterns into reusable helpers:
  - `_split_csv()` - Consistent comma-separated value parsing
  - `_parse_tracks_json()` - Standardized JSON tracks array parsing
  - `_validate_track_object()` - Unified track object validation
  - `_find_matching_catalog_song()` - Shared catalog search with partial matching
  - `_build_track_results()` - Consistent success/error message formatting
  - Reduced code duplication across `add_to_library`, `add_to_playlist`, `remove_from_library`, `remove_from_playlist`

## [0.2.8] - 2025-12-23

### Added

- **Configurable storefront via system tool** - Set your Apple Music region:
  - `system(action="set-pref", preference="storefront", string_value="gb")` - Set UK region
  - `system(action="list-storefronts")` - List all available regions
  - `system()` - Now shows current storefront in preferences
  - Supports all Apple Music storefronts (175+ countries)
  - Enables non-US users to get localized catalog results
- **`auto_search` preference now displayed** in `system()` info output

### Changed

- **Thread-safe track cache** - Singleton initialization now uses double-check locking pattern
- **Named constants for play_track retry loop** - Magic numbers extracted to descriptive constants:
  - `PLAY_TRACK_INITIAL_DELAY`, `PLAY_TRACK_RETRY_DELAY`, `PLAY_TRACK_MAX_ATTEMPTS`, `PLAY_TRACK_READD_AT_ATTEMPT`
- **Consolidated storefront functionality** - `get_storefronts()` tool still available, but `system(action="list-storefronts")` preferred

### Fixed

- **Request timeouts** - All 51 API calls now have 30-second timeout (prevents indefinite hangs)
- **Cache error logging** - Track cache load/save errors now logged instead of silently swallowed

## [0.2.7] - 2025-12-22

### Changed

- **`check_playlist` → `search_playlist`** - Renamed for clarity and enhanced:
  - Uses native AppleScript search on macOS (fast, same as Music app search field)
  - API path manually filters tracks (cross-platform support maintained)
  - Now searches album field in addition to name/artist
  - Better name reflects actual functionality

### Fixed

- **Album search** - API path now searches album field (was missing)

## [0.2.6] - 2025-12-22

### Added

- **Auto-search feature** - Automatically find and add tracks from catalog when not in library (opt-in):
  - New `auto_search` parameter for `add_to_playlist` (uses preference if not specified, default: false)
  - When track not in library: searches catalog → adds to library → adds to playlist (one operation!)
  - Uses optimized API flow: `/catalog/{catalog_id}/library` to get library ID instantly (no retry loop)
  - Includes API verification to confirm track added to playlist
  - Reduces 7-step manual process to 1 call
  - Set via `system(action="set-pref", preference="auto_search", value=True)` to enable by default
- **New `auto_search` preference** - Control automatic catalog search behavior (default: false, respects user choice)

### Changed

- **Partial matching everywhere** - ALL track operations now support partial name matching:
  - `add_track_to_playlist` - Changed from `is` to `contains` (CRITICAL FIX)
  - `love_track` - Now supports partial matching
  - `dislike_track` - Now supports partial matching
  - `get_rating` - Now supports partial matching
  - `set_rating` - Now supports partial matching
  - No more frustration with exact titles like "Song (Live at Venue, Date)"
- **Optimized auto_search flow** - Minimal API calls:
  1. Search catalog → get catalog_id
  2. Add to library via API
  3. Get library ID from `/catalog/{catalog_id}/library` (instant!)
  4. Get playlist ID from name (AppleScript, local)
  5. Add to playlist via API
  6. Verify via API

### Fixed

- **Critical:** `add_to_playlist` with track names required EXACT match (now uses `contains`)
  - Example: "Give Up the Funk" now finds "Give up the Funk (Tear the Roof Off the Sucker)"
  - Fixes the user's exact scenario where 7 attempts were needed to add one song

## [0.2.5] - 2025-12-22

### Added

- **Track metadata caching system** - Intelligent caching for stable track metadata:
  - Dedicated `track_cache.py` module with clean interface
  - Multi-ID indexing: caches by persistent IDs (AppleScript), library IDs (API), and catalog IDs (universal)
  - Stores stable fields only: explicit status and ISRC
  - Eliminates redundant API calls (90% reduction for repeated checks)
  - Extensible design for adding more stable fields
  - 10-20x speedup for subsequent playlist explicit status checks
  - Cache persisted to `~/.cache/applemusic-mcp/track_cache.json`
- **Explicit content tracking** - Comprehensive explicit status throughout:
  - `[Explicit]` marker in all track output formats (text, JSON, CSV)
  - `fetch_explicit=True` parameter for `get_playlist_tracks()` to fetch explicit status via API
  - `clean_only=True` parameter for `search_catalog()` to filter explicit content
  - AppleScript mode shows "Unknown" by default (contentRating not exposed)
  - API mode shows accurate "Yes"/"No" explicit status
- **User preferences system** - Set defaults for common parameters:
  - `fetch_explicit` - always fetch explicit status (default: false)
  - `reveal_on_library_miss` - auto-reveal catalog tracks in Music app (default: false)
  - `clean_only` - filter explicit content in catalog searches (default: false)
  - Set via `system(action="set-pref", preference="...", value=True/False)`
  - View current preferences via `system()` info display
  - Stored in `~/.config/applemusic-mcp/config.json`
  - See `config.example.json` for format
- **New `system` tool** - Comprehensive system configuration and cache management:
  - `system()` - show preferences, track cache stats, and export files
  - `system(action="set-pref", ...)` - update preferences
  - `system(action="clear-tracks")` - clear track metadata cache separately
  - `system(action="clear-exports")` - clear CSV/JSON export files separately
  - Shows cache sizes, entry counts, file ages
  - Replaces old `cache` tool with more intuitive naming
- **Partial playlist matching** - Smart playlist name matching with exact-match priority:
  - "Jack & Norah" now finds "🤟👶🎸 Jack & Norah"
  - Exact matches always prioritized over partial matches
  - Applied to all playlist operations via `_find_playlist_applescript()` helper
- **Comprehensive documentation**:
  - `CACHING.md` - Multi-ID caching architecture, E2E flow, performance analysis
  - `COMPOSITE_KEYS.md` - Why we use composite keys for AppleScript ↔ API bridging
  - `config.example.json` - Example configuration with preferences
- **Test suite expansion** - 30 new tests (120 total: 26 track cache, 4 preferences)

### Changed

- **Error messages cleaned up** - Removed redundant playlist names from error responses
- **Helpful guidance** - Error messages suggest `search_catalog` + `add_to_library` workflow when tracks not found
- **Tool parameters** - `fetch_explicit`, `clean_only`, `reveal` now use `Optional[bool]` to support user preferences
- **Asymmetry fixes** - Systematic review and fixes for add/remove inconsistencies:
  - **`remove_from_playlist` enhanced**:
    - **Partial matching fixed** - Now uses `contains` instead of `is` (no more exact match requirement!)
    - **Array support** - Remove multiple tracks at once (comma-separated names, IDs, or JSON array)
    - **ID-based removal** - Remove by persistent IDs via `track_ids` parameter
    - **Better output** - Shows removed count, lists successes and failures separately
  - **`remove_from_library` enhanced** - Now matches `add_to_library` capabilities:
    - **Array support** - Remove multiple tracks: `track_name="Song1,Song2"` or `track_ids="ID1,ID2"`
    - **ID-based removal** - Remove by persistent IDs via `track_ids` parameter
    - **JSON array support** - Different artists: `tracks='[{"name":"Hey Jude","artist":"Beatles"}]'`
    - **Flexible formats** - Same 5 modes as `remove_from_playlist`
  - **`search_library` parameter standardized** - Renamed `search_type` → `types` to match `search_catalog`
  - **`copy_playlist` name support** - Added `source_playlist_name` parameter for macOS users (matches other playlist operations)

## [0.2.4] - 2025-12-21

### Added

- **No-credentials mode on macOS** - Many features now work without API setup:
  - `get_library_playlists` - Lists playlists via AppleScript first
  - `create_playlist` - Creates playlists via AppleScript first
  - `browse_library(songs)` - Lists library songs via AppleScript first
  - New `get_library_songs()` AppleScript helper function
- **Test cleanup** - Automatically removes test playlists after test runs

### Changed

- **AppleScript-first approach** - macOS tools try AppleScript before falling back to API
- **README** - Documents no-credentials mode, simplified requirements

## [0.2.3] - 2025-12-21

### Changed

- **format=csv** - Inline CSV output in response (in addition to text/json/none)
- **export=none** - Consistent "none" default instead of empty string
- **play_track response prefixes** - Shows `[Library]`, `[Catalog]`, or `[Catalog→Library]` to indicate source
- **Featured artist matching** - `play_track` matches "Bruno Mars" in "Uptown Funk (feat. Bruno Mars)"
- **Catalog song reveal** - `reveal=True` opens song in Music app via `music://` URL (user clicks play)
- **Add-to-library retry** - Retries add at 5s mark in case first attempt silently failed
- **URL validation** - `open_catalog_song` validates Apple Music URLs before opening

## [0.2.2] - 2025-12-20

### Added

- **MCP Resources for exports** - Claude Desktop can now read exported files:
  - `exports://list` - List all exported files
  - `exports://{filename}` - Read a specific export file

### Changed

- **Tool consolidation (55 → 42 tools)** - The answer to life, the universe, and everything:
  - `browse_library(type=songs|albums|artists|videos)` - merged 4 library listing tools
  - `rating(action=love|dislike|get|set)` - merged 5 rating tools into one
  - `playback_settings(volume, shuffle, repeat)` - merged 4 settings tools
  - `search_library` - now uses AppleScript on macOS (faster), API fallback elsewhere
  - `airplay` - list or switch devices (merged 2 tools)
  - `cache` - view or clear cache (merged 2 tools)
- **Unified output format** - List tools now support:
  - `format="text"` (default), `"json"`, `"csv"`, or `"none"` (export only)
  - `export="none"` (default), `"csv"`, or `"json"` to write files
  - `full=True` to include all metadata
- **Extended iCloud sync wait** - `play_track` now waits ~10s for add-to-library sync (was ~7s)

## [0.2.1] - 2025-12-20

### Added

- **`remove_from_library`** - Remove tracks from library via AppleScript (macOS only)
- **`check_playlist`** - Quick check if song/artist is in a playlist (cross-platform)
- **`set_airplay_device`** - Switch audio output to AirPlay device (macOS)
- **`_rate_song_api`** - Internal helper for rating songs via API

### Changed

- **`love_track` / `dislike_track` now cross-platform** - Uses AppleScript on macOS, falls back to API elsewhere

- **play_track enhanced** - Now properly handles catalog tracks not in library:
  - `add_to_library=True`: Adds song to library first, then plays
  - `reveal=True`: Opens song in Music app for manual play
  - Clear messaging about AppleScript's inability to auto-play non-library catalog tracks
- **Code refactoring** - Extracted `_search_catalog_songs()` and `_add_songs_to_library()` internal helpers to reduce duplication

### Fixed

- Fixed `play_track` calling non-existent `reveal_in_music` (now correctly calls `reveal_track`)
- Replaced misleading `play_catalog_track` AppleScript function with honest `open_catalog_song`

## [0.2.0] - 2024-12-20

### Added

- **AppleScript integration for macOS** - 16 new tools providing capabilities not available via REST API:
  - Playback control: `play_track`, `play_playlist`, `playback_control`, `get_now_playing`, `seek_to_position`
  - Volume/settings: `set_volume`, `get_volume_and_playback`, `set_shuffle`, `set_repeat`
  - Playlist management: `remove_from_playlist`, `delete_playlist`
  - Track ratings: `love_track`, `dislike_track`
  - Other: `reveal_in_music`, `get_airplay_devices`, `local_search_library`
- **Clipped output tier** - New tier between Full and Compact that truncates long names while preserving all metadata fields (album, year, genre)
- **Platform Capabilities table** in README showing feature availability across macOS and Windows/Linux
- **Cross-platform OS classifiers** in pyproject.toml (Windows, Linux in addition to macOS)
- **Security documentation** for AppleScript input escaping

### Changed

- Renamed package from `mcp-applemusic-api` to `mcp-applemusic` (repo rename pending)
- Updated README with comprehensive macOS-only tools documentation
- Improved input sanitization: backslash escaping added to prevent edge cases in AppleScript strings
- Test count increased from 48 to 71 tests

### Fixed

- Exception handling in AppleScript module: replaced bare `except:` with specific exception types

## [0.1.0] - 2024-12-15

### Added

- Initial release with REST API integration
- 33 cross-platform MCP tools for Apple Music
- Playlist management (create, add tracks, copy)
- Library browsing and search
- Catalog search and recommendations
- Tiered output formatting (Full, Compact, Minimal)
- CSV export for large track listings
- Developer token generation and user authorization
- Comprehensive test suite (48 tests)
