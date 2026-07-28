"""Unit tests for the amp-api operations module (the `api` engine).

Mocks the network (responses) and asserts the request shapes that were
captured/verified against the live web player — so a regression in URL/param/body
format is caught in CI even though the live calls need a signed-in account.
"""

import re

import pytest
import requests
import responses

from applemusic_mcp import amp_api


@pytest.fixture(autouse=True)
def _fake_auth(monkeypatch):
    monkeypatch.setattr(amp_api.auth, "resolve_developer_token", lambda: "DEV")
    monkeypatch.setattr(amp_api.auth, "resolve_web_token", lambda: "DEV")
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
def test_remove_from_library_deletes_by_library_id():
    # Verified live: DELETE /me/library/songs/{libraryId} -> 204.
    responses.add(responses.DELETE, f"{amp_api.AMP}/me/library/songs/i.ABC", status=204)
    ok, msg = amp_api.remove_from_library("i.ABC")
    assert ok and "library" in msg.lower()
    assert "/me/library/songs/i.ABC" in responses.calls[0].request.url


@responses.activate
def test_search_library_songs_shape():
    responses.add(
        responses.GET,
        f"{amp_api.AMP}/me/library/search",
        json={
            "results": {
                "library-songs": {
                    "data": [
                        {
                            "id": "i.XYZ",
                            "attributes": {
                                "name": "Time",
                                "artistName": "Pink Floyd",
                                "playParams": {"catalogId": "123"},
                            },
                        }
                    ]
                }
            }
        },
        status=200,
    )
    out = amp_api.search_library_songs("Time")
    assert out == [{"id": "i.XYZ", "name": "Time", "artist": "Pink Floyd", "catalog_id": "123"}]


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
    assert not ok and "login" in msg.lower()


@responses.activate
def test_create_folder_posts_with_parent_relationship():
    responses.add(
        responses.POST,
        f"{amp_api.AMP}/me/library/playlist-folders",
        json={"data": [{"id": "p.FOLDER"}]},
        status=201,
    )
    ok, fid = amp_api.create_folder("Mixes")
    assert ok and fid == "p.FOLDER"
    import json

    body = json.loads(responses.calls[0].request.body)
    assert body["attributes"]["name"] == "Mixes"
    assert body["relationships"]["parent"]["data"][0]["type"] == "library-playlist-folders"


@responses.activate
def test_move_playlist_to_folder_puts_parent():
    responses.add(responses.PUT, f"{amp_api.AMP}/me/library/playlists/p.1/parent", status=204)
    ok, _ = amp_api.move_playlist_to_folder("p.1", "p.FOLDER")
    assert ok
    import json

    body = json.loads(responses.calls[0].request.body)
    assert body == {"data": [{"id": "p.FOLDER", "type": "library-playlist-folders"}]}


@responses.activate
def test_rate_love_puts_value_1():
    responses.add(responses.PUT, f"{amp_api.AMP}/me/ratings/songs/123", status=200)
    ok, msg = amp_api.rate("123", 1)
    assert ok and msg == "Loved"
    import json

    assert json.loads(responses.calls[0].request.body) == {"attributes": {"value": 1}}


@responses.activate
def test_rate_clear_deletes():
    responses.add(responses.DELETE, f"{amp_api.AMP}/me/ratings/songs/123", status=204)
    ok, msg = amp_api.rate("123", 0)
    assert ok and "Cleared" in msg


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


@responses.activate
def test_error_body_is_surfaced_and_logged(caplog):
    """A failed mutation must carry the response body into the message AND log
    the full body (enolgor's #37 ask — 500 bodies were being discarded)."""
    body = '{"errors":[{"status":"500","detail":"upstream boom"}]}'
    responses.add(
        responses.POST,
        f"{amp_api.AMP}/me/library/playlists/p.x/tracks",
        body=body,
        status=500,
    )
    import logging

    with caplog.at_level(logging.WARNING, logger="applemusic_mcp.amp_api"):
        ok, msg = amp_api.add_tracks("p.x", ["123"])
    assert ok is False
    assert "status 500" in msg
    assert "upstream boom" in msg  # body snippet in the user-facing reason
    assert any(
        "upstream boom" in r.message or "upstream boom" in r.getMessage() for r in caplog.records
    )


