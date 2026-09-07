"""Tests for `cli/report_format.py`'s exit-code wiring (docs/SPEC.md §12).

`compute_exit_code` is the shared piece `drifter run` and `drifter report`
both call once they have a `RunResult` in hand — these tests build minimal
`RunResult`s directly (not through a real comparison) since the logic under
test is pure verdict-reading, not comparison machinery.
"""

import dataclasses

from evaluate.baseline import BaselineResult, ExcludedRun
from evaluate.effect_size import EffectSizeResult
from cli.report_format import RunResult, budget_exceeded_from_excluded_runs, compute_exit_code
from evaluate.assertions import AssertionFailure, TaskResult
from policy.safety import SafetyResult

_EMPTY_BASELINE = BaselineResult(
    task_id="t", total_runs=0, valid_runs=0, excluded_runs=(),
    dominant_path=None, variant_frequencies={}, natural_variation=None,
    baseline_spread=None, baseline_fidelity=None,
)


def _result(*, effect_verdict, safety_verdict="NO_VIOLATION", budget_exceeded=False, baseline=None, mutated=None):
    return RunResult(
        task_id="t",
        operator="description_update",
        baseline=baseline or _EMPTY_BASELINE,
        mutated=mutated or _EMPTY_BASELINE,
        effect=EffectSizeResult(deviation_rate=None, effect_size=None, verdict=effect_verdict),
        mutation_log=[],
        safety=SafetyResult(verdict=safety_verdict, findings=()),
        budget_exceeded=budget_exceeded,
    )


def test_no_regression_and_no_violation_exits_clean():
    assert compute_exit_code(_result(effect_verdict="NO_REGRESSION")) == 0


def test_inconclusive_and_unknown_are_not_flagged_as_regressions():
    """Honest uncertainty (INCONCLUSIVE/UNKNOWN) is not the same as a
    confirmed problem -- only REGRESSION triggers exit code 1."""
    assert compute_exit_code(_result(effect_verdict="INCONCLUSIVE")) == 0
    assert compute_exit_code(_result(effect_verdict="UNKNOWN")) == 0


def test_behavior_regression_exits_1():
    assert compute_exit_code(_result(effect_verdict="REGRESSION")) == 1


def test_safety_violation_exits_3():
    assert compute_exit_code(_result(effect_verdict="NO_REGRESSION", safety_verdict="VIOLATION")) == 3


def test_budget_exceeded_exits_5():
    assert compute_exit_code(_result(effect_verdict="NO_REGRESSION", budget_exceeded=True)) == 5


def test_safety_violation_outranks_a_behavior_regression():
    result = _result(effect_verdict="REGRESSION", safety_verdict="VIOLATION")
    assert compute_exit_code(result) == 3


def test_safety_violation_outranks_budget_exceeded():
    result = _result(effect_verdict="NO_REGRESSION", safety_verdict="VIOLATION", budget_exceeded=True)
    assert compute_exit_code(result) == 3


def test_budget_exceeded_outranks_a_behavior_regression():
    """A budget-exhausted run's remaining repeats were skipped, not
    completed -- its REGRESSION verdict may rest on less data than it
    looks like, so the budget signal takes precedence."""
    result = _result(effect_verdict="REGRESSION", budget_exceeded=True)
    assert compute_exit_code(result) == 5


def test_budget_exceeded_from_excluded_runs_detects_the_known_marker():
    baseline = dataclasses.replace(
        _EMPTY_BASELINE,
        excluded_runs=(ExcludedRun(session_id=None, path=None, reason="run_once raised: tool-call budget exhausted: 4/4 spent"),),
    )
    result = _result(effect_verdict="NO_REGRESSION", baseline=baseline)
    assert budget_exceeded_from_excluded_runs(result) is True


def test_budget_exceeded_from_excluded_runs_is_false_for_unrelated_exclusions():
    baseline = dataclasses.replace(
        _EMPTY_BASELINE,
        excluded_runs=(ExcludedRun(session_id=None, path=None, reason="tool_manifest_hash is null"),),
    )
    result = _result(effect_verdict="NO_REGRESSION", baseline=baseline)
    assert budget_exceeded_from_excluded_runs(result) is False


# --- F-24: exit code 2 (assertion failure) is reachable now -----------------


def _task(verdict, failures=()):
    return TaskResult(
        verdict=verdict, runs_evaluated=3,
        runs_passed=3 if verdict == "PASS" else 0,
        failures=failures,
    )


def _with_tasks(baseline_verdict, mutated_verdict, effect_verdict="NO_REGRESSION", **kw):
    base = _result(effect_verdict=effect_verdict, **kw)
    return dataclasses.replace(
        base, baseline_task=_task(baseline_verdict), mutated_task=_task(mutated_verdict)
    )


def test_a_mutation_breaking_the_task_exits_2():
    """The case exit code 2 exists for: the baseline satisfied its
    assertions and the mutated arm didn't, so the mutation broke the task."""
    assert compute_exit_code(_with_tasks("PASS", "FAIL")) == 2


def test_a_task_failing_in_both_arms_does_not_exit_2():
    """A baseline that already fails its own assertions means the task or
    the corpus is wrong, not that the mutation broke anything -- reporting
    that as this run's headline failure would point the user at the wrong
    thing."""
    assert compute_exit_code(_with_tasks("FAIL", "FAIL")) == 0


def test_assertion_failure_outranks_a_behavior_regression():
    """A failed assertion is a DETERMINISTIC statement that the task broke;
    a behavior regression is a statistical claim that the path shifted."""
    assert compute_exit_code(_with_tasks("PASS", "FAIL", effect_verdict="REGRESSION")) == 2


def test_safety_still_outranks_an_assertion_failure():
    result = _with_tasks("PASS", "FAIL", safety_verdict="VIOLATION")
    assert compute_exit_code(result) == 3


def test_budget_exceeded_still_outranks_an_assertion_failure():
    result = _with_tasks("PASS", "FAIL", budget_exceeded=True)
    assert compute_exit_code(result) == 5


def test_a_passing_task_alongside_a_regression_still_exits_1():
    assert compute_exit_code(_with_tasks("PASS", "PASS", effect_verdict="REGRESSION")) == 1


def test_unknown_task_verdicts_leave_the_behavior_verdict_in_charge():
    """The default state for every task without an authored oracle -- must
    not change any pre-F-24 exit code."""
    assert compute_exit_code(_with_tasks("UNKNOWN", "UNKNOWN", effect_verdict="REGRESSION")) == 1
    assert compute_exit_code(_with_tasks("UNKNOWN", "UNKNOWN")) == 0
