"""Live pre-release integration gate for the API engine (catalog→library/playlist,
folders, move, ratings).

WHY THIS FILE EXISTS
--------------------
GitHub CI cannot validate these flows: they require a real Apple Music account
(active subscription) with a developer token (generated OR harvested) and a
captured media-user-token. That gap is exactly how #28/#37 shipped breakage —
a change that "passes CI" can still be broken against the live account. These
tests are the local gate that runs on a real, signed-in account BEFORE a
release (see ``scripts/preflight.sh`` / ``RELEASING.md``).

They hit the live ``amp-api.music.apple.com`` and MUTATE the real account, so:
  * they are gated behind ``TEST_API=1`` and skipped otherwise (CI, headless),
  * they SKIP cleanly (never fail spuriously) when the environment isn't ready
    (no tokens, no network, storefront can't resolve a probe track),
  * playlists/folders are deleted in teardown, and every playlist/folder name
    uses the ``_UI_TEST_`` prefix that ``conftest.py``'s session sweep also
    cleans (belt and suspenders if a test aborts before its inline cleanup),
  * ratings are cleared in teardown, and the probe song the library-add test
    files is removed again via the verified DELETE /me/library/songs/{id} — so
    the gate is fully self-cleaning and leaves NO residue.

This is the API successor to the old UI-automation gate: the fragile Music.app
UI add path (split across macOS/Music.app versions) was removed in favor of the
cross-platform API, so the gate now exercises the API surface instead.

Run locally (on the signed-in account)::

    TEST_API=1 uv run pytest tests/test_live_integration.py -v

The whole suite is also marked ``ui`` so the default ``-m "not ui"`` run skips it.
"""

import os
import time

import pytest

from applemusic_mcp import amp_api
from applemusic_mcp import auth
from applemusic_mcp import server

# --- environment preflight -------------------------------------------------

_PREFIX = "_UI_TEST_"


def _env_skip_reason() -> str:
    """Non-empty reason to skip, or '' when the live API environment is ready.
    Skips are CLEAN (env not ready) — never spurious failures."""
    if not os.environ.get("TEST_API"):
        return "live API gate is opt-in; run with TEST_API=1"
    if not auth.has_any_developer_token():
        return "no developer token (generate-token or a harvestable one) — can't reach the API"
    try:
        auth.get_user_token()
    except Exception:
        return "no media-user-token — run `applemusic-mcp signin` (or `authorize`)"
    return ""


pytestmark = [
    pytest.mark.ui,
    pytest.mark.skipif(bool(_env_skip_reason()), reason=_env_skip_reason()),
]


def _unique(suffix: str) -> str:
    """A collision-proof, sweep-matchable test name."""
    return f"{_PREFIX}{suffix}_{time.time_ns()}"


def _probe_catalog_song() -> dict:
    """Resolve a stable, widely-available catalog song for the signed-in
    storefront. Skips (not fails) if the catalog search can't resolve one —
    that's an environment problem, not a regression."""
    for term in ("Bohemian Rhapsody Queen", "Billie Jean Michael Jackson", "Yesterday Beatles"):
        songs = amp_api.search_catalog_songs(term, 1)
        if songs:
            return songs[0]
    pytest.skip("catalog search resolved no probe song (storefront/network)")


def _wait_for_track(playlist_id: str, name_fragment: str, timeout: float = 12.0) -> dict | None:
    """Poll a playlist until a track whose name contains ``name_fragment`` shows
    up (newly-added tracks propagate with a short lag), or return None on timeout."""
    deadline = time.time() + timeout
    frag = name_fragment.lower()
    while time.time() < deadline:
        for t in amp_api.get_tracks(playlist_id):
            if frag in t.get("name", "").lower():
                return t
        time.sleep(1.0)
    return None


