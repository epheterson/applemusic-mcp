"""Unit tests for the amp-api operations module (the `api` engine).

Mocks the network (responses) and asserts the request shapes that were
captured/verified against the live web player — so a regression in URL/param/body
format is caught in CI even though the live calls need a signed-in account.
"""

import re

import pytest
import responses

from applemusic_mcp import amp_api


@pytest.fixture(autouse=True)
def _fake_auth(monkeypatch):
    monkeypatch.setattr(amp_api.auth, "resolve_developer_token", lambda: "DEV")
    monkeypatch.setattr(amp_api.auth, "get_user_token", lambda: "USER")
    monkeypatch.setattr(amp_api.auth, "get_user_preferences", lambda: {"storefront": "us"})


def test_headers_include_origin_and_tokens():
    h = amp_api._headers()
    assert h["Authorization"] == "Bearer DEV"
    assert h["Music-User-Token"] == "USER"
    assert h["Origin"] == "https://music.apple.com"


@responses.activate
def test_create_playlist():
    responses.add(
        responses.POST,
        f"{amp_api.AMP}/me/library/playlists",
        json={"data": [{"id": "p.NEW"}]},
        status=201,
    )
    ok, pid = amp_api.create_playlist("My List")
    assert ok and pid == "p.NEW"
    assert responses.calls[0].request.headers["Origin"] == "https://music.apple.com"


@responses.activate
def test_rename_uses_patch_attributes_name():
    responses.add(responses.PATCH, f"{amp_api.AMP}/me/library/playlists/p.1", status=204)
    ok, _ = amp_api.rename_playlist("p.1", "New Name")
    assert ok
    import json

    body = json.loads(responses.calls[0].request.body)
    assert body == {"attributes": {"name": "New Name"}}


@responses.activate
def test_add_tracks_body_is_catalog_songs():
    responses.add(responses.POST, f"{amp_api.AMP}/me/library/playlists/p.1/tracks", status=204)
    ok, _ = amp_api.add_tracks("p.1", ["111", "222"])
    assert ok
    import json

    body = json.loads(responses.calls[0].request.body)
    assert body == {"data": [{"id": "111", "type": "songs"}, {"id": "222", "type": "songs"}]}


@responses.activate
def test_remove_track_real_query_format():
    # The captured web-player call: DELETE .../tracks?ids[library-songs]=<rel>&mode=all
    responses.add(
        responses.DELETE,
        re.compile(rf"{re.escape(amp_api.AMP)}/me/library/playlists/p\.1/tracks.*"),
        status=204,
    )
    ok, _ = amp_api.remove_track("p.1", "i.REL")
    assert ok
    url = responses.calls[0].request.url
    assert "ids%5Blibrary-songs%5D=i.REL" in url or "ids[library-songs]=i.REL" in url
    assert "mode=all" in url


@responses.activate
def test_delete_playlist_hits_amp_host():
    responses.add(responses.DELETE, f"{amp_api.AMP}/me/library/playlists/p.1", status=204)
    ok, _ = amp_api.delete_playlist("p.1")
    assert ok
    assert "amp-api.music.apple.com" in responses.calls[0].request.url


@responses.activate
def test_delete_playlist_401_message():
    responses.add(responses.DELETE, f"{amp_api.AMP}/me/library/playlists/p.1", status=401)
    ok, msg = amp_api.delete_playlist("p.1")
    assert not ok and "signin" in msg.lower()


@responses.activate
def test_resolve_playlist_id_prefers_exact():
    responses.add(
        responses.GET,
        f"{amp_api.AMP}/me/library/playlists",
        json={
            "data": [
                {"id": "p.loose", "attributes": {"name": "Workout 2", "canEdit": True}},
                {"id": "p.exact", "attributes": {"name": "Workout", "canEdit": True}},
            ]
        },
        status=200,
    )
    assert amp_api.resolve_playlist_id("Workout") == "p.exact"
