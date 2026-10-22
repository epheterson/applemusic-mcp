"""Permission levels: parsing, the env contract, and refusal messages."""

import pytest

from applemusic_mcp import capabilities as caps


def test_unset_is_unrestricted():
    lvl = caps.parse(None, None)
    assert (lvl.name, lvl.network, lvl.web, lvl.ui, lvl.explicit) == (
        "unrestricted",
        True,
        True,
        True,
        False,
    )


@pytest.mark.parametrize(
    "raw,network,web",
    [
        ("music", False, False),
        ("api", True, False),
        ("web", True, True),
        (" Music ", False, False),
        ("API", True, False),
    ],
)
def test_levels_normalize(raw, network, web):
    lvl = caps.parse(raw, None)
    assert (lvl.network, lvl.web, lvl.ui, lvl.explicit) == (network, web, False, True)


@pytest.mark.parametrize("raw", ["apis", "full", "", "native"])
def test_bad_level_raises(raw):
    with pytest.raises(ValueError, match="music, api, web"):
        caps.parse(raw, None)


@pytest.mark.parametrize("ui,expected", [("1", True), ("true", True), ("0", False), (None, False)])
def test_ui_flag(ui, expected):
    assert caps.parse("music", ui).ui is expected


def test_current_reads_env(monkeypatch):
    monkeypatch.setenv("APPLEMUSIC_LEVEL", "api")
    monkeypatch.setenv("APPLEMUSIC_ALLOW_UI", "1")
    lvl = caps.current()
    assert (lvl.name, lvl.ui) == ("api", True)


def test_require_denies_with_client_config_hint(monkeypatch):
    monkeypatch.setenv("APPLEMUSIC_LEVEL", "music")
    with pytest.raises(caps.CapabilityDenied) as e:
        caps.require("network", "Catalog search")
    msg = str(e.value)
    assert "Catalog search" in msg and "--level api" in msg and "(music)" in msg
    assert "client config" in msg


def test_require_ui_names_the_flag(monkeypatch):
    monkeypatch.setenv("APPLEMUSIC_LEVEL", "web")
    with pytest.raises(caps.CapabilityDenied, match="--allow-ui"):
        caps.require("ui", "Playing a song that isn't in your library")


def test_require_passes_when_allowed(monkeypatch):
    monkeypatch.setenv("APPLEMUSIC_LEVEL", "web")
    caps.require("network", "x")
    caps.require("web", "x")


def test_require_passes_unrestricted():
    caps.require("network", "x")
    caps.require("web", "x")
    caps.require("ui", "x")


def test_modes_by_level():
    assert caps.MODES_BY_LEVEL["music"] == ("auto", "native")
    assert "api" in caps.MODES_BY_LEVEL["api"] and "safari" not in caps.MODES_BY_LEVEL["api"]
    assert {"safari", "chrome", "web"} <= set(caps.MODES_BY_LEVEL["web"])


# -- network guard at the music level ---------------------------------------

import requests  # noqa: E402


def test_guard_blocks_requests_at_music(monkeypatch):
    monkeypatch.setattr(requests.Session, "request", requests.Session.request)  # restored on teardown
    monkeypatch.setenv("APPLEMUSIC_LEVEL", "music")
    assert caps.install_network_guard() is True
    with pytest.raises(caps.CapabilityDenied, match="--level api") as e:
        requests.get("https://api.music.apple.com/v1/catalog/us/search")
    assert "api.music.apple.com" in str(e.value)


@pytest.mark.parametrize("level", ["api", "web", None])
def test_guard_not_installed_above_music(monkeypatch, level):
    original = requests.Session.request
    monkeypatch.setattr(requests.Session, "request", original)
    if level:
        monkeypatch.setenv("APPLEMUSIC_LEVEL", level)
    assert caps.install_network_guard() is False
    assert requests.Session.request is original


def test_guard_is_not_a_requests_exception():
    # Callers catch requests.RequestException as "Apple is unreachable"; a
    # refusal must not be swallowed and reported as an outage.
    assert not issubclass(caps.CapabilityDenied, requests.RequestException)
