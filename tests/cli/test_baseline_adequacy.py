"""BASELINE TASK ADEQUACY is a separate gate from the behavioural verdict.

docs/SPEC.md §15 limitation 19: a treatment arm reported
`BEHAVIOR NO_REGRESSION` beside a 0.88/1.00 coverage figure, on four runs
where the agent never answered the task correctly. The verdict itself was
not a miscomputation -- if both arms fail in similar shapes, "no detected
behavioural degradation" is the correct output of the rule. The defect was
PRESENTATION: nothing made it unmistakable that the baseline had no task
capability for the mutation to preserve.

So the rule this file asserts: a baseline that cannot perform the task
cannot support a claim that the mutation preserved task capability. When an
oracle establishes the baseline failed, the report must say so prominently
and must not let `NO_REGRESSION` stand unqualified.
"""

from __future__ import annotations

from mcp_drifter.cli.report_format import RunResult, TaskResult, render_run_result
from mcp_drifter.evaluate.baseline import BaselineResult
from mcp_drifter.evaluate.effect_size import EffectSizeResult
from mcp_drifter.policy.safety import SafetyResult


def _arm(valid: int, fidelity: float) -> BaselineResult:
    return BaselineResult(
        task_id="t", total_runs=valid, valid_runs=valid, excluded_runs=[],
        dominant_path=("a",), variant_frequencies={("a",): valid},
        natural_variation=0.0, baseline_spread=0.0, baseline_fidelity=fidelity,
        provenance_breakdown={"exact": valid, "inverse": 0, "semantic": 0,
                              "synthetic": 0, "synthetic_miss": 0, "unresolved": 0},
    )


def _result(baseline_task: TaskResult, mutated_task: TaskResult) -> RunResult:
    return RunResult(
        task_id="t", operator="description_update",
        baseline=_arm(4, 0.88), mutated=_arm(3, 1.00),
        effect=EffectSizeResult(0.0, 0.0, "NO_REGRESSION"),
        safety=SafetyResult(verdict="NO_VIOLATION", findings=[]),
        mutation_log=[],
        baseline_task=baseline_task, mutated_task=mutated_task,
    )


def test_a_failing_baseline_is_called_out_prominently(tmp_path):
    """The limitation-19 shape: high coverage, NO_REGRESSION, and a
    baseline that could not do the task."""
    out = render_run_result(_result(
        baseline_task=TaskResult(verdict="FAIL", runs_passed=0, runs_evaluated=4, failures=[]),
        mutated_task=TaskResult(verdict="FAIL", runs_passed=0, runs_evaluated=3, failures=[]),
    ))

    assert "BASELINE INADEQUATE" in out
    # And the behavioural verdict must not stand unqualified beside it.
    lowered = out.lower()
    assert "cannot support" in lowered or "no task capability" in lowered


def test_an_adequate_baseline_produces_no_warning():
    out = render_run_result(_result(
        baseline_task=TaskResult(verdict="PASS", runs_passed=4, runs_evaluated=4, failures=[]),
        mutated_task=TaskResult(verdict="PASS", runs_passed=3, runs_evaluated=3, failures=[]),
    ))

    assert "BASELINE INADEQUATE" not in out


def test_an_unknown_baseline_is_not_reported_as_inadequate():
    """UNKNOWN means no oracle established anything. Claiming inadequacy
    from it would be the same overreach in the other direction -- and
    UNKNOWN is the default, so this would fire on almost every run.
    """
    out = render_run_result(_result(
        baseline_task=TaskResult(verdict="UNKNOWN", runs_passed=0, runs_evaluated=0, failures=[], reason="no oracle"),
        mutated_task=TaskResult(verdict="UNKNOWN", runs_passed=0, runs_evaluated=0, failures=[], reason="no oracle"),
    ))

    assert "BASELINE INADEQUATE" not in out
