"""Tests for behavior effect-size scoring (F-23), docs/SPEC.md §8.

BaselineResult instances built directly (not via run_baseline/
aggregate_baseline_runs) -- this module tests the pure comparison
logic given two already-computed results, not the aggregation that
produces them (already covered in tests/evaluate/test_baseline.py).
"""

import dataclasses

import pytest

from mcp_drifter.evaluate.baseline import BaselineResult
from mcp_drifter.evaluate.effect_size import compute_behavior_effect_size
from mcp_drifter.record.calibration import Calibration


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


# --- minimum-evidence gate (docs/SPEC.md §15 limitation 16) -----------------
# Written and confirmed to FAIL against the pre-gate implementation (which
# returned a confident REGRESSION for the 1-valid-baseline-run shape below)
# BEFORE the gate was added, per CLAUDE.md's required procedure.


def test_a_single_surviving_baseline_run_is_unknown_not_a_confident_regression():
    """The exact shape docs/SPEC.md §15 limitation 16 recorded from the real
    blind-agent Gate 4 test: 10 repeats per arm, 9/10 baseline and 8/10
    mutated runs excluded for low fidelity, leaving 1 valid baseline and 2
    valid mutated runs -- which the report then presented as a confident
    "BEHAVIOR REGRESSION at 100% deviation from baseline."

    That verdict was structurally guaranteed, not bad luck: with one valid
    baseline run, `natural_variation` is 0.0 (a single run always matches
    its own dominant path) and `baseline_spread` is 0.0 (pstdev of one
    sample), so the zero-spread branch reports REGRESSION for ANY nonzero
    deviation in the mutated arm. One run cannot measure natural variation,
    so there is nothing for a deviation to be "beyond."
    """
    baseline = _result(("a", "b"), {("a", "b"): 1}, natural_variation=0.0, baseline_spread=0.0, valid_runs=1)
    baseline = dataclasses.replace(baseline, total_runs=10)
    mutated = _result(("a", "c"), {("a", "c"): 2}, natural_variation=0.0, baseline_spread=0.0, valid_runs=2)
    mutated = dataclasses.replace(mutated, total_runs=10)

    result = compute_behavior_effect_size(baseline, mutated)

    assert result.verdict == "UNKNOWN"
    assert result.effect_size is None
    assert result.deviation_rate is None


def test_two_valid_runs_are_still_too_few_to_measure_natural_variation():
    """Two identical runs give baseline_spread == 0.0 legitimately, but
    still cannot distinguish "genuinely stable" from "we only looked
    twice" -- below the calibrated minimum, the honest answer is UNKNOWN."""
    baseline = _result(("a",), {("a",): 2}, natural_variation=0.0, baseline_spread=0.0, valid_runs=2)
    mutated = _result(("b",), {("b",): 2}, natural_variation=0.0, baseline_spread=0.0, valid_runs=2)

    result = compute_behavior_effect_size(baseline, mutated)
    assert result.verdict == "UNKNOWN"


def test_the_unknown_verdict_states_why_rather_than_being_bare():
    """Limitation 16's other half: the report gave "no visible signal to a
    cold reader that its verdict rests on mostly-excluded runs." An UNKNOWN
    that doesn't say why reproduces exactly that problem in a quieter
    form."""
    baseline = _result(("a",), {("a",): 1}, natural_variation=0.0, baseline_spread=0.0, valid_runs=1)
    mutated = _result(("b",), {("b",): 1}, natural_variation=0.0, baseline_spread=0.0, valid_runs=1)

    result = compute_behavior_effect_size(baseline, mutated)
    assert result.verdict == "UNKNOWN"
    assert result.reason is not None
    assert "1" in result.reason  # names the actual surviving-run count


def test_a_thin_arm_is_gated_even_when_the_other_arm_is_healthy():
    """Only ONE arm needs to be too thin for the comparison between them to
    be unsupportable -- the gate is on both, not on their average."""
    healthy = _result(("a",), {("a",): 10}, natural_variation=0.0, baseline_spread=0.1, valid_runs=10)
    thin = _result(("b",), {("b",): 1}, natural_variation=0.0, baseline_spread=0.0, valid_runs=1)

    assert compute_behavior_effect_size(healthy, thin).verdict == "UNKNOWN"
    assert compute_behavior_effect_size(thin, healthy).verdict == "UNKNOWN"


def test_at_the_minimum_a_real_verdict_is_still_computed_not_gated_away():
    """The gate must not be so conservative it swallows legitimate small-N
    results: exactly at min_valid_runs, a real verdict is still produced."""
    calibration = Calibration()
    n = calibration.min_valid_runs
    baseline = _result(("a",), {("a",): n}, natural_variation=0.0, baseline_spread=0.0, valid_runs=n)
    mutated = _result(("b",), {("b",): n}, natural_variation=0.0, baseline_spread=0.0, valid_runs=n)

    result = compute_behavior_effect_size(baseline, mutated, calibration=calibration)
    assert result.verdict != "UNKNOWN"
    assert result.deviation_rate == pytest.approx(1.0)


def test_min_valid_runs_is_calibration_driven_not_hardcoded():
    calibration = Calibration()
    calibration.min_valid_runs = 1  # a caller who genuinely wants N=1 results
    baseline = _result(("a",), {("a",): 1}, natural_variation=0.0, baseline_spread=0.0, valid_runs=1)
    mutated = _result(("b",), {("b",): 1}, natural_variation=0.0, baseline_spread=0.0, valid_runs=1)

    result = compute_behavior_effect_size(baseline, mutated, calibration=calibration)
    assert result.verdict == "REGRESSION"  # the old, ungated behavior, now opt-in
