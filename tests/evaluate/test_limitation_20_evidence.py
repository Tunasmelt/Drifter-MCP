"""docs/SPEC.md §15 limitation 20, as executable evidence.

The real-agent experiment's records live in
tests/fixtures/experiments/limitation_20 (shape-only sessions, captured final
answers, the authored fixture, the harness). These tests recompute every
documented claim from those records with Drifter's own analysis code, so the
claim cannot silently outlive its evidence. The previous experiment's corpus
was lost to OS temp cleanup; this one is committed.

Deliberately NOT asserted: session ids, timestamps, file names -- nothing that
is only incidentally stable. Only semantic claims: arm counts, validity,
trajectory, provenance, navigation and oracle results.
"""

import hashlib
import json
import re
from pathlib import Path

import pytest
import yaml

from mcp_drifter.evaluate.assertions import TaskAssertions, evaluate_task
from mcp_drifter.evaluate.baseline import aggregate_baseline_runs
from mcp_drifter.record.reader import read_session
from mcp_drifter.record.redact import redact_string
from mcp_drifter.record.schema import ToolCall
from mcp_drifter.replay.authored_responses import load_authored_responses

BUNDLE = Path(__file__).parent.parent / "fixtures" / "experiments" / "limitation_20"
MANIFEST = json.loads((BUNDLE / "manifest.json").read_text(encoding="utf-8"))
CLAIMS = MANIFEST["claims"]
LIVE_TRAJECTORY = tuple(CLAIMS["fx-on"]["trajectory"])


def _sessions(*parts: str) -> list[Path]:
    return sorted((BUNDLE.joinpath(*parts)).glob("*.jsonl"))


def _calls(path: Path) -> list[ToolCall]:
    return [r for r in read_session(path) if isinstance(r, ToolCall)]


def _navigated(path: Path) -> bool:
    """Reached the real file: an unfaulted read of data/readings.csv."""
    return any(
        c.tool_name == "read_text_file"
        and not c.fault
        and re.search(r"[\\/]data[\\/]readings\.csv$", c.arguments.get("path", ""))
        for c in _calls(path)
    )


def _oracle() -> TaskAssertions:
    task = MANIFEST["task"]
    return TaskAssertions(calls=tuple(task["calls"]), answer_matches=task["answer_matches"])


def test_every_bundle_file_matches_its_manifest_hash():
    on_disk = {
        str(p.relative_to(BUNDLE)).replace("\\", "/")
        for p in BUNDLE.rglob("*")
        if p.is_file() and p.name != "manifest.json"
    }
    assert on_disk == set(MANIFEST["files"])
    for rel, digest in MANIFEST["files"].items():
        assert hashlib.sha256((BUNDLE / rel).read_bytes()).hexdigest() == digest, rel


def test_the_harness_config_carries_the_manifest_oracle():
    config = yaml.safe_load((BUNDLE / "harness" / "drifter.yaml").read_text(encoding="utf-8"))
    for task in config["tasks"]:
        assert task["assert"]["answer_matches"] == MANIFEST["task"]["answer_matches"]
        assert task["prompt"] == MANIFEST["task"]["prompt"]


def test_the_authoring_script_would_regenerate_the_same_oracle():
    """author_fixture.py rewrites drifter.yaml's tasks. Checking only the
    finished config missed that re-running it would restore the old,
    broader oracle."""
    source = (BUNDLE / "harness" / "author_fixture.py").read_text(encoding="utf-8")
    assert f'"answer_matches": r"{MANIFEST["task"]["answer_matches"]}"' in source
    # Raw and string-escaped spellings: the old status line printed "\\b2\\b".
    assert not re.search(r"\\{1,2}b2\\{1,2}b", source)


def test_the_corpus_is_real_and_one_trajectory():
    sessions = _sessions("corpus")
    calls = [c for s in sessions for c in _calls(s)]
    requests = {(c.tool_name, json.dumps(c.arguments, sort_keys=True)) for c in calls}

    assert len(sessions) == CLAIMS["corpus"]["sessions"]
    assert len(calls) == CLAIMS["corpus"]["calls"]
    assert len(requests) == CLAIMS["corpus"]["distinct_requests"]
    assert {c.result_provenance for c in calls} == {"real"}
    assert {tuple(c.tool_name for c in _calls(s)) for s in sessions} == {LIVE_TRAJECTORY}


