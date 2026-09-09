"""Projected replay coverage as a function of corpus size.

`coverage.py` answers "how good is this corpus?" for one corpus. This
answers the question docs/SPEC.md §15 limitation 16 actually turns on, which
is about the DERIVATIVE: as more sessions of the same task are recorded,
does projected coverage climb toward `calibration.fidelity_floor`, or does
it plateau below it?

Those outcomes mean opposite things. Climbing means corpus-based replay
works and the remaining cost is recording effort. A plateau means
exact/semantic-tier replay cannot be made adequate for an exploratory agent
by ANY amount of recording -- DEC-027's premise (replay adequacy is a
property of the corpus) would be true but unreachable, and the honest
response would be to change the architecture or narrow the claim, not to
record more.

The measurement so far, from DEC-027(c) against the project's own 5-session
corpus: 10.0% / 17.1% / 22.2% / 26.7% at sizes 2/3/4/5. Rising, real, and
visibly decelerating far short of 0.70. Too few points to call a plateau,
which is exactly why this module exists.

**Three choices that keep the curve honest.**

Subsets are SAMPLED, not prefixes. Taking "the first n sessions" would make
the curve an artifact of recording order -- if the first two sessions happen
to overlap heavily the curve starts high and flattens, telling you about
your file naming rather than your corpus.

Sampling is SEEDED. A reported curve has to be reproducible, and two runs of
the experiment (before and after recording more) have to be comparable.

Every point carries its SPREAD, not just a mean. At small n the variance
between subsets is large; a bare mean would imply a precision the
measurement does not have. This is the same discipline the minimum-evidence
gate enforces for verdicts -- do not report a confident central value when
the evidence cannot support one.

Zero execution: this reads recordings only, like `cli/score.py`. No agent
runs, no API calls, nothing spawned.
"""

from __future__ import annotations

import itertools
import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from mcp_drifter.record.calibration import Plateau
from mcp_drifter.record.reader import read_session
from mcp_drifter.record.schema import ToolCall
from mcp_drifter.replay.coverage import estimate_coverage

# Below this many sessions, leave-one-out cross-validation has nothing to
# hold out against -- see `coverage.estimate_coverage`'s own single-session
# handling, which reports un-estimable rather than a misleading 0%.
MIN_CROSS_VALIDATABLE = 2

# The two plateau constants now live in `calibration.yaml` under
# `plateau:` (CLAUDE.md: an invented constant belongs there, not
# hardcoded), specifically so they can be tuned against the real
# single-task corpus this instrument exists to measure, without a code
# change. Imported here only as the fallback when no calibration is
# passed. See `record/calibration.Plateau` for the reasoning and for why
# `gain_per_session` is the first thing to suspect if a real curve
# declares a plateau while still visibly rising.


@dataclass(frozen=True)
class CurvePoint:
    """Projected coverage at one corpus size, over sampled subsets."""

    corpus_size: int
    mean_coverage: float
    min_coverage: float
    max_coverage: float
    subsets_sampled: int
    # Mean coverage gained per session added since the previous point.
    # `None` at the first point, which has nothing to compare against --
    # never 0.0, which would read as "adding a session gained nothing."
    marginal_gain: float | None
    # True when this size had only ONE possible subset (corpus_size ==
    # the whole contributing corpus). Such a point is exact rather than
    # noisy, but its `marginal_gain` compares a sampled mean against a
    # single deterministic value and is near-zero by construction --
    # so it must never be counted as evidence of flatness. Found by
    # running the real corpus, which declared a PLATEAU partly from this.
    exhaustive: bool = False


