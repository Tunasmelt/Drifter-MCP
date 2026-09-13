"""docs/SPEC.md §15 limitation 21 (release gate E2), as executable evidence.

Recomputes every documented E2 claim from the committed bundle in
tests/fixtures/experiments/limitation_21_e2 with Drifter's own analysis code,
and checks the pre-registered acceptance conditions. Like limitation 20's
evidence test, it asserts no session ids, timestamps or file names.
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

BUNDLE = Path(__file__).parent.parent / "fixtures" / "experiments" / "limitation_21_e2"
MANIFEST = json.loads((BUNDLE / "manifest.json").read_text(encoding="utf-8"))
CLAIMS = MANIFEST["claims"]
SERVER = "orders"


def _sessions(*parts: str) -> list[Path]:
    return sorted(BUNDLE.joinpath(*parts).glob("*.jsonl"))


def _calls(path: Path) -> list[ToolCall]:
    return [r for r in read_session(path) if isinstance(r, ToolCall)]


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


def test_the_archived_server_is_the_committed_fixture_server():
    fixture = Path(__file__).parent.parent / "fixtures" / "orders_server.py"
    assert (BUNDLE / "harness" / "orders_server.py").read_bytes() == fixture.read_bytes()


def test_the_harness_carries_the_preregistered_prompt_and_oracle():
    config = yaml.safe_load((BUNDLE / "harness" / "drifter.yaml").read_text(encoding="utf-8"))
    (task,) = config["tasks"]
    assert task["prompt"] == MANIFEST["task"]["prompt"]
    assert task["assert"]["calls"] == MANIFEST["task"]["calls"]
    assert task["assert"]["answer_matches"] == MANIFEST["task"]["answer_matches"]
    prereg = (BUNDLE / "harness" / "PREREGISTRATION.txt").read_text(encoding="utf-8")
    assert MANIFEST["task"]["prompt"] in prereg
    assert MANIFEST["task"]["answer_matches"] in prereg


def test_the_corpus_is_two_live_sessions_of_the_same_trajectory():
    sessions = _sessions("corpus")
    assert len(sessions) == CLAIMS["corpus"]["sessions"]
    expected = [tuple(step) for step in CLAIMS["corpus"]["trajectory"]]
    for session in sessions:
        calls = _calls(session)
        assert [(c.tool_name, c.arguments) for c in calls] == [(name, args) for name, args in expected]
        assert {(c.result_provenance, c.fault) for c in calls} == {("real", False)}


def test_the_authored_fixture_binds_to_the_recorded_requests():
    fixture = load_authored_responses(BUNDLE / "harness" / "responses.yaml", SERVER, _sessions("corpus"))
    assert len(fixture.by_key) == 2


def test_the_mutation_renamed_only_the_called_id_parameter():
    audit = [json.loads(line) for line in (BUNDLE / "run" / "e2" / "mutations.jsonl").read_text(encoding="utf-8").splitlines()]
    renamed = {entry["tool_name"] if "tool_name" in entry else None: entry["inverse"] for entry in audit if entry.get("inverse")}
    assert list(renamed.values()) == list(CLAIMS["renamed"].values())


@pytest.mark.parametrize("arm", ["baseline", "mutated"])
def test_each_arm_matches_its_documented_result(arm):
    claim = CLAIMS[arm]
    sessions = _sessions("run", "e2", arm)

    result = aggregate_baseline_runs(arm, sessions)

    assert result.total_runs == claim["total"]
    assert result.valid_runs == claim["valid"]
    assert result.baseline_fidelity == 1.0
    assert result.dominant_path == ("find_order", "get_order")
    assert evaluate_task(list(result.valid_session_paths), _oracle()).verdict == claim["task"]
    for session in sessions:
        get_order = [c for c in _calls(session) if c.tool_name == "get_order"]
        assert [(list(c.arguments), c.fault, c.match_tier, c.result_provenance) for c in get_order] == [
            ([claim["id_argument"]], False, claim["tier"], "authored_fixture")
        ]


def test_the_preregistered_acceptance_conditions_hold():
    acceptance = CLAIMS["acceptance"]
    sessions = _sessions("run", "e2", "mutated")
    result = aggregate_baseline_runs("mutated", sessions)
    task = evaluate_task(list(result.valid_session_paths), _oracle())
    get_orders = [c for s in sessions for c in _calls(s) if c.tool_name == "get_order"]

    assert result.valid_runs >= acceptance["min_valid"]
    assert task.runs_evaluated == result.valid_runs == task.runs_passed
    assert get_orders and all("orderId" in c.arguments and "order_id" not in c.arguments for c in get_orders)


def test_the_bundle_holds_no_payloads_secrets_or_machine_paths():
    for path in BUNDLE.rglob("*"):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        assert redact_string(text) == text, f"secret-shaped value in {path.name}"
        assert not re.search(r"(?i)\b[a-z]:[\\/]{1,2}(users|windows)|/c/users|appdata", text), path.name
    for session in _sessions("corpus") + [p for p in (BUNDLE / "run").rglob("*.jsonl") if p.name != "mutations.jsonl"]:
        for line in session.read_text(encoding="utf-8").splitlines():
            assert "result" not in json.loads(line), f"payload recorded in {session.name}"
