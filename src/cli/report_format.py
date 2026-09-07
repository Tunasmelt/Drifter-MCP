"""`drifter run`'s report shape and rendering (docs/SPEC.md §13), split out
from `cli/run.py` so it can be shared with `cli/report.py` (F-36) without
either pulling in the other's concerns.

`RunResult`/`render_run_result` describe and render a comparison's result —
they say nothing about HOW that comparison happened. `cli/run.py` builds a
`RunResult` by actually running real agent subprocesses; `cli/report.py`
(F-36's "drifter report") builds an equivalent `RunResult` purely by reading
already-recorded sessions off disk, zero new execution. Keeping this module
free of any execution-capable import (`cli.subprocess_adapter`,
`replay.replay_proxy`, `mcp.client`/`mcp.server`, anything spawning a process
or opening a connection) is the whole point of the split: `cli/report.py`
needs `RunResult`/`render_run_result` to be genuinely, structurally
execution-free to import, not just conceptually so — importing `cli.run`
directly would transitively pull in `cli.subprocess_adapter`'s real
subprocess-spawning code merely by importing the module, even though
`cli/report.py` would never call any of it. `test_report.py` asserts this
module's own imports stay clean by inspecting its AST directly, matching
`cli/score.py`'s established precedent for the same class of guarantee.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass

from evaluate.baseline import BaselineResult
from evaluate.effect_size import EffectSizeResult
from mutate.description_update import MutationLogEntry
from policy.safety import SafetyResult


@dataclass(frozen=True)
class RunResult:
    task_id: str
    operator: str
    baseline: BaselineResult
    mutated: BaselineResult
    effect: EffectSizeResult
    mutation_log: list[MutationLogEntry]
    safety: SafetyResult
    budget_exceeded: bool = False


_BUDGET_EXCEEDED_REASON_MARKER = "budget exhausted"


def budget_exceeded_from_excluded_runs(result: RunResult) -> bool:
    """Best-effort reconstruction of `RunResult.budget_exceeded` from
    already-recorded `ExcludedRun.reason` text alone — used by
    `cli/report.py`, which (unlike `cli/run.py`, which has the live
    `policy.budget.BudgetTracker` on hand) only ever sees what
    `evaluate.baseline.run_baseline` already wrote into
    `BaselineResult.excluded_runs`. `policy/budget.py`'s own
    `BudgetExceededError` messages both contain the literal substring
    checked here (docs/SPEC.md §12's exit code 5) — this is a real,
    stated limitation, not a structured flag: a differently-worded
    failure that happens to contain this substring would be
    misclassified, though nothing else in this codebase raises with it.
    """
    marker = _BUDGET_EXCEEDED_REASON_MARKER
    return any(
        marker in excluded.reason
        for run in (result.baseline, result.mutated)
        for excluded in run.excluded_runs
    )


def compute_exit_code(result: RunResult) -> int:
    """docs/SPEC.md §12's verdict-specific exit codes, computed from an
    already-built `RunResult` — shared by `drifter run` and `drifter
    report`, since both produce the same `RunResult` shape. Precedence
    when more than one condition holds: SAFETY (3) outranks everything
    else (the highest-value finding class per docs/SPEC.md §8, never
    gated by the other axes); a spent BUDGET (5) outranks BEHAVIOR (1)
    because a budget-exhausted run's remaining repeats were skipped, not
    completed, so its behavioral verdict may rest on less data than it
    looks like. Exit code 2 (assertion failure) is defined here but can
    never actually fire yet: TASK is unconditionally UNKNOWN today (no
    assertion engine is wired into `RunResult` — see
    `render_run_result`), so there's no assertion-failure signal to
    read. `0` covers NO_REGRESSION and the honestly-uncertain
    INCONCLUSIVE/UNKNOWN behavior verdicts alike — this scheme flags
    genuine problems, not "we don't know."
    """
    if result.safety.verdict == "VIOLATION":
        return 3
    if result.budget_exceeded:
        return 5
    if result.effect.verdict == "REGRESSION":
        return 1
    return 0


def _path_str(path: tuple[str, ...] | None) -> str:
    if path is None:
        return "N/A"
    return " → ".join(path) if path else "(no tool calls)"


def _provenance_str(breakdown: dict[str, int] | None) -> str:
    """Renders `BaselineResult.provenance_breakdown` as docs/SPEC.md §13's
    CONFIDENCE line's own parenthetical (`exact 71% · inverse 23% ·
    synthetic 6%` in that section's illustrative text) -- all three replay
    tiers are real buckets as of F-12/F-40. A zero-count bucket is omitted
    rather than printing a misleading "unresolved 0%" on every clean run."""
    if breakdown is None:
        return "N/A (no valid runs)"
    total = sum(breakdown.values())
    if total == 0:
        return "N/A (no calls made)"
    parts = [f"{label} {count / total * 100:.0f}%" for label, count in breakdown.items() if count > 0]
    return " · ".join(parts)


def render_run_result(result: RunResult) -> str:
    lines: list[str] = []
    lines.append(f"DRIFTER RUN — {result.task_id}  (mutation: {result.operator})")
    lines.append("")
    lines.append(f"BASELINE  {result.baseline.valid_runs}/{result.baseline.total_runs} valid runs")
    lines.append(f"          dominant path: {_path_str(result.baseline.dominant_path)}")
    lines.append(f"MUTATED   {result.mutated.valid_runs}/{result.mutated.total_runs} valid runs")
    lines.append(f"          dominant path: {_path_str(result.mutated.dominant_path)}")
    lines.append("")

    lines.append(f"BEHAVIOR  {result.effect.verdict}")
    # docs/SPEC.md §15 limitation 16: an UNKNOWN with no stated cause is the
    # same "no visible signal to a cold reader" failure in a quieter form.
    # Wrapped rather than printed as one long line -- this is the sentence
    # a user most needs to actually read.
    if result.effect.reason:
        for chunk in textwrap.wrap(result.effect.reason, width=68):
            lines.append(f"          {chunk}")
    if result.effect.deviation_rate is not None:
        lines.append(f"          deviation from baseline: {result.effect.deviation_rate * 100:.0f}%")
    if result.effect.effect_size is not None:
        lines.append(f"          effect size: {result.effect.effect_size:.2f}×")
    elif result.effect.verdict != "UNKNOWN":
        lines.append("          effect size: undefined (baseline had zero natural variation)")

    lines.append("")
    lines.append("TASK      UNKNOWN — no oracle configured")
    lines.append("")

    # F-25: reported even when Behavior shows NO_REGRESSION (docs/SPEC.md §8's
    # own text) -- this is the highest-value finding class, so it's never
    # folded into or gated by the Behavior/Task verdicts above.
    lines.append(f"SAFETY    {result.safety.verdict.replace('_', ' ')}")
    for finding in result.safety.findings:
        lines.append(f"          {finding.detail}")
    lines.append("")

    # docs/SPEC.md §13's CONFIDENCE section, the replay-provenance half of
    # it only (the fidelity_floor/calibration footnote there is separate,
    # unbuilt scope -- see docs/PHASES.md's "v1 remaining scope"). Reported
    # per arm, not merged, since baseline and mutated can genuinely differ
    # (e.g. a mutation that adds a synthetic tool only affects the mutated
    # arm's breakdown).
    baseline_fid = "N/A" if result.baseline.baseline_fidelity is None else f"{result.baseline.baseline_fidelity:.2f}"
    mutated_fid = "N/A" if result.mutated.baseline_fidelity is None else f"{result.mutated.baseline_fidelity:.2f}"
    lines.append(f"CONFIDENCE  baseline fidelity {baseline_fid} ({_provenance_str(result.baseline.provenance_breakdown)})")
    lines.append(f"            mutated  fidelity {mutated_fid} ({_provenance_str(result.mutated.provenance_breakdown)})")
    lines.append("")

    for run, label in ((result.baseline, "baseline"), (result.mutated, "mutated")):
        if run.excluded_runs:
            lines.append(f"{label.upper()} EXCLUSIONS:")
            for excluded in run.excluded_runs:
                who = excluded.session_id or (str(excluded.path) if excluded.path is not None else "<no session>")
                lines.append(f"  {who}: {excluded.reason}")
            lines.append("")

    if result.mutation_log:
        lines.append("MUTATION LOG:")
        for entry in result.mutation_log:
            lines.append(f"  {entry.tool_name} ({entry.operator}), seed={entry.seed}, inverse={entry.inverse}")

    return "\n".join(lines) + "\n"