@dataclass(frozen=True)
class CoverageCurve:
    """The full curve, plus the two questions worth asking of it."""

    points: tuple[CurvePoint, ...]
    server: str
    # Every session handed in, including ones holding no calls for this
    # server.
    sessions_available: int
    # Sessions actually carrying at least one call for this server -- the
    # corpus the curve is really about. See `coverage_curve`.
    contributing_sessions: int
    # Total calls across those sessions. The curve's sample size, printed
    # because a percentage computed from 8 calls and one from 800 must not
    # look alike.
    total_calls: int
    seed: int
    samples_per_size: int
    # The thresholds this curve's own plateau call was made against,
    # carried on the result so a rendered curve states the constants it
    # used rather than leaving a reader to look them up.
    plateau: Plateau = field(default_factory=Plateau)

    def reaches(self, floor: float) -> bool:
        """True when the corpus already projects at or above `floor`.

        Reads the LAST point rather than the best one: the question is
        what the corpus does at full size, and a mid-curve subset scoring
        higher by sampling luck is not evidence the corpus is adequate.
        """
        return bool(self.points) and self.points[-1].mean_coverage >= floor

    def plateaued(self, floor: float, plateau: Plateau | None = None) -> bool:
        """True when the curve has gone flat while still below `floor` --
        the outcome that would falsify corpus-growth as a strategy.

        Both halves are required. A flat curve AT or ABOVE the floor is
        success, not a plateau, and reporting it as one would be exactly
        the kind of alarming-but-wrong signal this project tries not to
        emit.
        """
        plateau = plateau or self.plateau
        if self.reaches(floor):
            return False
        # Exhaustive points are excluded: their near-zero gain is an
        # artifact of running out of corpus, not a measurement of
        # flatness.
        usable = [p for p in self.points if not p.exhaustive and p.marginal_gain is not None]
        if len(usable) < plateau.window:
            return False
        gains = [p.marginal_gain for p in usable[-plateau.window :]]
        return all(g < plateau.gain_per_session for g in gains)

    @property
    def verdict(self) -> str:
        return "curve computed"


def _subsets_for(paths: Sequence[Path], size: int, samples: int, rng: random.Random) -> list[tuple[Path, ...]]:
    """Up to `samples` distinct subsets of `size` sessions.

    When the total number of possible subsets is small, every one is used
    and sampling is skipped entirely -- an exhaustive answer is both
    cheaper and exact, and it removes sampling noise from the small-n end
    of the curve where the noise is worst.
    """
    all_indices = range(len(paths))
    total = _combination_count(len(paths), size)
    if total <= samples:
        return [tuple(paths[i] for i in combo) for combo in itertools.combinations(all_indices, size)]

    seen: set[tuple[int, ...]] = set()
    # Bounded: a fixed multiple of the target rather than `while
    # len(seen) < samples`, so a pathological rng cannot spin forever.
    for _ in range(samples * 10):
        if len(seen) >= samples:
            break
        seen.add(tuple(sorted(rng.sample(list(all_indices), size))))
    return [tuple(paths[i] for i in combo) for combo in sorted(seen)]


def _combination_count(n: int, k: int) -> int:
    from math import comb

    return comb(n, k)


def _contributing(session_paths: Sequence[Path], server: str) -> tuple[list[Path], int]:
    """Sessions carrying at least one call for `server`, and the total
    number of such calls.

    Sessions for a DIFFERENT server are dropped rather than counted as
    corpus size. Found by running this against the project's own
    `.drifter/runs/`: 108 sessions, of which only 4 held any `filesystem`
    calls. Counting the other 104 meant most sampled subsets contained no
    relevant calls, were discarded as un-estimable, and the surviving
    points were computed from one or two subsets while being printed to a
    tenth of a percent. It also answered the wrong question -- "how many
    recordings of THIS task do I need" cannot be answered by counting
    recordings of a different one.
    """
    contributing: list[Path] = []
    total_calls = 0
    for path in session_paths:
        calls = sum(
            1 for record in read_session(path) if isinstance(record, ToolCall) and record.server == server
        )
        if calls:
            contributing.append(path)
            total_calls += calls
    return contributing, total_calls


def coverage_curve(
    session_paths: Sequence[Path],
    server: str,
    samples_per_size: int = 12,
    seed: int = 0,
    plateau: Plateau | None = None,
) -> CoverageCurve:
    """Projected coverage at every corpus size from 2 up to the number of
    CONTRIBUTING sessions -- those actually carrying calls for `server`.
    """
    all_paths = list(session_paths)
    paths, total_calls = _contributing(all_paths, server)
    rng = random.Random(seed)
    points: list[CurvePoint] = []
    previous: float | None = None

    for size in range(MIN_CROSS_VALIDATABLE, len(paths) + 1):
        rates: list[float] = []
        for subset in _subsets_for(paths, size, samples_per_size, rng):
            estimate = estimate_coverage(subset, server)
            # An un-estimable subset contributes nothing rather than a 0.0:
            # zero would be a measurement claim, and the whole reason
            # `estimate_coverage` reports un-estimable is that it is not one.
            if estimate.estimable and estimate.coverage is not None:
                rates.append(estimate.coverage)
        if not rates:
            continue
        mean = sum(rates) / len(rates)
        points.append(
            CurvePoint(
                corpus_size=size,
                mean_coverage=mean,
                min_coverage=min(rates),
                max_coverage=max(rates),
                subsets_sampled=len(rates),
                marginal_gain=None if previous is None else mean - previous,
                exhaustive=_combination_count(len(paths), size) == 1,
            )
        )
        previous = mean

    return CoverageCurve(
        points=tuple(points),
        server=server,
        sessions_available=len(all_paths),
        contributing_sessions=len(paths),
        total_calls=total_calls,
        seed=seed,
        samples_per_size=samples_per_size,
        plateau=plateau or Plateau(),
    )


