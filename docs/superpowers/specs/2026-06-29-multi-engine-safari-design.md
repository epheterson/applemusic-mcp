# Multi-engine architecture + Safari as a first-class engine

**Status:** approved (design) — 2026-06-29
**Branch:** `feat/browser-playback`

## Problem

The server reaches Apple Music through several backends, but the user-facing model
is a single `mode` knob with only `native` / `web` (+ `api` alias). "web" is hard-wired
to the Playwright Chrome web player. That misses two things:

1. **Safari** can drive MusicKit directly on macOS via the same AppleScript
   `do JavaScript` channel used for token harvest. Proven live: search + `setQueue`
   work in the user's signed-in Safari, MusicKit is authorized natively, and Safari
   decodes Apple Music DRM with no Widevine workaround. That makes the entire
   Chrome/Playwright stack optional on macOS for playback + queue, not just sign-in.
2. Users should be able to pin **any single engine** — app-only, chrome-only,
   safari-only, api-only — or let `auto` mix the best per operation.

## Goals

- Add **Safari** as a first-class playback + queue engine (macOS).
- One coherent selection model: `mode` = `auto | native | safari | chrome | api`,
  plus a per-call `engine=` override for playback/queue.
- `auto` routes each capability to the best available engine ("best of all worlds").
- No JS duplication: one source of truth for the MusicKit JS, two transports.
- Honest about the macOS-only and intrusiveness constraints.

## Non-goals

- Replacing Chrome/Playwright (it stays the off-mac web engine and a macOS option).
- Changing the write-rail (sanctioned API → web → native) — reused as-is.
- A Music.app Up Next implementation (native still has no exposed queue; queue stays
  a web-player capability — Safari or Chrome).

## Engine model

`mode` preference values:

| value | meaning |
|---|---|
| `auto` (default) | mix the best engine per operation (table below) |
| `native` | Music.app only (macOS); no Up Next; no account needed |
| `safari` | drive signed-in Safari (macOS); playback + queue; data/writes via API |
| `chrome` | Playwright web player (any OS); playback + queue; data/writes via API |
| `api` | REST only; data + writes; no playback/queue (actionable error) |

Back-compat: legacy `web` → "auto's web pick" (Safari on macOS, Chrome off-mac);
`api` unchanged. `APPLEMUSIC_FORCE_API_MODE=1` still forces `api`.

Per-call override: `playback(..., engine=...)` and `queue(..., engine=...)` accept
`native | safari | chrome | web | auto`, winning over `mode` for that one call.

### Auto routing (best-of-all-worlds)

| Capability | macOS `auto` | non-mac `auto` |
|---|---|---|
| Data reads (catalog/library/playlist) | API (native if no creds) | API |
| Writes (playlist/library edits) | sanctioned API → web → native | API → web |
| Play song / album / playlist / URL | **Music.app** | Chrome |
| Up Next queue (all ops) | **Safari** | Chrome |
| control / now_playing | **active engine** (see below) | Chrome |

Pinned modes force one engine and accept its gaps:
- `api` → no playback: return an actionable error ("set mode to native/safari/chrome").
- `native` → no Up Next: queue ops return "use engine=safari (or mode=safari)".
- `safari` / `chrome` → native-only features unavailable: ratings (1–5★), favorites,
  AirPlay, library snapshot, nested folder paths → actionable error.

## Components

### `musickit_js.py` (new — shared JS, single source of truth)

Move the MusicKit JS constants out of `browser.py`:
`_PLAY_SONG_JS`, `_PLAY_QUEUE_JS`, `_CONTROL_JS`, `_SETTINGS_JS`, `_NOW_PLAYING_JS`,
`_QUEUE_LIST_JS`, `_QUEUE_PLAY_NEXT_JS`, `_QUEUE_PLAY_LATER_JS`, `_QUEUE_SET_JS`,
`_QUEUE_REMOVE_JS`, `_QUEUE_JUMP_JS`, `_QUEUE_JUMP_BY_ID_JS`, `_QUEUE_AUTOPLAY_JS`,
`_MUSICKIT_READY`, `_AUTHORIZED`. Each is a JS arrow-function expression taking a
single arg (string or object), returning a value or a promise. `browser.py` imports
them (Playwright `evaluate` awaits promises). `safari_player.py` imports the same.

### `safari_player.py` (new — Safari MusicKit transport)

Public surface MIRRORS `browser.py` exactly so `server.py` can route to either:
`play_catalog_track(id)`, `play_descriptor(desc, shuffle)`, `play_url(url, shuffle)`,
`reveal_url(url)`, `playback_control(action, seconds)`, `now_playing()`,
`queue_list()`, `queue_play_next(id)`, `queue_play_later(id)`, `queue_set(ids)`,
`queue_remove(index)`, `queue_jump(index)`, `queue_jump_id(id)`, `queue_clear()`,
`queue_autoplay(enabled)`. All return the same `(ok, value)` / `Optional[dict]` shapes.

