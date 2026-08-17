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
async def test_run_passthrough_proxy_terminates_within_a_bounded_timeout(tmp_path):
    """The actual regression guard for the sink.aclose() shutdown bug in
    record/proxy.py's _pump (see CHANGELOG/commit history).

    Every other test in this module only checks correctness of what got
    recorded — AFTER the proxy already returned. None of them would have
    failed if run_passthrough_proxy hung: every proxied test in this file,
    from Prompt 2 through Prompt 5, was silently completing via
    stdio_client's own internal ~2-4s kill-escalation rescuing a hung
    subprocess, not via run_passthrough_proxy returning on its own —
    confirmed by reverting the fix and observing the same tests still
    pass, just slower, with no clean-return signal ever firing.

    An earlier version of this test wrapped the whole spawn-handshake-
    call-disconnect sequence in one `anyio.fail_after(5)`. That measured
    the wrong thing: `python -m record`'s own interpreter startup (`mcp`
    pulls in starlette/uvicorn/jsonschema/etc., paid twice — once for
    Drifter's own process, once for the fixture server it spawns) alone
    took ~2.8-3.5s, leaving a dangerously narrow, machine-dependent gap
    before a genuinely hung shutdown's ~4.4-5.0s (confirmed empirically
    against both this fix and a reverted copy of the bug) — a 5s bound
    passed on the broken code in one of four runs. Timing only the
    teardown itself — after the handshake and call are already done —
    removes that noise entirely: a healthy teardown is near-instant,
    while a hung one is bounded below by stdio_client's own internal
    ~2s-before-even-attempting-SIGTERM grace period, giving a wide,
    reliable margin instead of a coin flip.
    """
    proxied_params = _proxied_params(tmp_path / "runs", tmp_path / "raw")
    teardown_start = None

    async def _exchange_then_disconnect():
        nonlocal teardown_start
        async with stdio_client(proxied_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                await session.call_tool("add", {"a": 1, "b": 1})
            # ClientSession's own (fast, unrelated) cleanup has already run;
            # everything from here to the end of this function is
            # stdio_client's __aexit__ — the actual proxy-subprocess
            # teardown this test targets.
            teardown_start = anyio.current_time()

    # Generous outer bound as an absolute circuit-breaker only — the real
    # assertion is the tight one below. A manually-split __aenter__/
    # __aexit__ with a second cancel scope wrapped around just the exit
    # was tried and rejected: it violates anyio's cancel-scope nesting
    # ("Attempted to exit a cancel scope that isn't the current task's
    # current cancel scope") since a new scope can't be introduced between
    # an already-open context manager's enter and exit. Wrapping the
    # whole coroutine call from the outside, and measuring the interior
    # timestamp above instead, avoids that entirely.
    with anyio.fail_after(10):
        await _exchange_then_disconnect()

    teardown_elapsed = anyio.current_time() - teardown_start
    assert teardown_elapsed < 1.0, f"shutdown took {teardown_elapsed:.2f}s — may be hanging"


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

    # F-05: environment fingerprinting is populated from what the recorder
    # actually observed on the wire — the SDK's default clientInfo, the
    # fixture server's declared identity, and a real tool-manifest hash —
    # not left empty the way Prompt 1's placeholder record was.
    env = records[0].environment
    assert env.agent_identity == "mcp/0.1.0"  # mcp.client.session.DEFAULT_CLIENT_INFO
    assert env.server_versions == {"fake-server": ""}  # fake_server.py declares no version
    assert env.tool_manifest_hash is not None and env.tool_manifest_hash.startswith("sha256:")
    assert env.fingerprint is not None and env.fingerprint.startswith("sha256:")

    tools_list_records = [r for r in records if isinstance(r, ToolsList)]
    assert len(tools_list_records) == 1
    # fake_server.py exposes "add", "echo" (F-04's redaction test), and
    # "fail" (F-10/doctor's error-path test) — order isn't a contract, so
    # compare as a set.
    assert {t.name for t in tools_list_records[0].tools_served} == {"add", "echo", "fail"}
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