def render_curve(curve: CoverageCurve, floor: float) -> str:
    """Human-readable curve, with its foundation stated alongside it.

    limitation 16's lesson drives the shape: a confident-looking number
    whose basis is invisible is worse than no number. So the spread, the
    subset count and the seed are printed next to every mean, and the
    closing line says what the curve implies rather than leaving a reader
    to infer it.
    """
    dropped = curve.sessions_available - curve.contributing_sessions
    dropped_note = f", {dropped} with no calls for this server ignored" if dropped else ""

    if not curve.points:
        return (
            f"PROJECTED COVERAGE CURVE ({curve.server})\n"
            f"  Not computable: {curve.contributing_sessions} session(s) carry calls for "
            f"{curve.server}{dropped_note}, and at least {MIN_CROSS_VALIDATABLE} are needed "
            f"to cross-validate.\n"
        )

    lines = [
        f"PROJECTED COVERAGE CURVE ({curve.server})",
        f"  {curve.contributing_sessions} contributing sessions{dropped_note}"
        f" | {curve.total_calls} calls total",
        f"  up to {curve.samples_per_size} subsets per size | seed {curve.seed}",
        "",
        "  corpus size   projected coverage        range        subsets   gain/session",
    ]
    for point in curve.points:
        gain = "     —" if point.marginal_gain is None else f"{point.marginal_gain:+.1%}"
        lines.append(
            f"  {point.corpus_size:>11}   {point.mean_coverage:>17.1%}   "
            f"{point.min_coverage:>5.0%}-{point.max_coverage:<5.0%}   "
            f"{point.subsets_sampled:>7}   {gain:>12}"
        )

    last = curve.points[-1]
    lines.append("")
    lines.append(f"  Fidelity floor: {floor:.2f}")

    if curve.reaches(floor):
        # Deliberately does NOT say "corpus-based replay is viable for this
        # task", which is what this line used to claim. docs/SPEC.md §15
        # limitation 17 disproves that implication directly: a corpus
        # measured at 100% coverage produced 0/4 valid runs, because replay
        # serves content-empty responses and the agent could no longer
        # construct the arguments it had constructed live. Coverage is a
        # necessary condition, not a sufficient one, and the good path is
        # exactly where overclaiming does the damage.
        lines.append(
            f"  REACHES THE FLOOR at {last.corpus_size} sessions "
            f"({last.mean_coverage:.1%}) — lookup coverage is no longer the limiting factor."
        )
        lines.append(
            "  This does NOT establish that replay reproduces the task: it measures whether "
            "RECORDED calls resolve, and those calls' arguments already encode response content "
            "your agent will not receive under content-empty replay (docs/SPEC.md §15 "
            "limitation 17). Confirm with an UNMUTATED `drifter run` before trusting a verdict."
        )
    elif curve.plateaued(floor):
        lines.append(
            f"  PLATEAUED at {last.mean_coverage:.1%}, below the {floor:.0%} floor — the last "
            f"{curve.plateau.window} points each gained under "
            f"{curve.plateau.gain_per_session:.0%} per session "
            f"(calibration.yaml `plateau:`)."
        )
        lines.append(
            "  Recording more sessions of this task is not projected to close the gap. "
            "This is evidence against corpus growth as the fix, not a reason to record harder."
        )
    else:
        needed = floor - last.mean_coverage
        rate = last.marginal_gain
        lines.append(
            f"  STILL CLIMBING: {last.mean_coverage:.1%} at {last.corpus_size} sessions, "
            f"{needed:.1%} short of the floor."
        )
        if rate and rate > 0:
            lines.append(
                f"  At the current {rate:.1%}/session that is ~{int(needed / rate) + 1} more sessions — "
                "a linear extrapolation, and this curve has no reason to stay linear. "
                "Record more, then re-run this."
            )
        else:
            lines.append("  Too few points to extrapolate. Record more, then re-run this.")

    return "\n".join(lines) + "\n"
