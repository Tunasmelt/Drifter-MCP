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

import functools

import anyio
import jsonschema
import pytest
from mcp import ClientSession
from mcp.shared.exceptions import MCPError
from mcp.shared.memory import create_client_server_memory_streams

from mcp_drifter.record.reader import read_session
from mcp_drifter.record.schema import ToolDescriptor, ToolCall
from mcp_drifter.replay.replay_proxy import REPLAY_FAULT_CODE, REPLAY_MISS_CODE, run_replay_proxy, tools_served_from_session
from mcp_drifter.replay.replay_store import RecordedResponse, ReplayStore, replay_key

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


# --- F-19: cache-busting on tools/list responses ----------------------------
#
# A raw-wire-level check, not golden_session.list_tools()'s own PARSED
# result: the MCP SDK's ListToolsResult has real ttl_ms/cache_scope
# fields with defaults (0 / "private"), so a client-side parse would
# report those defaults regardless of whether the SERVER actually sent
# them -- confirmed empirically before writing this test (a raw capture
# of the real wire bytes showed a bare {"tools": [...]}, no ttlMs/
# cacheScope at all, because on_list_tools's return value never set
# them and the SDK's own wire serialization uses exclude_unset=True).
# Only tapping the literal JSON on the wire can tell "explicitly sent"
# apart from "client-side default."


async def _tap_tools_list_response_json(store: ReplayStore, tools_served) -> str:
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        client_read, client_write = client_streams
        send, recv = anyio.create_memory_object_stream(0)
        raw_log: list[str] = []

        async def _tap():
            async with client_read:
                async for msg in client_read:
                    if not isinstance(msg, Exception):
                        raw_log.append(msg.message.model_dump_json(by_alias=True, exclude_unset=True))
                    await send.send(msg)

        async with anyio.create_task_group() as tg:
            tg.start_soon(run_replay_proxy, *server_streams, store, GOLDEN_SERVER, tools_served)
            tg.start_soon(_tap)
            async with ClientSession(recv, client_write) as session:
                await session.initialize()
                await session.list_tools()
            tg.cancel_scope.cancel()

    # "tools":[ (the array) distinguishes the actual tools/list response
    # from the earlier initialize response, which also happens to
    # contain the substring "tools" as part of its capabilities object
    # ("tools":{"listChanged":false}) -- found the hard way, a first
    # version of this helper matched the wrong line.
    return next(line for line in raw_log if '"tools":[' in line)


@pytest.mark.anyio
async def test_tools_list_response_has_no_ttlms_or_cachescope_a_confirmed_gap():
    """docs/SPEC.md's original C8 claim (and the now-corrected F-19 entry in
    docs/FEATURES.md) called for every tools/list response to set ttlMs: 0
    and a private cacheScope. Investigated directly against the real,
    currently-negotiated MCP protocol rather than assumed from the SDK's
    newest type definitions: those fields exist ONLY on
    `mcp_types._v2026_07_28.ListToolsResult`, a draft, not-yet-real
    protocol version. Every currently-negotiable version (2024-11-05
    through 2025-11-25 -- everything any real MCP client speaks today)
    validates server results through the older surface model, which has
    `extra="ignore"` and silently strips anything else before it reaches
    the wire -- confirmed here by a real wire capture, not by inspecting
    a client-parsed result object (whose own ttl_ms/cache_scope fields
    carry non-None defaults regardless of what the server actually sent).

    This test locks in the current, honest behavior -- a bare
    {"tools": [...]} with neither field present -- as a documented,
    permanent-for-now limitation (docs/SPEC.md §15), not something to
    force green by setting fields that a real client will never see.
    See replay_proxy.py's on_list_tools for the full account and the
    real MCP mechanism (`notifications/tools/list_changed`) this doesn't
    map cleanly onto, since Drifter's baseline/mutated arms are always
    separate fresh connections, not one connection whose manifest
    changes mid-session."""
    store = ReplayStore()
    store.index_session(GOLDEN_FIXTURE)
    tools_served = tools_served_from_session(GOLDEN_FIXTURE)

    wire_json = await _tap_tools_list_response_json(store, tools_served)
    assert '"ttlMs"' not in wire_json, wire_json
    assert '"cacheScope"' not in wire_json, wire_json


