"""Tests for the harvested (fallback) developer-token path in auth.py.

The generated (paid) token is always preferred; harvesting the public
AMPWebPlay web-player token is the fallback for users without an Apple
Developer account. These tests mock the network so they're fast and offline.
"""

import pytest
import responses

from applemusic_mcp import auth

# A string shaped like the AMPWebPlay JWT the extractor looks for
# (header.payload.signature, payload starting with the AMPWebPlay issuer claim).
FAKE_TOKEN = "eyJ0eXAiOiJKV1QabcDEF123.eyJpc3MiOiJBTVBXZWJQbGF5ghiJKL456.sigMNO789"


def test_extract_finds_token_in_bundle():
    js = f"!function(){{var dev='{FAKE_TOKEN}';return dev}}()"
    assert auth.extract_developer_token(js) == FAKE_TOKEN


def test_extract_missing_raises():
    with pytest.raises(RuntimeError):
        auth.extract_developer_token("no token anywhere in here")


@responses.activate
def test_harvest_locates_bundle_then_extracts():
    responses.add(
        responses.GET,
        "https://music.apple.com/us/browse",
        body='<html><script src="/assets/index~deadbeef99.js"></script></html>',
    )
    responses.add(
        responses.GET,
        "https://music.apple.com/assets/index~deadbeef99.js",
        body=f"globalThis.cfg={{token:'{FAKE_TOKEN}'}};",
    )
    assert auth.harvest_developer_token() == FAKE_TOKEN


@responses.activate
def test_harvest_raises_when_no_bundle():
    responses.add(
        responses.GET, "https://music.apple.com/us/browse", body="<html>no bundle here</html>"
    )
    with pytest.raises(RuntimeError):
        auth.harvest_developer_token()


def test_resolve_prefers_generated_token(monkeypatch):
    monkeypatch.setattr(auth, "get_developer_token", lambda: "GENERATED-PAID-TOKEN")
    # Should never reach the harvest path.
    monkeypatch.setattr(
        auth, "harvest_developer_token", lambda: pytest.fail("must not harvest when generated")
    )
    assert auth.resolve_developer_token() == "GENERATED-PAID-TOKEN"


def test_resolve_falls_back_to_cached_harvest(monkeypatch):
    def no_generated():
        raise FileNotFoundError

    monkeypatch.setattr(auth, "get_developer_token", no_generated)
    monkeypatch.setattr(auth, "_load_harvested_token", lambda: "CACHED-HARVEST")
    monkeypatch.setattr(
        auth, "harvest_developer_token", lambda: pytest.fail("must not re-harvest when cached")
    )
    assert auth.resolve_developer_token() == "CACHED-HARVEST"


def test_resolve_harvests_fresh_when_no_cache(monkeypatch):
    def no_generated():
        raise ValueError("expired")

    saved = {}
    monkeypatch.setattr(auth, "get_developer_token", no_generated)
    monkeypatch.setattr(auth, "_load_harvested_token", lambda: None)
    monkeypatch.setattr(auth, "harvest_developer_token", lambda: "FRESH-HARVEST")
    monkeypatch.setattr(auth, "_save_harvested_token", lambda t: saved.update(token=t))
    assert auth.resolve_developer_token() == "FRESH-HARVEST"
    assert saved["token"] == "FRESH-HARVEST"  # fresh harvest gets cached
