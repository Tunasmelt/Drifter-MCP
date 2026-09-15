"""docs/PHASES.md R1: run lifecycle and recording schema.

Written BEFORE implementation, per CLAUDE.md's schema-evolution procedure, and
confirmed failing first. Every new field is nullable; a historical corpus
recorded before these fields existed must read and aggregate exactly as before.

Acceptance matrix (PHASES R1), every case asserted end to end through the real
HTTP adapter: exit 9 after tools/list; timeout; interrupt; genuine no-tool task;
connectivity probe; late manifest; historical pre-change corpus. None of the
faulty cases may become a valid baseline run.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import anyio
import pytest
import mcp_types as types
from mcp.shared.message import SessionMessage

from mcp_drifter.cli.subprocess_adapter import run_agent_subprocess_http
from mcp_drifter.evaluate.baseline import aggregate_baseline_runs
from mcp_drifter.record.proxy import Direction
from mcp_drifter.record.reader import read_session
from mcp_drifter.record.schema import SessionEnd, SessionStart, ToolCall
from mcp_drifter.record.writer import SessionRecorder
from mcp_drifter.replay.replay_proxy import tools_served_from_session
from mcp_drifter.replay.replay_store import ReplayStore

FIXTURES = Path(__file__).parent.parent / "fixtures"
GOLDEN = FIXTURES / "golden_v0.1.jsonl"
AGENT = FIXTURES / "lifecycle_agent.py"
SERVER = "filesystem"


# --- writer level --------------------------------------------------------------


def _rec(tmp_path: Path) -> SessionRecorder:
    return SessionRecorder(session_dir=tmp_path / "runs", raw_dir=tmp_path / "raw", server_name=SERVER)


def _send(recorder: SessionRecorder, direction: Direction, message) -> None:
    recorder.observe(direction, SessionMessage(message))


def _initialize(recorder: SessionRecorder) -> None:
    _send(recorder, Direction.AGENT_TO_SERVER, types.JSONRPCRequest(jsonrpc="2.0", id=0, method="initialize",
          params={"clientInfo": {"name": "t", "version": "1"}}))
    _send(recorder, Direction.SERVER_TO_AGENT, types.JSONRPCResponse(jsonrpc="2.0", id=0, result={"serverInfo": {"name": "s", "version": "1"}}))


def _list(recorder: SessionRecorder, req_id: int) -> None:
    _send(recorder, Direction.AGENT_TO_SERVER, types.JSONRPCRequest(jsonrpc="2.0", id=req_id, method="tools/list", params={}))
    _send(recorder, Direction.SERVER_TO_AGENT, types.JSONRPCResponse(jsonrpc="2.0", id=req_id,
          result={"tools": [{"name": "a", "description": "d", "inputSchema": {}}]}))


def _call(recorder: SessionRecorder, req_id: int, answer: bool = True) -> None:
    _send(recorder, Direction.AGENT_TO_SERVER, types.JSONRPCRequest(jsonrpc="2.0", id=req_id, method="tools/call",
          params={"name": "a", "arguments": {}}))
    if answer:
        _send(recorder, Direction.SERVER_TO_AGENT, types.JSONRPCResponse(jsonrpc="2.0", id=req_id,
              result={"content": [{"type": "text", "text": "ok"}], "isError": False}))


def test_close_appends_a_session_end_carrying_the_outcome(tmp_path):
    recorder = _rec(tmp_path)
    _initialize(recorder)
    _list(recorder, 1)
    _call(recorder, 2)
    recorder.close(run_outcome="crashed", exit_code=9, task_attempted=True)

    records = list(read_session(recorder.jsonl_path))
    end = records[-1]
    assert isinstance(end, SessionEnd)
    assert (end.run_outcome, end.exit_code, end.task_attempted) == ("crashed", 9, True)
    assert end.seq == max(r.seq for r in records[:-1]) + 1
    assert end.tool_manifest_hash == records[0].environment.tool_manifest_hash is not None


def test_a_late_tools_list_gets_a_home_in_session_end(tmp_path):
    """Limitation 14: a tool call before the first tools/list locks SessionStart
    with a null hash. The hash now also lands in SessionEnd."""
    recorder = _rec(tmp_path)
    _initialize(recorder)
    _call(recorder, 1)
    _list(recorder, 2)
    recorder.close(run_outcome="completed", exit_code=0, task_attempted=True)

    records = list(read_session(recorder.jsonl_path))
    assert records[0].environment.tool_manifest_hash is None
    assert records[-1].tool_manifest_hash is not None


def test_a_call_that_never_got_a_response_is_recorded_at_close(tmp_path):
    """A hang used to be ABSENT from the record. Now it is a faulted, unanswered call."""
    recorder = _rec(tmp_path)
    _initialize(recorder)
    _list(recorder, 1)
    _call(recorder, 2, answer=False)
    recorder.close(run_outcome="timeout", exit_code=None, task_attempted=True)

    calls = [r for r in read_session(recorder.jsonl_path) if isinstance(r, ToolCall)]
    assert len(calls) == 1
    assert calls[0].fault is True and calls[0].unanswered is True
    assert calls[0].fault_code is None and calls[0].result_shape is None


def test_answered_calls_are_not_marked_unanswered(tmp_path):
    recorder = _rec(tmp_path)
    _initialize(recorder)
    _list(recorder, 1)
    _call(recorder, 2)
    recorder.close()
    (call,) = [r for r in read_session(recorder.jsonl_path) if isinstance(r, ToolCall)]
    assert call.unanswered is False


def test_a_plain_close_still_writes_session_end_with_unknown_outcome(tmp_path):
    """Callers that cannot know the outcome (observe, replay-serve) leave it null,
    never a guessed "completed"."""
    recorder = _rec(tmp_path)
    _initialize(recorder)
    recorder.close()
    end = list(read_session(recorder.jsonl_path))[-1]
    assert isinstance(end, SessionEnd)
    assert end.run_outcome is None and end.exit_code is None and end.task_attempted is None


# --- historical corpus: nothing changes ------------------------------------------


def test_a_pre_change_corpus_reads_and_aggregates_exactly_as_before():
    records = list(read_session(GOLDEN))
    assert not any(isinstance(r, SessionEnd) for r in records)
    assert all(r.unanswered is None for r in records if isinstance(r, ToolCall))
    result = aggregate_baseline_runs("golden", [GOLDEN])
    assert result.valid_runs == 1 and result.excluded_runs == []


# --- baseline exclusion, from hand-written sessions ---------------------------------


def _session(tmp_path: Path, name: str, *, calls: int, outcome, exit_code, attempted, late=False) -> Path:
    recorder = SessionRecorder(session_dir=tmp_path / name, raw_dir=tmp_path / (name + "_raw"), server_name=SERVER)
    _initialize(recorder)
    if late:
        _call(recorder, 1)
        _list(recorder, 2)
    else:
        _list(recorder, 1)
        for i in range(calls):
            _call(recorder, 10 + i)
    recorder.close(run_outcome=outcome, exit_code=exit_code, task_attempted=attempted)
    return recorder.jsonl_path


@pytest.mark.parametrize(
    ("outcome", "exit_code", "reason"),
    [("crashed", 9, "run crashed (exit code 9)"), ("timeout", None, "run timeout"), ("interrupted", None, "run interrupted")],
)
def test_a_run_that_did_not_complete_is_excluded(tmp_path, outcome, exit_code, reason):
    path = _session(tmp_path, "s", calls=1, outcome=outcome, exit_code=exit_code, attempted=True)
    result = aggregate_baseline_runs("t", [path])
    assert result.valid_runs == 0
    assert reason in result.excluded_runs[0].reason


def test_a_connectivity_probe_is_excluded_but_a_genuine_no_tool_task_is_valid(tmp_path):
    probe = _session(tmp_path, "probe", calls=0, outcome="completed", exit_code=0, attempted=False)
    genuine = _session(tmp_path, "genuine", calls=0, outcome="completed", exit_code=0, attempted=True)
    result = aggregate_baseline_runs("t", [probe, genuine])
    assert result.valid_runs == 1
    assert result.valid_session_paths == (genuine,)
    assert "no task attempt" in result.excluded_runs[0].reason


def test_a_late_manifest_session_is_now_valid(tmp_path):
    path = _session(tmp_path, "late", calls=0, outcome="completed", exit_code=0, attempted=True, late=True)
    result = aggregate_baseline_runs("t", [path])
    assert result.valid_runs == 1


def test_unknown_lifecycle_fields_keep_the_old_behaviour(tmp_path):
    """observe/replay-serve sessions: outcome and attempt unknown. Not excluded on
    that basis; tracked, not guessed."""
    path = _session(tmp_path, "observe", calls=1, outcome=None, exit_code=None, attempted=None)
    assert aggregate_baseline_runs("t", [path]).valid_runs == 1


# --- the acceptance matrix, through the real HTTP adapter ---------------------------


def _adapter_run(tmp_path: Path, argv: list[str], timeout_s: float | None) -> Path:
    store = ReplayStore()
    store.index_session(GOLDEN)
    return anyio.run(lambda: run_agent_subprocess_http(
        command=[sys.executable, str(AGENT), *argv], replay_store=store, server_name=SERVER,
        tools_served=tools_served_from_session(GOLDEN), session_dir=tmp_path / "runs", raw_dir=tmp_path / "raw",
        timeout_s=timeout_s,
    ))


def _end(path: Path) -> SessionEnd:
    end = list(read_session(path))[-1]
    assert isinstance(end, SessionEnd)
    return end


def _first_golden_call() -> ToolCall:
    return next(r for r in read_session(GOLDEN) if isinstance(r, ToolCall))


def test_matrix_exit_9_after_tools_list(tmp_path):
    path = _adapter_run(tmp_path, ["crash"], timeout_s=60)
    end = _end(path)
    assert (end.run_outcome, end.exit_code) == ("crashed", 9)
    assert aggregate_baseline_runs("t", [path]).valid_runs == 0


def test_matrix_timeout_is_recorded_and_prompt(tmp_path):
    started = time.monotonic()
    path = _adapter_run(tmp_path, ["hang"], timeout_s=3)
    elapsed = time.monotonic() - started
    end = _end(path)
    assert end.run_outcome == "timeout" and end.exit_code is None
    assert elapsed < 20, f"timeout path took {elapsed:.1f}s"
    assert aggregate_baseline_runs("t", [path]).valid_runs == 0


def test_matrix_interrupt_still_writes_session_end_promptly(tmp_path):
    store = ReplayStore()
    store.index_session(GOLDEN)
    session_dir = tmp_path / "runs"

    async def interrupted() -> None:
        with anyio.move_on_after(3):
            await run_agent_subprocess_http(
                command=[sys.executable, str(AGENT), "hang"], replay_store=store, server_name=SERVER,
                tools_served=tools_served_from_session(GOLDEN), session_dir=session_dir, raw_dir=tmp_path / "raw",
                timeout_s=None,
            )

    started = time.monotonic()
    anyio.run(interrupted)
    elapsed = time.monotonic() - started
    (path,) = session_dir.glob("*.jsonl")
    assert _end(path).run_outcome == "interrupted"
    assert elapsed < 20, f"interrupt path took {elapsed:.1f}s"
    assert aggregate_baseline_runs("t", [path]).valid_runs == 0


def test_matrix_connectivity_probe(tmp_path):
    path = _adapter_run(tmp_path, ["probe"], timeout_s=60)
    end = _end(path)
    assert (end.run_outcome, end.exit_code, end.task_attempted) == ("completed", 0, False)
    assert aggregate_baseline_runs("t", [path]).valid_runs == 0


def test_matrix_genuine_no_tool_task(tmp_path):
    path = _adapter_run(tmp_path, ["answer"], timeout_s=60)
    end = _end(path)
    assert (end.run_outcome, end.exit_code, end.task_attempted) == ("completed", 0, True)
    assert aggregate_baseline_runs("t", [path]).valid_runs == 1


def test_matrix_late_manifest(tmp_path):
    call = _first_golden_call()
    path = _adapter_run(tmp_path, [f"late:{call.tool_name}", json.dumps(call.arguments)], timeout_s=60)
    records = list(read_session(path))
    start = next(r for r in records if isinstance(r, SessionStart))
    assert start.environment.tool_manifest_hash is not None or records[-1].tool_manifest_hash is not None
    assert _end(path).run_outcome == "completed"
    assert aggregate_baseline_runs("t", [path]).valid_runs == 1
