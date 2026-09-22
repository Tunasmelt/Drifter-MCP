"""docs/PHASES.md R4: live unchanged-agent trials, as executable evidence.

Three real tasks, each replayed with NO mutation, through Drifter's own
baseline path (make_run_once + run_baseline), then split 2,000 ways into
20-vs-20 A/A comparisons scored by the real Behavior rule
(compute_behavior_effect_size). Recomputed here from the committed session
records, so the reported false-alarm rates cannot silently outlive their
evidence -- the same discipline as the limitation_20/21/22/23 bundles.

- orders, econ: both came back p=1.0 (the agent always took the one sane
  path) -- confirms the bar holds, but only in the case least likely to
  false-alarm.
- variance: a new controlled server offering two equally valid tools for the
  same question, specifically to test the region the simulation flagged as
  risky (moderate consistency). Two attempts, each cut short by the same
  account usage limit and individually below the 40-run threshold for a
  split analysis; a third, POST-HOC and NOT PRE-REGISTERED pooling of both
  (disclosed as such in PREREGISTRATION.txt) reaches 55 valid runs at
  p=0.891 and is the only evidence in this project of the rule's behaviour
  at moderate, real agent consistency.

Deliberately NOT asserted: session ids, timestamps, file names.
"""

import hashlib
import json
import random
from collections import Counter
from pathlib import Path

import pytest

from mcp_drifter.evaluate.assertions import TaskAssertions, evaluate_task
from mcp_drifter.evaluate.baseline import BaselineResult, aggregate_baseline_runs
from mcp_drifter.evaluate.effect_size import PATH_SOURCE_CORPUS, compute_behavior_effect_size, path_of_interest_from_sessions
from mcp_drifter.record.calibration import load_calibration
from mcp_drifter.record.reader import read_session
from mcp_drifter.record.redact import redact_string
from mcp_drifter.record.schema import ToolCall

BUNDLE = Path(__file__).parent.parent / "fixtures" / "experiments" / "r4_live_trials"
MANIFEST = json.loads((BUNDLE / "manifest.json").read_text(encoding="utf-8"))
CLAIMS = MANIFEST["claims"]
DESIGN = MANIFEST["design"]
CALIBRATION = load_calibration()

ORACLES = {
    "orders": TaskAssertions(calls=("find_order", "get_order"), answer_matches=r"(?i)\b1,?240\b"),
    "econ": TaskAssertions(calls=("econ_index_get_usage_by_country",), answer_matches=r"\b1\.91\b"),
    "variance": TaskAssertions(answer_matches=r"(?i)\b(?:7|seven)\b"),
}
SERVERS = {"orders": "orders", "econ": "econ", "variance": "variance"}


def _pool_sessions(name: str) -> list[Path]:
    return sorted((BUNDLE / f"{name}_pool").glob("*.jsonl"))


def _corpus_sessions(name: str) -> list[Path]:
    return sorted((BUNDLE / f"{name}_corpus").glob("*.jsonl"))


def _arm(paths: list) -> BaselineResult:
    counts = Counter(paths)
    return BaselineResult(
        task_id="split", total_runs=len(paths), valid_runs=len(paths),
        dominant_path=max(counts, key=lambda p: counts[p]), variant_frequencies=dict(counts),
        natural_variation=0.0, baseline_spread=0.0, baseline_fidelity=1.0, excluded_runs=[],
    )


def _run_splits(per_run: list, path_of_interest) -> dict:
    rng = random.Random(DESIGN["seed"])
    verdicts = Counter()
    arm = DESIGN["arm_size"]
    for _ in range(DESIGN["splits"]):
        order = rng.sample(range(len(per_run)), 2 * arm)
        base, mut = _arm([per_run[i] for i in order[:arm]]), _arm([per_run[i] for i in order[arm:]])
        verdicts[compute_behavior_effect_size(
            base, mut, calibration=CALIBRATION, path_of_interest=path_of_interest, path_source=PATH_SOURCE_CORPUS,
        ).verdict] += 1
    return {k: verdicts[k] / DESIGN["splits"] for k in ("REGRESSION", "INCONCLUSIVE", "NO_REGRESSION", "UNKNOWN")}


def test_every_bundle_file_matches_its_manifest_hash():
    on_disk = {
        str(p.relative_to(BUNDLE)).replace("\\", "/")
        for p in BUNDLE.rglob("*") if p.is_file() and p.name != "manifest.json"
    }
    assert on_disk == set(MANIFEST["files"])
    for rel, digest in MANIFEST["files"].items():
        assert hashlib.sha256((BUNDLE / rel).read_bytes()).hexdigest() == digest, rel


