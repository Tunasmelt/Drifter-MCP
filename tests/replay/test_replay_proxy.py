"""Tests for the replay-serving proxy mode.

Same pattern as Gate 1 Prompt 2's fixture-server tests: a real
`ClientSession` drives the real MCP protocol stack against the thing under
test — no hand-rolled fake client, no calling internal handler functions
directly. The connection itself uses the SDK's own in-process memory-stream
helper (`mcp.shared.memory.create_client_server_memory_streams`) instead of
a subprocess: `run_replay_proxy` never spawns anything (see its module
docstring's structural guarantee), so there's no live server for a
subprocess-based test to spawn in the first place — proving the round trip
this way, with a real client and a real (SDK-framework) server exchanging
real wire messages over real streams, in-process, is the honest match for
what this component actually is.
"""

from pathlib import Path

import anyio
import pytest
from mcp import ClientSession
from mcp.shared.exceptions import MCPError
from mcp.shared.memory import create_client_server_memory_streams

from record.reader import read_session
from record.schema import ToolCall
from replay.replay_proxy import REPLAY_FAULT_CODE, REPLAY_MISS_CODE, run_replay_proxy, tools_served_from_session
from replay.replay_store import ReplayStore

GOLDEN_FIXTURE = Path(__file__).parent.parent / "fixtures" / "golden_v0.1.jsonl"
GOLDEN_SERVER = "filesystem"


def _golden_calls() -> list[ToolCall]:
    return [r for r in read_session(GOLDEN_FIXTURE) if isinstance(r, ToolCall)]


@pytest.fixture
async def golden_session():
    """A real ClientSession connected to a replay proxy serving the
    golden fixture, running for the duration of one test."""
    store = ReplayStore()
    store.index_session(GOLDEN_FIXTURE)
    tools_served = tools_served_from_session(GOLDEN_FIXTURE)

    async with create_client_server_memory_streams() as (client_streams, server_streams):
        async with anyio.create_task_group() as tg:
            tg.start_soon(run_replay_proxy, *server_streams, store, GOLDEN_SERVER, tools_served)
            async with ClientSession(*client_streams) as session:
                await session.initialize()
                yield session
            tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_all_seven_golden_calls_hit_with_the_recorded_is_error(golden_session):
    for call in _golden_calls():
        result = await golden_session.call_tool(call.tool_name, call.arguments)
        assert result.is_error == bool(call.is_error), f"seq={call.seq} tool={call.tool_name}"


@pytest.mark.anyio
async def test_tools_list_matches_the_golden_fixtures_recorded_manifest(golden_session):
    result = await golden_session.list_tools()
    served_names = {t.name for t in result.tools}
    expected = {t.name for t in tools_served_from_session(GOLDEN_FIXTURE)}
    assert served_names == expected
    assert "list_directory" in served_names  # sanity: not an empty manifest


@pytest.mark.anyio
async def test_unrecorded_call_is_a_distinguishable_miss_not_a_crash_or_fake_success(golden_session):
    with pytest.raises(MCPError) as exc_info:
        await golden_session.call_tool("list_directory", {"path": "C:\\nowhere\\never\\recorded"})
    assert exc_info.value.code == REPLAY_MISS_CODE
    # This is a real assertion, not a formality: a MISS must arrive as a
    # protocol-level error (MCPError), never as a CallToolResult at all --
    # a recorded is_error:true HIT returns normally (see the test above),
    # so "did the call raise" is itself already the distinguishing signal,
    # independent of the code check.


@pytest.mark.anyio
async def test_miss_error_code_is_distinct_from_fault_error_code():
    assert REPLAY_MISS_CODE != REPLAY_FAULT_CODE


def test_replay_error_codes_never_collide_with_any_mcp_types_defined_code():
    """Regression test for a real bug: REPLAY_MISS_CODE originally sat at
    -32001, which is REQUEST_TIMEOUT (mcp_types' own reserved-range
    constant) exactly -- a client checking `.code == REQUEST_TIMEOUT`
    would have silently misread a replay MISS as a request timeout.
    Checks against every negative-integer constant mcp_types actually
    defines right now, not just the ones named in the module comment, so
    this stays true if a future SDK version adds more reserved codes --
    the same failure mode recurring later is exactly what moving outside
    the whole reserved band was meant to prevent.
    """
    import mcp_types as types

    from replay.replay_proxy import REPLAY_FAULT_CODE, REPLAY_MISS_CODE

    reserved_codes = {
        getattr(types, name)
        for name in dir(types)
        if name.isupper() and isinstance(getattr(types, name), int) and getattr(types, name) < 0
    }
    assert REPLAY_MISS_CODE not in reserved_codes
    assert REPLAY_FAULT_CODE not in reserved_codes
    # And outside JSON-RPC 2.0's entire reserved band outright (not just
    # the codes mcp_types happens to define today).
    assert not (-32768 <= REPLAY_MISS_CODE <= -32000)
    assert not (-32768 <= REPLAY_FAULT_CODE <= -32000)


