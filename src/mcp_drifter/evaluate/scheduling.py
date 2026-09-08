"""Adaptive repeat scheduling (F-27), docs/SPEC.md §8.

Spend real agent runs only where the answer is still in doubt. F-27's own
"Done when" is the bar: fewer total runs than fixed-N, with **the same
final verdicts**.

**Reframed against two premises that no longer hold**, in the same way
F-31/F-32 were (docs/CHANGELOG.md), rather than built to the letter of a
spec written before the code existed:

1. *"1 run × all mutations (screen) → 5 → 20."* That is a budget-allocation
   strategy ACROSS many mutations. `drifter run` compares exactly one
   operator against one baseline (Gate 3's deliberate scope, unchanged);
   there is no set of mutations to triage between. The same PRINCIPLE —
   stop spending once the answer is settled — applies within a single
   comparison as sequential early stopping, and that is what this builds.
2. *"1 run (screen)"* is now impossible to act on at all. DEC-027's
   minimum-evidence gate (`calibration.min_valid_runs`, 3) means a
   one-run arm yields UNKNOWN by construction, so a 1-run screening stage
   could only ever report "don't know" — it could never flag or clear
   anything. `calibration.yaml`'s `mutation.repeats.screen: 1` and
   `confirm: 5` predate that gate and are stale as a three-stage ladder;
   `resolve: 20` survives intact as this module's ceiling.

**The stopping rule is a proof, not a peek.** The obvious implementation —
recompute the verdict after each run and stop when it "looks decided" — is
optional stopping, which inflates false positives precisely because
stopping is correlated with noise favouring the current answer. Rather
than adopt that and caveat it, this stops only when it is CERTAIN: after
each run, the best and worst cases still reachable by every remaining run
are both evaluated, and it stops only if they yield the same verdict. When
no remaining outcome can change the answer, stopping cannot bias it. The
saving is smaller than an aggressive heuristic's would be, and it is free
of the statistical objection.

The bound, stated explicitly since its correctness is the whole guarantee.
With `m` valid mutated runs so far of which `matching` matched the
baseline's dominant path, and `r` attempts remaining, the final matching
ratio is bounded by:

    best  (lowest deviation):  (matching + r) / (m + r)   — every remaining run valid and matching
    worst (highest deviation):  matching      / (m + r)   — every remaining run valid and deviating

A remaining run that is EXCLUDED rather than valid leaves the ratio at
`matching / m`, which lies between those two bounds whenever
`matching <= m` (always true) — so excluded runs need no separate case.

**A real asymmetry this produces, found by running it rather than by
reasoning, and correct rather than a defect.** Savings are large when
confirming a REGRESSION and can be zero when confirming its absence — but
only when `baseline_spread` is exactly 0. A perfectly stable baseline
makes the verdict rule infinitely sharp (`compute_behavior_effect_size`'s
zero-spread branch: any deviation at all outranks a natural variation of
zero), so a single deviating run among the remaining ones would flip
NO_REGRESSION to REGRESSION, and no number of clean runs can rule that
out in advance. Measured on synthetic arms at a ceiling of 20: a clear
regression settles in 3 runs (17 saved); a clean result against a
zero-spread baseline uses all 20 (0 saved); the same clean result against
a baseline with real spread settles in 8 (12 saved); a genuinely mixed
case settles in 15 (5 saved). The zero-saving case is the scheduler
correctly refusing to claim certainty it does not have — softening it
would mean changing the verdict rule, which is a separate decision and
emphatically not something to do as a side effect of a scheduling feature.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from mcp_drifter.evaluate.baseline import (
    BaselineResult,
    ExcludedRun,
    _run_once_failed_reason,
    aggregate_baseline_runs,
)
from mcp_drifter.evaluate.effect_size import verdict_for_deviation
from mcp_drifter.record.calibration import Calibration, load_calibration


@dataclass(frozen=True)
class SchedulingDecision:
    """Whether to spend another real agent run, and why — the reason is
    surfaced in `drifter run`'s output, since "why did this stop at 4 runs
    when I asked for 20" is exactly the question a cost-conscious user
    asks."""

    should_continue: bool
    reason: str


def _baseline_settles_nothing(baseline: BaselineResult, calibration: Calibration) -> str | None:
    """A reason to stop before the mutated arm starts at all, or None.

    Falls directly out of DEC-027's minimum-evidence gate and is the
    single largest saving here: if the BASELINE arm didn't clear
    `min_valid_runs`, the comparison is UNKNOWN no matter what the mutated
    arm does, so every mutated run is guaranteed waste. Before that gate
    existed there was no way to know this in advance.
    """
    if not baseline.has_data:
        return (
            f"baseline produced no valid runs ({baseline.valid_runs}/{baseline.total_runs}), so the "
            f"verdict is UNKNOWN regardless of the mutated arm — skipping it entirely"
        )
    if baseline.valid_runs < calibration.min_valid_runs:
        return (
            f"baseline has only {baseline.valid_runs}/{baseline.total_runs} valid runs, below the "
            f"{calibration.min_valid_runs} calibration.min_valid_runs requires — the verdict is "
            f"UNKNOWN regardless of the mutated arm, so no mutated run can change it"
        )
    return None


def next_decision(
    baseline: BaselineResult,
    mutated_so_far: BaselineResult,
    attempts_remaining: int,
    calibration: Calibration,
) -> SchedulingDecision:
    """Whether another mutated-arm run could still change the verdict.

    `mutated_so_far` is the aggregation over the runs completed already —
    the same `aggregate_baseline_runs` output the final scoring uses, so
    this reasons over exactly the data the verdict will be computed from.
    """
    if attempts_remaining <= 0:
        return SchedulingDecision(False, "reached the configured repeat ceiling")

    baseline_reason = _baseline_settles_nothing(baseline, calibration)
    if baseline_reason is not None:
        return SchedulingDecision(False, baseline_reason)

    m = mutated_so_far.valid_runs
    if m < calibration.min_valid_runs:
        return SchedulingDecision(
            True,
            f"mutated arm has {m} valid run(s), below the {calibration.min_valid_runs} needed for any verdict",
        )

    matching = mutated_so_far.variant_frequencies.get(baseline.dominant_path, 0)
    r = attempts_remaining
    # See the module docstring for why these two bracket every reachable
    # outcome, excluded runs included.
    deviation_if_all_match = 1.0 - (matching + r) / (m + r)
    deviation_if_none_match = 1.0 - matching / (m + r)

    best_verdict, _ = verdict_for_deviation(
        deviation_if_all_match, baseline.natural_variation, baseline.baseline_spread, calibration
    )
    worst_verdict, _ = verdict_for_deviation(
        deviation_if_none_match, baseline.natural_variation, baseline.baseline_spread, calibration
    )

    if best_verdict == worst_verdict:
        return SchedulingDecision(
            False,
            f"verdict is already settled at {best_verdict} after {m} valid run(s) — no outcome of the "
            f"remaining {r} could change it",
        )

    return SchedulingDecision(
        True,
        f"still undecided after {m} valid run(s) (remaining runs could yield "
        f"{best_verdict} or {worst_verdict})",
    )


@dataclass(frozen=True)
class AdaptiveRunResult:
    """A mutated arm run adaptively: the same `BaselineResult` a fixed-N
    run produces, plus what scheduling actually did — `attempts_made`
    against `attempts_allowed` is the saving, and `stop_reason` is why."""

    result: BaselineResult
    attempts_made: int
    attempts_allowed: int
    stop_reason: str

    @property
    def runs_saved(self) -> int:
        return self.attempts_allowed - self.attempts_made


def run_mutated_adaptively(
    task_id: str,
    run_once: Callable[[], Path],
    baseline: BaselineResult,
    max_repeats: int,
    calibration: Calibration | None = None,
) -> AdaptiveRunResult:
    """`evaluate.baseline.run_baseline`'s loop, stopping as soon as the
    verdict is provably settled (`next_decision`).

    Deliberately applies to the MUTATED arm only. The baseline arm cannot
    be shortened the same way: it exists to ESTIMATE `natural_variation`
    and `baseline_spread`, and those estimates are the very thing the
    stopping rule reasons against — there is no fixed reference to prove
    anything against while the reference itself is still being measured.
    Adaptive scheduling of the baseline would need a genuinely different
    (precision-of-estimate) criterion; not attempted here rather than
    approximated.

    Exclusion handling matches `run_baseline` exactly, including recording
    a `run_once` failure as a `pre_excluded` entry rather than aborting the
    arm — an adaptive loop that crashed on one flaky repeat would be
    strictly worse than the fixed loop it replaces.
    """
    calibration = calibration or load_calibration()

    session_paths: list[Path] = []
    pre_excluded: list[ExcludedRun] = []
    attempts = 0
    # Checked BEFORE the first run: a baseline that already fails the
    # minimum-evidence gate makes every mutated run guaranteed waste.
    decision = next_decision(baseline, aggregate_baseline_runs(task_id, [], calibration=calibration),
                             max_repeats, calibration)

    while decision.should_continue:
        try:
            session_paths.append(run_once())
        except Exception as exc:
            pre_excluded.append(ExcludedRun(session_id=None, path=None, reason=_run_once_failed_reason(exc)))
        attempts += 1
        so_far = aggregate_baseline_runs(
            task_id, session_paths, calibration=calibration, pre_excluded=pre_excluded
        )
        decision = next_decision(baseline, so_far, max_repeats - attempts, calibration)

    return AdaptiveRunResult(
        result=aggregate_baseline_runs(
            task_id, session_paths, calibration=calibration, pre_excluded=pre_excluded
        ),
        attempts_made=attempts,
        attempts_allowed=max_repeats,
        stop_reason=decision.reason,
    )
