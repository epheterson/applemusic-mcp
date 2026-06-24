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
