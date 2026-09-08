"""Tests for adaptive repeat scheduling (F-27), docs/SPEC.md §8.

F-27's own "Done when": fewer total runs than fixed-N, **with the same
final verdicts**. The equivalence half is the one that matters — a
scheduler that saves runs by changing answers has not implemented this
feature, it has broken the one above it.
"""

from pathlib import Path

import pytest

from mcp_drifter.evaluate.baseline import BaselineResult, aggregate_baseline_runs, run_baseline
from mcp_drifter.evaluate.effect_size import compute_behavior_effect_size
from mcp_drifter.evaluate.scheduling import next_decision, run_mutated_adaptively
from mcp_drifter.record.calibration import Calibration
from mcp_drifter.record.schema import Environment, SessionStart, ToolCall


def _write_session(dir_path: Path, session_id: str, tool_names: list[str]) -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    lines = [
        SessionStart(
            session_id=session_id, seq=0, started_at="2026-08-25T00:00:00Z",
            environment=Environment(tool_manifest_hash="h"), raw_frame_offset=0,
        ).model_dump_json()
    ]
    for i, tool_name in enumerate(tool_names, start=1):
        lines.append(
            ToolCall(
                session_id=session_id, seq=i, timestamp="2026-08-25T00:00:01Z", server="srv",
                tool_name=tool_name, arguments={}, result_shape={"type": "object", "keys": []},
                is_error=False, duration_ms=1.0, fault=False, raw_frame_offset=i * 100,
            ).model_dump_json()
        )
    path = dir_path / f"{session_id}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _runner(tmp_path: Path, paths_per_run: list[list[str]], counter: dict):
    """A run_once yielding one session per call, cycling `paths_per_run`."""

    def run_once() -> Path:
        i = counter["n"]
        counter["n"] += 1
        return _write_session(tmp_path, f"r{i}", paths_per_run[i % len(paths_per_run)])

    return run_once


def _baseline(tmp_path: Path, paths: list[list[str]], task="t") -> BaselineResult:
    session_paths = [_write_session(tmp_path / "base", f"b{i}", p) for i, p in enumerate(paths)]
    return aggregate_baseline_runs(task, session_paths)


# --- the decision rule -------------------------------------------------------


def test_stops_before_the_mutated_arm_when_the_baseline_cannot_support_a_verdict(tmp_path):
    """The largest saving here, and one that only became knowable once
    DEC-027's minimum-evidence gate existed: a baseline below
    min_valid_runs makes the verdict UNKNOWN no matter what the mutated
    arm does, so every mutated run is guaranteed waste."""
    thin = _baseline(tmp_path, [["a"]])  # 1 valid run, below min_valid_runs=3
    empty = aggregate_baseline_runs("t", [])

    decision = next_decision(thin, empty, attempts_remaining=20, calibration=Calibration())

    assert decision.should_continue is False
    assert "regardless of the mutated arm" in decision.reason


def test_continues_while_the_mutated_arm_is_below_the_minimum(tmp_path):
    baseline = _baseline(tmp_path, [["a"], ["a"], ["a"]])
    partial = aggregate_baseline_runs("m", [_write_session(tmp_path / "mut", "m0", ["a"])])

    decision = next_decision(baseline, partial, attempts_remaining=19, calibration=Calibration())

    assert decision.should_continue is True
    assert "below the 3" in decision.reason


def test_stops_once_no_remaining_run_could_change_the_verdict(tmp_path):
    """The proof, not a peek: a perfectly stable baseline plus three
    mutated runs all deviating means even every remaining run matching
    cannot pull the verdict back."""
    baseline = _baseline(tmp_path, [["a"], ["a"], ["a"], ["a"]])
    mutated_dir = tmp_path / "mut"
    mutated = aggregate_baseline_runs(
        "m", [_write_session(mutated_dir, f"m{i}", ["b"]) for i in range(3)]
    )

    decision = next_decision(baseline, mutated, attempts_remaining=1, calibration=Calibration())

    assert decision.should_continue is False
    assert "already settled" in decision.reason


def test_continues_while_the_outcome_is_genuinely_still_open(tmp_path):
    """With many runs left and a mixed picture so far, remaining runs CAN
    still flip the answer — so it must not stop."""
    baseline = _baseline(tmp_path, [["a"], ["a"], ["a"], ["b"]])  # some natural variation
    mutated_dir = tmp_path / "mut"
    paths = [_write_session(mutated_dir, "m0", ["a"]), _write_session(mutated_dir, "m1", ["b"]),
             _write_session(mutated_dir, "m2", ["a"])]
    mutated = aggregate_baseline_runs("m", paths)

    decision = next_decision(baseline, mutated, attempts_remaining=17, calibration=Calibration())

    assert decision.should_continue is True


def test_stops_at_the_ceiling_regardless(tmp_path):
    baseline = _baseline(tmp_path, [["a"], ["a"], ["a"]])
    mutated = aggregate_baseline_runs("m", [])

    decision = next_decision(baseline, mutated, attempts_remaining=0, calibration=Calibration())

    assert decision.should_continue is False
    assert "ceiling" in decision.reason


