"""Tests for the Behavior verdict (F-23), as replaced under docs/PHASES.md R4.

BaselineResult instances are built directly: this module tests the comparison
given two already-aggregated arms, not the aggregation (tests/evaluate/
test_baseline.py). The pre-registered acceptance bars are checked separately by
tests/evaluate/test_r4_verdict_rule_simulation.py.
"""

import dataclasses
from pathlib import Path

import pytest

from mcp_drifter.evaluate.baseline import BaselineResult
from mcp_drifter.evaluate.effect_size import (
    PATH_SOURCE_BASELINE,
    PATH_SOURCE_CORPUS,
    compute_behavior_effect_size,
    newcombe_difference_interval,
    path_of_interest_from_sessions,
    verdict_for_counts,
    wilson_interval,
)
from mcp_drifter.record.calibration import Calibration
from mcp_drifter.record.schema import Environment, SessionStart, ToolCall

A, B = ("a", "b"), ("a", "c")


def _arm(counts: dict, total_runs: int | None = None) -> BaselineResult:
    valid = sum(counts.values())
    if valid == 0:
        return BaselineResult(
            task_id="t", total_runs=total_runs or 0, valid_runs=0, dominant_path=None, variant_frequencies={},
            natural_variation=None, baseline_spread=None, baseline_fidelity=None, excluded_runs=[],
        )
    dominant = max(counts, key=lambda p: counts[p])
    return BaselineResult(
        task_id="t", total_runs=total_runs or valid, valid_runs=valid, dominant_path=dominant,
        variant_frequencies=dict(counts), natural_variation=0.0, baseline_spread=0.0,
        baseline_fidelity=1.0, excluded_runs=[],
    )


# --- the interval ---------------------------------------------------------------


def test_wilson_matches_a_textbook_value():
    """0 of 10 at z=1.96: Wilson upper bound 0.2775 (standard reference value)."""
    lower, upper = wilson_interval(0, 10, 1.96)
    assert lower == 0.0
    assert upper == pytest.approx(0.2775, abs=5e-4)


def test_wilson_is_bounded_and_symmetric():
    assert wilson_interval(10, 10, 1.645)[1] == pytest.approx(1.0)
    lo, hi = wilson_interval(3, 10, 1.645)
    lo2, hi2 = wilson_interval(7, 10, 1.645)
    assert lo == pytest.approx(1 - hi2) and hi == pytest.approx(1 - lo2)


def test_newcombe_interval_contains_the_point_estimate():
    d, lower, upper = newcombe_difference_interval(17, 20, 9, 20, 1.645)
    assert d == pytest.approx(0.4)
    assert lower < d < upper


# --- the rule, on counts at the pre-registered N=20, margin 0.3 -----------------


def test_the_old_rules_false_alarm_shape_is_now_no_regression():
    """The case R4 measured: a stable baseline (20/20 on path) and ONE mutated
    run off path. The old zero-spread branch called this REGRESSION."""
    verdict, d, lower, upper = verdict_for_counts(20, 20, 19, 20, margin=0.3, z=1.645)
    assert verdict == "NO_REGRESSION"
    assert d == pytest.approx(0.05)
    assert upper < 0.3


def test_identical_arms_are_no_regression():
    assert verdict_for_counts(20, 20, 20, 20, 0.3, 1.645)[0] == "NO_REGRESSION"
    assert verdict_for_counts(18, 20, 18, 20, 0.3, 1.645)[0] == "NO_REGRESSION"


def test_a_large_drop_is_regression():
    verdict, d, lower, _ = verdict_for_counts(20, 20, 4, 20, 0.3, 1.645)
    assert verdict == "REGRESSION"
    assert d == pytest.approx(0.8) and lower > 0.0


def test_a_drop_just_under_the_margin_is_inconclusive_not_forced():
    """d = 0.25: provably above 0, but neither provably below the margin nor at it."""
    verdict, d, lower, upper = verdict_for_counts(20, 20, 15, 20, 0.3, 1.645)
    assert d == pytest.approx(0.25) and lower > 0.0 and upper >= 0.3
    assert verdict == "INCONCLUSIVE"


