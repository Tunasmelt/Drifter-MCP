"""End-to-end integration test: ReplayStore + run_replay_proxy +
run_agent_subprocess + run_baseline, wired together for real via
make_run_once (cli/subprocess_adapter.py).

First time all four Gate 2 pieces run as one pipeline. Deliberately
uses the same golden fixture and scripted test-agent every other test
this gate already relies on, not new fixtures — the milestone here is
the wiring, not new test data. Unlike every other evaluate/baseline.py
test so far (all built on synthetic, hand-written session data), this
run computes dominant_path/natural_variation/baseline_spread from three
genuinely independent, subprocess-spawned, replay-served runs.

No `pytest.mark.anyio` here: run_baseline is a plain sync function (by
design — evaluate/baseline.py's own scope decision), and make_run_once
returns a sync callable that bridges to the async adapter internally
via anyio.run per call. Driving this from a sync test matches exactly
how a real (eventual) caller would use it.
"""

import sys
from pathlib import Path

from cli.subprocess_adapter import make_run_once
from evaluate.baseline import run_baseline
from record.calibration import Calibration
from record.reader import read_session
from record.schema import ToolCall
from replay.replay_proxy import tools_served_from_session
from replay.replay_store import ReplayStore

GOLDEN_FIXTURE = Path(__file__).parent.parent / "fixtures" / "golden_v0.1.jsonl"
SCRIPTED_AGENT = Path(__file__).parent.parent / "fixtures" / "scripted_agent.py"
GOLDEN_SERVER = "filesystem"


def _golden_calls() -> list[ToolCall]:
    return [r for r in read_session(GOLDEN_FIXTURE) if isinstance(r, ToolCall)]


def _spec(tool_name: str, arguments: dict) -> str:
    import json

    return f"{tool_name}|{json.dumps(arguments)}"


def test_full_pipeline_produces_a_real_baseline_from_three_subprocess_runs(tmp_path):
    store = ReplayStore()
    store.index_session(GOLDEN_FIXTURE)
    tools_served = tools_served_from_session(GOLDEN_FIXTURE)

    # The scripted agent's own tool-call sequence is fixed by these argv
    # entries -- deterministic across all 3 repeats, since it's driven
    # purely by argv, not by anything that could vary run to run. This
    # is what dominant_path is asserted against below.
    calls = _golden_calls()[:3]
    expected_path = tuple(c.tool_name for c in calls)
    command = [sys.executable, str(SCRIPTED_AGENT), *(_spec(c.tool_name, c.arguments) for c in calls)]

    run_once = make_run_once(
        command=command,
        replay_store=store,
        server_name=GOLDEN_SERVER,
        tools_served=tools_served,
        session_dir=tmp_path / "runs",
        raw_dir=tmp_path / "raw",
        timeout_s=30.0,
    )

    result = run_baseline("integration_task", run_once=run_once, repeats=3)

    assert result.has_data is True
    assert result.valid_runs == 3
    assert result.excluded_runs == []
    assert result.dominant_path == expected_path
    assert result.variant_frequencies == {expected_path: 3}
    # All 3 runs are driven by the identical deterministic argv, so
    # there is no real variation to observe here -- 0.0 is the correct,
    # meaningful answer (a perfectly stable baseline), not a sign
    # nothing was computed. has_data/valid_runs above are what actually
    # confirm that distinction (see BaselineResult's own docstring).
    assert result.natural_variation == 0.0
    assert result.baseline_spread == 0.0
    # Every one of the 3 runs' calls were real golden-fixture hits --
    # fidelity gating's own real pipeline is exercised separately below
    # (test_low_fidelity_repeat_is_excluded...), but a clean pipeline
    # should still report full fidelity, not leave it uncomputed.
    assert result.baseline_fidelity == 1.0

    # Confirm distinct session files really were produced -- not the
    # same file read 3 times (the exact failure mode requirement 1
    # asked to rule out explicitly, not assume).
    session_files = sorted((tmp_path / "runs").glob("*.jsonl"))
    assert len(session_files) == 3
    session_ids = {p.stem for p in session_files}
    assert len(session_ids) == 3  # genuinely distinct, not 3 paths to 1 id


