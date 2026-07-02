"""MusicKit JS — the single source of truth for the in-page Apple Music commands.

Each constant is a JavaScript arrow-function *expression* taking one argument (a
string or an object) and returning a value or a promise. The same JS runs through
two transports:

- ``browser.py`` (Playwright): ``page.evaluate(JS, arg)`` — awaits returned promises.
- ``safari_player.py`` (Safari): wrapped in an async IIFE and run via AppleScript
  ``do JavaScript`` (which does not await), then polled.

Keeping the JS in one place means the Chrome and Safari engines drive MusicKit
identically — fix a queue quirk once, both engines get it. Method names are
verified live against MusicKit v3 (``mk.playNext`` / ``mk.playLater`` /
``mk.changeToMediaAtIndex`` / ``mk.clearQueue`` / ``mk.autoplayEnabled`` /
``mk.queue.remove(i)``).
"""

# True once MusicKit is configured with both tokens (the web app sets them async).
_MUSICKIT_READY = (
    "() => { try { return !!(window.MusicKit && window.MusicKit.getInstance "
    "&& window.MusicKit.getInstance().developerToken "
    "&& window.MusicKit.getInstance().musicUserToken); } catch (e) { return false; } }"
)

# In-page add: make the SAME request the web player's "Add to Library" button
# fires — POST /v1/me/library?ids[songs]=<id> — using MusicKit's own tokens, from
# inside the authenticated music.apple.com page. No DOM clicking, no token
# exfiltration; the call is indistinguishable from the player's own traffic.
# (Validated out-of-band: this endpoint returns 202 on success.)
_ADD_JS = """
async (songId) => {
  const mk = window.MusicKit.getInstance();
  const resp = await fetch(
    'https://amp-api.music.apple.com/v1/me/library?ids[songs]=' + encodeURIComponent(songId),
    { method: 'POST',
      headers: { 'Authorization': 'Bearer ' + mk.developerToken,
                 'Music-User-Token': mk.musicUserToken } }
  );
  return resp.status;
}
"""

_PLAY_SONG_JS = """
async (songId) => {
  const mk = window.MusicKit.getInstance();
  await mk.setQueue({ song: songId, startPlaying: true });
  // mk.play() can HANG forever when the browser blocks audio until a real user
  // gesture (Safari "sticky activation"). Race it against a timeout so we return a
  // clear state fast instead of eating the whole poll budget.
  await Promise.race([mk.play().catch(() => {}), new Promise(r => setTimeout(r, 4000))]);
  const nm = mk.nowPlayingItem ? mk.nowPlayingItem.attributes.name : 'playing';
  // play() can resolve/‑time-out without audio actually starting (autoplay-blocked) —
  // don't claim "Playing" if the player isn't playing.
  return mk.isPlaying ? nm : (nm + ' [no audio — the player did not start]');
}
"""

_PLAY_QUEUE_JS = """
async (q) => {
  const mk = window.MusicKit.getInstance();
  const shuffle = !!q.__shuffle; delete q.__shuffle;
  await mk.setQueue(Object.assign({ startPlaying: false }, q));
  mk.shuffleMode = shuffle ? 1 : 0;
  // Race play() so a gesture-blocked player (Safari sticky activation) can't hang.
  await Promise.race([mk.play().catch(() => {}), new Promise(r => setTimeout(r, 4000))]);
  const nm = mk.nowPlayingItem ? mk.nowPlayingItem.attributes.name : 'playing';
  return mk.isPlaying ? nm : (nm + ' [no audio — the player did not start]');
}
"""

_CONTROL_JS = """
async (args) => {
  const mk = window.MusicKit.getInstance();
  const { action, seconds } = args;
  switch (action) {
    case 'play':     await Promise.race([mk.play().catch(() => {}), new Promise(r => setTimeout(r, 4000))]); return mk.isPlaying ? 'ok' : 'no-audio';
    case 'pause':    await mk.pause(); break;
    case 'stop':     await mk.stop(); break;
    case 'next':     await mk.skipToNextItem(); break;
    case 'previous': await mk.skipToPreviousItem(); break;
    case 'seek':     await mk.seekToTime(seconds || 0); break;
    default: return 'unknown action: ' + action;
  }
  return 'ok';
}
"""

# volume 0-100 (-1 = leave); shuffle/repeat null = leave.
_SETTINGS_JS = """
async (s) => {
  const mk = window.MusicKit.getInstance();
  if (s.volume >= 0) mk.volume = Math.max(0, Math.min(1, s.volume / 100));
  if (s.shuffle !== null) mk.shuffleMode = s.shuffle ? 1 : 0;
  if (s.repeat !== null) {
    // 'off' is the native engine's word for 'none' — accept both for parity.
    const map = { none: 0, off: 0, one: 1, all: 2 };
    mk.repeatMode = map[s.repeat] ?? 0;
  }
  return { volume: Math.round(mk.volume * 100), shuffle: mk.shuffleMode, repeat: mk.repeatMode };
}
"""

