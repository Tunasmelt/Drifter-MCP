"""Tests for the HTTP-serving side of the replay proxy (F-38),
cli/http_proxy.py.

Real end-to-end: a genuine `mcp.client.streamable_http.streamable_http_client`
connection, over a real loopback TCP socket — not an in-memory stream pair,
since the whole point of this module is the real network transport.
"""

from __future__ import annotations

from pathlib import Path

import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from cli.http_proxy import serve_replay_over_http
from replay.replay_proxy import tools_served_from_session
from replay.replay_store import ReplayStore
from record.reader import read_session
from record.schema import ToolCall

GOLDEN_FIXTURE = Path(__file__).parent.parent / "fixtures" / "golden_v0.1.jsonl"
GOLDEN_SERVER = "filesystem"


def _golden_calls() -> list[ToolCall]:
    return [r for r in read_session(GOLDEN_FIXTURE) if isinstance(r, ToolCall)]


def _replay_store() -> ReplayStore:
    store = ReplayStore()
    store.index_session(GOLDEN_FIXTURE)
    return store


@pytest.mark.anyio
async def test_serves_a_real_session_over_real_http():
    tools_served = tools_served_from_session(GOLDEN_FIXTURE)
    calls = _golden_calls()[:3]

    async with serve_replay_over_http(_replay_store(), GOLDEN_SERVER, tools_served) as url:
        assert url.startswith("http://127.0.0.1:")
        assert url.endswith("/mcp")

        async with streamable_http_client(url) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert {t.name for t in tools.tools} == {t.name for t in tools_served}

                for call in calls:
                    result = await session.call_tool(call.tool_name, call.arguments)
                    assert result.is_error == bool(call.is_error)


@pytest.mark.anyio
async def test_binds_to_loopback_only():
    tools_served = tools_served_from_session(GOLDEN_FIXTURE)
    async with serve_replay_over_http(_replay_store(), GOLDEN_SERVER, tools_served) as url:
        assert url.startswith("http://127.0.0.1:")


@pytest.mark.anyio
async def test_each_invocation_gets_a_distinct_ephemeral_port():
    tools_served = tools_served_from_session(GOLDEN_FIXTURE)
    async with serve_replay_over_http(_replay_store(), GOLDEN_SERVER, tools_served) as url_a:
        async with serve_replay_over_http(_replay_store(), GOLDEN_SERVER, tools_served) as url_b:
            assert url_a != url_b  # two real, simultaneously-bound ports, never colliding


@pytest.mark.anyio
async def test_a_foreign_browser_style_origin_is_rejected():
    """The concrete DNS-rebinding defense this module exists to provide:
    a browser-supplied Origin header that isn't loopback must be
    rejected, not silently accepted -- confirmed against a REAL request,
    not just by reading mcp.server.transport_security's source."""
    tools_served = tools_served_from_session(GOLDEN_FIXTURE)
    async with serve_replay_over_http(_replay_store(), GOLDEN_SERVER, tools_served) as url:
        async with httpx2.AsyncClient() as client:
            response = await client.post(
                url,
                headers={
                    "Origin": "https://evil.example.com",
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                },
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            )
    assert response.status_code == 403


@pytest.mark.anyio
async def test_a_request_with_no_origin_header_is_not_rejected_by_origin_check():
    """Real, non-browser MCP clients (this project's own agents included)
    don't send an Origin header at all -- confirmed this must still work,
    not just that foreign origins are blocked (the two are independent
    claims; a naive "reject unless explicitly allowed" implementation
    would break every real client, not just attackers)."""
    tools_served = tools_served_from_session(GOLDEN_FIXTURE)
    async with serve_replay_over_http(_replay_store(), GOLDEN_SERVER, tools_served) as url:
        async with httpx2.AsyncClient() as client:
            response = await client.post(
                url,
                headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}},
            )
    assert response.status_code != 403


@pytest.mark.anyio
async def test_shuts_down_promptly_after_a_real_client_connects_and_disconnects():
    """Timed, per CLAUDE.md's explicit requirement for any code that
    spawns a process, reads a stream, or (as here) runs a server task —
    "it passed" is not sufficient evidence for shutdown-path code in this
    codebase specifically. This is not a hypothetical for this exact
    module: forcibly cancelling uv_server's task via
    tg.cancel_scope.cancel() (this module's first implementation) raised
    a real OSError (WinError 995) while uvicorn's asyncio server was
    mid-accept() on Windows -- found empirically while building this
    context manager, fixed by using `should_exit` and a graceful
    task-group exit instead (see cli/http_proxy.py's own docstring).
    """
    import time

    tools_served = tools_served_from_session(GOLDEN_FIXTURE)

    started = time.monotonic()
    async with serve_replay_over_http(_replay_store(), GOLDEN_SERVER, tools_served) as url:
        async with streamable_http_client(url) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                try:
                    await session.call_tool("read_file", {"path": "/foo"})
                except Exception:
                    pass  # a MISS is fine here -- only the shutdown timing is under test
    elapsed = time.monotonic() - started

    assert elapsed < 5.0, f"HTTP proxy shutdown took {elapsed:.2f}s after a real client disconnect -- investigate a hang"


@pytest.fixture
def anyio_backend():
    return "asyncio"
