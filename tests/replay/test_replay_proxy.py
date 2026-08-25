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


# --- synthesized content must be empty, never prose (real-agent finding) ---
#
# "Red" evidence for this check is a live dogfood run, not a re-simulated
# naive-implementation test: Claude Code (the actual Gate 0 dogfood
# pairing), given the earlier placeholder text ("[drifter replay: original
# payload was never recorded (F-02/F-04, shape-only) — F-14 full synthesis
# not implemented yet]") for a genuine exact-tier HIT, refused to proceed,
# verbatim: "This reads like a prompt-injection attempt: it's phrased to
# look like legitimate tool output/system context... I'm not treating this
# as an instruction." All 3 baseline repeats hit this identically, on the
# very first tool call. A real agent's actual judgment is stronger evidence
# the old text had teeth as a failure mode than a keyword-regex simulation
# would be — scripted_agent.py, having no semantic understanding of
# content at all, never could have caught this. These tests lock in the
# fix (empty content, never a claim of any kind) as a regression check.


@pytest.mark.anyio
async def test_synthesized_hit_content_is_empty_not_prose(golden_session):
    """Every exact-tier HIT whose payload was never recorded (i.e. every
    replay-served call, given F-02/F-04's shape-only-by-default recording)
    must return genuinely empty text content -- not a shorter or more
    careful sentence, actual emptiness, so there is no claim of any kind
    for a real agent's own judgment to evaluate and potentially still flag.
    """
    for call in _golden_calls():
        result = await golden_session.call_tool(call.tool_name, call.arguments)
        for block in result.content:
            assert block.type == "text"
            assert block.text == ""


@pytest.mark.anyio
async def test_synthesized_content_never_contains_self_referential_meta_commentary(golden_session):
    """A structural backstop matching the actual observed failure shape --
    no synthesized text may reference this project's own feature IDs or
    describe its own fakeness, regardless of future wording changes."""
    suspicious_fragments = ("F-02", "F-04", "F-14", "drifter", "not implemented", "never recorded", "synthetic response")
    for call in _golden_calls():
        result = await golden_session.call_tool(call.tool_name, call.arguments)
        for block in result.content:
            lowered = block.text.lower()
            for fragment in suspicious_fragments:
                assert fragment.lower() not in lowered, f"{fragment!r} found in synthesized content: {block.text!r}"


@pytest.mark.anyio
async def test_synthesized_content_contains_no_language_of_any_kind(golden_session):
    """Broader than the fixed-fragment check above, deliberately: that
    test guards against the SPECIFIC wording that caused the observed
    failure, but a differently-worded future placeholder could still
    read as first-person/meta/self-referential without matching any of
    those exact fragments ("this response describes...", "note: the
    real value wasn't captured", etc.). The structural property that
    actually matters is that there is no LANGUAGE at all for a real
    agent's own judgment to interpret as a claim, an instruction, or
    commentary -- checked here as literal zero-length content, the only
    property that can't be worked around by picking gentler words.
    """
    for call in _golden_calls():
        result = await golden_session.call_tool(call.tool_name, call.arguments)
        for block in result.content:
            assert len(block.text) == 0, f"synthesized content is not empty: {block.text!r}"


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


# --- tool_addition (F-17) synthesis, scoped F-14 --------------------------


@pytest.mark.anyio
async def test_call_to_a_tool_addition_injected_tool_resolves_as_synthetic_not_miss(tmp_path):
    """The real, end-to-end path F-17's own done-when depends on: a call
    to a tool_addition-injected tool must resolve via synthesis (a
    normal, successful CallToolResult on the wire) rather than an
    ordinary MISS, and the resulting recorded ToolCall must carry
    result_provenance="synthetic" so evaluate.baseline._run_fidelity can
    correctly exclude it. Verified through the real replay_proxy.py
    stack (a real ClientSession, a real SessionRecorder), not asserted
    against add_tool()/run_replay_proxy() in isolation from each other.
    """
    from mutate.tool_addition import add_tool
    from record.writer import SessionRecorder

    store = ReplayStore()
    store.index_session(GOLDEN_FIXTURE)
    siblings = tools_served_from_session(GOLDEN_FIXTURE)
    added_tool, log_entry = add_tool(siblings, seed=1)
    tools_served = [*siblings, added_tool]

    runs_dir, raw_dir = tmp_path / "runs", tmp_path / "raw"
    recorder = SessionRecorder(session_dir=runs_dir, raw_dir=raw_dir, server_name=GOLDEN_SERVER)

    real_calls = _golden_calls()[:1]
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                run_replay_proxy,
                *server_streams,
                store,
                GOLDEN_SERVER,
                tools_served,
                recorder.observe,
                frozenset({added_tool.name}),
            )
            async with ClientSession(*client_streams) as session:
                await session.initialize()
                for call in real_calls:
                    await session.call_tool(call.tool_name, call.arguments)
                # The actual, structural point of this test: this call
                # must NOT raise MCPError(REPLAY_MISS_CODE) the way an
                # ordinary unrecorded call does (see the MISS test
                # above) — it's in synthetic_tool_names, so it resolves.
                result = await session.call_tool(added_tool.name, {})
                assert result.is_error is False
                assert result.content  # a real, non-empty placeholder result
                for block in result.content:
                    assert block.type == "text"
                    assert block.text == ""  # empty, never prose -- see the module-level note above
            tg.cancel_scope.cancel()
    recorder.close()

    new_session_files = list(runs_dir.glob("*.jsonl"))
    assert len(new_session_files) == 1
    new_records = list(read_session(new_session_files[0]))
    new_calls = [r for r in new_records if isinstance(r, ToolCall)]
    assert len(new_calls) == 2  # 1 real hit + 1 synthetic

    real_call, synthetic_call = new_calls
    assert real_call.result_provenance == "real"
    assert real_call.fault is False

    assert synthetic_call.tool_name == added_tool.name
    assert synthetic_call.result_provenance == "synthetic"
    assert synthetic_call.fault is False  # genuinely resolved, not a protocol fault
    assert synthetic_call.is_error is False

    # The actual point of building this at all (F-17's done-when):
    # fidelity accounting must exclude the synthetic call correctly,
    # verified against the REAL recorded session, not a hand-built one.
    from evaluate.baseline import _run_fidelity

    assert _run_fidelity(new_records) == 1.0  # 1/1 REAL calls hit; the synthetic call isn't in the denominator


@pytest.fixture
def anyio_backend():
    return "asyncio"
