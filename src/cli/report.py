"""`drifter report` (F-36's own second half), docs/SPEC.md §12/§13.

`drifter score` (`cli/score.py`) already meets F-36's literal "Done when" bar
(Gate 2's exit test: re-score a corpus with zero new execution) by aggregating
a whole runs directory as one undifferentiated group. What it does NOT do —
and what this module is for — is render docs/SPEC.md §13's actual report format
(BEHAVIOR/TASK/SAFETY, the exact shape `drifter run` prints right after a real
comparison) from a comparison that already happened, without re-running
anything. `cli/run.py`'s `render_run_result` already exists and is reused
directly, not reimplemented — this module's only job is reconstructing a
`RunResult` purely from what `run_mutation_comparison` already wrote to disk.

Zero execution, structurally, matching `cli/score.py`'s own established
standard — and genuinely, not just by this file's own direct imports:
`RunResult`/`render_run_result` live in `cli/report_format.py`, a small,
deliberately execution-free module split out of `cli/run.py` specifically so
this file never has to import `cli.run` itself (which transitively pulls in
`cli.subprocess_adapter`'s real subprocess-spawning code merely by being
imported, even though this module would never call any of it — importing it
"just for the dataclass" would have quietly broken the zero-execution
guarantee). This file imports only `cli.config`/`cli.report_format`/
`cli.stats`/`evaluate.baseline`/`evaluate.effect_size`/`policy.safety`/
`record.calibration` — none of which import `cli.subprocess_adapter`,
`replay.replay_proxy`, or any MCP client/server package, checked directly,
not assumed transitively clean. `test_report.py` asserts this by inspecting
the module's actual imports, matching `test_score.py`'s own precedent.

Real, stated scope gap, not silently glossed: `RunResult.mutation_log` (which
tool was mutated, old→after description) is genuinely NOT reconstructable from
disk — nothing in the recorded session schema persists which mutation
operator or seed produced a given `session_dir`, and neither does
`run_mutation_comparison` write that metadata anywhere itself. Rebuilt reports
always carry an empty `mutation_log` (so `render_run_result`'s own "MUTATION
LOG:" section is simply omitted, no special-casing needed) and a `operator`
field that says so explicitly, rather than guessing or requiring the caller to
re-assert something this module can't actually verify.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TextIO

from cli.config import ConfigError, DrifterConfig, PolicyConfig, load_config
from cli.report_format import RunResult, render_run_result
from cli.stats import resolve_runs_dir
from evaluate.baseline import aggregate_baseline_runs
from evaluate.effect_size import compute_behavior_effect_size
from policy.safety import evaluate_safety_across_arms
from record.calibration import Calibration, load_calibration

_OPERATOR_UNKNOWN = "(unknown — reconstructed from stored sessions, not re-verified)"


def build_report_result(
    task_id: str,
    runs_dir: Path,
    policy: PolicyConfig | None = None,
    calibration: Calibration | None = None,
) -> RunResult:
    """The pure reconstruction core — given a `runs_dir` matching
    `cli/run.py`'s own `session_dir = runs_dir / "run" / task_id` layout
    (with `baseline/`/`mutated/` subdirectories of session JSONL, exactly
    what `run_mutation_comparison` already writes), rebuilds the same
    `RunResult` shape `drifter run` produced live, purely from disk.

    Raises `ConfigError` if `task_id` was never run at all (the directory
    doesn't exist) — an actionable message, not a bare "0 sessions found"
    that could be confused with "ran, but nothing valid came of it."
    """
    session_dir = runs_dir / "run" / task_id
    if not session_dir.exists():
        raise ConfigError(
            f"no recorded `drifter run` found for task {task_id!r} under {runs_dir} "
            f"(expected {session_dir}) — run `drifter run --task-id {task_id}` first."
        )

    calibration = calibration or load_calibration()
    policy = policy or PolicyConfig()

    baseline_dir = session_dir / "baseline"
    mutated_dir = session_dir / "mutated"
    baseline_paths = sorted(baseline_dir.glob("*.jsonl")) if baseline_dir.exists() else []
    mutated_paths = sorted(mutated_dir.glob("*.jsonl")) if mutated_dir.exists() else []

    baseline_result = aggregate_baseline_runs(task_id, baseline_paths, calibration=calibration)
    mutated_result = aggregate_baseline_runs(f"{task_id}__mutated", mutated_paths, calibration=calibration)
    effect = compute_behavior_effect_size(baseline_result, mutated_result, calibration=calibration)
    safety = evaluate_safety_across_arms(session_dir, policy.destructive, policy.confirmation_required)

    return RunResult(
        task_id=task_id,
        operator=_OPERATOR_UNKNOWN,
        baseline=baseline_result,
        mutated=mutated_result,
        effect=effect,
        mutation_log=[],
        safety=safety,
    )


def run_report(
    config_path: Path | None = None,
    runs_dir: Path | None = None,
    task_id: str = "task",
    output_stream: TextIO = sys.stdout,
) -> None:
    config: DrifterConfig | None = None
    if runs_dir is None:
        config = load_config(config_path)
        runs_dir = resolve_runs_dir(config)
    policy = config.policy if config is not None else None

    result = build_report_result(task_id, runs_dir, policy=policy)
    output_stream.write(render_run_result(result))