@responses.activate
def test_401_reason_hints_signin():
    responses.add(
        responses.DELETE,
        f"{amp_api.AMP}/me/library/playlists/p.x",
        body='{"errors":[{"status":"401"}]}',
        status=401,
    )
    ok, msg = amp_api.delete_playlist("p.x")
    assert ok is False
    assert "login" in msg.lower()


@responses.activate
def test_get_rating_returns_value():
    responses.add(
        responses.GET,
        f"{amp_api.AMP}/me/ratings/songs/123",
        json={"data": [{"attributes": {"value": 1}}]},
        status=200,
    )
    assert amp_api.get_rating("123") == 1


@responses.activate
def test_get_rating_none_when_unrated():
    responses.add(responses.GET, f"{amp_api.AMP}/me/ratings/songs/123", status=404)
    assert amp_api.get_rating("123") is None


# --- session_status (every status branch) ----------------------------------


@responses.activate
def test_session_status_ok():
    responses.add(responses.GET, f"{amp_api.AMP}/me/library/playlists", status=200)
    assert amp_api.session_status() == "ok"
    # cheap probe: limit=1
    assert "limit=1" in responses.calls[0].request.url


@responses.activate
@pytest.mark.parametrize("code", [401, 403])
def test_session_status_expired(code):
    responses.add(responses.GET, f"{amp_api.AMP}/me/library/playlists", status=code)
    assert amp_api.session_status() == "expired"


@responses.activate
def test_session_status_throttled():
    responses.add(responses.GET, f"{amp_api.AMP}/me/library/playlists", status=429)
    assert amp_api.session_status() == "throttled"


@responses.activate
def test_session_status_error_status():
    responses.add(responses.GET, f"{amp_api.AMP}/me/library/playlists", status=500)
    assert amp_api.session_status() == "error"


@responses.activate
def test_session_status_error_on_exception():
    responses.add(
        responses.GET,
        f"{amp_api.AMP}/me/library/playlists",
        body=requests.exceptions.ConnectTimeout("boom"),
    )
    assert amp_api.session_status() == "error"


# --- list_playlists (pagination, non-200, exception) -----------------------


@responses.activate
def test_list_playlists_paginates_and_normalizes():
    # page 1 carries a `next`; the module builds the amp-api host URL from it.
    responses.add(
        responses.GET,
        f"{amp_api.AMP}/me/library/playlists",
        match=[responses.matchers.query_param_matcher({"limit": "100"})],
        json={
            "data": [{"id": "p.1", "attributes": {"name": "A"}}],
            "next": "/v1/me/library/playlists?offset=100",
        },
        status=200,
    )
    responses.add(
        responses.GET,
        f"{amp_api.AMP}/me/library/playlists",
        match=[responses.matchers.query_param_matcher({"offset": "100"})],
        json={"data": [{"id": "p.2", "attributes": {"name": "B", "canEdit": False}}]},
        status=200,
    )
    out = amp_api.list_playlists()
    assert out == [
        {"id": "p.1", "name": "A", "canEdit": True, "globalId": ""},
        {"id": "p.2", "name": "B", "canEdit": False, "globalId": ""},
    ]
    # second request hit the amp-api host built from `next`
    assert (
        "amp-api.music.apple.com/v1/me/library/playlists?offset=100"
        in responses.calls[1].request.url
    )


@responses.activate
def test_list_playlists_non_200_breaks_empty():
    responses.add(responses.GET, f"{amp_api.AMP}/me/library/playlists", status=500)
    assert amp_api.list_playlists() == []


@responses.activate
def test_list_playlists_exception_swallowed():
    responses.add(
        responses.GET,
        f"{amp_api.AMP}/me/library/playlists",
        body=requests.exceptions.ConnectionError("down"),
    )
    assert amp_api.list_playlists() == []


# --- resolve_playlist_id (loose match, no match, skips non-editable) --------


