"""Integration test for record/proxy.py (F-01).

"Done when" per FEATURES.md: an agent using the proxied server behaves
identically to using the server directly, with zero added latency the user
would notice. This test drives the same requests against the fake fixture
server twice — once directly, once through Drifter's proxy — and asserts
the responses are identical.

Comparing `ClientSession`'s parsed result objects (`Tool`, `CallToolResult`)
after `.model_dump()` would only prove that two independent parses of two
separate wire streams produced equal Python objects — not that the bytes on
the wire were the same. Since a passthrough proxy's entire job happens one
layer below that (at the JSON-RPC message level, before any domain-object
reconstruction), `_tap_reads` below records each message's exact serialized
text as it comes off the transport — using the same `model_dump_json(
by_alias=True, exclude_unset=True)` call the SDK itself uses to put bytes on
the wire — and re-exposes the stream unchanged so `ClientSession` still
drives the handshake normally. The raw logs from the two runs are then
compared directly.
"""

import sys
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

FIXTURE_SERVER = str(Path(__file__).parent.parent / "fixtures" / "fake_server.py")

# Drifter itself, invoked exactly as an MCP client config would invoke it:
# `python -m record <real command> <real args...>`. See record/__main__.py.
DRIFTER_PROXY_COMMAND = [sys.executable, "-m", "record", sys.executable, FIXTURE_SERVER]


@asynccontextmanager
async def _tap_reads(source):
    """Re-exposes `source` unchanged, while recording each message's exact
    wire-serialized JSON text in the returned list, in arrival order."""
    send, receive = anyio.create_memory_object_stream(0)
    raw_log: list[str] = []

    async def _forward() -> None:
        try:
            async with source:
                async for message in source:
                    if not isinstance(message, Exception):
                        raw_log.append(message.message.model_dump_json(by_alias=True, exclude_unset=True))
                    await send.send(message)
        finally:
            await send.aclose()

    async with anyio.create_task_group() as tg:
        tg.start_soon(_forward)
        try:
            yield receive, raw_log
        finally:
            tg.cancel_scope.cancel()


async def _list_and_call(params: StdioServerParameters) -> list[str]:
    async with stdio_client(params) as (read, write):
        async with _tap_reads(read) as (tapped_read, raw_log):
            async with ClientSession(tapped_read, write) as session:
                await session.initialize()
                await session.list_tools()
                await session.call_tool("add", {"a": 3, "b": 4})
        return raw_log


@pytest.mark.anyio
async def test_proxy_is_byte_for_byte_transparent():
    direct_params = StdioServerParameters(command=sys.executable, args=[FIXTURE_SERVER])
    proxied_params = StdioServerParameters(
        command=DRIFTER_PROXY_COMMAND[0], args=DRIFTER_PROXY_COMMAND[1:]
    )

    direct_log = await _list_and_call(direct_params)
    proxied_log = await _list_and_call(proxied_params)

    # Raw JSON-RPC response text, exactly as it arrived off the transport —
    # not SDK-reconstructed Python objects independently parsed twice.
    assert proxied_log == direct_log
    assert len(direct_log) >= 3  # initialize result, tools/list result, tools/call result


@pytest.fixture
def anyio_backend():
    return "asyncio"