# --- F-20: mutated calls structurally cannot forward live -------------------


def test_replay_proxy_module_imports_nothing_capable_of_a_live_forward():
    """CLAUDE.md's own non-negotiable invariant: mutation testing never
    forwards a live call under a mutated schema, verified structurally, not
    just true by default configuration. `replay_proxy.py`'s own module
    docstring already claims this ("this module never imports
    `mcp.client.stdio` or anything else that spawns a subprocess or opens
    an outbound connection... confirmed by inspection, not by a flag
    defaulting the 'right' way") -- found while auditing F-20 that nothing
    actually locks that claim in: nothing would fail loudly if a future
    edit added a live-forwarding import to this file. This test makes the
    inspection itself the regression check, at the same layer the
    guarantee lives (imports), rather than trusting the docstring's word
    or a behavioral test that could pass by accident (e.g. a fixture that
    never happens to exercise a newly-added live path).
    """
    import ast
    import inspect

    import mcp_drifter.replay.replay_proxy as module

    source = inspect.getsource(module)
    tree = ast.parse(source)

    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_names.add(node.module)

    # Anything under mcp.client (the SDK's own outbound-connection
    # machinery) or the stdlib's subprocess module would be capable of
    # reaching a live server -- neither may ever appear here.
    live_capable = {name for name in imported_names if name == "subprocess" or name.startswith("mcp.client")}
    assert live_capable == set(), f"replay_proxy.py imports live-forward-capable modules: {live_capable}"


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

    from mcp_drifter.replay.replay_proxy import REPLAY_FAULT_CODE, REPLAY_MISS_CODE

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
    from mcp_drifter.record.writer import SessionRecorder

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
        assert recorded.match_tier == "exact"  # F-13/F-15: every golden-fixture call is an exact replay
    assert new_calls[3].tool_name == "list_directory"
    assert new_calls[3].fault is True  # the MISS, recorded as a protocol-level fault (see module docstring)
    assert new_calls[3].match_tier is None  # no tier resolved a MISS -- nothing to tag

    trajectory_ends = [r for r in new_records if r.record_type == "trajectory_end"]
    assert len(trajectory_ends) == 1
    assert trajectory_ends[0].call_seqs == [c.seq for c in new_calls]


@pytest.mark.anyio
async def test_a_semantic_hit_is_recorded_with_match_tier_semantic(tmp_path):
    """End-to-end confirmation of the MATCH_TIER_MARKER_KEY threading
    (F-13/F-15): a call whose arguments only resolve via the golden
    fixture's SEMANTIC index (a real recorded call's tool/value, looked up
    under a different parameter name) must come back through the real
    proxy AND be recorded with `match_tier == "semantic"` -- not just
    resolve successfully, which test_a_renamed_parameter_resolves_via_
    semantic_match_when_exact_misses (replay_store.py's own unit test)
    already confirms at the store layer alone.
    """
    from mcp_drifter.record.writer import SessionRecorder

    store = ReplayStore()
    store.index_session(GOLDEN_FIXTURE)
    tools_served = tools_served_from_session(GOLDEN_FIXTURE)

    call = next(c for c in _golden_calls() if len(c.arguments) == 1)
    original_key = next(iter(call.arguments))
    renamed_arguments = {f"{original_key}_renamed": call.arguments[original_key]}

    runs_dir, raw_dir = tmp_path / "runs", tmp_path / "raw"
    recorder = SessionRecorder(session_dir=runs_dir, raw_dir=raw_dir, server_name=GOLDEN_SERVER)

    async with create_client_server_memory_streams() as (client_streams, server_streams):
        async with anyio.create_task_group() as tg:
            tg.start_soon(run_replay_proxy, *server_streams, store, GOLDEN_SERVER, tools_served, recorder.observe)
            async with ClientSession(*client_streams) as session:
                await session.initialize()
                await session.call_tool(call.tool_name, renamed_arguments)
            tg.cancel_scope.cancel()
    recorder.close()

    new_records = list(read_session(next(runs_dir.glob("*.jsonl"))))
    new_call = next(r for r in new_records if isinstance(r, ToolCall))
    assert new_call.fault is False
    assert new_call.match_tier == "semantic"


