"""Manual off-mac smoke test. Run inside the Linux try-out container AFTER
`applemusic-mcp login`. Exercises the cross-platform path end to end: catalog
search (API) → play (Chrome web player) → now_playing → queue.

    python ~/smoke.py

This calls the MCP tool functions directly (no MCP client needed). It's a
starting point — edit the track names to taste.
"""

import time

from applemusic_mcp import server


def show(label, result):
    print(f"\n=== {label} ===\n{result}")


# 1) Catalog search — pure API, works with no browser.
show("catalog search (API)", server.catalog(action="search", query="Coltrane Naima", limit=3))

# 2) Play a track — routes to the Chrome web player off-mac.
show(
    "play (Chrome web player)",
    server.playback(action="play", track="Naima", artist="John Coltrane"),
)
time.sleep(6)

# 3) What's playing — should show the Chrome engine.
show("now_playing", server.playback(action="now_playing"))

# 4) Queue something next, then list the queue.
show("queue play_next", server.queue(action="play_next", track="So What", artist="Miles Davis"))
show("queue list", server.queue(action="list"))

print("\nDone. If play/now_playing show a track, the off-mac path works.")
