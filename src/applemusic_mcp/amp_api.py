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

from typing import Optional

import requests

from . import auth

AMP = "https://amp-api.music.apple.com/v1"
ORIGIN = "https://music.apple.com"
TIMEOUT = 30
_OK = (200, 201, 202, 204)


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {auth.resolve_developer_token()}",
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
    real cause (an expired session or a 429), at the cost of one extra GET."""
    try:
        r = requests.get(
            f"{AMP}/me/library/playlists", headers=_headers(), params={"limit": 1}, timeout=TIMEOUT
        )
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
    """All editable library playlists: [{id, name}]. Paginates."""
    out: list[dict] = []
    url: Optional[str] = f"{AMP}/me/library/playlists?limit=100"
    try:
        h = _headers()
        while url:
            r = requests.get(url, headers=h, timeout=TIMEOUT)
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


def resolve_playlist_id(name: str) -> Optional[str]:
    """Library playlist id (p.xxxx) by name — exact match first, then loose."""
    loose = None
    for pl in list_playlists():
        if not pl.get("canEdit", True):
            continue
        if _loose_eq(pl["name"], name):
            return pl["id"]
        if name.strip().lower() in pl["name"].strip().lower():
            loose = loose or pl["id"]
    return loose


def get_tracks(playlist_id: str) -> list[dict]:
    """Tracks in a playlist: [{relationship_id, name, artist, catalog_id}]. Paginates."""
    out: list[dict] = []
    url: Optional[str] = f"{AMP}/me/library/playlists/{playlist_id}/tracks?limit=100"
    try:
        h = _headers()
        while url:
            r = requests.get(url, headers=h, timeout=TIMEOUT)
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
        return False, f"create failed (status {r.status_code})"
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
            f"Renamed to {name}" if r.status_code in _OK else f"status {r.status_code}"
        )
    except Exception as e:
        return False, str(e)


def add_tracks(playlist_id: str, catalog_ids: list[str]) -> tuple[bool, str]:
    """Add catalog songs to a playlist by catalog id."""
    if not catalog_ids:
        return False, "no track ids"
    try:
        r = requests.post(
            f"{AMP}/me/library/playlists/{playlist_id}/tracks",
            headers=_headers(),
            json={"data": [{"id": cid, "type": "songs"} for cid in catalog_ids]},
            timeout=TIMEOUT,
        )
        return (r.status_code in _OK), (
            f"Added {len(catalog_ids)} track(s)"
            if r.status_code in _OK
            else f"status {r.status_code}"
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
            "Removed" if r.status_code in _OK else f"status {r.status_code}"
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
        if r.status_code in (401, 403):
            return False, f"not authorized (status {r.status_code}) — re-run signin"
        return False, f"status {r.status_code}"
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
        if r.status_code in (401, 403):
            return False, f"not authorized (status {r.status_code}) — re-run signin"
        return False, f"status {r.status_code}"
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
        return False, f"status {r.status_code}"
    except Exception as e:
        return False, str(e)


def delete_folder(folder_id: str) -> tuple[bool, str]:
    try:
        r = requests.delete(
            f"{AMP}/me/library/playlist-folders/{folder_id}", headers=_headers(), timeout=TIMEOUT
        )
        return (r.status_code in _OK), (
            "Deleted" if r.status_code in _OK else f"status {r.status_code}"
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
            "Moved" if r.status_code in _OK else f"status {r.status_code}"
        )
    except Exception as e:
        return False, str(e)


# --- ratings (love / dislike) ----------------------------------------------


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
        return (r.status_code in _OK), (
            label if r.status_code in _OK else f"status {r.status_code}"
        )
    except Exception as e:
        return False, str(e)
