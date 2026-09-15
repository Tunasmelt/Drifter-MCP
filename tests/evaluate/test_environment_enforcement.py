"""docs/PHASES.md R2 / docs/SPEC.md §15 limitation 18 open finding: environment
fingerprints were recorded but never enforced.

Written before implementation and confirmed failing first.

Rule: within one arm, sessions must share agent identity, model name and server
versions (and the manifest hash), or the odd ones are excluded with the
differing fields named. Across arms, the same fields must match or the Behavior
verdict is UNKNOWN; only the tool manifest may differ, because that difference
IS the mutation.
"""

from __future__ import annotations

from pathlib import Path

from mcp_drifter.evaluate.baseline import aggregate_baseline_runs
from mcp_drifter.evaluate.effect_size import compute_behavior_effect_size
from mcp_drifter.record.fingerprint import build_environment
from mcp_drifter.record.schema import SessionStart, ToolCall


def _session(dir_path: Path, sid: str, *, agent="claude-code/1.0", model=None, servers=None, manifest="sha256:m",
             tools=("a",)) -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    env = build_environment(agent, model, servers or {"drifter-replay-srv": ""}, manifest)
    lines = [SessionStart(session_id=sid, seq=0, started_at="2026-09-15T00:00:00Z", environment=env, raw_frame_offset=0).model_dump_json()]
    for i, name in enumerate(tools, start=1):
        lines.append(ToolCall(session_id=sid, seq=i, timestamp="2026-09-15T00:00:01Z", server="srv", tool_name=name,
                              arguments={}, result_shape={"type": "object"}, is_error=False, duration_ms=1.0,
                              fault=False, raw_frame_offset=i).model_dump_json())
    path = dir_path / f"{sid}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# --- within an arm ------------------------------------------------------------------


def test_a_session_from_a_different_agent_version_is_excluded_from_the_arm(tmp_path):
    same = [_session(tmp_path, f"s{i}") for i in range(3)]
    odd = _session(tmp_path, "odd", agent="claude-code/2.0")

    result = aggregate_baseline_runs("t", [*same, odd])

    assert result.valid_runs == 3
    (excluded,) = result.excluded_runs
    assert excluded.session_id == "odd"
    assert "environment differs" in excluded.reason
    assert "agent_identity: 'claude-code/2.0' != 'claude-code/1.0'" in excluded.reason


def test_model_and_server_version_differences_are_also_excluded(tmp_path):
    same = [_session(tmp_path, f"s{i}") for i in range(3)]
    model = _session(tmp_path, "model", model="other-model")
    server = _session(tmp_path, "server", servers={"drifter-replay-srv": "9"})

    result = aggregate_baseline_runs("t", [*same, model, server])

    assert result.valid_runs == 3
    reasons = {e.session_id: e.reason for e in result.excluded_runs}
    assert "model_name" in reasons["model"]
    assert "server 'drifter-replay-srv' version" in reasons["server"]


def test_the_reference_is_the_most_common_environment_not_the_first_session(tmp_path):
    first_odd = _session(tmp_path, "a_odd", agent="claude-code/2.0")
    majority = [_session(tmp_path, f"b{i}") for i in range(3)]

    result = aggregate_baseline_runs("t", [first_odd, *majority])

    assert result.valid_runs == 3
    assert [e.session_id for e in result.excluded_runs] == ["a_odd"]
    assert result.reference_environment.agent_identity == "claude-code/1.0"


def test_a_manifest_difference_within_an_arm_is_also_excluded(tmp_path):
    """Within one arm every session was served the same manifest; a different
    hash means a different experiment's session got mixed in."""
    same = [_session(tmp_path, f"s{i}") for i in range(3)]
    other = _session(tmp_path, "other", manifest="sha256:different")

    result = aggregate_baseline_runs("t", [*same, other])

    assert result.valid_runs == 3
    assert "tool_manifest_hash" in result.excluded_runs[0].reason


def test_a_consistent_arm_is_unchanged(tmp_path):
    paths = [_session(tmp_path, f"s{i}") for i in range(4)]
    result = aggregate_baseline_runs("t", paths)
    assert result.valid_runs == 4 and result.excluded_runs == []


# --- across arms ---------------------------------------------------------------------


def _arm(tmp_path: Path, name: str, n: int = 20, **env) -> object:
    return aggregate_baseline_runs(name, [_session(tmp_path / name, f"{name}{i}", **env) for i in range(n)])


def test_arms_from_different_agents_cannot_be_compared(tmp_path):
    baseline = _arm(tmp_path, "base")
    mutated = _arm(tmp_path, "mut", agent="claude-code/2.0", manifest="sha256:mutated")

    effect = compute_behavior_effect_size(baseline, mutated)

    assert effect.verdict == "UNKNOWN"
    assert "different environments" in effect.reason
    assert "agent_identity" in effect.reason
    assert "tool_manifest_hash" not in effect.reason  # the mutation's own difference is not a mismatch


def test_the_manifest_difference_the_mutation_introduces_is_permitted(tmp_path):
    baseline = _arm(tmp_path, "base")
    mutated = _arm(tmp_path, "mut", manifest="sha256:mutated")

    effect = compute_behavior_effect_size(baseline, mutated)

    assert effect.verdict == "NO_REGRESSION"
