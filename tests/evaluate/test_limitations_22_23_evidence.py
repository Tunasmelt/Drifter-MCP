"""docs/SPEC.md §15 limitations 22 and 23, as executable evidence.

Three committed bundles under tests/fixtures/experiments:
- limitation_22_gate: fresh wheel outside the repo, upstream server
  unavailable during `run`, report rebuilt from disk;
- limitation_23_s1_time: real `mcp-server-time`, adaptation to a rename;
- limitation_23_s2_econ: real public Economic Index server, adaptation, plus
  one run that recovered from its own schema-rejected guess.

Every documented claim is recomputed from the records with Drifter's own
analysis code. Raw frames are not committed (they carry response payloads);
fault_codes.json holds only the JSON-RPC code of each faulted call, extracted
from the raw mirror when the bundle was built. No session ids or timestamps
are asserted.
"""

import hashlib
import json
import re
from pathlib import Path

import pytest

from mcp_drifter.evaluate.assertions import TaskAssertions, evaluate_task
from mcp_drifter.evaluate.baseline import aggregate_baseline_runs
from mcp_drifter.record.reader import read_session
from mcp_drifter.record.redact import redact_string
from mcp_drifter.record.schema import ToolCall

EXPERIMENTS = Path(__file__).parent.parent / "fixtures" / "experiments"
BUNDLES = {
    "limitation_22_gate": ("gate", "orders", "get_order"),
    "limitation_23_s1_time": ("tokyo-ny", "time", "convert_time"),
    "limitation_23_s2_econ": ("japan-index", "econ", "econ_index_get_usage_by_country"),
}


def _manifest(name: str) -> dict:
    return json.loads((EXPERIMENTS / name / "manifest.json").read_text(encoding="utf-8"))


def _sessions(name: str, arm: str) -> list[Path]:
    task = BUNDLES[name][0]
    return sorted((EXPERIMENTS / name / "run" / task / arm).glob("*.jsonl"))


def _calls(path: Path) -> list[ToolCall]:
    return [r for r in read_session(path) if isinstance(r, ToolCall)]


def _oracle(name: str) -> TaskAssertions:
    oracle = _manifest(name)["oracle"]
    return TaskAssertions(calls=tuple(oracle["calls"]), answer_matches=oracle["answer_matches"])


@pytest.mark.parametrize("name", list(BUNDLES))
def test_every_bundle_file_matches_its_manifest_hash(name):
    bundle = EXPERIMENTS / name
    manifest = _manifest(name)
    on_disk = {str(p.relative_to(bundle)).replace("\\", "/") for p in bundle.rglob("*") if p.is_file() and p.name != "manifest.json"}
    assert on_disk == set(manifest["files"])
    for rel, digest in manifest["files"].items():
        assert hashlib.sha256((bundle / rel).read_bytes()).hexdigest() == digest, rel


@pytest.mark.parametrize("name", list(BUNDLES))
@pytest.mark.parametrize("arm", ["baseline", "mutated"])
def test_each_arm_matches_its_documented_result(name, arm):
    claim = _manifest(name)["claims"][arm]
    tool = BUNDLES[name][2]
    sessions = _sessions(name, arm)

    result = aggregate_baseline_runs(arm, sessions)

    assert result.total_runs == claim["total"]
    assert result.valid_runs == claim["valid"]
    assert evaluate_task(list(result.valid_session_paths), _oracle(name)).verdict == claim["task"]
    argument = claim.get("argument") or claim["id_argument"]
    for session in result.valid_session_paths:
        succeeded = [c for c in _calls(session) if c.tool_name == tool and c.fault is False]
        assert succeeded, session.name
        for call in succeeded:
            assert argument in call.arguments
            assert call.match_tier == claim["tier"]
            assert call.result_provenance == "authored_fixture"