_NOW_PLAYING_JS = """
() => {
  const mk = window.MusicKit.getInstance();
  const it = mk && mk.nowPlayingItem;
  if (!it) return null;
  const a = it.attributes || {};
  // `state` mirrors native get_current_track so the server's engine-split logic
  // (which reads .state) shows web play/paused and suppresses the split nag when
  // the web "other" engine is paused.
  return {
    name: a.name, artist: a.artistName, album: a.albumName,
    playing: !!mk.isPlaying, state: mk.isPlaying ? 'playing' : 'paused',
    position: Math.round(mk.currentPlaybackTime || 0),
    duration: Math.round((a.durationInMillis || 0) / 1000),
  };
}
"""

_QUEUE_LIST_JS = """
() => {
  const mk = window.MusicKit.getInstance();
  const q = mk.queue;
  if (!q) return { position: -1, autoplay: !!mk.autoplayEnabled, items: [] };
  // Prefer the ACTUAL now-playing item for the position marker — q.position can
  // lag the real playhead when the queue auto-advances, so the ▶ marker and
  // now_playing would disagree. Fall back to q.position if no now-playing item.
  const np = mk.nowPlayingItem;
  const npid = np && np.id;
  let pos = q.position;
  const items = q.items.map((it, i) => {
    const a = (it && it.attributes) || {};
    if (npid && it.id === npid) pos = i;
    return { index: i, id: (it && it.id) || '', name: a.name || '', artist: a.artistName || '' };
  });
  return { position: pos, autoplay: !!mk.autoplayEnabled, items };
}
"""

_QUEUE_PLAY_NEXT_JS = """
async (id) => {
  const mk = window.MusicKit.getInstance();
  mk.autoplayEnabled = false;  // don't let a curated audition queue balloon into a station
  await mk.playNext({ song: id });
  return mk.queue.items.length;
}
"""

_QUEUE_PLAY_LATER_JS = """
async (id) => {
  const mk = window.MusicKit.getInstance();
  mk.autoplayEnabled = false;  // don't let a curated audition queue balloon into a station
  await mk.playLater({ song: id });
  return mk.queue.items.length;
}
"""

_QUEUE_SET_JS = """
async (ids) => {
  const mk = window.MusicKit.getInstance();
  mk.autoplayEnabled = false;  // a hand-set queue is curated — don't append a station
  await mk.setQueue({ songs: ids });
  return mk.queue.items.length;
}
"""

_QUEUE_REMOVE_JS = """
async (i) => {
  const mk = window.MusicKit.getInstance();
  const items = mk.queue.items;
  if (i < 0 || i >= items.length) return -1;
  // MusicKit throws an opaque mk-007 INVALID_ARGUMENTS when removing the
  // currently-playing item — detect it up front and signal -2 for a clear error.
  // Use the actual now-playing item's index (same basis as _QUEUE_LIST_JS's marker),
  // since mk.queue.position can lag the real playhead.
  const np = mk.nowPlayingItem;
  const playingIdx = np ? items.findIndex(it => it && it.id === np.id) : mk.queue.position;
  if (i === playingIdx) return -2;
  const r = mk.queue.remove(i);
  if (r && typeof r.then === 'function') await r;
  return mk.queue.items.length;
}
"""

_QUEUE_JUMP_BY_ID_JS = """
async (id) => {
  const mk = window.MusicKit.getInstance();
  mk.autoplayEnabled = false;  // jumping near the end shouldn't spawn an autoplay station
  const idx = mk.queue.items.findIndex(it => it && it.id === id);
  if (idx < 0) return -1;
  await mk.changeToMediaAtIndex(idx);
  return mk.queue.position;
}
"""

_QUEUE_CLEAR_JS = """
async () => {
  const mk = window.MusicKit.getInstance();
  await mk.clearQueue();
  return mk.queue.items.length;
}
"""

_QUEUE_JUMP_JS = """
async (i) => {
  const mk = window.MusicKit.getInstance();
  mk.autoplayEnabled = false;  // jumping near the end shouldn't spawn an autoplay station
  if (i < 0 || i >= mk.queue.items.length) return -1;
  await mk.changeToMediaAtIndex(i);
  return mk.queue.position;
}
"""

_QUEUE_AUTOPLAY_JS = """
(on) => {
  const mk = window.MusicKit.getInstance();
  mk.autoplayEnabled = !!on;
  return !!mk.autoplayEnabled;
}
"""