@pytest.mark.anyio
async def test_an_inverse_map_hit_is_recorded_with_match_tier_inverse(tmp_path):
    """F-12's real, wired-through-the-proxy confirmation, mirroring the
    semantic-tier test above exactly: a live call using a RENAMED
    parameter name, with the exact `inverse_map` a real `mutate.
    parameter_rename` mutation would have produced, must resolve via the
    proxy's `run_replay_proxy(..., inverse_map=...)` parameter and be
    recorded with `match_tier == "inverse"` -- not just resolve
    successfully at the ReplayStore layer alone (already confirmed by
    replay_store.py's own unit tests)."""
    from mcp_drifter.record.writer import SessionRecorder

    store = ReplayStore()
    store.index_session(GOLDEN_FIXTURE)
    tools_served = tools_served_from_session(GOLDEN_FIXTURE)

    call = next(c for c in _golden_calls() if len(c.arguments) == 1)
    original_key = next(iter(call.arguments))
    new_key = f"{original_key}_renamed"
    renamed_arguments = {new_key: call.arguments[original_key]}
    inverse_map = {call.tool_name: {new_key: original_key}}

    runs_dir, raw_dir = tmp_path / "runs", tmp_path / "raw"
    recorder = SessionRecorder(session_dir=runs_dir, raw_dir=raw_dir, server_name=GOLDEN_SERVER)

    async with create_client_server_memory_streams() as (client_streams, server_streams):
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                run_replay_proxy, *server_streams, store, GOLDEN_SERVER, tools_served, recorder.observe,
                frozenset(), inverse_map,
            )
            async with ClientSession(*client_streams) as session:
                await session.initialize()
                await session.call_tool(call.tool_name, renamed_arguments)
            tg.cancel_scope.cancel()
    recorder.close()

    new_records = list(read_session(next(runs_dir.glob("*.jsonl"))))
    new_call = next(r for r in new_records if isinstance(r, ToolCall))
    assert new_call.fault is False
    assert new_call.match_tier == "inverse"


# --- F-14/F-15: content_length reconstruction edge cases -------------------


async def _single_call_session(store: ReplayStore, tools_served):
    """A minimal replay session serving exactly one hand-built store,
    for tests that need a RecordedResponse the golden fixture doesn't
    naturally contain."""
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        async with anyio.create_task_group() as tg:
            tg.start_soon(run_replay_proxy, *server_streams, store, GOLDEN_SERVER, tools_served)
            async with ClientSession(*client_streams) as session:
                await session.initialize()
                yield session
            tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_a_recorded_zero_length_content_array_synthesizes_as_genuinely_empty():
    """`_synthesize_call_tool_result`'s content_length defaults to 1 when
    array_lengths has no "content" entry -- but a REAL recorded response
    whose content array was genuinely empty (array_lengths["content"] ==
    0, a real, valid shape a tool can return) must synthesize as an
    EMPTY content list, not silently fall back to the length-1 default.
    Never exercised by the golden fixture (every real call there has
    real content) -- hand-built here."""
    tools_served = tools_served_from_session(GOLDEN_FIXTURE)
    store = ReplayStore()
    key = replay_key(GOLDEN_SERVER, tools_served[0].name, {})
    store._index[key] = RecordedResponse(
        result_shape={"type": "object", "keys": ["content"], "array_lengths": {"content": 0}},
        is_error=False,
        fault=False,
        match_tier="exact",
    )

    async for session in _single_call_session(store, tools_served):
        result = await session.call_tool(tools_served[0].name, {})
        assert result.content == []


