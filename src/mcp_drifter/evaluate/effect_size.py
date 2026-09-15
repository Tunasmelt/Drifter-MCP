"""Behavior verdict (F-23), docs/SPEC.md §8 — replaced under docs/PHASES.md R4.

**Why the old rule was replaced.** It scored `(deviation_rate -
natural_variation) / baseline_spread` and, when the baseline happened to show
zero spread, reported REGRESSION for ANY deviation. Simulated against unchanged
agents (both arms drawn from the same distribution), it raised a false
REGRESSION 12-26% of the time whenever the agent was not deterministic, and
more runs did not fix it (p=0.95: 24% at N=10, 23% at N=20). Recorded in
docs/PHASES.md R4 with the pre-registration this module implements.

**The rule (pre-registered, approved margin 0.3, N=20).**

- Path of interest `P`: the most frequent whole-session tool path in the
  CORPUS for this server, chosen before either arm runs and persisted by
  `drifter run`. Choosing it from the baseline arm biases the comparison (the
  arm is then scored on the path it was selected to favour); that is only the
  fallback for run directories without the persisted choice, and it is
  labelled as biased.
- `s_b`, `s_m`: the share of each arm's VALID runs whose path equals `P`;
  `d = s_b - s_m`, the drop.
- Interval: Newcombe's hybrid score interval for a difference of independent
  proportions, built from Wilson score intervals at `z` (standard, published
  technique; implemented from first principles, not from any other project).
- REGRESSION if `lower > 0` and `d >= margin`; NO_REGRESSION if
  `upper < margin`; INCONCLUSIVE otherwise.
- UNKNOWN when either arm has fewer than `calibration.min_valid_runs` valid
  runs (docs/SPEC.md §15 limitation 16) — unchanged, and checked first.

Field names `deviation_rate` / `effect_size` are kept for compatibility with
existing callers: `deviation_rate` is now the mutated arm's share of runs NOT
on `P`, and `effect_size` is `d`, the drop in on-path share.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from mcp_drifter.evaluate.baseline import BaselineResult
from mcp_drifter.record.calibration import Calibration, load_calibration
from mcp_drifter.record.fingerprint import diff_environments
from mcp_drifter.record.reader import read_session
from mcp_drifter.record.schema import ToolCall

Verdict = Literal["NO_REGRESSION", "INCONCLUSIVE", "REGRESSION", "UNKNOWN"]

PATH_SOURCE_CORPUS = "corpus"
PATH_SOURCE_BASELINE = "baseline arm (biased)"


@dataclass(frozen=True)
class EffectSizeResult:
    """`deviation_rate` / `effect_size` / `interval` are `None` exactly when
    `verdict` is UNKNOWN; `reason` is set only then."""

    deviation_rate: float | None
    effect_size: float | None
    verdict: Verdict
    reason: str | None = None
    interval: tuple[float, float] | None = None
    margin: float | None = None
    baseline_share: float | None = None
    mutated_share: float | None = None
    path_of_interest: tuple[str, ...] | None = None
    path_source: str | None = None
    z: float | None = None


def wilson_interval(successes: int, n: int, z: float) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion."""
    if n <= 0:
        return 0.0, 1.0
    p = successes / n
    denominator = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1.0 - p) / n + z * z / (4 * n * n)) / denominator
    return max(0.0, centre - half), min(1.0, centre + half)


def newcombe_difference_interval(
    x_baseline: int, n_baseline: int, x_mutated: int, n_mutated: int, z: float
) -> tuple[float, float, float]:
    """`(d, lower, upper)` for `d = p_baseline - p_mutated`, Newcombe's hybrid
    score method (his method 10) from the two Wilson intervals."""
    p_b, p_m = x_baseline / n_baseline, x_mutated / n_mutated
    l_b, u_b = wilson_interval(x_baseline, n_baseline, z)
    l_m, u_m = wilson_interval(x_mutated, n_mutated, z)
    d = p_b - p_m
    lower = d - math.sqrt((p_b - l_b) ** 2 + (u_m - p_m) ** 2)
    upper = d + math.sqrt((u_b - p_b) ** 2 + (p_m - l_m) ** 2)
    return d, lower, upper


def verdict_for_counts(
    x_baseline: int, n_baseline: int, x_mutated: int, n_mutated: int, margin: float, z: float
) -> tuple[Verdict, float, float, float]:
    """The rule alone, on counts: `(verdict, d, lower, upper)`."""
    d, lower, upper = newcombe_difference_interval(x_baseline, n_baseline, x_mutated, n_mutated, z)
    if lower > 0.0 and d >= margin:
        return "REGRESSION", d, lower, upper
    if upper < margin:
        return "NO_REGRESSION", d, lower, upper
    return "INCONCLUSIVE", d, lower, upper


