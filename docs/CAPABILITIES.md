# Capabilities — what runs where, and why

Every capability in `applemusic-mcp`, mapped to the three engines it can run on,
with the reason each cell is what it is. For the short version, see the
["Modes & what runs where" table in the README](../README.md#modes--what-runs-where).

**Engines**

- **Native** — the local **Music.app** on macOS, driven by AppleScript / UI scripting. Works with no Apple account.
- **API** — Apple Music's web API (`amp-api.music.apple.com`), any OS, after `signin` or a developer token.
- **Browser** — a local **Google Chrome** window running MusicKit JS, any OS, after `signin`. Needed for DRM audio playback.

`✓` works · `✗` not possible on that engine · `—` not applicable / not exposed there

| Capability | Native (macOS) | API (any OS) | Browser (any OS) | How / why |
|---|:---:|:---:|:---:|---|
| Catalog search / browse | ✓ | ✓ | — | API is the clean path; tokenless macOS falls back to Music.app UI search |
| Recommendations / charts / suggestions | ✗ | ✓ | — | Only the Apple Music API exposes these |
| Library search / browse | ✓ | ✓ | — | AppleScript locally, or API with a token |
| Genre search | ✓ | ✗ | — | Genre filtering only exists in the local Music app |
| Recently played / added | ✓ | ✓ | — | Both engines expose it |
| Add catalog → library | ✓ | ✓ | ✓ | Native: AppleScript/UI · API: dev + user token · Browser: in-page POST |
| Remove from library | ✓ | ✓ | — | API `DELETE /me/library/songs/{id}`; native via AppleScript |
| Love / dislike | ✓ | ✓ | — | API ratings endpoint or AppleScript |
| 1–5 star ratings | ✓ | ✗ | ✗ | Apple's API has no star ratings — local Music app only |
| Favorites list | ✓ | ✗ | ✗ | Not exposed by the API |
| Playlist create / add / remove / rename | ✓ | ✓ | — | Full API parity with native |
| Playlist copy | ✓ | ✗ | — | No clean API; native reads tracks + recreates |
| Playlist delete | ✓ | ✓ ¹ | — | ¹ amp-api **only** with the harvested web token (a generated developer token 401s) |
| Folders — single level + move in/out | ✓ | ✓ | — | API goes beyond the web UI, which only drag-moves |
| Folders — nested paths / tree / path | ✓ | ✗ | ✗ | AppleScript folder tree only |
| Playback — play song / album / playlist / URL | ✓ | — | ✓ | macOS Music app, or Chrome (MusicKit). In `auto`, native falls back to browser |
| Controls — pause / stop / next / prev / seek | ✓ | — | ✓ | Same two engines |
| Settings — volume / shuffle / repeat | ✓ | — | ✓ | Same two engines |
| now_playing | ✓ | — | ✓ | Same two engines |
| Up Next queue — view/next/last/remove/jump/clear/autoplay | ✗ | — | ✓ | **Browser only** — no REST endpoint, and AppleScript can't reach the queue |
| Reveal in app | ✓ | — | ✓ | Native reveals in Music; browser navigates the page |
| AirPlay device select | ✓ | ✗ | ✗ | AppleScript only |
| Library snapshot / integrity | ✓ | ✗ | ✗ | AppleScript full-library read only |
| Works with no Apple account | ✓ | ✗ | ✗ | Local Music app needs no account; API/browser do |
| Cross-platform (Windows / Linux) | ✗ | ✓ | ✓ | Native is macOS-only |

## Notes

- **Writes are sanctioned-first, and pick their path by credential, not by the
  playback `mode`.** The API column above is really two paths: the **sanctioned**
  Apple Music API (`api.music.apple.com`, your generated developer token) and the
  **web** path (`amp-api.music.apple.com`, a harvested web-player token, the same
  approach as Cider / Music Assistant). A write (create / add / remove / delete /
  rename / move / rate) takes the official API when you hold a developer token;
  it falls back to the web path only for the operations the public API can't do
  (delete a playlist, add to a Music.app-created playlist, move) or when you have
  no developer token. On macOS, local Music.app (AppleScript, tokenless) handles
  writes too. Choosing web *playback* does not push your writes onto the web path,
  and each write reports the path it took. `config(action="status")` shows the
  resolved write rail on its `Writes:` line.
- **One `mode` knob** drives both data and playback: `auto` (default; native Music.app on macOS, web API + Chrome web player elsewhere), `native` (local Music.app), or `web` (web API + web player, any OS). Playback always follows the engine, so there is no separate playback preference. `auto` falls back native→browser when a native play can't start (e.g. Accessibility not granted); `native` stays Music-app-only.
- **Native catalog playback** drives Music.app via UI scripting: it foregrounds the app and moves the cursor to click Play, so it needs **Accessibility permission** and an unlocked screen.
- **Browser playback / queue** open a real Chrome window (not for headless servers) and need an Apple Music **subscription** for full-track DRM audio — without one, MusicKit serves ~30-second previews.
