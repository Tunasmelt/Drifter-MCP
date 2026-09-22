"""docs/SPEC.md §15 limitations 21/22: the report must show adaptation.

E2's report read `authored_fixture 100%` in both arms, identical to a
mutation with no effect, while every mutated call had in fact used the renamed
parameter and resolved at the `inverse` tier. Content provenance and request
match tier are different facts, so they are counted and rendered separately.
"""

from pathlib import Path

from mcp_drifter.cli.report_format import RunResult, render_run_result
from mcp_drifter.evaluate.baseline import BaselineResult, aggregate_baseline_runs
from mcp_drifter.evaluate.effect_size import EffectSizeResult
from mcp_drifter.mutate.description_update import MutationLogEntry
from mcp_drifter.policy.safety import SafetyResult
from mcp_drifter.record.schema import Environment, SessionStart, ToolCall, ToolDescriptor, ToolsList


def _session(dir_path: Path, sid: str, calls: list[dict]) -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    tool = ToolDescriptor(name="convert_time", description="d", input_schema={})
    records = [
        SessionStart(session_id=sid, seq=0, started_at="2026-09-14T00:00:00Z",
                     environment=Environment(tool_manifest_hash="h"), raw_frame_offset=0),
        ToolsList(session_id=sid, seq=1, timestamp="2026-09-14T00:00:00Z", server="time",
                  tools_raw=[tool], tools_served=[tool], raw_frame_offset=1),
    ]
    for i, call in enumerate(calls, start=2):
        records.append(ToolCall(
            session_id=sid, seq=i, timestamp="2026-09-14T00:00:01Z", server="time", tool_name="convert_time",
            arguments={}, result_shape={"type": "object"}, is_error=False, duration_ms=1.0,
            raw_frame_offset=i, **call,
        ))
    path = dir_path / f"{sid}.jsonl"
    path.write_text("\n".join(r.model_dump_json() for r in records) + "\n", encoding="utf-8")
    return path


def test_match_tier_is_counted_independently_of_content_provenance(tmp_path):
    paths = [
        _session(tmp_path, "m0", [
            {"fault": False, "match_tier": "inverse", "result_provenance": "authored_fixture"},
            {"fault": False, "match_tier": "exact", "result_provenance": "authored_fixture"},
        ]),
        # 3 hits + 1 miss = coverage 0.75, above the 0.70 floor, so the run is valid.
        _session(tmp_path, "m1", [
            {"fault": False, "match_tier": None, "result_provenance": "real"},  # pre-field hit == exact
            {"fault": False, "match_tier": None, "result_provenance": "real"},
            {"fault": False, "match_tier": "exact", "result_provenance": "real"},
            {"fault": True, "match_tier": None, "result_provenance": "real"},  # a miss has no tier
        ]),
    ]

    result = aggregate_baseline_runs("t", paths)

    assert result.valid_runs == 2
    assert result.provenance_breakdown["authored_fixture"] == 2
    assert result.match_tier_breakdown == {"exact": 4, "inverse": 1, "semantic": 0}


def test_match_tier_breakdown_is_none_without_valid_runs(tmp_path):
    path = _session(tmp_path, "x", [{"fault": True, "match_tier": None, "result_provenance": "real"}])
    assert aggregate_baseline_runs("t", [path]).match_tier_breakdown is None


def _arm(tiers: dict[str, int]) -> BaselineResult:
    return BaselineResult(
        task_id="t", total_runs=4, valid_runs=4, dominant_path=("convert_time",),
        variant_frequencies={("convert_time",): 4}, natural_variation=0.0, baseline_spread=0.0,
        baseline_fidelity=1.0, excluded_runs=[],
        provenance_breakdown={"authored_fixture": 4}, match_tier_breakdown=tiers,
    )


def _render(baseline_tiers: dict[str, int], mutated_tiers: dict[str, int]) -> str:
    return render_run_result(RunResult(
        task_id="t", operator="parameter_rename",
        baseline=_arm(baseline_tiers), mutated=_arm(mutated_tiers),
        effect=EffectSizeResult(deviation_rate=0.0, effect_size=0.0, verdict="NO_REGRESSION"),
        mutation_log=[MutationLogEntry(
            tool_name="convert_time", operator="parameter_rename",
            before="source_timezone", after="sourceTimezone",
            inverse={"sourceTimezone": "source_timezone"}, seed=42,
            injection_flagged=False,
        )], safety=SafetyResult(verdict="NO_VIOLATION", findings=()),
    ))


def test_the_report_shows_an_adapted_mutated_arm():
    output = _render({"exact": 4, "inverse": 0, "semantic": 0}, {"exact": 0, "inverse": 4, "semantic": 0})

    assert "REQUEST MATCH  baseline exact 100%" in output
    assert "mutated  inverse 100%" in output
    assert "4 of 4 mutated call(s) used the mutation's renamed arguments (inverse tier)" in output
    assert "matched without translation" not in output


def test_a_partly_inverse_arm_explains_the_exact_share():
    """E2: `find_order` was never renamed, so half the mutated calls were exact."""
    output = _render({"exact": 8, "inverse": 0, "semantic": 0}, {"exact": 4, "inverse": 4, "semantic": 0})

    assert "4 of 8 mutated call(s) used the mutation's renamed arguments (inverse tier)" in output
    assert "the other 4 matched without translation" in output


def test_no_adaptation_note_when_the_mutated_arm_matched_the_old_contract():
    """The git probe case: a renamed parameter the agent never sent, or an
    old name a permissive schema accepted. Nothing adapted; say nothing."""
    output = _render({"exact": 3, "inverse": 0, "semantic": 0}, {"exact": 3, "inverse": 0, "semantic": 0})

    assert "mutated  exact 100%" in output
    assert "renamed arguments" not in output
    assert "did not exercise any renamed argument" in output


def test_a_semantic_tier_hit_is_flagged_as_exploratory():
    """docs/PHASES.md R3: semantic matching hashes the multiset of argument
    VALUES ignoring parameter names -- against any served schema with
    additionalProperties:false (the enforced default since the
    parameter_rename fix), it is unreachable by construction, and even where
    reachable it can bind two schema-valid calls that carry the same values
    in different semantic roles. A report showing a real semantic hit must
    say so is exploratory, not present it as an ordinary tier alongside
    exact/inverse."""
    output = _render({"exact": 4, "inverse": 0, "semantic": 0}, {"exact": 3, "inverse": 0, "semantic": 1})

    assert "mutated  exact 75% · semantic 25%" in output
    assert "semantic" in output.lower() and "exploratory" in output.lower()


def test_no_semantic_warning_when_no_semantic_hit_occurred():
    output = _render({"exact": 4, "inverse": 0, "semantic": 0}, {"exact": 4, "inverse": 0, "semantic": 0})
    assert "exploratory" not in output.lower()
