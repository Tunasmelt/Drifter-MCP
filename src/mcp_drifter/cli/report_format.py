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
from dataclasses import dataclass, field

from mcp_drifter.evaluate.assertions import TaskResult
from mcp_drifter.evaluate.baseline import BaselineResult
from mcp_drifter.evaluate.effect_size import EffectSizeResult, confidence_percent
from mcp_drifter.mutate.description_update import MutationLogEntry
from mcp_drifter.policy.safety import SafetyResult

# docs/PHASES.md R4: where `drifter run` persists the Behavior verdict's path of
# interest inside a run directory, so `drifter report` rebuilds the same verdict.
BEHAVIOR_PATH_FILE = "behavior_path.json"


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
    # F-24's Task axis, per arm. Defaults to a bare UNKNOWN rather than
    # `None` so every consumer has a real verdict object to render and the
    # "no oracle configured" case stays a stated result rather than a
    # missing one — the Task axis's own UNKNOWN-never-PASS invariant reads
    # more safely as a value than as an absence.
    baseline_task: TaskResult = field(
        default_factory=lambda: TaskResult(
            verdict="UNKNOWN", runs_evaluated=0, runs_passed=0, failures=(),
            reason="no assertions configured for this task",
        )
    )
    mutated_task: TaskResult = field(
        default_factory=lambda: TaskResult(
            verdict="UNKNOWN", runs_evaluated=0, runs_passed=0, failures=(),
            reason="no assertions configured for this task",
        )
    )
    # F-27: what adaptive scheduling actually did, or None when the run was
    # fixed-N. Surfaced because "why did this stop at 4 runs when I asked
    # for 20" is exactly the question a cost-conscious user asks, and an
    # unexplained short run looks like a crash rather than a saving.
    scheduling_note: str | None = None
    # docs/PHASES.md R2: which experiment directory this result describes.
    # None for a pre-R2 run directory, which has no experiment.json.
    experiment_id: str | None = None


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
    looks like. Exit code 2 (assertion failure) became reachable with F-24
    and outranks BEHAVIOR: a failed assertion is a DETERMINISTIC statement
    that the task itself broke, which is a stronger and more specific
    finding than a statistical claim that the agent's path shifted. It is
    read from the MUTATED arm only — a baseline that already fails its own
    assertions means the task or the corpus is wrong, not that the mutation
    broke anything, and reporting that as this run's headline failure would
    point the user at the wrong thing. `0` covers NO_REGRESSION and the
    honestly-uncertain INCONCLUSIVE/UNKNOWN verdicts alike — this scheme
    flags genuine problems, not "we don't know."
    """
    if result.safety.verdict == "VIOLATION":
        return 3
    if result.budget_exceeded:
        return 5
    if result.mutated_task.verdict == "FAIL" and result.baseline_task.verdict != "FAIL":
        return 2
    if result.effect.verdict == "REGRESSION":
        return 1
    return 0


def _task_lines(result: RunResult) -> list[str]:
    """docs/SPEC.md §8's Task axis (F-24), per arm.

    Both arms are shown rather than one combined verdict, for the same
    reason BEHAVIOR shows both dominant paths: "the mutation broke the
    task" is precisely a baseline-vs-mutated difference, and collapsing it
    would hide the one comparison this axis exists to make. Failure detail
    is printed for the mutated arm specifically — a baseline failure means
    the task or the corpus is wrong, a mutated-only failure is the finding.
    """
    baseline, mutated = result.baseline_task, result.mutated_task

    if baseline.verdict == "UNKNOWN" and mutated.verdict == "UNKNOWN":
        reason = baseline.reason or mutated.reason or "no oracle configured"
        return [f"TASK      UNKNOWN — {reason}"]

    lines = [
        f"TASK      baseline {baseline.verdict}"
        f" ({baseline.runs_passed}/{baseline.runs_evaluated} runs passed)",
        f"          mutated  {mutated.verdict}"
        f" ({mutated.runs_passed}/{mutated.runs_evaluated} runs passed)",
    ]
    if mutated.verdict == "FAIL":
        for failure in mutated.failures:
            for chunk in textwrap.wrap(f"{failure.kind}: {failure.detail}", width=64):
                lines.append(f"            {chunk}")

    # BASELINE TASK ADEQUACY (docs/SPEC.md §15 limitation 19) -- a separate gate
    # from the behavioural verdict, and printed with the Task axis so a
    # reader meets it before drawing a conclusion from BEHAVIOR.
    #
    # In limitation 19's experiment the report said NO_REGRESSION beside a
    # 0.88/1.00 coverage figure while the baseline agent never once answered
    # the task. That verdict was not a miscomputation -- two arms failing in
    # similar shapes genuinely show no detected degradation -- but nothing
    # made it unmistakable that there was no task capability for the
    # mutation to preserve. Only a FAIL triggers this: UNKNOWN means no
    # oracle established anything, and since UNKNOWN is the default,
    # treating it as inadequacy would fire on nearly every run.
    if baseline.verdict == "FAIL":
        lines.append("")
        lines.append("BASELINE INADEQUATE — the baseline arm did not perform the task")
        lines.append("          "
                     f"({baseline.runs_passed}/{baseline.runs_evaluated} runs passed). A baseline with no task")
        lines.append("          capability cannot support a claim that the mutation preserved")
        lines.append("          it, so the BEHAVIOR verdict above describes drift between two")
        lines.append("          failing arms and must not be read as task capability.")
    return lines


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
    if result.experiment_id:
        lines.append(f"experiment {result.experiment_id}")
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
    effect = result.effect
    # docs/PHASES.md R4: the drop in on-path share, its interval, the margin and
    # where the path of interest came from -- the evidence behind the verdict,
    # kept separate from TASK so behavioural drift is not read as task harm.
    if effect.interval is not None and effect.baseline_share is not None and effect.mutated_share is not None:
        lower, upper = effect.interval
        label = f"{confidence_percent(effect.z):.0f}% interval" if effect.z else "interval"
        lines.append(
            f"          on-path share: baseline {effect.baseline_share * 100:.0f}% → mutated "
            f"{effect.mutated_share * 100:.0f}% (drop {effect.effect_size:.2f}, {label} {lower:.2f} to {upper:.2f})"
        )
        lines.append(
            f"          regression margin {effect.margin:.2f} · path of interest: "
            f"{_path_str(effect.path_of_interest)} (from {effect.path_source})"
        )
    elif effect.deviation_rate is not None:
        lines.append(f"          deviation from baseline: {effect.deviation_rate * 100:.0f}%")

    lines.append("")
    lines.extend(_task_lines(result))
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
    # Renamed from "fidelity" per docs/SPEC.md §15 limitation 19. The number
    # measures whether an agent's calls RESOLVED against recordings -- nothing
    # about whether the response content was faithful, and nothing about
    # whether the task succeeded. In that experiment four runs scored 0.88/1.00
    # here while none answered the task correctly, and the old label invited
    # exactly that misreading.
    lines.append(f"CONFIDENCE  baseline request-match coverage {baseline_fid} ({_provenance_str(result.baseline.provenance_breakdown)})")
    lines.append(f"            mutated  request-match coverage {mutated_fid} ({_provenance_str(result.mutated.provenance_breakdown)})")
    lines.append("            (calls resolving against recordings — NOT response fidelity, NOT task success)")
    # Separate from the provenance parenthetical above, which files authored
    # bodies before looking at the tier and so hid adaptation entirely
    # (docs/SPEC.md §15 limitations 21/22).
    lines.append(
        f"REQUEST MATCH  baseline {_provenance_str(result.baseline.match_tier_breakdown)}"
        f"  ·  mutated  {_provenance_str(result.mutated.match_tier_breakdown)}"
    )
    mutated_tiers = result.mutated.match_tier_breakdown or {}
    mutated_hits = sum(mutated_tiers.values())
    if mutated_tiers.get("inverse"):
        lines.append(
            f"            {mutated_tiers['inverse']} of {mutated_hits} mutated call(s) used the mutation's "
            f"renamed arguments (inverse tier)"
        )
        others = mutated_hits - mutated_tiers["inverse"]
        if others:
            # E2's report read "4 of 8": the other four were `find_order`, which
            # the mutation never renamed. Without this line that reads as half
            # the calls failing to adapt.
            lines.append(
                f"            the other {others} matched without translation: arguments the mutation did "
                f"not rename"
            )
    elif any(entry.inverse for entry in result.mutation_log):
        lines.append(
            "            warning: mutated calls did not exercise any renamed argument; "
            "this verdict does not test the rename"
        )
    # docs/PHASES.md R3: semantic matches the multiset of argument VALUES,
    # ignoring parameter names -- unreachable against any served schema with
    # additionalProperties:false (the enforced default since the
    # parameter_rename fix), and even where reachable it can bind two
    # schema-valid calls that carry the same values in different semantic
    # roles. A real semantic hit is flagged, not presented as an ordinary
    # tier alongside exact/inverse.
    baseline_tiers = result.baseline.match_tier_breakdown or {}
    semantic_hits = baseline_tiers.get("semantic", 0) + mutated_tiers.get("semantic", 0)
    if semantic_hits:
        lines.append(
            f"            {semantic_hits} call(s) resolved via the semantic tier (exploratory, "
            f"docs/SPEC.md §15): matched on argument VALUES only, ignoring parameter names"
        )
    if result.scheduling_note:
        for chunk in textwrap.wrap(f"scheduling: {result.scheduling_note}", width=66):
            lines.append(f"            {chunk}")
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