def test_a_drop_exactly_at_the_margin_with_a_positive_lower_bound_is_regression():
    """The pre-registered rule is `d >= margin`, inclusive. (A first draft of this
    test expected INCONCLUSIVE at d = 0.30; that contradicted the rule as written.)"""
    verdict, d, lower, _ = verdict_for_counts(20, 20, 14, 20, 0.3, 1.645)
    assert d == pytest.approx(0.3) and lower > 0.0
    assert verdict == "REGRESSION"


def test_a_significant_but_small_drop_is_not_regression():
    """lower > 0 alone is not enough; the drop must also reach the margin."""
    verdict, d, lower, upper = verdict_for_counts(200, 200, 170, 200, 0.3, 1.645)
    assert d == pytest.approx(0.15) and lower > 0.0
    assert verdict == "NO_REGRESSION" if upper < 0.3 else verdict == "INCONCLUSIVE"
    assert verdict != "REGRESSION"


def test_three_runs_cannot_establish_no_regression():
    """Why R4 moved the default to 20: at 3 runs, even identical arms leave an
    interval wider than the margin."""
    assert verdict_for_counts(3, 3, 3, 3, 0.3, 1.645)[0] == "INCONCLUSIVE"


def test_margin_and_z_come_from_calibration():
    calibration = Calibration()
    calibration.behavior.margin = 0.5
    result = compute_behavior_effect_size(_arm({A: 20}), _arm({A: 12, B: 8}), calibration=calibration)
    assert result.margin == 0.5
    assert result.verdict == "INCONCLUSIVE"  # d=0.4 is below this margin, but not provably


# --- compute_behavior_effect_size ------------------------------------------------


def test_the_result_carries_the_evidence_behind_the_verdict():
    result = compute_behavior_effect_size(_arm({A: 20}), _arm({A: 4, B: 16}), path_of_interest=A, path_source=PATH_SOURCE_CORPUS)
    assert result.verdict == "REGRESSION"
    assert result.baseline_share == 1.0 and result.mutated_share == pytest.approx(0.2)
    assert result.effect_size == pytest.approx(0.8)
    assert result.deviation_rate == pytest.approx(0.8)
    assert result.path_of_interest == A and result.path_source == "corpus"
    assert result.interval[0] > 0.0


def test_without_a_corpus_path_the_baseline_path_is_used_and_labelled_biased():
    result = compute_behavior_effect_size(_arm({A: 20}), _arm({A: 20}))
    assert result.path_of_interest == A
    assert result.path_source == PATH_SOURCE_BASELINE == "baseline arm (biased)"


def test_the_corpus_path_is_not_re_picked_from_the_arms():
    """The path of interest stays the corpus's even when the arms favour another
    path. Before amendment A this scored 0% vs 0% and returned NO_REGRESSION;
    that silent pass is exactly what the amendment removed."""
    result = compute_behavior_effect_size(_arm({B: 20}), _arm({B: 20}), path_of_interest=A, path_source=PATH_SOURCE_CORPUS)
    assert result.path_of_interest == A
    assert result.baseline_share == 0.0
    assert result.verdict == "UNKNOWN"


# --- amendment A: a corpus path this task does not usually take ----------------


def test_a_corpus_path_the_baseline_never_takes_is_unknown_not_no_regression():
    """The planted-break shape that motivated amendment A: the corpus path is a
    longer trajectory than the task. Both arms score 0% on it; without the guard
    this read INCONCLUSIVE at small N and NO_REGRESSION at N=20."""
    full_session = ("list_directory", "search_files", "read_text_file")
    result = compute_behavior_effect_size(
        _arm({("list_directory",): 20}), _arm({(): 20}),
        path_of_interest=full_session, path_source=PATH_SOURCE_CORPUS,
    )
    assert result.verdict == "UNKNOWN"
    assert "0/20 baseline runs" in result.reason and "Record a corpus of this task" in result.reason
    assert result.interval is None and result.effect_size is None
    assert result.path_of_interest == full_session and result.baseline_share == 0.0


def test_the_guard_uses_a_majority_of_baseline_runs():
    below = compute_behavior_effect_size(_arm({A: 9, B: 11}), _arm({A: 9, B: 11}), path_of_interest=A, path_source=PATH_SOURCE_CORPUS)
    at = compute_behavior_effect_size(_arm({A: 10, B: 10}), _arm({A: 10, B: 10}), path_of_interest=A, path_source=PATH_SOURCE_CORPUS)
    assert below.verdict == "UNKNOWN"
    assert at.verdict != "UNKNOWN"