@responses.activate
def test_resolve_playlist_id_loose_and_skips_app_made():
    responses.add(
        responses.GET,
        f"{amp_api.AMP}/me/library/playlists",
        json={
            "data": [
                {"id": "p.ro", "attributes": {"name": "Gym Mix", "canEdit": False}},
                {"id": "p.loose", "attributes": {"name": "Gym Mix Deluxe", "canEdit": True}},
            ]
        },
        status=200,
    )
    # exact "Gym" doesn't exist; read-only "Gym Mix" is skipped; loose substring wins
    assert amp_api.resolve_playlist_id("Gym") == "p.loose"


@responses.activate
def test_resolve_playlist_id_none_when_no_match():
    responses.add(
        responses.GET,
        f"{amp_api.AMP}/me/library/playlists",
        json={"data": [{"id": "p.1", "attributes": {"name": "Jazz", "canEdit": True}}]},
        status=200,
    )
    assert amp_api.resolve_playlist_id("Techno") is None


@responses.activate
def test_resolve_playlist_id_includes_app_made_when_asked():
    # A Music.app-made playlist (canEdit=False) is skipped by default but found
    # when api_created_only=False (the web/native rails can write it).
    responses.add(
        responses.GET,
        f"{amp_api.AMP}/me/library/playlists",
        json={"data": [{"id": "p.app", "attributes": {"name": "Jack & Norah", "canEdit": False}}]},
        status=200,
    )
    assert amp_api.resolve_playlist_id("Jack & Norah") is None
    responses.add(
        responses.GET,
        f"{amp_api.AMP}/me/library/playlists",
        json={"data": [{"id": "p.app", "attributes": {"name": "Jack & Norah", "canEdit": False}}]},
        status=200,
    )
    assert amp_api.resolve_playlist_id("Jack & Norah", api_created_only=False) == "p.app"


def test_is_api_created():
    assert amp_api.is_api_created({"canEdit": True}) is True
    assert amp_api.is_api_created({"canEdit": False}) is False
    assert amp_api.is_api_created({}) is True  # default: assume writable


# --- get_tracks (pagination, non-200, exception) ---------------------------


@responses.activate
def test_get_tracks_paginates_and_shapes():
    responses.add(
        responses.GET,
        f"{amp_api.AMP}/me/library/playlists/p.1/tracks",
        match=[responses.matchers.query_param_matcher({"limit": "100"})],
        json={
            "data": [
                {
                    "id": "i.REL1",
                    "attributes": {
                        "name": "Song1",
                        "artistName": "Artist1",
                        "playParams": {"catalogId": "c1"},
                    },
                }
            ],
            "next": "/v1/me/library/playlists/p.1/tracks?offset=100",
        },
        status=200,
    )
    responses.add(
        responses.GET,
        f"{amp_api.AMP}/me/library/playlists/p.1/tracks",
        match=[responses.matchers.query_param_matcher({"offset": "100"})],
        json={
            "data": [
                {
                    "id": "i.REL2",
                    "attributes": {"name": "Song2", "playParams": {"id": "c2"}},
                }
            ]
        },
        status=200,
    )
    out = amp_api.get_tracks("p.1")
    assert out == [
        {"relationship_id": "i.REL1", "name": "Song1", "artist": "Artist1", "catalog_id": "c1"},
        {"relationship_id": "i.REL2", "name": "Song2", "artist": "", "catalog_id": "c2"},
    ]
    assert "amp-api.music.apple.com/v1/me/library/playlists/p.1/tracks?offset=100" in (
        responses.calls[1].request.url
    )


@responses.activate
def test_get_tracks_non_200_breaks_empty():
    responses.add(responses.GET, f"{amp_api.AMP}/me/library/playlists/p.1/tracks", status=503)
    assert amp_api.get_tracks("p.1") == []


@responses.activate
def test_get_tracks_exception_swallowed():
    responses.add(
        responses.GET,
        f"{amp_api.AMP}/me/library/playlists/p.1/tracks",
        body=requests.exceptions.ReadTimeout("slow"),
    )
    assert amp_api.get_tracks("p.1") == []


