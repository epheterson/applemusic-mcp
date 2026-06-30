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

pytestmark = [pytest.mark.ui]

# Captured before conftest's autouse harvest stub, so the live gate can harvest a
# REAL web token from Apple (amp-api rejects the stub).
_REAL_HARVEST = auth.harvest_developer_token


def _env_skip_reason() -> str:
    """Non-empty reason to skip, or '' when the live API environment is ready.
    Skips are CLEAN (env not ready) — never spurious failures."""
    if not os.environ.get("TEST_API"):
        return "live API gate is opt-in; run with TEST_API=1"
    if not auth.has_any_developer_token():
        return "no developer token (login --dev or a harvestable one) — can't reach the API"
    try:
        auth.get_user_token()
    except Exception:
        return "no media-user-token — run `applemusic-mcp signin` (or `authorize`)"
    return ""


@pytest.fixture(autouse=True)
def _real_token_storage(monkeypatch):
    """This gate runs against the user's REAL token storage (the OS keychain when
    available), NOT conftest's file-mode guard — otherwise a user whose tokens
    live in the keychain gets spurious 401s. Done with monkeypatch (per-test,
    auto-restored) so it NEVER leaks into the rest of the session. The skip check
    runs here, after the env is cleared, so it sees the real tokens."""
    monkeypatch.delenv("APPLEMUSIC_NO_KEYRING", raising=False)
    monkeypatch.setattr(auth, "harvest_developer_token", _REAL_HARVEST)  # real web token
    reason = _env_skip_reason()
    if reason:
        pytest.skip(reason)


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


_PROBE_TERMS = (
    "Bohemian Rhapsody Queen",
    "Billie Jean Michael Jackson",
    "Yesterday Beatles",
    "Smells Like Teen Spirit Nirvana",
    "Take On Me a-ha",
    "Africa Toto",
    "Mr. Brightside The Killers",
)


def _in_library(catalog_id: str, name: str) -> bool:
    """True if a catalog song already exists in the user's library."""
    for s in amp_api.search_library_songs(name):
        if s.get("catalog_id") == catalog_id or name.lower() in s.get("name", "").lower():
            return True
    return False


def _fresh_library_probe() -> dict:
    """A catalog song that ISN'T already in the user's library, so the add/remove
    round-trip only ever touches a song we add (never deletes one they own).
    Skips cleanly if every candidate is already in the library."""
    for term in _PROBE_TERMS:
        for song in amp_api.search_catalog_songs(term, 3):
            if not _in_library(song["id"], song["name"]):
                return song
    pytest.skip(
        "every probe candidate is already in your library — can't add/remove without touching it"
    )


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


def _remove_song_from_library(catalog_id: str, name: str, timeout: float = 20.0) -> bool:
    """Find the library-song copy of a catalog song and DELETE it (the verified
    /me/library/songs/{id}). Library indexing lags an add, so it polls."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for s in amp_api.search_library_songs(name):
            if s.get("catalog_id") == catalog_id or name.lower() in s.get("name", "").lower():
                ok, _ = amp_api.remove_from_library(s["id"])
                return ok
        time.sleep(2.0)
    return False


class TestLiveApiGate:
    """One consolidated mutation lifecycle + one rating roundtrip. Designed for
    MINIMAL live API calls and ZERO residue on the real account: a single
    throwaway playlist + folder + one fresh song, all cleaned up; the rating is
    snapshotted and restored. Together these still cover every mutation path:
    create/add/remove/delete playlist, folder create/move/delete, catalog→library
    add, remove-from-library (DELETE), and love/clear."""

    def test_full_mutation_lifecycle(self):
        """Thread ONE fresh song through ONE playlist inside ONE folder, then
        scrub everything. Covers playlist create/add/remove/delete, folder
        create/move/delete, library add, and remove-from-library — the
        DELETE-on-amp-host regression included (delete_playlist must 2xx where
        the public host 401s)."""
        song = _fresh_library_probe()  # not already in your library → safe to remove
        fid = pid = None
        added_to_library = False
        try:
            ok, fid = amp_api.create_folder(_unique("GATE_FOLDER"))
            assert ok, f"create_folder failed: {fid}"

            ok, pid = amp_api.create_playlist(_unique("GATE"))
            assert ok, f"create_playlist failed: {pid}"

            moved, mmsg = amp_api.move_playlist_to_folder(pid, fid)
            assert moved, f"move_playlist_to_folder failed: {mmsg}"
            assert _wait_for_folder(fid), "new folder never appeared in the listing"

            # catalog → library add (the #37 flow), then add that to the playlist.
            result = server.library(action="add", track=song["id"])
            assert "Error" not in result, result
            added_to_library = True

            added, amsg = amp_api.add_tracks(pid, [song["id"]])
            assert added, f"add_tracks failed: {amsg}"
            track = _wait_for_track(pid, song["name"])
            assert track is not None, f"added track never appeared in playlist {pid}"

            removed, rmsg = amp_api.remove_track(pid, track["relationship_id"])
            assert removed, f"remove_track failed: {rmsg}"

            assert _remove_song_from_library(
                song["id"], song["name"]
            ), "added song never became removable from the library (indexing lag)"
            added_to_library = False
        finally:
            # Scrub everything, defensively, regardless of where an assert fired.
            if pid:
                amp_api.move_playlist_to_folder(pid, amp_api.ROOT_FOLDER)
                amp_api.delete_playlist(pid)
            if fid:
                amp_api.delete_folder(fid)
            if added_to_library:
                _remove_song_from_library(song["id"], song["name"])

    def test_rating_roundtrip(self):
        """Love + verify the love/dislike surface WITHOUT clobbering a rating you
        already have: snapshot, love, verify, restore the original (clear if it
        was unrated). Net change to your data: zero."""
        song = _probe_catalog_song()
        prev = amp_api.get_rating(song["id"])  # 1 | -1 | None
        try:
            loved, lmsg = amp_api.rate(song["id"], 1)
            assert loved, f"love failed: {lmsg}"
            assert amp_api.get_rating(song["id"]) == 1, "love didn't take"
        finally:
            amp_api.rate(song["id"], prev if prev else 0)
