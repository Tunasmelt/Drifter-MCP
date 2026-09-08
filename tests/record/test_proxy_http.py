"""Tests for F-39: `record/proxy.py`'s real-server connection over
Streamable HTTP (`servers[].url`), not just stdio (F-01).

Reuses F-38's own real-HTTP-server test infra (`cli/http_proxy.py`'s
`serve_replay_over_http`) as the "real server" side, rather than building a
second one: it already serves a genuine MCP server over real loopback
sockets with the correct security settings (Origin validation, loopback-
only binding). What's actually new and under test here is the CLIENT/
connecting side — `record/proxy.py`'s `connect_to_server`/`ServerTarget`
picking Streamable HTTP instead of stdio when handed a URL string.
`_pump`'s own forwarding correctness is unchanged and already covered by
F-01's existing stdio tests (`tests/record/test_proxy.py`); this file
doesn't re-prove that, only that the new transport selection actually
connects and round-trips real MCP traffic.
"""

from __future__ import annotations

import sys
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from mcp_drifter.cli.http_proxy import serve_replay_over_http
from mcp_drifter.record.proxy import connect_to_server
from mcp_drifter.record.reader import read_session
from mcp_drifter.record.schema import ToolCall
from mcp_drifter.replay.replay_proxy import tools_served_from_session
from mcp_drifter.replay.replay_store import ReplayStore

GOLDEN_FIXTURE = Path(__file__).parent.parent / "fixtures" / "golden_v0.1.jsonl"
GOLDEN_SERVER = "filesystem"

RUNNER_SCRIPT = str(Path(__file__).parent.parent / "fixtures" / "proxy_over_http_runner.py")


def _replay_store() -> ReplayStore:
    store = ReplayStore()
    store.index_session(GOLDEN_FIXTURE)
    return store


def _golden_calls() -> list[ToolCall]:
    return [r for r in read_session(GOLDEN_FIXTURE) if isinstance(r, ToolCall)]


@pytest.mark.anyio
async def test_connect_to_server_opens_a_real_http_round_trip_against_a_real_server():
    """The actual new F-39 unit: `connect_to_server(url)` — a bare string —
    must pick Streamable HTTP and produce a working, real MCP session
    (`initialize` + a real tool call), not silently fail or fall back to
    trying to spawn `url` as a shell command."""
    tools_served = tools_served_from_session(GOLDEN_FIXTURE)
    call = _golden_calls()[0]

    async with serve_replay_over_http(_replay_store(), GOLDEN_SERVER, tools_served) as url:
        async with connect_to_server(url) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(call.tool_name, call.arguments)
                assert result.is_error == bool(call.is_error)


@pytest.mark.anyio
async def test_connect_to_server_still_spawns_a_real_subprocess_for_stdio_params():
    """The other half of the same branch: `StdioServerParameters` (not a
    bare string) must still go through `stdio_client`, matching F-01's
    existing, unchanged behavior — confirmed here rather than only assumed
    from `connect_to_server`'s own isinstance check reading correctly."""
    from mcp.client.stdio import StdioServerParameters

    fixture_server = str(Path(__file__).parent.parent / "fixtures" / "fake_server.py")
    params = StdioServerParameters(command=sys.executable, args=[fixture_server])

    async with connect_to_server(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert tools.tools  # a real handshake with a real spawned process happened


def test_run_passthrough_proxy_over_a_real_http_server_end_to_end():
    """F-39's own docs/FEATURES.md "Done when" bar, driven through the REAL
    invocation shape end to end: a separate process runs `record.proxy.
    run_passthrough_proxy` exactly as `drifter observe` would (agent-facing
    stdio via `stdio_server()`, real-server-facing Streamable HTTP via
    `connect_to_server`), reached over ITS real stdio by this test acting
    as the agent — the identical subprocess-based methodology `tests/
    record/test_proxy.py`'s F-01 tests already use for the stdio case
    (spawn Drifter itself, connect via `stdio_client`), applied to the
    HTTP-real-server case for the first time. From the connecting agent's
    point of view, Drifter always presents as an ordinary stdio MCP server
    — which real server it's actually forwarding to is invisible, exactly
    matching this feature's "no code path cares which transport originally
    recorded it" bar.
    """
    tools_served = tools_served_from_session(GOLDEN_FIXTURE)
    calls = _golden_calls()

    async def _serve_and_run_client() -> list[bool | None]:
        async with serve_replay_over_http(_replay_store(), GOLDEN_SERVER, tools_served) as url:
            params = StdioServerParameters(command=sys.executable, args=[RUNNER_SCRIPT, url])
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    hits: list[bool | None] = []
                    for call in calls[:2]:
                        result = await session.call_tool(call.tool_name, call.arguments)
                        hits.append(result.is_error)
                    return hits

    hits = anyio.run(_serve_and_run_client)
    assert len(hits) == 2
    for call, is_error in zip(calls[:2], hits):
        assert is_error == bool(call.is_error)