def test_the_guard_threshold_comes_from_calibration():
    calibration = Calibration()
    calibration.behavior.min_baseline_share = 0.9
    result = compute_behavior_effect_size(_arm({A: 17, B: 3}), _arm({A: 17, B: 3}), calibration=calibration,
                                          path_of_interest=A, path_source=PATH_SOURCE_CORPUS)
    assert result.verdict == "UNKNOWN" and "90%" in result.reason


# --- minimum-evidence gate (docs/SPEC.md §15 limitation 16), unchanged ------------


def test_verdict_is_unknown_when_either_arm_has_no_data():
    assert compute_behavior_effect_size(_arm({}), _arm({A: 20})).verdict == "UNKNOWN"
    assert compute_behavior_effect_size(_arm({A: 20}), _arm({})).verdict == "UNKNOWN"


def test_a_single_surviving_baseline_run_is_unknown_not_a_confident_regression():
    """The exact Gate 4 blind-agent shape limitation 16 recorded."""
    baseline = dataclasses.replace(_arm({A: 1}), total_runs=10)
    mutated = dataclasses.replace(_arm({B: 2}), total_runs=10)
    result = compute_behavior_effect_size(baseline, mutated)
    assert result.verdict == "UNKNOWN"
    assert result.effect_size is None and result.deviation_rate is None and result.interval is None


def test_the_unknown_verdict_states_why():
    result = compute_behavior_effect_size(_arm({A: 1}), _arm({B: 1}))
    assert result.verdict == "UNKNOWN"
    assert result.reason is not None and "1" in result.reason


def test_a_thin_arm_is_gated_even_when_the_other_arm_is_healthy():
    healthy, thin = _arm({A: 20}), _arm({B: 1})
    assert compute_behavior_effect_size(healthy, thin).verdict == "UNKNOWN"
    assert compute_behavior_effect_size(thin, healthy).verdict == "UNKNOWN"


def test_min_valid_runs_is_calibration_driven():
    calibration = Calibration()
    calibration.min_valid_runs = 1
    result = compute_behavior_effect_size(_arm({A: 1}), _arm({B: 1}), calibration=calibration)
    assert result.verdict != "UNKNOWN"


# --- path of interest from the corpus ---------------------------------------------


def _session(dir_path: Path, sid: str, tools: list[str], server: str = "srv", provenance: str = "real") -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    lines = [SessionStart(session_id=sid, seq=0, started_at="2026-09-15T00:00:00Z",
                          environment=Environment(tool_manifest_hash="h"), raw_frame_offset=0).model_dump_json()]
    for i, name in enumerate(tools, start=1):
        lines.append(ToolCall(session_id=sid, seq=i, timestamp="2026-09-15T00:00:01Z", server=server, tool_name=name,
                              arguments={}, result_shape={"type": "object"}, is_error=False, duration_ms=1.0,
                              fault=False, result_provenance=provenance, raw_frame_offset=i).model_dump_json())
    path = dir_path / f"{sid}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_path_of_interest_is_the_most_frequent_corpus_path(tmp_path):
    paths = [_session(tmp_path, "s0", ["a", "c"]), _session(tmp_path, "s1", ["a", "b"]), _session(tmp_path, "s2", ["a", "b"])]
    assert path_of_interest_from_sessions(paths, "srv") == ("a", "b")


def test_path_of_interest_ties_break_by_first_appearance(tmp_path):
    paths = [_session(tmp_path, "s0", ["x"]), _session(tmp_path, "s1", ["y"])]
    assert path_of_interest_from_sessions(paths, "srv") == ("x",)


def test_path_of_interest_ignores_other_servers_and_non_real_calls(tmp_path):
    paths = [
        _session(tmp_path, "s0", ["other"], server="elsewhere"),
        _session(tmp_path, "s1", ["authored"], provenance="authored_fixture"),
        _session(tmp_path, "s2", ["a"]),
    ]
    assert path_of_interest_from_sessions(paths, "srv") == ("a",)
    assert path_of_interest_from_sessions([paths[0]], "srv") is None
