# Signing in

Sign-in unlocks the `api` and `web` [permission levels](../README.md#choose-how-much-it-can-reach): the full catalog, adding to your library, recommendations, and playback off the Mac. On a Mac, your library and playback work without it. The terminal `login` works at any level; the features it unlocks need `api` or higher, and signing in through the assistant needs `web`.

**Web sign-in** is what plain `applemusic-mcp login` does, and all most people need. Your password never touches this tool, sign-in persists, and tokens re-fetch before they expire. At `--level web` you can also just ask your assistant to sign you in. (Web sign-in uses Apple's web-player API, the same path as open-source clients like [Cider](https://github.com/ciderapp/Cider-2) and [Music Assistant](https://www.music-assistant.io/music-providers/apple-music/).)

- **macOS — reads from a signed-in Safari (no Chrome, no ~500 MB Playwright):**

  ```bash
  applemusic-mcp login            # macOS default: harvests from Safari
  applemusic-mcp status           # verify
  ```

  One-time Safari setting (a security toggle — *you* enable it, the tool never flips it): **Safari → Settings → Advanced → "Show features for web developers"**, then the **Develop** menu → **"Allow JavaScript from Apple Events."** Sign into Apple Music at [music.apple.com](https://music.apple.com) in Safari first. That setting only lets the tool read **one cookie** — your Apple Music token — from your own signed-in Safari, and you can switch it back off afterward. If it's off or you're not signed in, `login` prints exactly how to fix it (or use `--dev`, or `--chrome`). Prefer Chrome on a Mac? `pip install 'applemusic-mcp[browser]'`, then `applemusic-mcp login --chrome`. Combined with native Music.app playback, a Mac needs no Chrome at all.

- **Windows / Linux — opens a local Chrome** (Playwright ships by default there; it's the only path):

  ```bash
  applemusic-mcp login            # opens Chrome to music.apple.com; sign in once
  ```

**Your own developer token, for bulk work.** Web sign-in uses Apple's *public* web-player token, whose request quota is shared. Interactive use never gets near it, but a few hundred catalog searches in an hour (a playlist import, a library migration) will hit `HTTP 429`, and the window is **rolling and ~60 minutes long**, so retrying extends it. The tool says so when you're throttled, rather than reporting "not found." With an [Apple Developer Program](https://developer.apple.com/programs/) membership you get your own, much larger quota, through Apple's sanctioned route:

1. [Developer Portal → Keys](https://developer.apple.com/account/resources/authkeys/list) → **+** → check **MusicKit** → Register → download the `.p8` (one time). Note your **Key ID** and **Team ID**.
2. Run `applemusic-mcp login --dev`. It prompts for the Team ID, Key ID and `.p8` path (or pass `--team-id`, `--key-id`, `--key-path`), writes the config, mints a 180-day token that renews on use, and authorizes you.

## What's sanctioned vs web

This server reaches Apple Music three ways and prefers the most official one available:

- **Apple Music API (sanctioned).** Your own developer token (from `login --dev`) against `api.music.apple.com`, Apple's documented API.
- **Web player (community path).** A token from a signed-in `music.apple.com` session against the web player's backend — the same approach as [Cider](https://github.com/ciderapp/Cider-2) and [Music Assistant](https://www.music-assistant.io/music-providers/apple-music/). It fills the few gaps the public API doesn't expose.
- **Music.app (macOS).** Local AppleScript automation of your own app. No tokens, no network.

With a developer token, writes go through the sanctioned API; the web path is used only for the operations Apple's public API can't do. With web sign-in alone, everything runs on the web path. On macOS, library and playlist edits can also run locally through Music.app. Each write tells you which path it took.

| Write | Apple Music API | Web player | Music.app (macOS) |
|---|:---:|:---:|:---:|
| Add to library | ✓ | ✓ | ✓ |
| Create playlist | ✓ | ✓ | ✓ |
| Add tracks (API-made playlist) | ✓ | ✓ | ✓ |
| Add tracks (Music.app-made playlist) | ✗ | ✗ | ✓ |
| Rate 1 to 5 | ✗ | ✗ | ✓ |
| Love / dislike | ✓ | ✓ | ✓ |
| Delete playlist | ✗ | ✓ | ✓ |
| Rename / move into folder | partial | ✓ | ✓ |

The `✗` cells are operations the column's path can't do, so they route elsewhere. One Apple constraint to know: **only the client that created a playlist can edit it**, so a playlist made in Music.app can't be written by *either* the dev-token API or the web player — on macOS those adds go through Music.app locally; off macOS, add to an API/web-created playlist instead.