@pytest.mark.parametrize("name", ["limitation_23_s1_time", "limitation_23_s2_econ"])
def test_the_mutation_renamed_exactly_the_documented_argument(name):
    manifest = _manifest(name)
    task = BUNDLES[name][0]
    audit = [json.loads(line) for line in (EXPERIMENTS / name / "run" / task / "mutations.jsonl").read_text(encoding="utf-8").splitlines()]
    tool = BUNDLES[name][2]
    assert {e["tool_name"]: e["inverse"] for e in audit if e["tool_name"] == tool} == manifest["claims"]["renamed"]


def test_s2_recovered_run_is_documented_exactly():
    """The run finding A was about: a guessed `country` rejected with -31003,
    a retry with `countryCode`, a correct answer, and still excluded because
    the record predates `fault_code`."""
    name = "limitation_23_s2_econ"
    claim = _manifest(name)["claims"]["recovered_run"]
    codes = json.loads((EXPERIMENTS / name / "fault_codes.json").read_text(encoding="utf-8"))
    oracle = _oracle(name)

    result = aggregate_baseline_runs("mutated", _sessions(name, "mutated"))
    (excluded,) = result.excluded_runs
    calls = _calls(excluded.path)

    assert [sorted(c.arguments) for c in calls] == [claim["first_call_arguments"], claim["retry_arguments"]]
    assert [c.fault for c in calls] == [True, False]
    assert calls[0].fault_code is claim["fault_code_field_on_record"]
    rel = str(excluded.path.relative_to(EXPERIMENTS / name / "run" / "japan-index")).replace("\\", "/")
    assert [c["code"] for c in codes[rel]] == [claim["first_call_code"]]
    answer = excluded.path.with_suffix(".stdout.txt").read_text(encoding="utf-8")
    assert bool(re.search(oracle.answer_matches, re.sub(r"[*_`]+", "", answer))) is claim["answer_correct"]
    assert claim["still_excluded_under_c44ed82"] is True


def test_gate_replay_ran_with_the_upstream_server_unavailable():
    log = (EXPERIMENTS / "limitation_22_gate" / "harness" / "gate_log.txt").read_text(encoding="utf-8")
    order = [log.index(marker) for marker in (
        "== upstream made unavailable", "[FAIL] server 'orders'", "run exit 0", "== server still absent:", "orders_server.py.unavailable",
    )]
    assert order == sorted(order)
    assert "\norders_server.py\n" not in log


def test_gate_rebuilt_report_is_byte_identical_to_the_live_report():
    harness = EXPERIMENTS / "limitation_22_gate" / "harness"
    live = (harness / "run_output.txt").read_bytes()
    live_report = live[live.index(b"DRIFTER RUN"):]
    assert (harness / "report_fixed_1.txt").read_bytes() == live_report
    assert (harness / "report_fixed_2.txt").read_bytes() == live_report
    # Before the fix, the rebuild lost the operator and the mutation log.
    before = (harness / "report_1.txt").read_bytes()
    assert b"(mutation: (unknown" in before and b"MUTATION LOG" not in before
    assert b"MUTATION LOG" in live_report


@pytest.mark.parametrize("name", list(BUNDLES))
def test_the_bundle_holds_no_payloads_secrets_or_machine_paths(name):
    bundle = EXPERIMENTS / name
    for path in bundle.rglob("*"):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"(?i)\b[a-z]:[\\/]{1,2}(users|windows)|/c/users|appdata", text), path.name
        if path.name != "responses.yaml":  # authored from public server output; scanned for known shapes below
            assert redact_string(text) == text, f"secret-shaped value in {path.name}"
        assert not re.search(r"sk-[A-Za-z0-9_-]{20,}|[Bb]earer\s+[A-Za-z0-9\-_.=]{8,}", text), path.name
    for session in list((bundle / "corpus").glob("*.jsonl")) + [p for p in (bundle / "run").rglob("*.jsonl") if p.name != "mutations.jsonl"]:
        for line in session.read_text(encoding="utf-8").splitlines():
            assert "result" not in json.loads(line), f"payload recorded in {session.name}"
    assert not list(bundle.rglob("*.frames")), "raw frames must not be committed"