# --- search_library_songs (non-200, exception) -----------------------------


@responses.activate
def test_search_library_songs_non_200_empty():
    responses.add(responses.GET, f"{amp_api.AMP}/me/library/search", status=500)
    assert amp_api.search_library_songs("x") == []


@responses.activate
def test_search_library_songs_exception_empty():
    responses.add(
        responses.GET,
        f"{amp_api.AMP}/me/library/search",
        body=requests.exceptions.ConnectionError("down"),
    )
    assert amp_api.search_library_songs("x") == []


# --- search_catalog_songs (ok, non-200, exception) -------------------------


@responses.activate
def test_search_catalog_songs_shape_and_storefront():
    responses.add(
        responses.GET,
        f"{amp_api.AMP}/catalog/us/search",
        json={
            "results": {
                "songs": {
                    "data": [
                        {"id": "c1", "attributes": {"name": "Money", "artistName": "Pink Floyd"}}
                    ]
                }
            }
        },
        status=200,
    )
    out = amp_api.search_catalog_songs("Money", limit=5)
    assert out == [{"id": "c1", "name": "Money", "artist": "Pink Floyd"}]
    req = responses.calls[0].request
    assert "types=songs" in req.url and "limit=5" in req.url


@responses.activate
def test_search_catalog_songs_non_200_empty():
    responses.add(responses.GET, f"{amp_api.AMP}/catalog/us/search", status=404)
    assert amp_api.search_catalog_songs("x") == []


@responses.activate
def test_search_catalog_songs_exception_empty():
    responses.add(
        responses.GET,
        f"{amp_api.AMP}/catalog/us/search",
        body=requests.exceptions.ConnectionError("down"),
    )
    assert amp_api.search_catalog_songs("x") == []


# --- mutation exception paths ----------------------------------------------


@responses.activate
def test_create_playlist_exception():
    responses.add(
        responses.POST,
        f"{amp_api.AMP}/me/library/playlists",
        body=requests.exceptions.ConnectionError("network down"),
    )
    ok, msg = amp_api.create_playlist("X")
    assert not ok and "network down" in msg


@responses.activate
def test_create_playlist_includes_description():
    responses.add(
        responses.POST,
        f"{amp_api.AMP}/me/library/playlists",
        json={"data": [{"id": "p.D"}]},
        status=201,
    )
    ok, pid = amp_api.create_playlist("Named", description="my desc")
    assert ok and pid == "p.D"
    import json

    body = json.loads(responses.calls[0].request.body)
    assert body == {"attributes": {"name": "Named", "description": "my desc"}}


@responses.activate
def test_create_playlist_error_status():
    responses.add(responses.POST, f"{amp_api.AMP}/me/library/playlists", body="nope", status=500)
    ok, msg = amp_api.create_playlist("X")
    assert not ok and "status 500" in msg


@responses.activate
def test_rename_playlist_error_and_exception():
    responses.add(responses.PATCH, f"{amp_api.AMP}/me/library/playlists/p.1", status=403)
    ok, msg = amp_api.rename_playlist("p.1", "N")
    assert not ok and "login" in msg.lower()


@responses.activate
def test_rename_playlist_exception():
    responses.add(
        responses.PATCH,
        f"{amp_api.AMP}/me/library/playlists/p.1",
        body=requests.exceptions.ConnectionError("x"),
    )
    ok, msg = amp_api.rename_playlist("p.1", "N")
    assert not ok and msg == "x"


def test_add_tracks_no_items():
    ok, msg = amp_api.add_tracks("p.1", [])
    assert not ok and msg == "no track ids"


@responses.activate
def test_add_tracks_tuple_items_typed():
    responses.add(responses.POST, f"{amp_api.AMP}/me/library/playlists/p.1/tracks", status=201)
    ok, msg = amp_api.add_tracks("p.1", [("i.LIB", "library-songs")])
    assert ok and "1 track" in msg
    import json

    body = json.loads(responses.calls[0].request.body)
    assert body == {"data": [{"id": "i.LIB", "type": "library-songs"}]}