def _wait_for_folder(folder_id: str, timeout: float = 20.0) -> bool:
    """Poll the folder listing until ``folder_id`` shows up — brand-new folders
    propagate to the listing with a lag (longer than playlist tracks)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if any(f["id"] == folder_id for f in amp_api.list_folders()):
            return True
        time.sleep(1.5)
    return False


# --- the gate --------------------------------------------------------------


class TestLiveApiPlaylist:
    def test_playlist_add_remove_lifecycle(self):
        """create → add catalog track → it appears → remove that track → delete.
        Uses the playlist id from create throughout, so it doesn't depend on the
        name-resolution propagation lag."""
        song = _probe_catalog_song()
        ok, pid = amp_api.create_playlist(_unique("LIFECYCLE"))
        assert ok, f"create_playlist failed: {pid}"
        try:
            added, msg = amp_api.add_tracks(pid, [song["id"]])
            assert added, f"add_tracks failed: {msg}"

            track = _wait_for_track(pid, song["name"])
            assert track is not None, f"added track never appeared in playlist {pid}"

            removed, rmsg = amp_api.remove_track(pid, track["relationship_id"])
            assert removed, f"remove_track failed: {rmsg}"
        finally:
            amp_api.delete_playlist(pid)

    def test_delete_playlist_on_amp_host(self):
        """Regression for the DELETE-is-broken finding: the public host 401s,
        amp-api must accept it (this is what shipped broken in branch A)."""
        ok, pid = amp_api.create_playlist(_unique("DELETE"))
        assert ok, f"create_playlist failed: {pid}"
        deleted, msg = amp_api.delete_playlist(pid)
        assert deleted, f"delete_playlist failed on amp host: {msg}"


class TestLiveApiFolders:
    def test_folder_create_move_delete(self):
        """create folder → move a playlist into it (a capability the web UI only
        exposes via drag) → folder lists → clean up."""
        ok, fid = amp_api.create_folder(_unique("FOLDER"))
        assert ok, f"create_folder failed: {fid}"
        pid = None
        try:
            okp, pid = amp_api.create_playlist(_unique("FOLDER_CHILD"))
            assert okp, f"create_playlist failed: {pid}"

            moved, mmsg = amp_api.move_playlist_to_folder(pid, fid)
            assert moved, f"move_playlist_to_folder failed: {mmsg}"

            assert _wait_for_folder(fid), "new folder never appeared in the listing"
        finally:
            if pid:
                # Move back to root before deleting the folder (defensive), then delete both.
                amp_api.move_playlist_to_folder(pid, amp_api.ROOT_FOLDER)
                amp_api.delete_playlist(pid)
            amp_api.delete_folder(fid)


class TestLiveApiRatings:
    def test_love_then_clear(self):
        """love a catalog song, then clear the rating — the love/dislike surface
        that replaced the old star-rating exposure."""
        song = _probe_catalog_song()
        loved, lmsg = amp_api.rate(song["id"], 1)
        assert loved, f"love failed: {lmsg}"
        cleared, cmsg = amp_api.rate(song["id"], 0)
        assert cleared, f"clear-rating failed: {cmsg}"


class TestLiveLibraryAdd:
    def test_add_then_remove_from_library(self):
        """The server-level catalog→library add (the #37 flow) end to end, then
        remove it again via the verified DELETE /me/library/songs/{id} — so the
        gate leaves NO residue. Library indexing lags, so the removal polls."""
        song = _probe_catalog_song()
        result = server.library(action="add", track=song["id"])
        assert "Error" not in result, result
        assert song["id"] in result or song["name"].split()[0].lower() in result.lower()

        # Wait for the library to index it, then remove by its library-song id.
        deadline = time.time() + 20.0
        removed = False
        while time.time() < deadline and not removed:
            for s in amp_api.search_library_songs(song["name"]):
                if s.get("catalog_id") == song["id"] or song["name"].lower() in s["name"].lower():
                    ok, msg = amp_api.remove_from_library(s["id"])
                    assert ok, f"remove_from_library failed: {msg}"
                    removed = True
                    break
            if not removed:
                time.sleep(2.0)
        assert removed, "added song never became removable from the library (indexing lag)"
