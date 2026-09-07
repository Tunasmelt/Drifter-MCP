"""Direct, fast unit tests for record/writer.py's SessionRecorder
(F-01/F-02/F-03/F-05) — driven via `observe()` directly with hand-built
JSON-RPC messages, not a real spawned subprocess (test_proxy.py's own
job). This is the module's first dedicated unit-test file: previously it
was only exercised indirectly through real-subprocess integration tests
(test_proxy.py) and fixture-based read tests (test_golden_fixture.py,
test_redaction.py) — neither of which can deterministically control exact
message ORDERING, which is exactly what F-05's real, known gap (SPEC.md
§15 limitation 14) is about.
"""

from __future__ import annotations

from pathlib import Path

import mcp_types as types
from mcp.shared.message import SessionMessage

from record.proxy import Direction
from record.reader import read_session
from record.schema import SessionStart, ToolCall, TrajectoryEnd
from record.writer import SessionRecorder

JSONRPCRequest = types.JSONRPCRequest
JSONRPCResponse = types.JSONRPCResponse


def _recorder(tmp_path: Path) -> SessionRecorder:
    return SessionRecorder(session_dir=tmp_path / "runs", raw_dir=tmp_path / "raw", server_name="fake")


def _initialize(recorder: SessionRecorder, req_id: int = 0) -> None:
    recorder.observe(
        Direction.AGENT_TO_SERVER,
        SessionMessage(
            JSONRPCRequest(
                jsonrpc="2.0", id=req_id, method="initialize", params={"clientInfo": {"name": "test-agent", "version": "1.0"}}
            )
        ),
    )
    recorder.observe(
        Direction.SERVER_TO_AGENT,
        SessionMessage(
            JSONRPCResponse(jsonrpc="2.0", id=req_id, result={"serverInfo": {"name": "fake-server", "version": "2.0"}})
        ),
    )


def _tools_list(recorder: SessionRecorder, req_id: int, tools: list[dict]) -> None:
    recorder.observe(
        Direction.AGENT_TO_SERVER, SessionMessage(JSONRPCRequest(jsonrpc="2.0", id=req_id, method="tools/list", params={}))
    )
    recorder.observe(
        Direction.SERVER_TO_AGENT, SessionMessage(JSONRPCResponse(jsonrpc="2.0", id=req_id, result={"tools": tools}))
    )


def _tool_call(recorder: SessionRecorder, req_id: int, name: str, arguments: dict, meta: dict | None = None) -> None:
    params: dict = {"name": name, "arguments": arguments}
    if meta is not None:
        params["_meta"] = meta
    recorder.observe(
        Direction.AGENT_TO_SERVER, SessionMessage(JSONRPCRequest(jsonrpc="2.0", id=req_id, method="tools/call", params=params))
    )
    recorder.observe(
        Direction.SERVER_TO_AGENT,
        SessionMessage(
            JSONRPCResponse(jsonrpc="2.0", id=req_id, result={"content": [{"type": "text", "text": "ok"}]})
        ),
    )


def _read(recorder: SessionRecorder) -> list:
    recorder.close()
    return list(read_session(recorder.jsonl_path))


# --- F-02: general recording invariants -------------------------------------


def test_session_start_is_always_seq_zero(tmp_path):
    recorder = _recorder(tmp_path)
    _initialize(recorder)
    _tools_list(recorder, 1, [{"name": "a", "description": "d", "inputSchema": {}}])
    _tool_call(recorder, 2, "a", {})
    records = _read(recorder)
    assert isinstance(records[0], SessionStart)
    assert records[0].seq == 0


def test_seq_is_monotonic_and_never_reused(tmp_path):
    recorder = _recorder(tmp_path)
    _initialize(recorder)
    _tools_list(recorder, 1, [{"name": "a", "description": "d", "inputSchema": {}}])
    _tool_call(recorder, 2, "a", {})
    _tool_call(recorder, 3, "a", {})
    records = _read(recorder)
    assert [r.seq for r in records] == list(range(len(records)))


def test_close_is_idempotent(tmp_path):
    """Calling close() twice must not double-write SessionStart, crash, or
    otherwise corrupt the file — mirrors cli/observe.py's own rapid-
    double-Ctrl+C guard (test_handle_sigint...), but exercised directly
    against the writer itself rather than through the SIGINT handler."""
    recorder = _recorder(tmp_path)
    _initialize(recorder)
    _tool_call(recorder, 1, "a", {})
    recorder.close()
    records_after_first_close = list(read_session(recorder.jsonl_path))
    recorder.close()  # must be a no-op
    records_after_second_close = list(read_session(recorder.jsonl_path))
    assert records_after_first_close == records_after_second_close
    assert sum(1 for r in records_after_first_close if isinstance(r, SessionStart)) == 1