@responses.activate
def test_add_tracks_exception():
    responses.add(
        responses.POST,
        f"{amp_api.AMP}/me/library/playlists/p.1/tracks",
        body=requests.exceptions.ConnectionError("x"),
    )
    ok, msg = amp_api.add_tracks("p.1", ["1"])
    assert not ok and msg == "x"


@responses.activate
def test_remove_track_error_and_exception():
    responses.add(
        responses.DELETE,
        re.compile(rf"{re.escape(amp_api.AMP)}/me/library/playlists/p\.1/tracks.*"),
        status=500,
    )
    ok, msg = amp_api.remove_track("p.1", "i.R")
    assert not ok and "status 500" in msg


@responses.activate
def test_remove_track_exception():
    responses.add(
        responses.DELETE,
        re.compile(rf"{re.escape(amp_api.AMP)}/me/library/playlists/p\.1/tracks.*"),
        body=requests.exceptions.ConnectionError("x"),
    )
    ok, msg = amp_api.remove_track("p.1", "i.R")
    assert not ok and msg == "x"


@responses.activate
def test_remove_from_library_error_and_exception():
    responses.add(responses.DELETE, f"{amp_api.AMP}/me/library/songs/i.ABC", status=404)
    ok, msg = amp_api.remove_from_library("i.ABC")
    assert not ok and "status 404" in msg


@responses.activate
def test_remove_from_library_exception():
    responses.add(
        responses.DELETE,
        f"{amp_api.AMP}/me/library/songs/i.ABC",
        body=requests.exceptions.ConnectionError("x"),
    )
    ok, msg = amp_api.remove_from_library("i.ABC")
    assert not ok and msg == "x"


@responses.activate
def test_delete_playlist_exception():
    responses.add(
        responses.DELETE,
        f"{amp_api.AMP}/me/library/playlists/p.1",
        body=requests.exceptions.ConnectionError("x"),
    )
    ok, msg = amp_api.delete_playlist("p.1")
    assert not ok and msg == "x"


# --- folders ---------------------------------------------------------------


@responses.activate
def test_create_folder_error_and_exception():
    responses.add(responses.POST, f"{amp_api.AMP}/me/library/playlist-folders", status=500)
    ok, msg = amp_api.create_folder("F")
    assert not ok and "status 500" in msg


@responses.activate
def test_create_folder_exception():
    responses.add(
        responses.POST,
        f"{amp_api.AMP}/me/library/playlist-folders",
        body=requests.exceptions.ConnectionError("x"),
    )
    ok, msg = amp_api.create_folder("F")
    assert not ok and msg == "x"


@responses.activate
def test_delete_folder_ok():
    responses.add(responses.DELETE, f"{amp_api.AMP}/me/library/playlist-folders/p.F", status=204)
    ok, msg = amp_api.delete_folder("p.F")
    assert ok and msg == "Deleted"


@responses.activate
def test_delete_folder_error():
    responses.add(responses.DELETE, f"{amp_api.AMP}/me/library/playlist-folders/p.F", status=403)
    ok, msg = amp_api.delete_folder("p.F")
    assert not ok and "login" in msg.lower()


@responses.activate
def test_delete_folder_exception():
    responses.add(
        responses.DELETE,
        f"{amp_api.AMP}/me/library/playlist-folders/p.F",
        body=requests.exceptions.ConnectionError("x"),
    )
    ok, msg = amp_api.delete_folder("p.F")
    assert not ok and msg == "x"


@responses.activate
def test_list_folders_filters_by_type():
    responses.add(
        responses.GET,
        f"{amp_api.AMP}/me/library/playlist-folders/{amp_api.ROOT_FOLDER}/children",
        json={
            "data": [
                {"id": "p.F", "type": "library-playlist-folders", "attributes": {"name": "Mixes"}},
                {"id": "p.PL", "type": "library-playlists", "attributes": {"name": "NotAFolder"}},
            ]
        },
        status=200,
    )
    assert amp_api.list_folders() == [{"id": "p.F", "name": "Mixes"}]