def test_the_bundle_holds_no_payloads_secrets_or_machine_paths():
    import re
    for path in BUNDLE.rglob("*"):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"(?i)\b[a-z]:[\\/]{1,2}(users|windows)|/c/users|appdata", text), path.name
        assert redact_string(text) == text, f"secret-shaped value in {path.name}"
    for session in BUNDLE.rglob("*.jsonl"):
        for line in session.read_text(encoding="utf-8").splitlines():
            assert "result" not in json.loads(line), f"payload recorded in {session.name}"
    assert not list(BUNDLE.rglob("*.frames")), "raw frames must not be committed"


@pytest.mark.parametrize("name", ["orders", "econ", "variance_attempt1", "variance_attempt2"])
def test_each_pool_matches_its_documented_valid_count_and_p(name):
    claim = CLAIMS[name]
    sessions = _pool_sessions(name)
    result = aggregate_baseline_runs(name, sessions, calibration=CALIBRATION)

    assert result.total_runs == claim["total_runs"]
    assert result.valid_runs == claim["valid_runs"]

    server = "variance" if name.startswith("variance") else name
    corpus_name = "variance" if name.startswith("variance") else name
    corpus = _corpus_sessions(corpus_name)
    path_of_interest = path_of_interest_from_sessions(corpus, server)
    per_run = [tuple(c.tool_name for c in read_session(s) if isinstance(c, ToolCall)) for s in result.valid_session_paths]
    measured_p = sum(1 for p in per_run if p == path_of_interest) / len(per_run)
    assert measured_p == pytest.approx(claim["measured_p"])

    oracle_key = "variance" if name.startswith("variance") else name
    task = evaluate_task(list(result.valid_session_paths), ORACLES[oracle_key])
    assert task.verdict == "PASS"
    assert task.runs_passed == result.valid_runs


@pytest.mark.parametrize("name", ["orders", "econ"])
def test_the_deterministic_pools_meet_the_false_regression_bar(name):
    sessions = _pool_sessions(name)
    result = aggregate_baseline_runs(name, sessions, calibration=CALIBRATION)
    corpus = _corpus_sessions(name)
    path_of_interest = path_of_interest_from_sessions(corpus, SERVERS[name])
    per_run = [tuple(c.tool_name for c in read_session(s) if isinstance(c, ToolCall)) for s in result.valid_session_paths]

    assert len(per_run) >= 2 * DESIGN["arm_size"], "below the pre-registered split threshold"
    rates = _run_splits(per_run, path_of_interest)

    assert rates["REGRESSION"] <= DESIGN["acceptance_bar_false_regression"]
    assert rates["NO_REGRESSION"] == pytest.approx(1.0, abs=1e-6)  # p=1.0: both arms always match


def test_the_pooled_variance_result_is_below_40_runs_per_attempt_individually():
    """Confirms why each T3 attempt was reported unmeasurable on its own,
    per the pre-registered rule -- not asserted, verified."""
    for name in ("variance_attempt1", "variance_attempt2"):
        result = aggregate_baseline_runs(name, _pool_sessions(name), calibration=CALIBRATION)
        assert result.valid_runs < 2 * DESIGN["arm_size"]


def test_the_posthoc_pooled_variance_split_rates_match_the_recorded_claim():
    """POST-HOC, NOT PRE-REGISTERED (PREREGISTRATION.txt, T3 AMENDMENT 2).
    Pools both variance attempts' valid runs and reruns the same seeded
    split procedure; checked against the recorded analysis, not re-derived
    independently, since the pooling itself was a post-hoc decision."""
    claim = CLAIMS["variance_pooled_posthoc"]
    pooled = _pool_sessions("variance_attempt1") + _pool_sessions("variance_attempt2")
    result = aggregate_baseline_runs("pooled", pooled, calibration=CALIBRATION)
    assert result.valid_runs == claim["valid_runs"]

    corpus = _corpus_sessions("variance")
    path_of_interest = path_of_interest_from_sessions(corpus, "variance")
    per_run = [tuple(c.tool_name for c in read_session(s) if isinstance(c, ToolCall)) for s in result.valid_session_paths]
    measured_p = sum(1 for p in per_run if p == path_of_interest) / len(per_run)
    assert measured_p == pytest.approx(claim["measured_p"])

    rates = _run_splits(per_run, path_of_interest)
    for verdict, rate in claim["split_rates"].items():
        assert rates[verdict] == pytest.approx(rate, abs=1e-9)
    assert (rates["REGRESSION"] <= DESIGN["acceptance_bar_false_regression"]) is claim["acceptance_false_regression_le_5pct"]


def test_variance_shows_the_first_real_stochastic_signal_across_all_three_tasks():
    """orders and econ both p=1.0; variance is not, confirming the T3 design
    exercised the region the simulation flagged, which the other two did not."""
    assert CLAIMS["orders"]["measured_p"] == 1.0
    assert CLAIMS["econ"]["measured_p"] == 1.0
    assert CLAIMS["variance_pooled_posthoc"]["measured_p"] < 1.0
    assert 0.7 < CLAIMS["variance_pooled_posthoc"]["measured_p"] < 0.95
