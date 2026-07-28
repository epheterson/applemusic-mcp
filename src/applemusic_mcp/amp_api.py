"""amp-api operations — the cross-platform ``api`` engine.

These are the library/playlist operations that the music.apple.com web player
performs, against ``amp-api.music.apple.com`` (more permissive than the public
``api.music.apple.com`` — e.g. it accepts playlist DELETE where the public host
401s). Every call here was captured/verified against the live web player.

Auth: a developer token (generated, else harvested) + the captured media-user-token,
plus ``Origin: https://music.apple.com`` (the web-player token is origin-bound).

This module is engine-agnostic — it has no AppleScript and works on any platform
once the user has signed in. The server routes here in ``api`` mode (and in
``auto`` when native isn't the better fit).
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import requests

from . import auth

AMP = "https://amp-api.music.apple.com/v1"
ORIGIN = "https://music.apple.com"
TIMEOUT = 30
_OK = (200, 201, 202, 204)

logger = logging.getLogger(__name__)


# --- rate-limit state ------------------------------------------------------
#
# Apple throttles on a ROLLING 60-MINUTE window and returns no ``Retry-After``
# or ``X-Rate-Limit`` header on the web-player path, so remaining quota is
# unobservable — you only learn you're over by getting a 429 (#42). Two
# consequences this state exists to handle:
#
#   1. The reads below swallow non-200 and return empty, so a throttle looks
#      exactly like "no such song". Callers that would otherwise report a
#      false "not found" ask ``throttled_recently()`` instead.
#   2. Probing to find out (``session_status()``) costs a REQUEST, and every
#      request inside a rolling window pushes the recovery further out. So a
#      known-recent 429 short-circuits the probe rather than spending one.

_THROTTLE_STICKY_SECONDS = 120.0

# Tracked PER RAIL, because the two hosts don't share a quota: the public
# ``api.music.apple.com`` path uses whichever developer token is resolved, while
# amp-api uses the harvested web-player token. With a generated dev token those
# are genuinely separate budgets, so a 200 on one rail must NOT clear a live 429
# on the other — that would resurrect the false "not found" this exists to kill.
# (On the tokenless path both rails ride the same shared token and throttle
# together, which the per-rail split handles correctly by coincidence.)
WEB = "web"  # amp-api.music.apple.com, harvested web-player token
API = "api"  # api.music.apple.com, developer token
_last_429_at: dict[str, float] = {WEB: 0.0, API: 0.0}


def note_status(code: int, rail: str = WEB) -> None:
    """Record an HTTP status seen on ``rail``, so an empty result can later be
    attributed to a throttle rather than to "not found".

    The marker means "the last thing Apple told us on this rail was 429" — a
    success on the SAME rail clears it, so a genuine no-such-song is never
    mislabelled just because a throttle happened a minute ago."""
    if 200 <= code < 300:
        _last_429_at[rail] = 0.0
    elif code == 429:
        _last_429_at[rail] = time.monotonic()
        logger.warning(
            "Apple Music returned HTTP 429 (rate limited) on the %s rail. Apple's window "
            "is rolling and ~60 min long, and no Retry-After header is sent. Results may "
            "come back EMPTY rather than as errors until it clears. For bulk catalog work "
            "use `applemusic-mcp login --dev` (your own developer token gets its own "
            "quota), or resolve by ISRC to spend 25x fewer requests.",
            rail,
        )


def throttled_recently(
    within: float = _THROTTLE_STICKY_SECONDS, rail: Optional[str] = None
) -> bool:
    """True if a 429 was seen in the last ``within`` seconds — on ``rail`` if
    given, otherwise on either rail (the caller doesn't always know which rail
    produced the empty result it's trying to explain)."""
    now = time.monotonic()
    rails = [rail] if rail else list(_last_429_at)
    return any(_last_429_at[r] > 0 and (now - _last_429_at[r]) < within for r in rails)


def reset_throttle_state() -> None:
    """Clear the sticky 429 markers (tests, and after a confirmed-good call)."""
    for r in _last_429_at:
        _last_429_at[r] = 0.0


def _err(r: requests.Response, action: str) -> str:
    """Format a failed amp-api response into a user-facing reason — and log the
    full status + response body so failures are debuggable from the MCP logs
    (enolgor asked for the 500 error bodies in #37; they were being discarded).
    Apple returns a JSON ``errors`` array with the real cause, which is exactly
    what you need to tell a transient 500 from a bad request."""
    body = (r.text or "").strip()
    logger.warning("amp-api %s: HTTP %s — %s", action, r.status_code, body[:2000] or "(empty body)")
    note_status(r.status_code)
    reason = f"status {r.status_code}"
    if r.status_code in (401, 403):
        reason += " — not authorized, re-run `applemusic-mcp login`"
    if r.status_code == 429:
        reason += " — rate limited; wait (Apple's window is rolling, up to ~60 min)"
    snippet = body[:200].replace("\n", " ").strip()
    return f"{reason}: {snippet}" if snippet else reason


def _headers() -> dict:
    # amp-api.music.apple.com is the web player's host: it accepts the harvested
    # AMPWebPlay token and 401s a generated (Apple Developer) token. Always use
    # the web token here — NOT resolve_developer_token(), which prefers generated.
    return {
        "Authorization": f"Bearer {auth.resolve_web_token()}",
        "Music-User-Token": auth.get_user_token(),
        "Content-Type": "application/json",
        "Origin": ORIGIN,
    }


def _loose_eq(a: str, b: str) -> bool:
    return a.strip().lower() == b.strip().lower()


def session_status() -> str:
    """Cheap one-request probe of the live session: ``ok`` | ``expired`` |
    ``throttled`` | ``error``. The reads below swallow errors and return empty,
    so a resolver can't tell "genuinely not found" from "your token expired".
    Call this on the failure path to turn a misleading "not found" into the
    real cause (an expired session or a 429), at the cost of one extra GET.

    A 429 seen in the last couple of minutes answers without spending that GET:
    during a rolling-window throttle the probe would both fail and add to the
    very count that's keeping us throttled."""
    if throttled_recently():
        return "throttled"
    try:
        r = requests.get(
            f"{AMP}/me/library/playlists", headers=_headers(), params={"limit": 1}, timeout=TIMEOUT
        )
        note_status(r.status_code)
        if r.status_code == 200:
            return "ok"
        if r.status_code in (401, 403):
            return "expired"
        if r.status_code == 429:
            return "throttled"
        return "error"
    except Exception:
        return "error"


# --- reads -----------------------------------------------------------------


def list_playlists() -> list[dict]:
    """All library playlists: [{id, name, canEdit}]. Paginates. (canEdit is
    amp-api's API-created flag; callers decide whether to filter on it.)"""
    out: list[dict] = []
    url: Optional[str] = f"{AMP}/me/library/playlists?limit=100"
    try:
        h = _headers()
        while url:
            r = requests.get(url, headers=h, timeout=TIMEOUT)
            note_status(r.status_code)
            if r.status_code != 200:
                break
            data = r.json()
            for pl in data.get("data", []):
                attrs = pl.get("attributes", {})
                out.append(
                    {
                        "id": pl.get("id"),
                        "name": attrs.get("name", ""),
                        "canEdit": attrs.get("canEdit", True),
                        # globalId tells a USER playlist (pl.u-…) from an
                        # Apple-curated one (catalog hash) — see playlist_kind.
                        "globalId": (attrs.get("playParams") or {}).get("globalId") or "",
                    }
                )
            nxt = data.get("next")
            url = (
                (ORIGIN.replace("music.apple.com", "amp-api.music.apple.com") + nxt)
                if nxt
                else None
            )
    except Exception:
        pass
    return out


def playlist_kind(pl: dict) -> str:
    """Classify a library playlist by ORIGIN — what determines who can write it:

      ``api``    — canEdit=True: created via the API; the generated developer token
                   can write it (and the web/native rails can too).
      ``user``   — canEdit=False with no catalog globalId, or a ``pl.u-…`` one: the
                   user's OWN Music.app playlist. Writable via AppleScript on macOS;
                   the amp-api REST add 500s on these (verified 2026-06-28), so the
                   dev-token rail can't, and cross-platform support needs the
                   in-page web player.
      ``apple``  — canEdit=False with a CATALOG globalId (``pl.<hash>``): an
                   Apple-curated playlist added to the library. Read-only — NO rail
                   can add tracks to it.

    (Smart playlists also surface as ``user`` here; callers on macOS detect those
    via AppleScript's ``smart`` flag and reject the add separately.)"""
    if pl.get("canEdit", True):
        return "api"
    gid = pl.get("globalId") or ""
    if gid and not gid.startswith("pl.u-"):
        return "apple"
    return "user"


def is_api_created(pl: dict) -> bool:
    """True only for API-created playlists (the dev-token rail can write these).
    Thin wrapper over ``playlist_kind`` for the common two-way check."""
    return playlist_kind(pl) == "api"


def resolve_playlist(name: str, api_created_only: bool = True) -> Optional[dict]:
    """Resolve a library playlist by name to its full ``{id, name, canEdit}`` dict
    — exact match first, then loose substring. Returns the dict (so callers can
    see ``canEdit`` / origin), or None.

    ``api_created_only`` (default) considers only API-created playlists, because
    the generated-dev-token rail can only write those. Pass ``False`` to also
    match Music.app-made playlists — the web session can write those, and any rail
    can read/play them."""
    loose = None
    for pl in list_playlists():
        if api_created_only and not is_api_created(pl):
            continue
        if _loose_eq(pl["name"], name):
            return pl
        if name.strip().lower() in pl["name"].strip().lower():
            loose = loose or pl
    return loose


def resolve_playlist_id(name: str, api_created_only: bool = True) -> Optional[str]:
    """Library playlist id (p.xxxx) by name. Thin wrapper over resolve_playlist."""
    pl = resolve_playlist(name, api_created_only=api_created_only)
    return pl["id"] if pl else None


def get_tracks(playlist_id: str) -> list[dict]:
    """Tracks in a playlist: [{relationship_id, name, artist, catalog_id}]. Paginates."""
    out: list[dict] = []
    url: Optional[str] = f"{AMP}/me/library/playlists/{playlist_id}/tracks?limit=100"
    try:
        h = _headers()
        while url:
            r = requests.get(url, headers=h, timeout=TIMEOUT)
            note_status(r.status_code)
            if r.status_code != 200:
                break
            data = r.json()
            for t in data.get("data", []):
                a = t.get("attributes", {})
                pp = a.get("playParams", {})
                out.append(
                    {
                        "relationship_id": t.get("id"),  # i.xxx — needed for remove
                        "name": a.get("name", ""),
                        "artist": a.get("artistName", ""),
                        "catalog_id": pp.get("catalogId") or pp.get("id"),
                    }
                )
            nxt = data.get("next")
            url = ("https://amp-api.music.apple.com" + nxt) if nxt else None
    except Exception:
        pass
    return out


def search_library_songs(term: str, limit: int = 25) -> list[dict]:
    """Search the user's library: [{id, name, artist, catalog_id}]. The ``id`` is
    the library-song id (i.xxx) needed to remove it from the library."""
    try:
        r = requests.get(
            f"{AMP}/me/library/search",
            headers=_headers(),
            params={"term": term, "types": "library-songs", "limit": min(limit, 25)},
            timeout=TIMEOUT,
        )
        note_status(r.status_code)
        if r.status_code != 200:
            return []
        songs = r.json().get("results", {}).get("library-songs", {}).get("data", [])
        out = []
        for s in songs:
            a = s.get("attributes", {})
            pp = a.get("playParams", {})
            out.append(
                {
                    "id": s.get("id"),  # library-song id (i.xxx) — needed for removal
                    "name": a.get("name", ""),
                    "artist": a.get("artistName", ""),
                    "catalog_id": pp.get("catalogId") or pp.get("id"),
                }
            )
        return out
    except Exception:
        return []


def search_catalog_songs(term: str, limit: int = 5) -> list[dict]:
    """Catalog song search: [{id, name, artist}]."""
    try:
        store = auth.get_user_preferences().get("storefront", "us")
        r = requests.get(
            f"{AMP}/catalog/{store}/search",
            headers=_headers(),
            params={"term": term, "types": "songs", "limit": min(limit, 25)},
            timeout=TIMEOUT,
        )
        # An empty return on failure is indistinguishable from "no such song",
        # which is how a throttle turns into a bulk run full of false negatives
        # (#42). Record the status so the caller can name the real cause.
        note_status(r.status_code)
        if r.status_code != 200:
            return []
        songs = r.json().get("results", {}).get("songs", {}).get("data", [])
        return [
            {
                "id": s["id"],
                "name": s["attributes"].get("name", ""),
                "artist": s["attributes"].get("artistName", ""),
            }
            for s in songs
        ]
    except Exception:
        return []


# --- mutations -------------------------------------------------------------


def create_playlist(name: str, description: str = "") -> tuple[bool, str]:
    """Create a library playlist. Returns (ok, playlist_id or error)."""
    attrs: dict = {"name": name}
    if description:
        attrs["description"] = description
    try:
        r = requests.post(
            f"{AMP}/me/library/playlists",
            headers=_headers(),
            json={"attributes": attrs},
            timeout=TIMEOUT,
        )
        if r.status_code in _OK:
            return True, r.json()["data"][0]["id"]
        return False, _err(r, "create_playlist")
    except Exception as e:
        return False, str(e)


def rename_playlist(playlist_id: str, name: str) -> tuple[bool, str]:
    """Rename a library playlist (PATCH attributes.name)."""
    try:
        r = requests.patch(
            f"{AMP}/me/library/playlists/{playlist_id}",
            headers=_headers(),
            json={"attributes": {"name": name}},
            timeout=TIMEOUT,
        )
        return (r.status_code in _OK), (
            f"Renamed to {name}" if r.status_code in _OK else _err(r, "rename_playlist")
        )
    except Exception as e:
        return False, str(e)


def add_tracks(playlist_id: str, items: list) -> tuple[bool, str]:
    """Add songs to a playlist. ``items`` is a list of either catalog ids (str →
    ``type: songs``) or ``(id, type)`` tuples — e.g. ``(lib_id, "library-songs")``
    to add a song already in the library."""
    if not items:
        return False, "no track ids"
    data = [
        {"id": it[0], "type": it[1]} if isinstance(it, tuple) else {"id": it, "type": "songs"}
        for it in items
    ]
    try:
        r = requests.post(
            f"{AMP}/me/library/playlists/{playlist_id}/tracks",
            headers=_headers(),
            json={"data": data},
            timeout=TIMEOUT,
        )
        return (r.status_code in _OK), (
            f"Added {len(items)} track(s)" if r.status_code in _OK else _err(r, "add_tracks")
        )
    except Exception as e:
        return False, str(e)


def remove_track(playlist_id: str, relationship_id: str) -> tuple[bool, str]:
    """Remove ONE track from a playlist — the web player's real per-track call:
    DELETE .../tracks?ids[library-songs]={relationshipId}&mode=all (the id is the
    playlist-track relationship id, i.xxx, from get_tracks)."""
    try:
        r = requests.delete(
            f"{AMP}/me/library/playlists/{playlist_id}/tracks",
            headers=_headers(),
            params={"ids[library-songs]": relationship_id, "mode": "all"},
            timeout=TIMEOUT,
        )
        return (r.status_code in _OK), (
            "Removed" if r.status_code in _OK else _err(r, "remove_track")
        )
    except Exception as e:
        return False, str(e)


def remove_from_library(library_song_id: str) -> tuple[bool, str]:
    """Remove ONE song from the user's library — the web player's real call,
    verified live: DELETE /me/library/songs/{libraryId} (the id is the
    library-song id, i.xxx, from search_library_songs/get_tracks). Returns 204."""
    try:
        r = requests.delete(
            f"{AMP}/me/library/songs/{library_song_id}", headers=_headers(), timeout=TIMEOUT
        )
        if r.status_code in _OK:
            return True, "Removed from library"
        return False, _err(r, "remove_from_library")
    except Exception as e:
        return False, str(e)


def delete_playlist(playlist_id: str) -> tuple[bool, str]:
    """Delete a library playlist (amp-api accepts it; the public host 401s)."""
    try:
        r = requests.delete(
            f"{AMP}/me/library/playlists/{playlist_id}", headers=_headers(), timeout=TIMEOUT
        )
        if r.status_code in _OK:
            return True, "Deleted"
        return False, _err(r, "delete_playlist")
    except Exception as e:
        return False, str(e)


# --- folders (incl. moves the web UI doesn't even expose) ------------------

ROOT_FOLDER = "p.playlistsroot"
_FOLDER_TYPE = "library-playlist-folders"


def create_folder(name: str, parent_id: str = ROOT_FOLDER) -> tuple[bool, str]:
    """Create a playlist folder under ``parent_id`` (default the library root).
    Pass another folder's id to nest. Returns (ok, folder_id or error)."""
    try:
        r = requests.post(
            f"{AMP}/me/library/playlist-folders",
            headers=_headers(),
            json={
                "attributes": {"name": name},
                "relationships": {"parent": {"data": [{"id": parent_id, "type": _FOLDER_TYPE}]}},
            },
            timeout=TIMEOUT,
        )
        if r.status_code in _OK:
            return True, r.json()["data"][0]["id"]
        return False, _err(r, "create_folder")
    except Exception as e:
        return False, str(e)


def delete_folder(folder_id: str) -> tuple[bool, str]:
    try:
        r = requests.delete(
            f"{AMP}/me/library/playlist-folders/{folder_id}", headers=_headers(), timeout=TIMEOUT
        )
        return (r.status_code in _OK), (
            "Deleted" if r.status_code in _OK else _err(r, "delete_folder")
        )
    except Exception as e:
        return False, str(e)


def list_folders() -> list[dict]:
    """Top-level folders: [{id, name}]."""
    try:
        r = requests.get(
            f"{AMP}/me/library/playlist-folders/{ROOT_FOLDER}/children",
            headers=_headers(),
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            return []
        return [
            {"id": c["id"], "name": c["attributes"].get("name", "")}
            for c in r.json().get("data", [])
            if c.get("type") == _FOLDER_TYPE
        ]
    except Exception:
        return []


def resolve_folder_id(name: str) -> Optional[str]:
    for f in list_folders():
        if _loose_eq(f["name"], name):
            return f["id"]
    return None


def move_playlist_to_folder(playlist_id: str, folder_id: str = ROOT_FOLDER) -> tuple[bool, str]:
    """Move a playlist into a folder (or the root) — PUT the parent relationship.
    Note: the web player UI has no 'move to folder', so this goes beyond it."""
    try:
        r = requests.put(
            f"{AMP}/me/library/playlists/{playlist_id}/parent",
            headers=_headers(),
            json={"data": [{"id": folder_id, "type": _FOLDER_TYPE}]},
            timeout=TIMEOUT,
        )
        return (r.status_code in _OK), (
            "Moved" if r.status_code in _OK else _err(r, "move_playlist_to_folder")
        )
    except Exception as e:
        return False, str(e)


# --- ratings (love / dislike) ----------------------------------------------


def get_rating(catalog_id: str, content_type: str = "songs") -> Optional[int]:
    """Current rating for a catalog item: 1 (loved), -1 (disliked), or None if
    unrated. Lets a caller snapshot a rating before changing it and restore it
    afterward (the live test gate uses this to leave no residue). Returns None
    on any error rather than raising."""
    try:
        r = requests.get(
            f"{AMP}/me/ratings/{content_type}/{catalog_id}", headers=_headers(), timeout=TIMEOUT
        )
        if r.status_code != 200:
            return None  # 404 = unrated
        data = r.json().get("data", [])
        if not data:
            return None
        return data[0].get("attributes", {}).get("value")
    except Exception:
        return None


def rate(catalog_id: str, value: int, content_type: str = "songs") -> tuple[bool, str]:
    """Love (value=1) or dislike (value=-1) a catalog item. value=0 clears."""
    try:
        if value == 0:
            r = requests.delete(
                f"{AMP}/me/ratings/{content_type}/{catalog_id}", headers=_headers(), timeout=TIMEOUT
            )
        else:
            r = requests.put(
                f"{AMP}/me/ratings/{content_type}/{catalog_id}",
                headers=_headers(),
                json={"attributes": {"value": 1 if value > 0 else -1}},
                timeout=TIMEOUT,
            )
        rv = 1 if value > 0 else (-1 if value < 0 else 0)
        label = {1: "Loved", -1: "Disliked", 0: "Cleared rating"}[rv]
        return (r.status_code in _OK), (label if r.status_code in _OK else _err(r, "rate"))
    except Exception as e:
        return False, str(e)