@responses.activate
def test_list_folders_non_200_empty():
    responses.add(
        responses.GET,
        f"{amp_api.AMP}/me/library/playlist-folders/{amp_api.ROOT_FOLDER}/children",
        status=500,
    )
    assert amp_api.list_folders() == []


@responses.activate
def test_list_folders_exception_empty():
    responses.add(
        responses.GET,
        f"{amp_api.AMP}/me/library/playlist-folders/{amp_api.ROOT_FOLDER}/children",
        body=requests.exceptions.ConnectionError("x"),
    )
    assert amp_api.list_folders() == []


@responses.activate
def test_resolve_folder_id_match_and_none():
    responses.add(
        responses.GET,
        f"{amp_api.AMP}/me/library/playlist-folders/{amp_api.ROOT_FOLDER}/children",
        json={
            "data": [
                {"id": "p.F", "type": "library-playlist-folders", "attributes": {"name": "Mixes"}}
            ]
        },
        status=200,
    )
    assert amp_api.resolve_folder_id("mixes") == "p.F"  # loose-eq, case-insensitive
    assert amp_api.resolve_folder_id("Nope") is None


@responses.activate
def test_move_playlist_to_folder_error_and_exception():
    responses.add(responses.PUT, f"{amp_api.AMP}/me/library/playlists/p.1/parent", status=500)
    ok, msg = amp_api.move_playlist_to_folder("p.1", "p.F")
    assert not ok and "status 500" in msg


@responses.activate
def test_move_playlist_to_folder_exception():
    responses.add(
        responses.PUT,
        f"{amp_api.AMP}/me/library/playlists/p.1/parent",
        body=requests.exceptions.ConnectionError("x"),
    )
    ok, msg = amp_api.move_playlist_to_folder("p.1", "p.F")
    assert not ok and msg == "x"


# --- ratings ---------------------------------------------------------------


@responses.activate
def test_get_rating_empty_data_is_none():
    responses.add(
        responses.GET, f"{amp_api.AMP}/me/ratings/songs/123", json={"data": []}, status=200
    )
    assert amp_api.get_rating("123") is None


@responses.activate
def test_get_rating_exception_is_none():
    responses.add(
        responses.GET,
        f"{amp_api.AMP}/me/ratings/songs/123",
        body=requests.exceptions.ConnectionError("x"),
    )
    assert amp_api.get_rating("123") is None


@responses.activate
def test_rate_dislike_puts_value_negative_1():
    responses.add(responses.PUT, f"{amp_api.AMP}/me/ratings/songs/123", status=200)
    ok, msg = amp_api.rate("123", -1)
    assert ok and msg == "Disliked"
    import json

    assert json.loads(responses.calls[0].request.body) == {"attributes": {"value": -1}}


@responses.activate
def test_rate_error_status_surfaces_reason():
    responses.add(responses.PUT, f"{amp_api.AMP}/me/ratings/songs/123", status=500)
    ok, msg = amp_api.rate("123", 1)
    assert not ok and "status 500" in msg


@responses.activate
def test_rate_exception():
    responses.add(
        responses.PUT,
        f"{amp_api.AMP}/me/ratings/songs/123",
        body=requests.exceptions.ConnectionError("x"),
    )
    ok, msg = amp_api.rate("123", 1)
    assert not ok and msg == "x"


@responses.activate
def test_err_empty_body_yields_bare_reason(caplog):
    """_err with an empty body returns just the reason (no snippet suffix)."""
    responses.add(responses.DELETE, f"{amp_api.AMP}/me/library/playlists/p.1", body="", status=500)
    import logging

    with caplog.at_level(logging.WARNING, logger="applemusic_mcp.amp_api"):
        ok, msg = amp_api.delete_playlist("p.1")
    assert not ok and msg == "status 500"  # no ": snippet" tail
    assert any("(empty body)" in r.getMessage() for r in caplog.records)


# --- 429 / rate-limit state (#42) ------------------------------------------
#
# Apple's tokenless web-player path throttles on a rolling ~60-min window with
# NO Retry-After header, and the reads above swallow non-200 into an empty list
# — so without this state a throttle is indistinguishable from "no such song",
# and a bulk import silently records false negatives.