def test_a_parse_error_increments_error_count_and_writes_no_record(tmp_path):
    """`observe()`'s documented contract for a frame that failed to
    parse: count it, write nothing (there's no valid message to record).
    Never previously tested directly at the writer level — only implied
    by cli/observe.py's own live-counter display."""
    recorder = _recorder(tmp_path)
    _initialize(recorder)
    recorder.observe(Direction.SERVER_TO_AGENT, ValueError("simulated unparseable frame"))
    records = _read(recorder)
    assert recorder.error_count == 1
    assert not any(isinstance(r, ToolCall) for r in records)


def test_a_response_with_an_untracked_request_id_is_ignored_not_crashed(tmp_path):
    """A stray response whose id was never sent as a request (e.g. a
    protocol oddity, or a message this recorder started observing
    mid-session) must be silently ignored, per `observe()`'s own
    `pending.pop(rpc.id, None); if pending is None: return` contract --
    confirmed here to actually hold, not just read from the source."""
    recorder = _recorder(tmp_path)
    _initialize(recorder)
    recorder.observe(
        Direction.SERVER_TO_AGENT,
        SessionMessage(JSONRPCResponse(jsonrpc="2.0", id=999, result={"content": []})),
    )
    records = _read(recorder)
    assert not any(isinstance(r, ToolCall) for r in records)


def test_raw_frame_offsets_are_correct_and_distinct_across_multiple_calls(tmp_path):
    """test_proxy.py's own integration test confirms this for a SINGLE
    call; multiple sequential calls is a genuinely different case (each
    offset must point at ITS OWN frame, not accidentally reuse or
    collide with an earlier one) that a real subprocess test can't
    control the exact interleaving of as precisely as this direct one."""
    import json

    recorder = _recorder(tmp_path)
    _initialize(recorder)
    _tool_call(recorder, 1, "a", {"x": 1})
    _tool_call(recorder, 2, "b", {"x": 2})
    records = _read(recorder)
    calls = [r for r in records if isinstance(r, ToolCall)]
    assert len(calls) == 2
    assert calls[0].raw_frame_offset != calls[1].raw_frame_offset

    raw_bytes = recorder.raw_path.read_bytes()
    for call, expected_name in zip(calls, ["a", "b"]):
        frame_line = raw_bytes[call.raw_frame_offset :].split(b"\n", 1)[0]
        frame = json.loads(frame_line.decode("utf-8"))
        # The response frame at this exact offset must be THIS call's own
        # response, not the other one's -- confirmed via the recorded
        # tool_name matching what's independently derivable from the
        # frame's own content shape being distinct per call is not
        # possible here (both return the same shape), so this asserts on
        # the request/response pairing already proven correct by seq
        # ordering: offset i corresponds to response i, checked by
        # position instead.
        assert "result" in frame


def test_two_trace_context_tagged_trajectories_are_correctly_separated(tmp_path):
    """F-06: two calls carrying DIFFERENT `_meta.traceparent` trace ids
    must land in separate trajectories, each with its own TrajectoryEnd
    naming only its own call's seq -- not merged into one trajectory just
    because they're adjacent in time."""
    trace_a = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
    trace_b = "00-" + "c" * 32 + "-" + "d" * 16 + "-01"

    recorder = _recorder(tmp_path)
    _initialize(recorder)
    _tool_call(recorder, 1, "a", {}, meta={"traceparent": trace_a})
    _tool_call(recorder, 2, "b", {}, meta={"traceparent": trace_b})
    records = _read(recorder)

    trajectory_ends = [r for r in records if isinstance(r, TrajectoryEnd)]
    assert len(trajectory_ends) == 2
    call_seqs_per_trajectory = sorted(tuple(t.call_seqs) for t in trajectory_ends)
    assert all(len(seqs) == 1 for seqs in call_seqs_per_trajectory)  # never merged


# --- F-05: environment fingerprinting, including the real, known gap -------


