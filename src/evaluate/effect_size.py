"""Behavior effect-size scoring (F-23), docs/SPEC.md §8.

`effect_size = (deviation_rate − natural_variation) / baseline_spread`.
Scheduled as "partial" in Gate 2 (docs/PHASES.md: "baseline and scoring
only, no mutation yet") — `natural_variation`/`baseline_spread` were
computable from Gate 2 onward, but `deviation_rate` needs a real
mutation arm to compare against, which didn't exist until Gate 3's
`description_update`/`tool_addition` (F-16/F-17). This module is where
that gap closes, not a new, unplanned feature.

`deviation_rate`: the fraction of the MUTATED arm's valid runs whose
tool-call path differs from the BASELINE arm's own `dominant_path` —
computed from `BaselineResult.variant_frequencies` (an aggregate count
already available), not by threading individual per-run paths through
a new API. Comparing against the baseline's dominant path, not the
mutated arm's own, is the point: the question is "did behavior drift
away from what was normal," not "is the mutated arm internally
consistent with itself."

Verdict defaults to UNKNOWN when either arm has no data at all
(`has_data is False`) — same non-negotiable discipline as the task-
assertion default (CLAUDE.md, docs/SPEC.md §3): a verdict computed from
zero real observations is not a real verdict, and reporting NO_
REGRESSION by default would be exactly the "lies calmly" failure mode
CLAUDE.md warns about, just relocated to a different axis.

`baseline_spread == 0.0` (a perfectly stable baseline — the *best* real
outcome per BaselineResult's own docs, not degenerate data) makes the
formula's denominator zero. Decided explicitly, not left undefined:
if the mutated arm ALSO deviates at exactly the baseline's own rate
(both effectively zero drift), effect_size is reported as `0.0`
(genuinely no signal) rather than raising or silently returning None.
Any OTHER deviation against a zero-variance baseline has no finite
ratio to report — `effect_size` is `None` (undefined magnitude, not
"zero" and not "unknown data"), but the verdict is still resolved
directly from the sign of the comparison: a real baseline with zero
natural variation that the mutated arm deviates from at all is exactly
the shape of a genuine regression signal, calibration thresholds
notwithstanding (there's no ratio to compare against them with).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from evaluate.baseline import BaselineResult
from record.calibration import Calibration, load_calibration

Verdict = Literal["NO_REGRESSION", "INCONCLUSIVE", "REGRESSION", "UNKNOWN"]


@dataclass(frozen=True)
class EffectSizeResult:
    """`deviation_rate`/`effect_size` are `None` exactly when `verdict`
    is `"UNKNOWN"` OR when `baseline_spread == 0.0` produced an
    undefined-magnitude comparison (see module docstring) — check
    `verdict` first, don't infer "no signal" from a `None` `effect_size`
    alone, since a real `REGRESSION` verdict can carry `effect_size=None`
    in the zero-baseline-spread case.
    """

    deviation_rate: float | None
    effect_size: float | None
    verdict: Verdict
    # Why the verdict is UNKNOWN, in a form a cold reader can act on --
    # `None` for every other verdict. Added for docs/SPEC.md §15
    # limitation 16's second half ("the report gives no visible signal to
    # a cold reader that its verdict rests on mostly-excluded runs"): a
    # bare UNKNOWN reproduces that same problem in a quieter form, since
    # "not enough surviving runs" and "no data at all" are different
    # situations calling for different fixes from the user.
    reason: str | None = None


def compute_behavior_effect_size(
    baseline: BaselineResult,
    mutated: BaselineResult,
    calibration: Calibration | None = None,
) -> EffectSizeResult:
    """docs/SPEC.md §8's Behavior axis, with §15 limitation 16's
    minimum-evidence gate applied FIRST: below `calibration.min_valid_runs`
    valid runs in either arm, no verdict is computed at all.

    That gate is not a conservatism tweak — it closes a structural defect
    found by the real Gate 4 blind-agent test. With one surviving valid
    baseline run, `natural_variation` is 0.0 (a lone run trivially matches
    its own dominant path) and `baseline_spread` is 0.0 (pstdev of one
    sample). Both are artifacts of n=1, not measurements, and together they
    made the zero-spread branch below report a confident `REGRESSION` for
    ANY nonzero deviation in the mutated arm. That is exactly the
    "BEHAVIOR REGRESSION at 100% deviation" the real test saw while 85% of
    its runs had been silently excluded for low fidelity. Same failure
    shape as the "verdict defaults to UNKNOWN, never PASS" invariant
    CLAUDE.md names, pointed the other way: a confident FAILURE claim on
    evidence that cannot support any claim.
    """
    calibration = calibration or load_calibration()

    if not baseline.has_data or not mutated.has_data:
        return EffectSizeResult(
            deviation_rate=None,
            effect_size=None,
            verdict="UNKNOWN",
            reason=(
                f"no valid runs to compare "
                f"(baseline {baseline.valid_runs}/{baseline.total_runs}, "
                f"mutated {mutated.valid_runs}/{mutated.total_runs} survived exclusion)"
            ),
        )

    minimum = calibration.min_valid_runs
    if baseline.valid_runs < minimum or mutated.valid_runs < minimum:
        return EffectSizeResult(
            deviation_rate=None,
            effect_size=None,
            verdict="UNKNOWN",
            reason=(
                f"too few valid runs to measure natural variation: "
                f"baseline {baseline.valid_runs}/{baseline.total_runs}, "
                f"mutated {mutated.valid_runs}/{mutated.total_runs} survived exclusion, "
                f"below the {minimum} per arm calibration.min_valid_runs requires. "
                f"Record more sessions for this task, or check the exclusion "
                f"reasons below — a thin arm usually means replay fidelity, "
                f"not agent behavior."
            ),
        )

    matching = mutated.variant_frequencies.get(baseline.dominant_path, 0)
    deviation_rate = 1.0 - (matching / mutated.valid_runs)

    if baseline.baseline_spread == 0.0:
        if deviation_rate == baseline.natural_variation:
            effect_size: float | None = 0.0
            verdict: Verdict = "NO_REGRESSION"
        else:
            effect_size = None
            verdict = "REGRESSION" if deviation_rate > baseline.natural_variation else "NO_REGRESSION"
        return EffectSizeResult(deviation_rate=deviation_rate, effect_size=effect_size, verdict=verdict)

    effect_size = (deviation_rate - baseline.natural_variation) / baseline.baseline_spread
    if effect_size < calibration.effect_size.inconclusive:
        verdict = "NO_REGRESSION"
    elif effect_size < calibration.effect_size.regression:
        verdict = "INCONCLUSIVE"
    else:
        verdict = "REGRESSION"

    return EffectSizeResult(deviation_rate=deviation_rate, effect_size=effect_size, verdict=verdict)