@pytest.mark.anyio
async def test_a_recorded_multi_block_content_array_synthesizes_with_the_same_count():
    """The inverse case: a real recorded response with MULTIPLE content
    blocks (array_lengths["content"] == 3) must synthesize exactly 3
    empty placeholders, not the length-1 default -- confirming
    content_length genuinely reads the recorded value across its full
    real range, not just "present vs. absent"."""
    tools_served = tools_served_from_session(GOLDEN_FIXTURE)
    store = ReplayStore()
    key = replay_key(GOLDEN_SERVER, tools_served[0].name, {})
    store._index[key] = RecordedResponse(
        result_shape={"type": "object", "keys": ["content"], "array_lengths": {"content": 3}},
        is_error=False,
        fault=False,
        match_tier="exact",
    )

    async for session in _single_call_session(store, tools_served):
        result = await session.call_tool(tools_served[0].name, {})
        assert len(result.content) == 3
        assert all(block.text == "" for block in result.content)


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
    from mcp_drifter.mutate.tool_addition import add_tool
    from mcp_drifter.record.writer import SessionRecorder

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
    from mcp_drifter.evaluate.baseline import _run_fidelity

    assert _run_fidelity(new_records) == 1.0  # 1/1 REAL calls hit; the synthetic call isn't in the denominator


@pytest.fixture
def anyio_backend():
    return "asyncio"


# --- F-14: general synthesis on a miss, opt-in --------------------------


@pytest.fixture
async def synthesizing_session():
    """Same golden-fixture replay proxy, but with F-14 synthesis on."""
    store = ReplayStore()
    store.index_session(GOLDEN_FIXTURE)
    tools_served = tools_served_from_session(GOLDEN_FIXTURE)

    async with create_client_server_memory_streams() as (client_streams, server_streams):
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                functools.partial(
                    run_replay_proxy,
                    *server_streams,
                    store,
                    GOLDEN_SERVER,
                    tools_served,
                    synthesize_on_miss=True,
                )
            )
            async with ClientSession(*client_streams) as session:
                await session.initialize()
                yield session
            tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_synthesis_is_off_by_default_so_a_miss_still_errors(golden_session):
    """Guards the opt-in itself. Erroring on a miss is the existing
    documented contract; F-14 must not silently change it for callers
    that never asked, since answering instead changes the agent's
    trajectory (it continues where it would have stopped).
    """
    with pytest.raises(MCPError) as exc_info:
        await golden_session.call_tool("list_directory", {"path": "C:\nowhere\never\recorded"})
    assert exc_info.value.code == REPLAY_MISS_CODE


@pytest.mark.anyio
async def test_with_synthesis_on_a_miss_answers_instead_of_erroring(synthesizing_session):
    result = await synthesizing_session.call_tool("list_directory", {"path": "C:\nowhere\never\recorded"})

    assert result.is_error is False
    # Content-empty, exactly like every other synthesis path here -- see
    # this module's note on the real agent that read explanatory
    # placeholder prose and refused to proceed.
    assert [c.text for c in result.content] == [""]


@pytest.mark.anyio
async def test_a_synthesized_miss_carries_no_structured_content_when_the_tool_declared_no_output_schema(
    synthesizing_session,
):
    """The golden fixture predates `output_schema` entirely, so every
    tool in it reads back as `None`. Nothing is guessed in that case --
    the absence of an output contract must produce no structured claim
    at all, rather than a fabricated `{}`.
    """
    result = await synthesizing_session.call_tool("list_directory", {"path": "C:\nowhere\never\recorded"})

    assert result.structured_content is None


@pytest.mark.anyio
async def test_a_synthesized_miss_conforms_to_a_declared_output_schema():
    """F-14's stated "Done when": the synthesized response passes the
    tool's own declared schema validation.
    """
    declared = {
        "type": "object",
        "properties": {"entries": {"type": "array", "items": {"type": "string"}}, "count": {"type": "integer"}},
        "required": ["entries", "count"],
    }
    tools_served = [
        ToolDescriptor(name="list_directory", description="", input_schema={"type": "object"}, output_schema=declared),
    ]
    store = ReplayStore()  # empty: every call is a miss

    async with create_client_server_memory_streams() as (client_streams, server_streams):
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                functools.partial(
                    run_replay_proxy, *server_streams, store, GOLDEN_SERVER, tools_served, synthesize_on_miss=True
                )
            )
            async with ClientSession(*client_streams) as session:
                await session.initialize()
                result = await session.call_tool("list_directory", {"path": "/anything"})
            tg.cancel_scope.cancel()

    assert result.structured_content == {"entries": [], "count": 0}
    jsonschema.validate(instance=result.structured_content, schema=declared)
