# Issue #28 — tokenless catalog→library add across Music versions

Status: **RESOLVED (v0.12.0).** Root cause was an OS-version split in Music's UI
surfaces plus a resolver/flow bug. Validated live on macOS 26.5 (Music 1.6.5) and
macOS 15.7.3 (Music 1.5.6).

## The version matrix (the thing that confused everything)

| | macOS 15.7 / Music 1.5.6 | macOS 26.5 / Music 1.6.5 |
|---|---|---|
| Search autocomplete pop-over in AX tree | **No** (SwiftUI overlay, invisible) | **Yes** |
| Catalog deep-link (`open music://…?i=…`) navigates to a page | **Yes** | **No** — Apple changed it; just hands off to the player |

So each OS has exactly **one** working surface, and they are *opposite*:
- **15.7** → pop-over absent, but the **deep-link** navigates → add via the
  resolved-URL album page.
- **26.5** → deep-link dead, but the **pop-over** is in the AX tree → add via the
  pop-over → song detail page → "Add to Library".

The flow tries the pop-over first; on a genuine miss (absent on 15.x, or no row)
it falls back to the deep-link. Right surface per OS, no manual version check.

## The bugs fixed (v0.12.0)

1. **Pop-over queried with the raw user query, not the canonical title.** The
   code resolved the canonical title via the free iTunes Search API, then threw
   it away and searched with the raw name. Apple's autocomplete is
   ranking-sensitive, so obscure tracks never surfaced ("Lemons"/"Brye" showed
   Shawn Wasabi & Ashley Tisdale, never Brye). Fix: query the pop-over with the
   **canonical title** ("LEMONS (feat. Cavetown)") — the exact row then appears.
2. **Resolver mis-scored when an artist was given.** Exact-title outweighed
   artist, so "Lemons"/"Brye" silently resolved to "Lemons" by *Hairitage*. Fix:
   **artist-primary** scoring; reject a different artist's same-titled song.
3. **Post-add verify false-negatived.** It checked only local `library playlist
   1`, which lags the iCloud add ~3 s, so a real add looked like a failure and
   fell through to the (dead-on-26) deep-link. Fix: trust the pop-over UI's
   success; use the library lookup as confirmation, not a gate; widen the retry
   window (3→6).
4. **"Already in library" read as failure.** Music shows a "Download" button
   where "Add to Library" would be when the track is already in the library; the
   flow treated the missing Add button as an error. Fix: recognize Download as
   "already in library" = success.

## Investigation tooling worth remembering

- **AppleScript `entire contents` is broken on Music's SwiftUI windows**; direct
  child navigation works but index paths shift between builds. The robust tool is
  the **Accessibility C API via PyObjC** (`AXUIElementCreateApplication` +
  recursive `AXChildren` walk, matching by **role** `AXScrollArea → AXList →
  AXCell → AXStaticText`). That's how the results-page structure was finally read
  when AppleScript returned empty.
- **Testing over SSH is a trap for navigation/injection.** A plain
  `ssh … osascript` runs in a detached bootstrap not attached to the window
  server; AX *reads* work but GUI navigation/keystroke injection silently no-ops.
  Drive from the GUI session (Screen Sharing live) instead.
- **The committed search-results page** (press Return) surfaces a fuller Songs
  list than the autocomplete dropdown — but clicking a results **song row plays
  it**, it does not navigate to a detail page, and the rows expose no right-click
  context menu. So it is **not** a usable add surface; the canonical-title
  pop-over query is the better lever and was the fix.

## Remaining follow-ups (not blockers)

- **macOS CI runner.** CI is ubuntu-only, which is how #28 shipped — the darwin
  UI suite never runs in CI. A self-hosted/macOS runner that exercises the
  tokenless UI add (both Music versions, behind Accessibility) is the real
  pre-release gate. Until then, validate UI-flow changes live on both OS versions
  before release.
- **Live integration test** reproducing tokenless add on each Music version,
  gated on Accessibility permission being present.