@pytest.mark.anyio
async def test_recorded_fault_replays_as_a_protocol_error_distinct_from_miss(tmp_path):
    """A HIT whose recorded fault=True must replay as a protocol-level
    error too (faithfully reproducing "this call failed at the protocol
    level"), but with a different code than MISS -- "we have no
    recording" and "we recorded this exact call failing" are different
    facts and must stay distinguishable from each other, not just from a
    real is_error:true.
    """
    import json

    session_id = "fault_sess"
    lines = [
        {
            "schema_version": "0.1", "record_type": "session_start", "session_id": session_id, "seq": 0,
            "started_at": "2026-08-25T00:00:00Z",
            "environment": {"agent_identity": None, "model_name": None, "server_versions": {},
                             "tool_manifest_hash": "h", "fingerprint": "f"},
            "raw_frame_offset": 0,
        },
        {
            "schema_version": "0.1", "record_type": "tool_call", "session_id": session_id, "seq": 1,
            "timestamp": "2026-08-25T00:00:01Z", "server": "srv", "tool_name": "flaky_tool",
            "arguments": {}, "result_shape": None, "is_error": None, "duration_ms": 1.0, "fault": True,
            "result_provenance": "real", "references": [], "mutation_inverse": None, "raw_frame_offset": 100,
        },
    ]
    path = tmp_path / "fault.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")

    store = ReplayStore()
    store.index_session(path)

    async with create_client_server_memory_streams() as (client_streams, server_streams):
        async with anyio.create_task_group() as tg:
            tg.start_soon(run_replay_proxy, *server_streams, store, "srv", [])
            async with ClientSession(*client_streams) as session:
                await session.initialize()
                with pytest.raises(MCPError) as exc_info:
                    await session.call_tool("flaky_tool", {})
                assert exc_info.value.code == REPLAY_FAULT_CODE
                assert exc_info.value.code != REPLAY_MISS_CODE
            tg.cancel_scope.cancel()


# --- recording: SessionRecorder plugged into on_message, unchanged ------


@pytest.mark.anyio
async def test_on_message_lets_sessionrecorder_produce_a_valid_new_session(tmp_path):
    """The actual gap the STOP-AND-CHECK step closed: run_replay_proxy
    with a real SessionRecorder wired to on_message (exactly like
    cli/observe.py wires it for passthrough) must produce a real,
    parseable session JSONL describing what the CLIENT did this run --
    tool_manifest_hash populated (the eager bootstrap's whole point),
    ToolCall records matching the calls actually made, is_error/fault
    carried through correctly, and a closed trajectory.
    """
    from record.writer import SessionRecorder

    store = ReplayStore()
    store.index_session(GOLDEN_FIXTURE)
    tools_served = tools_served_from_session(GOLDEN_FIXTURE)

    runs_dir, raw_dir = tmp_path / "runs", tmp_path / "raw"
    recorder = SessionRecorder(session_dir=runs_dir, raw_dir=raw_dir, server_name=GOLDEN_SERVER)

    calls = _golden_calls()
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                run_replay_proxy, *server_streams, store, GOLDEN_SERVER, tools_served, recorder.observe
            )
            async with ClientSession(*client_streams) as session:
                await session.initialize()
                for call in calls[:3]:
                    await session.call_tool(call.tool_name, call.arguments)
                with pytest.raises(MCPError):
                    await session.call_tool("list_directory", {"path": "C:\\never\\recorded"})
            tg.cancel_scope.cancel()
    recorder.close()

    new_session_files = list(runs_dir.glob("*.jsonl"))
    assert len(new_session_files) == 1
    new_records = list(read_session(new_session_files[0]))

    session_start = next(r for r in new_records if r.record_type == "session_start")
    assert session_start.environment.tool_manifest_hash is not None  # the eager-bootstrap guarantee

    new_calls = [r for r in new_records if isinstance(r, ToolCall)]
    assert len(new_calls) == 4  # 3 real hits + 1 miss
    for original, recorded in zip(calls[:3], new_calls[:3]):
        assert recorded.tool_name == original.tool_name
        assert recorded.is_error == bool(original.is_error)
        assert recorded.fault is False  # a genuine HIT, not a fault
    assert new_calls[3].tool_name == "list_directory"
    assert new_calls[3].fault is True  # the MISS, recorded as a protocol-level fault (see module docstring)

    trajectory_ends = [r for r in new_records if r.record_type == "trajectory_end"]
    assert len(trajectory_ends) == 1
    assert trajectory_ends[0].call_seqs == [c.seq for c in new_calls]


@pytest.fixture
def anyio_backend():
    return "asyncio"
