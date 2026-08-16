"""Integration tests for record/proxy.py (F-01) and record/writer.py +
record/reader.py (F-02, F-03).

"Done when" per FEATURES.md: an agent using the proxied server behaves
identically to using the server directly, with zero added latency the user
would notice (F-01); and a recorded session, read back, exactly reconstructs
the sequence of tool calls that occurred (F-02).

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

import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from record.reader import read_session
from record.schema import SessionStart, ToolCall, ToolsList

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


def _proxied_params(runs_dir: Path | None = None, raw_dir: Path | None = None) -> StdioServerParameters:
    """Params to spawn Drifter itself as the proxied server (see record/__main__.py).

    Recording paths default to `.drifter/runs` / `.drifter/raw` relative to
    the subprocess's cwd; tests override both via env so they never touch
    the real repo's `.drifter/` directory.
    """
    env = {}
    if runs_dir is not None:
        env["DRIFTER_RUNS_DIR"] = str(runs_dir)
    if raw_dir is not None:
        env["DRIFTER_RAW_DIR"] = str(raw_dir)
    return StdioServerParameters(command=DRIFTER_PROXY_COMMAND[0], args=DRIFTER_PROXY_COMMAND[1:], env=env)


@pytest.mark.anyio
async def test_proxy_is_byte_for_byte_transparent(tmp_path):
    direct_params = StdioServerParameters(command=sys.executable, args=[FIXTURE_SERVER])
    proxied_params = _proxied_params(tmp_path / "runs", tmp_path / "raw")

    direct_log = await _list_and_call(direct_params)
    proxied_log = await _list_and_call(proxied_params)

    # Raw JSON-RPC response text, exactly as it arrived off the transport —
    # not SDK-reconstructed Python objects independently parsed twice.
    assert proxied_log == direct_log
    assert len(direct_log) >= 3  # initialize result, tools/list result, tools/call result


@pytest.mark.anyio
async def test_recorded_session_reconstructs_the_tool_call_sequence(tmp_path):
    runs_dir, raw_dir = tmp_path / "runs", tmp_path / "raw"
    proxied_params = _proxied_params(runs_dir, raw_dir)

    async with stdio_client(proxied_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            await session.list_tools()
            await session.call_tool("add", {"a": 3, "b": 4})

    jsonl_files = list(runs_dir.glob("*.jsonl"))
    assert len(jsonl_files) == 1
    records = list(read_session(jsonl_files[0]))

    # seq is assigned in write order and never reused
    assert [r.seq for r in records] == list(range(len(records)))
    assert isinstance(records[0], SessionStart)

    tools_list_records = [r for r in records if isinstance(r, ToolsList)]
    assert len(tools_list_records) == 1
    # fake_server.py exposes "add" and "echo" (the latter added for F-04's
    # redaction test) — order isn't a contract, so compare as a set.
    assert {t.name for t in tools_list_records[0].tools_served} == {"add", "echo"}
    assert tools_list_records[0].tools_raw == tools_list_records[0].tools_served  # no mutate/ yet

    tool_call_records = [r for r in records if isinstance(r, ToolCall)]
    assert len(tool_call_records) == 1
    call = tool_call_records[0]
    assert call.tool_name == "add"
    assert call.arguments == {"a": 3, "b": 4}
    assert call.result_shape["type"] == "object"
    assert "content" in call.result_shape["keys"]  # CallToolResult always has `content`

    # raw_frame_offset must point at the exact response frame the record
    # was built from, not just some byte position that happens to be valid.
    raw_files = list(raw_dir.glob("*.frames"))
    assert len(raw_files) == 1
    raw_bytes = raw_files[0].read_bytes()
    frame_line = raw_bytes[call.raw_frame_offset :].split(b"\n", 1)[0]
    frame_at_offset = json.loads(frame_line.decode("utf-8"))
    assert sorted(frame_at_offset["result"].keys()) == call.result_shape["keys"]


@pytest.fixture
def anyio_backend():
    return "asyncio"