Transport — `_run_musickit(js_fn_expr, arg=None) -> tuple[bool, Any]`:
1. Build an AppleScript that finds (or opens) a `music.apple.com` Safari tab.
2. Ensure MusicKit is ready (poll `_MUSICKIT_READY` up to a budget).
3. Kick: `window.__amR=''; (async()=>{ try { const f=(<JS>); const v=await
   f(<arg-json>); window.__amR=JSON.stringify({ok:1,v:v===undefined?null:v}); }
   catch(e){ window.__amR=JSON.stringify({ok:0,e:String((e&&e.message)||e)}); }})();`
4. Poll `window.__amR` (repeat `do JavaScript` with small `delay`) until non-empty or
   timeout; parse JSON → `(ok, v)` or `(False, error)`.
   The kick + poll loop live in ONE `run_applescript` call (AppleScript `repeat`/`delay`).

`is_available() -> bool`: darwin + a lightweight probe (`typeof MusicKit`) succeeds.
JS-blocked detection reuses `safari._looks_like_js_blocked` → actionable
"enable Allow JavaScript from Apple Events" message. Not-signed-in / not-authorized
and timeout produce distinct, honest messages (no "not found" disguises).

Intrusiveness: only ever touches a `music.apple.com` tab (reuse an open one, else open
one). Never commandeers an unrelated tab. Documented.

### `server.py` wiring

- `_playback_engine(engine_override=None, for_queue=False) -> str`:
  resolves `native | safari | chrome | none` from per-call override → `mode` → `auto`
  table → platform/availability. `_queue_engine(...)` = `_playback_engine(for_queue=True)`.
- `_web_player(engine) -> module`: returns `safari_player` or `browser` for the
  resolved web engine; native playback uses the existing `asc` path.
- `playback()` / `queue()` route through the resolver; pinned-mode gaps return
  actionable errors.
- **Active-engine tracking**: a persisted `active_playback` hint
  (`native | safari | chrome`) updated on every play/queue start. `control` and
  `now_playing` target it; fallback probes both (Music.app player state + Safari/Chrome
  MusicKit state) and picks whoever is actually playing; default native on macOS.
  This is what makes "play → Music.app, then queue → Safari" cohere.

### CLI / status / docs

- `status`: report the resolved engine + active playback engine + Safari/Chrome
  availability.
- README / SKILL / CHANGELOG: document the five modes, the auto table, the Safari
  setting requirement, and the "drives your real Safari" note.

## Error handling

- Safari setting off → actionable "enable Allow JavaScript from Apple Events".
- Safari not signed in / MusicKit not authorized → "sign into Apple Music in Safari".
- `mode=safari` off macOS → "safari engine is macOS-only; use chrome".
- `mode=api` + playback/queue → "api mode has no player; set mode native/safari/chrome".
- Timeouts surface as timeouts, never as "not found".

## Testing (TDD)

- `safari_player`: mock `applescript.run_applescript` to return canned osascript
  output; assert (a) the built AppleScript embeds the right JS + arg, (b) success
  parses `{ok:1,v:…}`, (c) js-blocked / not-authorized / timeout produce the right
  messages. Mirrors `test_browser_cov`'s fake-transport approach.
- Engine resolution: parametrized over `mode × platform(APPLESCRIPT_AVAILABLE) ×
  availability` → asserts `_playback_engine` / `_queue_engine` picks.
- `musickit_js`: import + shared-constant identity test (browser & safari use the same).
- Active-engine tracking: play → hint=native; queue → hint=safari; control targets hint.
- All hermetic — the @live guard blocks real osascript; `run_applescript` is mocked.
- Cross-platform: full suite green on macOS AND in the Linux container; `mode=safari`
  guarded macOS-only.

## Build sequence

1. Extract `musickit_js.py` (refactor; suite stays green).
2. Vertical slice: Safari transport + `play` + `queue_set`/`list` + `control` — TDD,
   live-validate on the iMac.
3. Complete the Safari surface (settings, now_playing, all queue ops, reveal).
4. Engine resolvers + per-call `engine=` + auto table.
5. Wire `server.py` playback()/queue() + active-engine tracking.
6. status / CLI / README / SKILL / CHANGELOG.
7. Validate: macOS live + Linux container.

## Risks

- AppleScript `do JavaScript` async ergonomics (kick+poll) are clunkier than Playwright
  `evaluate`; mitigated by the single-call repeat/delay poll loop + clear timeouts.
- Sharing the user's real Safari (a `music.apple.com` tab) — accepted tradeoff,
  documented; Chrome remains the isolated option.
- Tab-reference stability across `do JavaScript` calls — identify "our" tab by URL each
  call rather than holding a reference.
