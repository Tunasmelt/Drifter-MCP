"""Tests for the safety verdict engine (F-25), docs/SPEC.md §8/§10.

Two levels: `evaluate_safety` (pure, already-parsed records — fast,
precise) and `evaluate_safety_for_session` (a real JSONL round trip via
`record.writer.SessionRecorder` + `record.reader.read_session`, matching
this project's own "prefer fixture-based tests over hand-built dataclasses
where a real recording matters" precedent).
"""

from pathlib import Path

from mcp_drifter.record.schema import ToolCall, ToolDescriptor
from mcp_drifter.policy.safety import evaluate_safety, evaluate_safety_for_session


def _tool(name: str) -> ToolDescriptor:
    return ToolDescriptor(name=name, description="d", input_schema={})


def _call(seq: int, tool_name: str) -> ToolCall:
    return ToolCall(
        session_id="s",
        seq=seq,
        timestamp="2026-08-25T00:00:00Z",
        server="fake",
        tool_name=tool_name,
        arguments={},
        result_shape={"type": "object", "keys": []},
        is_error=False,
        duration_ms=1.0,
        fault=False,
        raw_frame_offset=seq * 10,
    )


# --- evaluate_safety: pure core ----------------------------------------------


def test_no_risky_calls_is_no_violation():
    tools = [_tool("get_customer"), _tool("create_invoice")]
    calls = [_call(1, "get_customer"), _call(2, "create_invoice")]
    result = evaluate_safety(calls, tools)
    assert result.verdict == "NO_VIOLATION"
    assert result.findings == ()


def test_a_destructive_tool_call_is_a_violation():
    """F-25's own docs/FEATURES.md 'Done when' bar: a planted unexpected write
    to a destructive tool is caught as a SAFETY VIOLATION."""
    tools = [_tool("get_customer"), _tool("delete_customer")]
    calls = [_call(1, "get_customer"), _call(2, "delete_customer")]
    result = evaluate_safety(calls, tools)
    assert result.verdict == "VIOLATION"
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.kind == "destructive_invocation"
    assert finding.tool_name == "delete_customer"
    assert finding.seq == 2


def test_an_irreversible_write_call_is_also_a_violation():
    tools = [_tool("send_email")]
    calls = [_call(1, "send_email")]
    result = evaluate_safety(calls, tools)
    assert result.verdict == "VIOLATION"
    assert result.findings[0].tool_name == "send_email"


def test_a_reversible_write_call_is_not_flagged_as_destructive():
    """docs/SPEC.md §10's taxonomy singles out destructive/irreversible_write for
    this check -- a reversible write must not trip it."""
    tools = [_tool("create_invoice")]
    calls = [_call(1, "create_invoice")]
    result = evaluate_safety(calls, tools)
    assert result.verdict == "NO_VIOLATION"


def test_a_read_only_call_is_never_flagged():
    tools = [_tool("get_customer"), _tool("list_files")]
    calls = [_call(1, "get_customer"), _call(2, "list_files")]
    result = evaluate_safety(calls, tools)
    assert result.verdict == "NO_VIOLATION"


def test_confirmation_required_tool_call_is_a_violation_even_when_otherwise_safe():
    """A read-only-classified tool the USER has flagged as
    confirmation_required must still be a finding -- the bypass check is
    independent of risk classification entirely."""
    tools = [_tool("get_customer")]
    calls = [_call(1, "get_customer")]
    result = evaluate_safety(calls, tools, confirmation_required=["get_customer"])
    assert result.verdict == "VIOLATION"
    assert len(result.findings) == 1
    assert result.findings[0].kind == "confirmation_required_bypass"


def test_a_call_can_produce_both_findings_at_once():
    """A destructive tool that's ALSO in confirmation_required -- both
    checks are independent, both must fire, not just the first one that
    matches."""
    tools = [_tool("delete_customer")]
    calls = [_call(1, "delete_customer")]
    result = evaluate_safety(calls, tools, confirmation_required=["delete_customer"])
    assert result.verdict == "VIOLATION"
    assert len(result.findings) == 2
    kinds = {f.kind for f in result.findings}
    assert kinds == {"destructive_invocation", "confirmation_required_bypass"}


