"""Integration tests for trajectory segmentation (F-06/F-07/F-08).

Three fixtures, per the Gate 1 prompt pack:
1. Trace context present -> segments at 0.99 confidence, zero manual correction.
2. No trace context, clear idle gap, a data-flow dependency between two calls
   -> heuristic segments correctly AND captures the reference.
3. Adversarial: no trace context, no idle gap, two calls unrelated by content
   -> documented as a known limitation if the heuristic gets it wrong
   (SPEC.md §15's practice of stating limitations plainly).
"""

import sys
import time
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from record.reader import read_session
from record.schema import ToolCall, TrajectoryEnd

FIXTURE_SERVER = str(Path(__file__).parent.parent / "fixtures" / "fake_server.py")
DRIFTER_PROXY_COMMAND = [sys.executable, "-m", "record", sys.executable, FIXTURE_SERVER]


def _proxied_params(runs_dir: Path, raw_dir: Path, extra_env: dict | None = None) -> StdioServerParameters:
    env = {"DRIFTER_RUNS_DIR": str(runs_dir), "DRIFTER_RAW_DIR": str(raw_dir)}
    env.update(extra_env or {})
    return StdioServerParameters(command=DRIFTER_PROXY_COMMAND[0], args=DRIFTER_PROXY_COMMAND[1:], env=env)


def _read_records(runs_dir: Path):
    jsonl_files = list(runs_dir.glob("*.jsonl"))
    assert len(jsonl_files) == 1
    return list(read_session(jsonl_files[0]))


@pytest.mark.anyio
async def test_trace_context_groups_calls_at_high_confidence(tmp_path):
    runs_dir, raw_dir = tmp_path / "runs", tmp_path / "raw"
    traceparent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"

    async with stdio_client(_proxied_params(runs_dir, raw_dir)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            # Two calls sharing one trace ID, with an unrelated call
            # between them, no data-flow link — trace context alone must
            # still group the two that share it.
            await session.call_tool("add", {"a": 1, "b": 1}, meta={"traceparent": traceparent})
            await session.call_tool("echo", {"payload": {"unrelated": True}})
            await session.call_tool("add", {"a": 2, "b": 2}, meta={"traceparent": traceparent})

    records = _read_records(runs_dir)
    calls = [r for r in records if isinstance(r, ToolCall)]
    trajectories = [r for r in records if isinstance(r, TrajectoryEnd)]
    assert len(calls) == 3

    traced_seqs = {c.seq for c in calls if c.tool_name == "add"}
    trace_trajectory = next((t for t in trajectories if set(t.call_seqs) == traced_seqs), None)
    assert trace_trajectory is not None, f"no trajectory matched {traced_seqs}; got {trajectories}"
    assert trace_trajectory.segmentation_method == "trace_context"
    assert trace_trajectory.segmentation_confidence == 0.99

    # The unrelated echo call must NOT have been folded into the traced
    # trajectory just because it happened in between.
    echo_seq = next(c.seq for c in calls if c.tool_name == "echo")
    assert echo_seq not in trace_trajectory.call_seqs


@pytest.mark.anyio
async def test_heuristic_segments_on_idle_gap_and_captures_data_flow_reference(tmp_path):
    runs_dir, raw_dir = tmp_path / "runs", tmp_path / "raw"
    # Small idle gap so the test doesn't need to sleep 30 real seconds.
    calibration_path = tmp_path / "calibration.yaml"
    calibration_path.write_text("segmentation:\n  idle_gap_seconds: 0.2\n", encoding="utf-8")
    env = {"DRIFTER_CALIBRATION_PATH": str(calibration_path)}

    async with stdio_client(_proxied_params(runs_dir, raw_dir, env)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            # Call 1 produces a value; call 2 immediately consumes it via a
            # matching argument value — must stay grouped even past the
            # idle gap, per F-07's "references link stays grouped".
            result = await session.call_tool("echo", {"payload": {"customer_id": "cust_42"}})
            await session.call_tool("echo", {"payload": {"id": "cust_42"}})
            time.sleep(0.4)  # exceed the idle gap with no data-flow link
            await session.call_tool("add", {"a": 9, "b": 9})

    records = _read_records(runs_dir)
    calls = [r for r in records if isinstance(r, ToolCall)]
    trajectories = [r for r in records if isinstance(r, TrajectoryEnd)]
    assert len(calls) == 3
    assert all(t.segmentation_method == "heuristic" for t in trajectories)

    echo_seqs = [c.seq for c in calls if c.tool_name == "echo"]
    add_seq = next(c.seq for c in calls if c.tool_name == "add")

    linked_trajectory = next((t for t in trajectories if set(t.call_seqs) == set(echo_seqs)), None)
    assert linked_trajectory is not None, f"no trajectory matched {echo_seqs}; got {trajectories}"
    assert add_seq not in linked_trajectory.call_seqs  # separated by the idle gap

    second_call = calls[1]
    assert second_call.references != []
    assert second_call.references[0].source_seq == echo_seqs[0]
    assert second_call.references[0].target_path == "$.payload.id"


@pytest.mark.anyio
async def test_adversarial_no_trace_no_gap_unrelated_calls_still_grouped(tmp_path):
    """Known limitation (SPEC.md §15): with no trace context and no idle
    gap, the heuristic has no signal to separate two calls that are
    unrelated by content — they get grouped into one trajectory. This
    test documents that behavior rather than hiding it.
    """
    runs_dir, raw_dir = tmp_path / "runs", tmp_path / "raw"

    async with stdio_client(_proxied_params(runs_dir, raw_dir)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            # Back-to-back, no shared values, no trace context.
            await session.call_tool("add", {"a": 1, "b": 2})
            await session.call_tool("echo", {"payload": {"totally": "unrelated"}})

    records = _read_records(runs_dir)
    calls = [r for r in records if isinstance(r, ToolCall)]
    trajectories = [r for r in records if isinstance(r, TrajectoryEnd)]
    assert len(calls) == 2
    assert len(trajectories) == 1  # the known limitation: incorrectly merged
    assert set(trajectories[0].call_seqs) == {c.seq for c in calls}
    assert trajectories[0].segmentation_method == "heuristic"
    # And correctly: no fabricated reference between genuinely unrelated calls.
    assert calls[1].references == []


@pytest.fixture
def anyio_backend():
    return "asyncio"
