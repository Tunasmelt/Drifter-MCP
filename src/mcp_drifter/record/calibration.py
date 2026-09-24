"""Calibration register loader (docs/SPEC.md §9).

Every constant here is an engineering default, not a research finding —
CLAUDE.md's non-negotiable invariant: don't treat these as authoritative,
and don't add new unlabeled constants elsewhere. `calibration.yaml` at the
repo root is the single place they live; this module's field defaults
exist only as a fallback for callers with no file at their cwd (e.g. a
package installed and run from an arbitrary directory before `drifter
init` has written a project-local copy), not as a second source of truth.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict


class EffectSize(BaseModel):
    """STALE, unread since docs/PHASES.md R4 replaced the effect-size rule. Kept
    so an existing user calibration.yaml that still sets it keeps loading."""

    model_config = ConfigDict(extra="allow")
    inconclusive: float = 1.0
    regression: float = 2.0


class Behavior(BaseModel):
    """The R4 Behavior verdict (evaluate/effect_size.py). Guesses in the
    calibration-register sense: chosen by simulation (docs/PHASES.md R4), not
    derived from real agents.

    `margin`: the drop in on-path share that counts as a practical regression.
    0.2 was rejected in simulation (5% false alarms at p=0.8, N=10; 10 runs could
    never establish NO_REGRESSION). `z`: 1.645, a 90% two-sided interval, i.e. a
    one-sided 5% bound on each decision."""

    model_config = ConfigDict(extra="allow")
    margin: float = 0.3
    z: float = 1.645
    # Amendment A (docs/PHASES.md R4): below this share of baseline runs on the
    # corpus path, the path is not the task's usual path and the verdict is
    # UNKNOWN. 0.5 = "the majority of baseline runs", a guess with that meaning.
    min_baseline_share: float = 0.5


class Segmentation(BaseModel):
    model_config = ConfigDict(extra="allow")
    idle_gap_seconds: float = 30
    heuristic_confidence: float = 0.6


class Baseline(BaseModel):
    model_config = ConfigDict(extra="allow")
    # 20 per arm under docs/PHASES.md R4: the pre-registered acceptance bars
    # (false REGRESSION <= 5%, power >= 90% for a 0.6 drop) were set at N=20.
    repeats: int = 20


class MutationRepeats(BaseModel):
    model_config = ConfigDict(extra="allow")
    screen: int = 1
    confirm: int = 5
    resolve: int = 20


class Mutation(BaseModel):
    model_config = ConfigDict(extra="allow")
    repeats: MutationRepeats = MutationRepeats()


class Doctor(BaseModel):
    model_config = ConfigDict(extra="allow")
    # Not in docs/SPEC.md §9's original table — added here per CLAUDE.md's
    # invariant that new invented constants belong in this file, not
    # hardcoded (same reasoning as segmentation.heuristic_confidence
    # above). How long `drifter doctor` waits for a configured server to
    # complete the MCP initialize handshake before reporting it
    # unreachable; a guess, not derived from anything.
    connectivity_timeout_seconds: float = 10


class Plateau(BaseModel):
    """When `drifter coverage --curve` calls a corpus-growth curve flat.

    Both constants are guesses with stated reasoning and NEITHER has been
    derived from a real single-task corpus yet -- the experiment that would
    do that (20-50 recordings of one narrow task, docs/SPEC.md §15
    limitation 16) has not been run. They live here rather than in
    `replay/coverage_curve.py` per CLAUDE.md's rule that an invented
    constant belongs in this file, and specifically so they can be tuned
    against that experiment's own output without a code change.

    Expect `gain_per_session` in particular to need revisiting. Coverage
    approaches an asymptote, so gain-per-session shrinks as the corpus
    grows even while genuinely still climbing; a fixed absolute threshold
    that reads correctly at 5 sessions may fire spuriously at 50. If the
    real curve declares a plateau while its own numbers are still visibly
    rising, this constant is the first thing to suspect -- not the curve.
    """

    model_config = ConfigDict(extra="allow")
    # Mean coverage gained per added session, below which a point counts as
    # flat. Deliberately a stated threshold rather than a significance
    # test: n is small, the effect being looked for is large (a 0.20-0.43
    # gap to the floor), and a test would imply more statistical machinery
    # than the data supports.
    gain_per_session: float = 0.02
    # How many consecutive non-exhaustive points must all be under that
    # threshold. Two small gains is weak evidence; three is where "still
    # climbing slowly" stops being the simpler explanation.
    window: int = 3


class Mine(BaseModel):
    """Thresholds for `drifter tasks mine` (F-28-F-30). Every one is a guess with
    stated reasoning; none has been derived from a real multi-week corpus, because
    none exists yet (docs/SPEC.md section 15 limitation 16 is why mining was deferred).
    They live here, not in `mine/`, per CLAUDE.md's rule that an invented constant
    belongs in this file -- so they can be tuned against real data without a code
    change.
    """

    model_config = ConfigDict(extra="allow")
    # Fewest trajectories a workflow must appear in to be proposed. An
    # ABSOLUTE count, not a fraction: a fraction of a 5-trajectory corpus is
    # noise, and 2 is the smallest number at which "recurs" means anything.
    min_support: int = 2
    # Shortest workflow worth proposing as a task. A single tool call is a
    # call, not a workflow; 2 is the smallest sequence with an order in it.
    min_length: int = 2
    # Longest pattern searched for. Also what keeps PrefixSpan tractable:
    # gapped subsequences of a long trajectory grow exponentially, so an
    # unbounded search on an exploring agent's 50-call session would not
    # terminate. 6 is a guess at "longer than a human would call one task".
    max_length: int = 6
    # How many candidates to write. Reviewing is the user's cost; a long
    # ranked tail is noise, and the ranking puts the strongest first.
    max_candidates: int = 10


class Calibration(BaseModel):
    model_config = ConfigDict(extra="allow")
    semantic_weight: float = 0.8
    fidelity_floor: float = 0.70
    fidelity_flag_threshold: float = 0.90
    # Minimum VALID (post-exclusion) runs required in EACH arm before the
    # Behavior axis will report anything but UNKNOWN -- docs/SPEC.md §15
    # limitation 16's minimum-evidence gate. Not in docs/SPEC.md §9's
    # original constant table; added here per CLAUDE.md's rule that a new
    # invented constant belongs in this file rather than hardcoded.
    #
    # 3 is a guess with a stated floor under it, not a research value: at
    # 1 valid run, `baseline_spread` is 0.0 as an ARTIFACT of n=1 (pstdev
    # of one sample) and `natural_variation` is 0.0 because a lone run
    # trivially matches its own dominant path -- neither is a measurement,
    # and their combination makes any nonzero mutated-arm deviation report
    # a confident REGRESSION. At 2 they are real but cannot distinguish
    # "genuinely stable" from "we only looked twice." 3 is the smallest n
    # where a zero spread is weak evidence rather than no evidence.
    # Re-derive against real corpus data before defending this number.
    min_valid_runs: int = 3
    plateau: Plateau = Plateau()
    mine: Mine = Mine()
    effect_size: EffectSize = EffectSize()
    behavior: Behavior = Behavior()
    segmentation: Segmentation = Segmentation()
    baseline: Baseline = Baseline()
    mutation: Mutation = Mutation()
    doctor: Doctor = Doctor()


def load_calibration(path: Path | None = None) -> Calibration:
    """Loads calibration.yaml, or this module's field defaults if absent.

    `path` defaults to `calibration.yaml` relative to the current working
    directory — the same convention `drifter.yaml` will follow once the
    config loader exists (F-33).
    """
    path = path or Path("calibration.yaml")
    if not path.exists():
        return Calibration()
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return Calibration.model_validate(data)