def test_low_fidelity_repeat_is_excluded_while_clean_repeats_still_contribute(tmp_path):
    """Same real pipeline, but one repeat's scripted agent makes a call
    that was never recorded in the ReplayStore -- a genuine MISS,
    surfaced by replay_proxy.py as fault=True (see its own docstring's
    MISS/fault-both-recorded-as-fault=True decision) and picked up by
    evaluate.baseline._run_fidelity exactly as it would for a real
    degraded run. Confirms fidelity gating end to end: the flaky
    repeat's fidelity is computed correctly (not 100%, not a crash),
    falls below a test-configured floor, and is excluded with the
    fidelity reason -- while the other 2 clean repeats still produce a
    real, non-degenerate baseline (has_data=True).
    """
    store = ReplayStore()
    store.index_session(GOLDEN_FIXTURE)
    tools_served = tools_served_from_session(GOLDEN_FIXTURE)

    calls = _golden_calls()[:2]
    expected_path = tuple(c.tool_name for c in calls)
    clean_command = [sys.executable, str(SCRIPTED_AGENT), *(_spec(c.tool_name, c.arguments) for c in calls)]
    miss_args = {"path": "C:\\nowhere\\never\\recorded\\by\\this\\integration\\test"}
    flaky_command = [
        sys.executable,
        str(SCRIPTED_AGENT),
        *(_spec(c.tool_name, c.arguments) for c in calls),
        _spec("list_directory", miss_args),
    ]

    session_dir, raw_dir = tmp_path / "runs", tmp_path / "raw"
    clean_run_once = make_run_once(
        command=clean_command,
        replay_store=store,
        server_name=GOLDEN_SERVER,
        tools_served=tools_served,
        session_dir=session_dir,
        raw_dir=raw_dir,
        timeout_s=30.0,
    )
    flaky_run_once = make_run_once(
        command=flaky_command,
        replay_store=store,
        server_name=GOLDEN_SERVER,
        tools_served=tools_served,
        session_dir=session_dir,
        raw_dir=raw_dir,
        timeout_s=30.0,
    )

    # Repeat 2 (of 3) is the flaky one -- fidelity 2/3 (2 hits, 1 miss),
    # a low but test-configured floor of 0.75 excludes it while a clean
    # 2/2 repeat clears it easily.
    schedule = iter([clean_run_once, flaky_run_once, clean_run_once])

    def run_once() -> Path:
        return next(schedule)()

    calibration = Calibration()
    calibration.fidelity_floor = 0.75

    result = run_baseline("integration_task_flaky_fidelity", run_once=run_once, repeats=3, calibration=calibration)

    assert result.has_data is True
    assert result.valid_runs == 2
    assert result.dominant_path == expected_path
    assert result.natural_variation == 0.0  # both surviving runs agree
    assert result.baseline_fidelity == 1.0  # only the 2 clean runs' fidelity is averaged in

    assert len(result.excluded_runs) == 1
    excluded = result.excluded_runs[0]
    assert excluded.reason.startswith("fidelity ")
    assert "below floor 0.75" in excluded.reason

    # Confirm the flaky run's own recorded session really does show a
    # miss -- not asserted purely via BaselineResult, so a bug in
    # _run_fidelity's own bookkeeping couldn't hide behind a bug in the
    # test's expectations for it.
    flaky_records = list(read_session(excluded.path))
    flaky_calls = [r for r in flaky_records if isinstance(r, ToolCall)]
    assert len(flaky_calls) == 3
    assert sum(1 for c in flaky_calls if c.fault is True) == 1
    assert sum(1 for c in flaky_calls if c.fault is False) == 2