@responses.activate
def test_catalog_search_429_records_throttle():
    """A throttled catalog search still returns [], but says so out of band."""
    responses.add(responses.GET, f"{amp_api.AMP}/catalog/us/search", status=429)
    assert amp_api.search_catalog_songs("x") == []
    assert amp_api.throttled_recently() is True


@responses.activate
def test_success_clears_throttle_marker():
    """The marker means 'the last thing Apple said was 429' — a 200 clears it, so
    a genuine miss a minute later isn't mislabelled as rate-limited."""
    amp_api.note_status(429)
    assert amp_api.throttled_recently() is True
    responses.add(
        responses.GET,
        f"{amp_api.AMP}/catalog/us/search",
        json={"results": {"songs": {"data": []}}},
        status=200,
    )
    assert amp_api.search_catalog_songs("nonexistent") == []
    assert amp_api.throttled_recently() is False


@responses.activate
def test_session_status_throttled_short_circuits_without_a_request():
    """Probing while throttled would fail AND add to the count keeping us
    throttled — a known-recent 429 answers without spending a request."""
    amp_api.note_status(429)
    assert amp_api.session_status() == "throttled"
    assert len(responses.calls) == 0


@responses.activate
def test_session_status_429_sets_marker():
    responses.add(responses.GET, f"{amp_api.AMP}/me/library/playlists", status=429)
    assert amp_api.session_status() == "throttled"
    assert amp_api.throttled_recently() is True


def test_throttled_recently_window_and_reset():
    amp_api.note_status(429)
    assert amp_api.throttled_recently(within=60) is True
    assert amp_api.throttled_recently(within=0) is False  # outside the window
    amp_api.reset_throttle_state()
    assert amp_api.throttled_recently() is False


def test_note_status_ignores_other_errors():
    """A 404/500 is a real answer, not a throttle — don't claim rate limiting."""
    amp_api.note_status(404)
    amp_api.note_status(500)
    assert amp_api.throttled_recently() is False


@responses.activate
def test_err_429_explains_the_rolling_window():
    responses.add(responses.DELETE, f"{amp_api.AMP}/me/library/playlists/p.1", body="", status=429)
    ok, msg = amp_api.delete_playlist("p.1")
    assert not ok
    assert "rate limited" in msg and "rolling" in msg
    assert amp_api.throttled_recently() is True


# --- per-rail throttle isolation -------------------------------------------


def test_rails_do_not_clear_each_other():
    """With a generated dev token the two hosts have separate quotas, so a 200 on
    api.music.apple.com must not clear a live 429 on amp-api — doing so would
    resurrect the false 'not found' this whole mechanism exists to prevent."""
    amp_api.note_status(429, amp_api.WEB)
    assert amp_api.throttled_recently(rail=amp_api.WEB) is True

    amp_api.note_status(200, amp_api.API)  # success on the OTHER rail
    assert amp_api.throttled_recently(rail=amp_api.WEB) is True
    assert amp_api.throttled_recently() is True  # either-rail default

    amp_api.note_status(200, amp_api.WEB)  # success on the SAME rail
    assert amp_api.throttled_recently(rail=amp_api.WEB) is False
    assert amp_api.throttled_recently() is False


def test_either_rail_default_sees_an_api_throttle():
    amp_api.note_status(429, amp_api.API)
    assert amp_api.throttled_recently() is True
    assert amp_api.throttled_recently(rail=amp_api.WEB) is False
    assert amp_api.throttled_recently(rail=amp_api.API) is True


def test_reset_clears_every_rail():
    amp_api.note_status(429, amp_api.WEB)
    amp_api.note_status(429, amp_api.API)
    amp_api.reset_throttle_state()
    assert amp_api.throttled_recently() is False


@responses.activate
def test_amp_api_reads_record_the_web_rail():
    responses.add(responses.GET, f"{amp_api.AMP}/catalog/us/search", status=429)
    amp_api.search_catalog_songs("x")
    assert amp_api.throttled_recently(rail=amp_api.WEB) is True
    assert amp_api.throttled_recently(rail=amp_api.API) is False