def test_the_authored_fixture_binds_to_exact_recorded_requests():
    fixture = load_authored_responses(BUNDLE / "harness" / "responses.yaml", "filesystem", _sessions("corpus"))
    assert len(fixture.by_key) == CLAIMS["corpus"]["distinct_requests"]


def test_fixture_listings_disclose_one_level_at_a_time():
    """What separates limitation 20 from R0: no listing reveals a path deeper
    than its own directory's direct children."""
    responses = yaml.safe_load((BUNDLE / "harness" / "responses.yaml").read_text(encoding="utf-8"))["responses"]
    for entry in responses:
        if entry["tool_name"] != "list_directory":
            continue
        text = entry["result"]["content"][0]["text"]
        assert "readings.csv" not in text or entry["arguments"]["path"].endswith("data")
        for line in text.splitlines():
            assert re.fullmatch(r"\[(DIR|FILE)\] [^\\/]+", line), line


def test_fx_off_shows_shape_only_replay_cannot_support_the_task():
    """fx-off is NOT a mutation comparison: its baseline failed, so the
    mutated arm was never scheduled."""
    claim = CLAIMS["fx-off"]
    baseline = _sessions("run", "fx-off", "baseline")

    result = aggregate_baseline_runs("fx-off", baseline)

    assert result.total_runs == claim["baseline_total"]
    assert result.valid_runs == claim["baseline_valid"]
    assert (BUNDLE / "run" / "fx-off" / "mutated").exists() is claim["mutated_arm_ran"]
    assert sum(_navigated(s) for s in baseline) == claim["navigation"]
    task = evaluate_task(baseline, _oracle())
    assert task.verdict == "FAIL"
    assert task.runs_passed == claim["correct_answers"]


@pytest.mark.parametrize("arm", ["baseline", "mutated"])
def test_fx_on_preserves_task_capability_in_both_arms(arm):
    claim = CLAIMS["fx-on"]
    sessions = _sessions("run", "fx-on", arm)

    result = aggregate_baseline_runs(f"fx-on/{arm}", sessions)

    assert result.valid_runs == claim[f"{arm}_valid"] == len(sessions)
    assert result.baseline_fidelity == claim["coverage"]
    assert result.dominant_path == LIVE_TRAJECTORY
    assert result.natural_variation == 0
    authored = result.provenance_breakdown["authored_fixture"]
    assert authored == len(sessions) * len(LIVE_TRAJECTORY)
    assert sum(result.provenance_breakdown.values()) == authored
    assert all(_navigated(s) for s in sessions)
    task = evaluate_task(sessions, _oracle())
    assert task.verdict == claim["task_verdict"][arm]
    assert task.runs_passed == len(sessions)


def test_fx_on_totals_match_the_documented_counts():
    claim = CLAIMS["fx-on"]
    sessions = _sessions("run", "fx-on", "baseline") + _sessions("run", "fx-on", "mutated")
    assert sum(_navigated(s) for s in sessions) == claim["navigation"]
    assert evaluate_task(sessions, _oracle()).runs_passed == claim["correct_answers"]


def test_the_tightened_oracle_rescores_the_run_time_verdicts_unchanged():
    old = re.compile(MANIFEST["task"]["answer_matches_at_run_time"])
    new = re.compile(MANIFEST["task"]["answer_matches"])
    answers = sorted((BUNDLE / "run").rglob("*.stdout.txt"))
    assert len(answers) == 12
    for path in answers:
        plain = re.sub(r"[*_`]+", "", path.read_text(encoding="utf-8"))
        assert bool(old.search(plain)) == bool(new.search(plain)), path.name


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("followed by 2 data rows.", True),
        ("It has **Two** data rows", True),
        ("1 data row", False),
        ("columns 1,2,3", False),
        ("2 columns and 3 data rows", False),
    ],
)
def test_the_tightened_oracle_binds_to_the_row_count_claim(answer, expected):
    assert bool(re.search(MANIFEST["task"]["answer_matches"], re.sub(r"[*_`]+", "", answer))) is expected


def test_the_bundle_holds_no_payloads_secrets_or_machine_paths():
    for path in BUNDLE.rglob("*"):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        assert redact_string(text) == text, f"secret-shaped value in {path.name}"
        assert not re.search(r"(?i)\b[a-z]:[\\/]{1,2}(users|windows)|/c/users|appdata", text), path.name
    for session in _sessions("corpus") + list((BUNDLE / "run").rglob("*.jsonl")):
        if session.name == "mutations.jsonl":
            continue
        for line in session.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            assert "result" not in record, f"payload recorded in {session.name}"