def test_multiple_findings_across_different_calls_are_all_captured():
    tools = [_tool("delete_a"), _tool("get_b"), _tool("delete_c")]
    calls = [_call(1, "delete_a"), _call(2, "get_b"), _call(3, "delete_c")]
    result = evaluate_safety(calls, tools)
    assert len(result.findings) == 2
    assert [f.tool_name for f in result.findings] == ["delete_a", "delete_c"]


def test_destructive_override_policy_affects_safety_evaluation():
    """A tool the heuristic would call read-only, but the user has listed
    under policy.destructive, must be caught here too -- safety evaluation
    reads the SAME override F-26's classifier does, not a separate copy."""
    tools = [_tool("get_customer")]
    calls = [_call(1, "get_customer")]
    result = evaluate_safety(calls, tools, destructive_override=["get_customer"])
    assert result.verdict == "VIOLATION"
    assert result.findings[0].kind == "destructive_invocation"


def test_a_call_to_a_tool_missing_from_the_served_manifest_does_not_crash():
    """Real edge case: a tool_addition-injected call, or any tool_name not
    present in tools_served for some other reason -- classify_manifest has
    no entry for it, must not KeyError, and produces no destructive-
    invocation finding for that specific call (nothing to classify it
    against) -- though a confirmation_required match still fires
    independently, since that check doesn't need a classification at all.
    """
    tools = [_tool("get_customer")]
    calls = [_call(1, "get_customer"), _call(2, "mystery_tool")]
    result = evaluate_safety(calls, tools, confirmation_required=["mystery_tool"])
    assert result.verdict == "VIOLATION"
    assert len(result.findings) == 1
    assert result.findings[0].tool_name == "mystery_tool"
    assert result.findings[0].kind == "confirmation_required_bypass"


def test_empty_session_is_no_violation():
    result = evaluate_safety([], [])
    assert result.verdict == "NO_VIOLATION"
    assert result.findings == ()


# --- evaluate_safety_for_session: real JSONL round trip ----------------------


def _record_session(tmp_path: Path, calls: list[tuple[str, dict]]) -> Path:
    from mcp.shared.message import SessionMessage
    from mcp_types import JSONRPCRequest, JSONRPCResponse

    from mcp_drifter.record.proxy import Direction
    from mcp_drifter.record.writer import SessionRecorder

    runs_dir, raw_dir = tmp_path / "runs", tmp_path / "raw"
    recorder = SessionRecorder(session_dir=runs_dir, raw_dir=raw_dir, server_name="fake", session_id="sess_safety")

    recorder.observe(
        Direction.AGENT_TO_SERVER,
        SessionMessage(JSONRPCRequest(jsonrpc="2.0", id=0, method="tools/list", params={})),
    )
    tools_payload = sorted({name for name, _ in calls})
    recorder.observe(
        Direction.SERVER_TO_AGENT,
        SessionMessage(
            JSONRPCResponse(
                jsonrpc="2.0",
                id=0,
                result={"tools": [{"name": n, "description": "d", "inputSchema": {}} for n in tools_payload]},
            )
        ),
    )
    for i, (name, arguments) in enumerate(calls, start=1):
        recorder.observe(
            Direction.AGENT_TO_SERVER,
            SessionMessage(
                JSONRPCRequest(jsonrpc="2.0", id=i, method="tools/call", params={"name": name, "arguments": arguments})
            ),
        )
        recorder.observe(
            Direction.SERVER_TO_AGENT,
            SessionMessage(JSONRPCResponse(jsonrpc="2.0", id=i, result={"content": []})),
        )
    recorder.close()
    return recorder.jsonl_path


def test_evaluate_safety_for_session_against_a_real_recorded_session_with_a_planted_violation(tmp_path):
    """The full, real end-to-end path: a genuinely recorded session (via
    SessionRecorder, not hand-built dataclasses) with a planted destructive
    call, read back and evaluated."""
    path = _record_session(tmp_path, [("get_customer", {}), ("delete_customer", {"id": "42"})])
    result = evaluate_safety_for_session(path)
    assert result.verdict == "VIOLATION"
    assert any(f.tool_name == "delete_customer" for f in result.findings)


def test_evaluate_safety_for_session_clean_pass_against_a_real_recorded_session(tmp_path):
    path = _record_session(tmp_path, [("get_customer", {}), ("list_files", {})])
    result = evaluate_safety_for_session(path)
    assert result.verdict == "NO_VIOLATION"
    assert result.findings == ()
