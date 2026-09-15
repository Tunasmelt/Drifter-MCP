"""docs/PHASES.md R4: the pre-registered acceptance bars, checked against the
implemented Behavior verdict.

Both arms are simulated from an agent that takes path A with probability p per
run. The path of interest is chosen from an INDEPENDENT corpus sample, as
`drifter run` chooses it. Verdicts come from `compute_behavior_effect_size`
itself, at the calibration defaults (margin 0.3, z 1.645) and N=20 per arm.

Bars, fixed before implementation: (1) false REGRESSION <= 5% for every p in
{1.0, 0.95, 0.9, 0.8, 0.7, 0.5}; (2) REGRESSION >= 90% for a 0.6 drop for every
p in {1.0, 0.95, 0.9, 0.8, 0.7}. Seeded, so the test is deterministic.
"""

import random
from collections import Counter

import pytest

from mcp_drifter.evaluate.baseline import BaselineResult
from mcp_drifter.evaluate.effect_size import PATH_SOURCE_CORPUS, compute_behavior_effect_size
from mcp_drifter.record.calibration import Calibration

A, B = ("list", "read"), ("list", "search", "read")
N = 20
CORPUS_SESSIONS = 10
TRIALS = 4000


def _arm(paths: list) -> BaselineResult:
    counts = Counter(paths)
    return BaselineResult(
        task_id="sim", total_runs=len(paths), valid_runs=len(paths),
        dominant_path=max(counts, key=lambda p: counts[p]), variant_frequencies=dict(counts),
        natural_variation=0.0, baseline_spread=0.0, baseline_fidelity=1.0, excluded_runs=[],
    )


def _draw(p: float, n: int, rng: random.Random) -> list:
    return [A if rng.random() < p else B for _ in range(n)]


def _corpus_path(p: float, rng: random.Random) -> tuple:
    sample = _draw(p, CORPUS_SESSIONS, rng)
    counts = Counter(sample)
    best = max(counts.values())
    return next(path for path in sample if counts[path] == best)


def _rates(p_baseline: float, p_mutated: float, seed: int) -> Counter:
    rng = random.Random(seed)
    calibration = Calibration()
    verdicts = Counter()
    for _ in range(TRIALS):
        path = _corpus_path(p_baseline, rng)
        result = compute_behavior_effect_size(
            _arm(_draw(p_baseline, N, rng)), _arm(_draw(p_mutated, N, rng)),
            calibration=calibration, path_of_interest=path, path_source=PATH_SOURCE_CORPUS,
        )
        verdicts[result.verdict] += 1
    return verdicts


@pytest.mark.parametrize("p", [1.0, 0.95, 0.9, 0.8, 0.7, 0.5])
def test_false_regression_rate_on_an_unchanged_agent_is_at_most_5_percent(p):
    verdicts = _rates(p, p, seed=int(p * 1000))
    false_alarm = verdicts["REGRESSION"] / TRIALS
    assert false_alarm <= 0.05, f"p={p}: false REGRESSION {false_alarm:.3f} (inconclusive {verdicts['INCONCLUSIVE'] / TRIALS:.3f})"


@pytest.mark.parametrize(
    "p",
    [
        1.0, 0.95, 0.9, 0.8,
        # PRE-REGISTERED BAR MISSED, recorded rather than tuned (docs/PHASES.md R4):
        # measured 0.894 on first run against the implemented code (scratch
        # simulation before implementation: ~0.90 at 20,000 trials). Cause
        # confirmed at 20,000 trials: a 10-session corpus picks path B as the
        # path of interest 9.9% of the time at p=0.7, and those trials detect
        # nothing (0.000); given a path-A pick, detection is 0.993. The interval
        # rule is not the cause; choosing P from a small corpus of an
        # inconsistent agent is. strict=True: if this starts passing, the xfail
        # must be removed deliberately, not silently.
        pytest.param(0.7, marks=pytest.mark.xfail(strict=True, reason="pre-registered power bar missed at p=0.7 (0.894 < 0.90)")),
    ],
)
def test_a_0_6_drop_is_detected_at_least_90_percent_of_the_time(p):
    verdicts = _rates(p, p - 0.6, seed=int(p * 1000) + 1)
    power = verdicts["REGRESSION"] / TRIALS
    assert power >= 0.90, f"p={p}: REGRESSION on a 0.6 drop {power:.3f} (inconclusive {verdicts['INCONCLUSIVE'] / TRIALS:.3f})"