# --- F-27's actual "Done when" ----------------------------------------------


def test_adaptive_uses_fewer_runs_than_fixed_n_on_a_clear_cut_case(tmp_path):
    """Half of the bar: measurably fewer runs."""
    baseline = _baseline(tmp_path, [["a"]] * 5)
    counter = {"n": 0}
    adaptive = run_mutated_adaptively(
        "m", _runner(tmp_path / "mut", [["b"]], counter), baseline, max_repeats=20
    )

    assert adaptive.attempts_made < 20
    assert adaptive.runs_saved > 0


@pytest.mark.parametrize(
    "mutated_paths",
    [
        [["a"]],              # identical to baseline -> NO_REGRESSION
        [["b"]],              # wholly different -> REGRESSION
        [["a"], ["b"]],       # mixed
    ],
)
def test_adaptive_and_fixed_n_reach_the_same_verdict(tmp_path, mutated_paths):
    """The half that actually matters. A scheduler that saves runs by
    changing answers has broken the feature above it, not implemented this
    one. Guaranteed by construction (it only stops when no remaining
    outcome could differ) -- asserted here rather than trusted."""
    baseline = _baseline(tmp_path, [["a"], ["a"], ["a"], ["a"], ["b"]])

    fixed_counter = {"n": 0}
    fixed = run_baseline(
        "m", _runner(tmp_path / "fixed", mutated_paths, fixed_counter), repeats=20
    )
    adaptive_counter = {"n": 0}
    adaptive = run_mutated_adaptively(
        "m", _runner(tmp_path / "adaptive", mutated_paths, adaptive_counter), baseline, max_repeats=20
    )

    fixed_verdict = compute_behavior_effect_size(baseline, fixed).verdict
    adaptive_verdict = compute_behavior_effect_size(baseline, adaptive.result).verdict

    assert adaptive_verdict == fixed_verdict
    assert adaptive.attempts_made <= 20


def test_a_thin_baseline_spends_zero_mutated_runs(tmp_path):
    """The guaranteed-waste case, end to end: not one agent run is spent."""
    thin = _baseline(tmp_path, [["a"]])
    counter = {"n": 0}
    adaptive = run_mutated_adaptively(
        "m", _runner(tmp_path / "mut", [["b"]], counter), thin, max_repeats=20
    )

    assert adaptive.attempts_made == 0
    assert counter["n"] == 0  # run_once was never called at all
    assert adaptive.runs_saved == 20


def test_adaptive_never_exceeds_its_ceiling(tmp_path):
    """An undecidable case must still terminate at max_repeats."""
    baseline = _baseline(tmp_path, [["a"], ["a"], ["b"], ["b"]])  # high natural variation
    counter = {"n": 0}
    adaptive = run_mutated_adaptively(
        "m", _runner(tmp_path / "mut", [["a"], ["b"]], counter), baseline, max_repeats=6
    )

    assert adaptive.attempts_made <= 6


def test_a_failing_run_once_is_excluded_not_fatal(tmp_path):
    """Matches run_baseline's own contract -- an adaptive loop that crashed
    on one flaky repeat would be strictly worse than the fixed loop it
    replaces."""
    baseline = _baseline(tmp_path, [["a"]] * 4)
    calls = {"n": 0}

    def flaky() -> Path:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("spawn failed")
        return _write_session(tmp_path / "mut", f"m{calls['n']}", ["b"])

    adaptive = run_mutated_adaptively("m", flaky, baseline, max_repeats=8)

    assert any("run_once raised" in e.reason for e in adaptive.result.excluded_runs)
    assert adaptive.result.valid_runs > 0


def test_a_zero_spread_baseline_cannot_stop_early_on_no_regression(tmp_path):
    """Intended, not a defect: with baseline_spread == 0 the verdict rule is
    infinitely sharp (any deviation at all beats a natural variation of
    zero), so one deviating run among those remaining would flip
    NO_REGRESSION to REGRESSION and no number of clean runs rules that out.
    The scheduler correctly refuses to claim certainty it doesn't have.
    Pinned so a future "optimization" that softens it has to argue with
    this test rather than quietly introduce optional-stopping bias."""
    baseline = _baseline(tmp_path, [["a"]] * 8)
    assert baseline.baseline_spread == 0.0

    counter = {"n": 0}
    adaptive = run_mutated_adaptively(
        "m", _runner(tmp_path / "mut", [["a"]], counter), baseline, max_repeats=10
    )

    assert adaptive.attempts_made == 10
    assert adaptive.runs_saved == 0


def test_the_same_clean_result_stops_early_when_the_baseline_has_real_spread(tmp_path):
    """The other half of that asymmetry: once the baseline has genuine
    natural variation, a clean mutated arm IS provably settled early."""
    baseline = _baseline(tmp_path, [["a"], ["a"], ["a"], ["a"], ["b"]])
    assert baseline.baseline_spread > 0.0

    counter = {"n": 0}
    adaptive = run_mutated_adaptively(
        "m", _runner(tmp_path / "mut", [["a"]], counter), baseline, max_repeats=20
    )

    assert adaptive.runs_saved > 0
