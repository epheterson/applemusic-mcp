"""Full line-coverage tests for applemusic_mcp.track_cache.

The cache dir is redirected via monkeypatch of track_cache.get_cache_dir
so nothing touches the real ~/.cache directory.
"""

import json
import logging

import pytest

from applemusic_mcp import track_cache


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    d = tmp_path / "cache"
    d.mkdir()
    monkeypatch.setattr(track_cache, "get_cache_dir", lambda: d)
    return d


@pytest.fixture
def cache(cache_dir):
    return track_cache.TrackCache()


# =====================================================================
# get_cache_dir / _normalize_name_key
# =====================================================================


def test_get_cache_dir_real(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLEMUSIC_MCP_HOME", str(tmp_path))
    d = track_cache.get_cache_dir()
    assert d == tmp_path / ".cache" / "applemusic-mcp"
    assert d.is_dir()


def test_normalize_name_key_with_and_without_artist():
    assert track_cache._normalize_name_key("Abbey Road", "The Beatles") == (
        "abbey road|the beatles"
    )
    assert track_cache._normalize_name_key("  Solo  ") == "solo"


# =====================================================================
# _load
# =====================================================================


def test_load_new_file_fills_missing_sections(cache_dir):
    # cache.json present but every section missing -> all get added (68-73)
    (cache_dir / "cache.json").write_text(json.dumps({"other": 1}))
    c = track_cache.TrackCache()
    assert c._cache["tracks"] == {}
    assert c._cache["albums"] == {}
    assert c._cache["name_index"] == {}
    assert c._cache["other"] == 1


def test_load_new_file_keeps_present_sections(cache_dir):
    # albums/name_index present, only tracks missing -> partial fill
    (cache_dir / "cache.json").write_text(
        json.dumps({"albums": {"l.1": {}}, "name_index": {"k": {}}})
    )
    c = track_cache.TrackCache()
    assert c._cache["tracks"] == {}
    assert c._cache["albums"] == {"l.1": {}}
    assert c._cache["name_index"] == {"k": {}}


def test_load_corrupt_new_file_falls_back_empty(cache_dir, caplog):
    (cache_dir / "cache.json").write_text("not json {")
    c = track_cache.TrackCache()
    assert c._cache == {"tracks": {}, "albums": {}, "name_index": {}}
    assert "Failed to load cache" in caplog.text


def test_load_migrates_legacy(cache_dir, caplog):
    caplog.set_level(logging.INFO)
    (cache_dir / "track_cache.json").write_text(json.dumps({"id1": {"explicit": "Yes"}}))
    c = track_cache.TrackCache()
    assert c._cache["tracks"] == {"id1": {"explicit": "Yes"}}
    assert c._cache["albums"] == {}
    assert c._cache["name_index"] == {}
    assert "Migrating legacy" in caplog.text


def test_load_corrupt_legacy_falls_back_empty(cache_dir, caplog):
    (cache_dir / "track_cache.json").write_text("garbage")
    c = track_cache.TrackCache()
    assert c._cache == {"tracks": {}, "albums": {}, "name_index": {}}
    assert "Failed to load legacy cache" in caplog.text


def test_load_no_files_returns_empty(cache):
    assert cache._cache == {"tracks": {}, "albums": {}, "name_index": {}}


# =====================================================================
# _save
# =====================================================================


def test_save_persists_to_disk(cache, cache_dir):
    cache.set_track_metadata("Yes", persistent_id="p1", name="Song", artist="Band")
    saved = json.loads((cache_dir / "cache.json").read_text())
    assert saved["tracks"]["p1"]["explicit"] == "Yes"
    assert saved["tracks"]["p1"]["name"] == "Song"
    assert saved["name_index"]["song|band"] == {"type": "track", "id": "p1"}


def test_save_failure_swallowed(cache, monkeypatch, caplog):
    # Redirect cache_file into a nonexistent dir so open() for write raises.
    cache.cache_file = cache.cache_file.parent / "missing" / "cache.json"
    cache._save()  # must not raise
    assert "Failed to save cache" in caplog.text


# =====================================================================
# Track methods
# =====================================================================


def test_get_explicit_hit_and_miss(cache):
    assert cache.get_explicit("nope") is None
    cache.set_track_metadata("No", persistent_id="p1")
    assert cache.get_explicit("p1") == "No"


def test_get_track_info_variants(cache):
    assert cache.get_track_info("missing") is None
    # cached but no name -> None (entry exists, no "name" key)
    cache.set_track_metadata("Yes", persistent_id="pn")
    assert cache.get_track_info("pn") is None
    # cached with full info
    cache.set_track_metadata("Yes", persistent_id="pf", name="Song", artist="Band", album="Album")
    assert cache.get_track_info("pf") == {
        "name": "Song",
        "artist": "Band",
        "album": "Album",
    }


def test_set_track_metadata_indexes_all_ids_and_isrc(cache):
    cache.set_track_metadata(
        "Yes",
        persistent_id="p1",
        library_id="i.1",
        catalog_id="c.1",
        isrc="ISRC1",
        name="N",
    )
    for tid in ("p1", "i.1", "c.1"):
        assert cache.get_explicit(tid) == "Yes"
        assert cache._cache["tracks"][tid]["isrc"] == "ISRC1"
    # primary id is the first present (persistent)
    assert cache.get_track_by_name("N") == "p1"


def test_set_track_metadata_no_ids_no_name(cache):
    cache.set_track_metadata("No")  # ids_to_cache empty, primary None
    assert cache.get_stats()["track_count"] == 0
    assert cache.get_stats()["name_index_count"] == 0


def test_set_track_metadata_does_not_clobber_existing(cache):
    cache.set_track_metadata("Yes", persistent_id="p1", name="First")
    cache.set_track_metadata("No", persistent_id="p1", name="Second")
    # existing id retained, not overwritten
    assert cache.get_explicit("p1") == "Yes"
    # name index key for "second" still added (different key, primary id p1)
    assert cache.get_track_by_name("Second") == "p1"
    # original "first" key also retained
    assert cache.get_track_by_name("First") == "p1"


def test_get_track_by_name_miss_and_wrong_type(cache):
    assert cache.get_track_by_name("ghost") is None
    cache.set_album_metadata(library_id="l.1", name="Shared")
    # "Shared" is indexed as an album, not a track
    assert cache.get_track_by_name("Shared") is None


# =====================================================================
# Album methods
# =====================================================================


def test_get_album_hit_and_miss(cache):
    assert cache.get_album("l.x") is None
    cache.set_album_metadata(
        library_id="l.1",
        catalog_id="c.1",
        name="Alb",
        artist="Art",
        track_count=10,
        year="1969",
    )
    meta = cache.get_album("l.1")
    assert meta == {"name": "Alb", "artist": "Art", "track_count": 10, "year": "1969"}
    # indexed by catalog id too
    assert cache.get_album("c.1") == meta
    assert cache.get_album_by_name("Alb", "Art") == "l.1"


def test_set_album_metadata_no_ids_no_name(cache):
    cache.set_album_metadata()  # empty ids, no name
    assert cache.get_stats()["album_count"] == 0
    assert cache.get_stats()["name_index_count"] == 0


def test_set_album_metadata_track_count_zero_included(cache):
    cache.set_album_metadata(library_id="l.0", track_count=0)
    assert cache.get_album("l.0") == {"track_count": 0}


def test_get_album_by_name_miss_and_wrong_type(cache):
    assert cache.get_album_by_name("ghost") is None
    cache.set_track_metadata("Yes", persistent_id="p1", name="TrackName")
    # "TrackName" indexed as a track, not album
    assert cache.get_album_by_name("TrackName") is None


# =====================================================================
# General methods
# =====================================================================


def test_clear(cache, cache_dir):
    cache.set_track_metadata("Yes", persistent_id="p1", name="N")
    cache.set_album_metadata(library_id="l.1", name="A")
    cache.clear()
    assert cache._cache == {"tracks": {}, "albums": {}, "name_index": {}}
    assert json.loads((cache_dir / "cache.json").read_text()) == cache._cache


def test_clear_tracks(cache):
    cache.set_track_metadata("Yes", persistent_id="p1")
    cache.set_album_metadata(library_id="l.1", name="A")
    cache.clear_tracks()
    assert cache.get_stats()["track_count"] == 0
    assert cache.get_stats()["album_count"] == 1


def test_clear_albums(cache):
    cache.set_track_metadata("Yes", persistent_id="p1")
    cache.set_album_metadata(library_id="l.1", name="A")
    cache.clear_albums()
    assert cache.get_stats()["album_count"] == 0
    assert cache.get_stats()["track_count"] == 1


def test_get_stats(cache):
    cache.set_track_metadata("Yes", persistent_id="p1", name="T")
    cache.set_album_metadata(library_id="l.1", name="A")
    stats = cache.get_stats()
    assert stats == {"track_count": 1, "album_count": 1, "name_index_count": 2}


# =====================================================================
# Global singleton
# =====================================================================


def test_get_track_cache_singleton(cache_dir, monkeypatch):
    monkeypatch.setattr(track_cache, "_track_cache", None)
    first = track_cache.get_track_cache()
    second = track_cache.get_track_cache()
    assert first is second
    assert isinstance(first, track_cache.TrackCache)