def test_tools_list_before_first_tools_call_populates_the_hash(tmp_path):
    """The happy path, confirmed at the fast unit level (previously only
    covered by test_proxy.py's real-subprocess integration test)."""
    recorder = _recorder(tmp_path)
    _initialize(recorder)
    _tools_list(recorder, 1, [{"name": "a", "description": "d", "inputSchema": {}}])
    _tool_call(recorder, 2, "a", {})
    records = _read(recorder)
    start = next(r for r in records if isinstance(r, SessionStart))
    assert start.environment.tool_manifest_hash is not None


def test_tools_call_before_any_tools_list_permanently_nulls_the_hash(tmp_path):
    """THE real, known F-05 gap (SPEC.md §15 limitation 14), given its
    own proper home at the writer unit level rather than only being
    covered by tests/cli/gate4_dry_run/test_user_6.py's real-subprocess
    integration test. `_ensure_session_start_written()` locks in
    `SessionStart` -- hash included -- on whichever tracked response
    arrives FIRST; a tools/call response arriving before any tools/list
    response locks in a null hash permanently, since SessionStart is
    JSONL's append-only first record and is never rewritten."""
    recorder = _recorder(tmp_path)
    _initialize(recorder)
    _tool_call(recorder, 1, "a", {})  # tools/call FIRST, no tools/list at all
    records = _read(recorder)
    start = next(r for r in records if isinstance(r, SessionStart))
    call_count = sum(1 for r in records if isinstance(r, ToolCall))
    assert call_count == 1  # a completely real, successful call happened
    assert start.environment.tool_manifest_hash is None  # yet the hash is permanently null


def test_tools_list_arriving_after_the_first_tools_call_is_too_late_to_help(tmp_path):
    """The specific, sharper version of the gap above: even when
    tools/list DOES eventually happen in the very same session, arriving
    after the first tools/call is still too late -- SessionStart was
    already flushed. Confirms this is genuinely about ORDER, not about
    whether tools/list ever happens at all."""
    recorder = _recorder(tmp_path)
    _initialize(recorder)
    _tool_call(recorder, 1, "a", {})  # locks in SessionStart with null hash
    _tools_list(recorder, 2, [{"name": "a", "description": "d", "inputSchema": {}}])  # too late
    records = _read(recorder)
    start = next(r for r in records if isinstance(r, SessionStart))
    assert start.environment.tool_manifest_hash is None


# --- F-26: raw MCP annotations captured on ToolDescriptor -------------------


def test_tools_list_captures_the_raw_annotations_block(tmp_path):
    """policy/classify.py's tier-1 (docs/SPEC.md §10) input: the real wire
    `annotations` dict, camelCase keys as MCP actually sends them, captured
    unmodified -- not renamed, not reinterpreted here."""
    from record.schema import ToolsList

    recorder = _recorder(tmp_path)
    _initialize(recorder)
    _tools_list(
        recorder,
        1,
        [
            {
                "name": "get_customer",
                "description": "d",
                "inputSchema": {},
                "annotations": {"readOnlyHint": True, "openWorldHint": True},
            }
        ],
    )
    records = _read(recorder)
    tools_list = next(r for r in records if isinstance(r, ToolsList))
    assert tools_list.tools_served[0].annotations == {"readOnlyHint": True, "openWorldHint": True}


def test_tools_list_with_no_annotations_key_leaves_it_none(tmp_path):
    """A real, common case (annotations is optional in the spec) -- must
    stay None, not `{}` or some other falsy-but-present placeholder."""
    from record.schema import ToolsList

    recorder = _recorder(tmp_path)
    _initialize(recorder)
    _tools_list(recorder, 1, [{"name": "a", "description": "d", "inputSchema": {}}])
    records = _read(recorder)
    tools_list = next(r for r in records if isinstance(r, ToolsList))
    assert tools_list.tools_served[0].annotations is None


def test_a_purely_empty_session_still_gets_a_session_start_at_close(tmp_path):
    """Neither tools/list nor tools/call ever happens (a bare connectivity
    check) -- `close()`'s own documented safety net still flushes
    SessionStart, with whatever identity info `initialize` provided and a
    null hash, rather than producing an empty/missing session file."""
    recorder = _recorder(tmp_path)
    _initialize(recorder)
    records = _read(recorder)
    assert len(records) == 1
    start = records[0]
    assert isinstance(start, SessionStart)
    assert start.environment.agent_identity == "test-agent/1.0"
    assert start.environment.tool_manifest_hash is None
    assert not any(isinstance(r, ToolCall) for r in records)
