"""Tests that `drifter run`/`drifter report`'s real CLI entrypoint
(`cli.app.main`) actually exits with docs/SPEC.md §12's verdict-specific
code -- `test_report_format.py` only tests `compute_exit_code` in
isolation; this confirms `cli/app.py` actually calls it and raises
`SystemExit` with the result, for both commands, not just that the pure
function returns the right number.
"""

import pytest

import mcp_drifter.cli.app as app
from mcp_drifter.cli.report_format import RunResult
from mcp_drifter.evaluate.baseline import BaselineResult
from mcp_drifter.evaluate.effect_size import EffectSizeResult
from mcp_drifter.policy.safety import SafetyResult

_EMPTY_BASELINE = BaselineResult(
    task_id="t", total_runs=0, valid_runs=0, excluded_runs=[],
    dominant_path=None, variant_frequencies={}, natural_variation=None,
    baseline_spread=None, baseline_fidelity=None,
)


def _result(effect_verdict, safety_verdict="NO_VIOLATION", budget_exceeded=False):
    return RunResult(
        task_id="t", operator="description_update",
        baseline=_EMPTY_BASELINE, mutated=_EMPTY_BASELINE,
        effect=EffectSizeResult(deviation_rate=None, effect_size=None, verdict=effect_verdict),
        mutation_log=[], safety=SafetyResult(verdict=safety_verdict, findings=()),
        budget_exceeded=budget_exceeded,
    )


def test_run_command_exits_1_on_a_real_regression(monkeypatch):
    monkeypatch.setattr("mcp_drifter.cli.run.run_run", lambda **kwargs: _result("REGRESSION"))
    monkeypatch.setattr("sys.argv", ["drifter", "run", "--fixture", "f.jsonl", "--server", "s"])
    with pytest.raises(SystemExit) as exc:
        app.main()
    assert exc.value.code == 1


def test_run_command_exits_3_on_a_safety_violation(monkeypatch):
    monkeypatch.setattr("mcp_drifter.cli.run.run_run", lambda **kwargs: _result("NO_REGRESSION", safety_verdict="VIOLATION"))
    monkeypatch.setattr("sys.argv", ["drifter", "run", "--fixture", "f.jsonl", "--server", "s"])
    with pytest.raises(SystemExit) as exc:
        app.main()
    assert exc.value.code == 3


def test_run_command_exits_0_on_dry_run_with_no_result(monkeypatch):
    monkeypatch.setattr("mcp_drifter.cli.run.run_run", lambda **kwargs: None)
    monkeypatch.setattr("sys.argv", ["drifter", "run", "--fixture", "f.jsonl", "--server", "s", "--dry-run"])
    with pytest.raises(SystemExit) as exc:
        app.main()
    assert exc.value.code == 0


def test_report_command_exits_5_on_budget_exceeded(monkeypatch):
    monkeypatch.setattr("mcp_drifter.cli.report.run_report", lambda **kwargs: _result("NO_REGRESSION", budget_exceeded=True))
    monkeypatch.setattr("sys.argv", ["drifter", "report", "--task-id", "t"])
    with pytest.raises(SystemExit) as exc:
        app.main()
    assert exc.value.code == 5


def test_report_command_exits_0_on_clean_no_regression(monkeypatch):
    monkeypatch.setattr("mcp_drifter.cli.report.run_report", lambda **kwargs: _result("NO_REGRESSION"))
    monkeypatch.setattr("sys.argv", ["drifter", "report", "--task-id", "t"])
    with pytest.raises(SystemExit) as exc:
        app.main()
    assert exc.value.code == 0
