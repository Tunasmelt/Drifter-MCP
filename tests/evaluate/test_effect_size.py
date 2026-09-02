"""Tests for behavior effect-size scoring (F-23), docs/SPEC.md §8.

BaselineResult instances built directly (not via run_baseline/
aggregate_baseline_runs) -- this module tests the pure comparison
logic given two already-computed results, not the aggregation that
produces them (already covered in tests/evaluate/test_baseline.py).
"""

import pytest

from evaluate.baseline import BaselineResult
from evaluate.effect_size import compute_behavior_effect_size
from record.calibration import Calibration


def _result(
    dominant_path,
    variant_frequencies,
    natural_variation,
    baseline_spread,
    valid_runs,
    has_data=True,
):
    return BaselineResult(
        task_id="t",
        total_runs=valid_runs,
        valid_runs=valid_runs if has_data else 0,
        dominant_path=dominant_path if has_data else None,
        variant_frequencies=variant_frequencies if has_data else {},
        natural_variation=natural_variation if has_data else None,
        baseline_spread=baseline_spread if has_data else None,
        baseline_fidelity=1.0 if has_data else None,
        excluded_runs=[],
    )


def test_identical_baseline_and_mutated_paths_is_no_regression():
    baseline = _result(("a", "b"), {("a", "b"): 10}, natural_variation=0.0, baseline_spread=0.0, valid_runs=10)
    mutated = _result(("a", "b"), {("a", "b"): 10}, natural_variation=0.0, baseline_spread=0.0, valid_runs=10)
    result = compute_behavior_effect_size(baseline, mutated)
    assert result.verdict == "NO_REGRESSION"
    assert result.deviation_rate == 0.0
    assert result.effect_size == 0.0


def test_total_deviation_against_a_zero_variance_baseline_is_regression():
    """baseline_spread == 0.0 (a perfectly stable baseline) makes the
    formula's denominator zero -- decided explicitly (module docstring):
    any real deviation from a rock-solid baseline is a genuine
    regression signal, reported with effect_size=None (undefined
    magnitude, not "zero" and not "no data")."""
    baseline = _result(("a", "b"), {("a", "b"): 10}, natural_variation=0.0, baseline_spread=0.0, valid_runs=10)
    mutated = _result(("a", "c"), {("a", "c"): 10}, natural_variation=0.0, baseline_spread=0.0, valid_runs=10)
    result = compute_behavior_effect_size(baseline, mutated)
    assert result.verdict == "REGRESSION"
    assert result.deviation_rate == 1.0
    assert result.effect_size is None  # undefined magnitude, not "no signal"


def test_verdict_is_unknown_when_baseline_has_no_data():
    baseline = _result(None, {}, None, None, valid_runs=0, has_data=False)
    mutated = _result(("a",), {("a",): 5}, natural_variation=0.0, baseline_spread=0.0, valid_runs=5)
    result = compute_behavior_effect_size(baseline, mutated)
    assert result.verdict == "UNKNOWN"
    assert result.deviation_rate is None
    assert result.effect_size is None


def test_verdict_is_unknown_when_mutated_has_no_data():
    baseline = _result(("a",), {("a",): 5}, natural_variation=0.0, baseline_spread=0.0, valid_runs=5)
    mutated = _result(None, {}, None, None, valid_runs=0, has_data=False)
    result = compute_behavior_effect_size(baseline, mutated)
    assert result.verdict == "UNKNOWN"


def test_moderate_deviation_within_natural_variation_is_no_regression():
    # Baseline itself naturally wobbles 20% of the time (spread 0.4);
    # mutated arm deviates at the same 20% rate -- no real signal.
    baseline = _result(
        ("a", "b"), {("a", "b"): 8, ("a", "c"): 2}, natural_variation=0.2, baseline_spread=0.4, valid_runs=10
    )
    mutated = _result(
        ("a", "b"), {("a", "b"): 8, ("a", "c"): 2}, natural_variation=0.2, baseline_spread=0.4, valid_runs=10
    )
    result = compute_behavior_effect_size(baseline, mutated)
    assert result.deviation_rate == pytest.approx(0.2)
    assert result.effect_size == pytest.approx(0.0, abs=1e-9)
    assert result.verdict == "NO_REGRESSION"


def test_large_deviation_beyond_natural_variation_is_regression():
    baseline = _result(
        ("a", "b"), {("a", "b"): 8, ("a", "c"): 2}, natural_variation=0.2, baseline_spread=0.2, valid_runs=10
    )
    # Mutated arm deviates from baseline's dominant_path in 9/10 runs --
    # far beyond baseline's own 20% natural wobble.
    mutated = _result(("a", "b"), {("a", "b"): 1, ("a", "d"): 9}, natural_variation=0.0, baseline_spread=0.0, valid_runs=10)
    result = compute_behavior_effect_size(baseline, mutated)
    assert result.deviation_rate == pytest.approx(0.9)
    calibration = Calibration()
    expected = (0.9 - 0.2) / 0.2
    assert result.effect_size == pytest.approx(expected)
    assert expected > calibration.effect_size.regression
    assert result.verdict == "REGRESSION"


def test_verdict_thresholds_use_calibration_constants_not_hardcoded():
    calibration = Calibration()
    calibration.effect_size.inconclusive = 0.5
    calibration.effect_size.regression = 0.6

    baseline = _result(("a",), {("a",): 10}, natural_variation=0.0, baseline_spread=0.2, valid_runs=10)
    mutated = _result(("a",), {("a",): 4, ("b",): 6}, natural_variation=0.0, baseline_spread=0.0, valid_runs=10)
    # deviation_rate = 0.6, effect_size = (0.6-0.0)/0.2 = 3.0 with default
    # thresholds (regression) -- but with the custom low thresholds
    # above, still comfortably above regression=0.6.
    result = compute_behavior_effect_size(baseline, mutated, calibration=calibration)
    assert result.effect_size == pytest.approx(3.0)
    assert result.verdict == "REGRESSION"
