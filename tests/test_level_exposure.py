"""Per-level exposure, end to end: launch the real server over MCP stdio at each
permission level and check exactly which tools a client sees, what the level
refuses, and what it reports. No network, no Music.app, no account: listing
tools and the refusals below touch none of them."""

from __future__ import annotations

import asyncio
import os
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

LIBRARY = {"config", "library", "playback", "playlist"}
ALL = LIBRARY | {"catalog", "discover", "queue"}

# (serve args) -> tools a client is offered
EXPOSURE = [
    (["--level", "music"], LIBRARY),
    (["--level", "music", "--allow-ui"], LIBRARY | {"catalog"}),
    (["--level", "api"], LIBRARY | {"catalog", "discover"}),
    (["--level", "api", "--allow-ui"], LIBRARY | {"catalog", "discover"}),
    (["--level", "web"], ALL),
    (["--level", "full"], ALL),
    ([], ALL),
]


def _session(args, home):
    env = {**os.environ, "APPLEMUSIC_MCP_HOME": str(home)}
    env.pop("APPLEMUSIC_LEVEL", None)
    env.pop("APPLEMUSIC_ALLOW_UI", None)
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "applemusic_mcp", "serve", *args], env=env
    )
    return stdio_client(params)


async def _with_server(args, home, fn):
    async with _session(args, home) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            return await fn(s)


async def _tools(s):
    return {t.name for t in (await s.list_tools()).tools}


async def _call(s, tool, arguments):
    res = await s.call_tool(tool, arguments)
    return res.content[0].text


def _config_file(home):
    return home / ".config" / "applemusic-mcp" / "config.json"


def _run(args, home, fn):
    return asyncio.run(_with_server(args, home, fn))


@pytest.mark.parametrize("args,expected", EXPOSURE, ids=[" ".join(a) or "unset" for a, _ in EXPOSURE])
def test_tools_offered_per_level(args, expected, tmp_path):
    assert _run(args, tmp_path, _tools) == expected


@pytest.mark.parametrize(
    "level,mode,needs",
    [
        ("music", "api", "--level api"),
        ("music", "safari", "--level web"),
        ("api", "safari", "--level web"),
        ("api", "chrome", "--level web"),
    ],
)
def test_mode_above_level_refused_over_the_wire(level, mode, needs, tmp_path):
    out = _run(
        ["--level", level],
        tmp_path,
        lambda s: _call(
            s, "config", {"action": "set-pref", "preference": "mode", "string_value": mode}
        ),
    )
    assert needs in out and "client config" in out
    assert not _config_file(tmp_path).exists()


def test_mode_within_level_is_saved_over_the_wire(tmp_path):
    # Positive control: the same call at web writes the file the refusals must not.
    out = _run(
        ["--level", "web"],
        tmp_path,
        lambda s: _call(
            s, "config", {"action": "set-pref", "preference": "mode", "string_value": "safari"}
        ),
    )
    assert "Updated: mode = safari" in out
    assert '"mode": "safari"' in _config_file(tmp_path).read_text()


@pytest.mark.parametrize("level", ["music", "api"])
def test_assistant_signin_refused_below_web(level, tmp_path):
    out = _run(["--level", level], tmp_path, lambda s: _call(s, "config", {"action": "signin"}))
    assert "--level web" in out and "applemusic-mcp login" in out


@pytest.mark.parametrize(
    "args,shown",
    [
        (["--level", "music"], "Permission level: music"),
        (["--level", "api", "--allow-ui"], "Permission level: api + UI scripting"),
        (["--level", "full"], "Permission level: full"),
        ([], "Permission level: unrestricted"),
    ],
    ids=["music", "api+ui", "full", "unset"],
)
def test_config_info_reports_level(args, shown, tmp_path):
    out = _run(args, tmp_path, lambda s: _call(s, "config", {"action": "info"}))
    assert shown in out