def path_of_interest_from_sessions(session_paths: Sequence[Path], server: str | None = None) -> tuple[str, ...] | None:
    """The most frequent whole-session tool path among `session_paths`, ties
    broken by first appearance. Only REAL calls count (the corpus is what the
    agent did against the live server). `None` if no session made a call."""
    counts: dict[tuple[str, ...], int] = {}
    for path in session_paths:
        tool_path = tuple(
            r.tool_name
            for r in read_session(path)
            if isinstance(r, ToolCall) and (server is None or r.server == server) and r.result_provenance == "real"
        )
        if tool_path:
            counts[tool_path] = counts.get(tool_path, 0) + 1
    if not counts:
        return None
    best = max(counts.values())
    return next(p for p, c in counts.items() if c == best)


def confidence_percent(z: float) -> float:
    """Two-sided coverage of a +/- z interval, for labelling the report."""
    return math.erf(z / math.sqrt(2.0)) * 100.0


def compute_behavior_effect_size(
    baseline: BaselineResult,
    mutated: BaselineResult,
    calibration: Calibration | None = None,
    path_of_interest: tuple[str, ...] | None = None,
    path_source: str | None = None,
) -> EffectSizeResult:
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
                f"too few valid runs to compare behavior: "
                f"baseline {baseline.valid_runs}/{baseline.total_runs}, "
                f"mutated {mutated.valid_runs}/{mutated.total_runs} survived exclusion, "
                f"below the {minimum} per arm calibration.min_valid_runs requires. "
                f"Record more sessions for this task, or check the exclusion "
                f"reasons below — a thin arm usually means replay fidelity, "
                f"not agent behavior."
            ),
        )

    # docs/PHASES.md R2: a behavior difference between arms that ran in
    # different environments cannot be attributed to the mutation. The tool
    # manifest is exempt: changing it is what the mutation does.
    if baseline.reference_environment is not None and mutated.reference_environment is not None:
        environment_diffs = diff_environments(
            mutated.reference_environment, baseline.reference_environment, compare_manifest=False
        )
        if environment_diffs:
            return EffectSizeResult(
                deviation_rate=None,
                effect_size=None,
                verdict="UNKNOWN",
                reason=(
                    "the arms ran in different environments, so a behavior difference cannot be attributed "
                    "to the mutation (mutated vs baseline): " + "; ".join(environment_diffs)
                ),
            )

    if path_of_interest is None:
        path_of_interest, path_source = baseline.dominant_path, PATH_SOURCE_BASELINE
    source = path_source or PATH_SOURCE_CORPUS

    margin, z = calibration.behavior.margin, calibration.behavior.z
    x_b = baseline.variant_frequencies.get(path_of_interest, 0)
    x_m = mutated.variant_frequencies.get(path_of_interest, 0)

    # Amendment A to the pre-registration (docs/PHASES.md R4), added after the
    # first implementation run: a corpus recorded for a DIFFERENT task (or a
    # longer version of this one) yields a path this task never takes. Both
    # arms then score 0% on it, and a planted break read INCONCLUSIVE at 3 runs
    # and would read NO_REGRESSION at 20. Below this share the path does not
    # describe the task, so no verdict is claimed.
    minimum_share = calibration.behavior.min_baseline_share
    if x_b / baseline.valid_runs < minimum_share:
        return EffectSizeResult(
            deviation_rate=None,
            effect_size=None,
            verdict="UNKNOWN",
            reason=(
                f"the path of interest ({' → '.join(path_of_interest) or '(no calls)'}, from {source}) was taken "
                f"by only {x_b}/{baseline.valid_runs} baseline runs, below the "
                f"{minimum_share:.0%} calibration.behavior.min_baseline_share requires — it is not this "
                f"task's usual path, so a drop on it says nothing. Record a corpus of this task."
            ),
            baseline_share=x_b / baseline.valid_runs,
            path_of_interest=path_of_interest,
            path_source=source,
        )
    verdict, d, lower, upper = verdict_for_counts(x_b, baseline.valid_runs, x_m, mutated.valid_runs, margin, z)
    return EffectSizeResult(
        deviation_rate=1.0 - x_m / mutated.valid_runs,
        effect_size=d,
        verdict=verdict,
        interval=(lower, upper),
        margin=margin,
        baseline_share=x_b / baseline.valid_runs,
        mutated_share=x_m / mutated.valid_runs,
        path_of_interest=path_of_interest,
        path_source=source,
        z=z,
    )
