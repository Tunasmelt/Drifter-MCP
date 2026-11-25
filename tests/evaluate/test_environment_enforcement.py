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


# --- F-46: the grant is part of the environment --------------------------------------------------------------
# A server may list different tools under different scopes, so sessions recorded under different grants are
# different experiments. Written before the cohort key and the comparison learned about auth.


def _authed(dir_path: Path, sid: str, **auth) -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    env = build_environment("claude-code/1.0", None, {"drifter-replay-srv": ""}, "sha256:m", **auth)
    lines = [
        SessionStart(session_id=sid, seq=0, started_at="2026-09-15T00:00:00Z", environment=env, raw_frame_offset=0).model_dump_json(),
        ToolCall(session_id=sid, seq=1, timestamp="2026-09-15T00:00:01Z", server="srv", tool_name="a", arguments={},
                 result_shape={"type": "object"}, is_error=False, duration_ms=1.0, fault=False, raw_frame_offset=1).model_dump_json(),
    ]
    path = dir_path / f"{sid}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_sessions_recorded_under_a_different_grant_are_excluded_from_the_arm_naming_the_scopes(tmp_path):
    same = [_authed(tmp_path, f"s{i}", auth_type="oauth", granted_scopes=["read"]) for i in range(3)]
    wide = _authed(tmp_path, "wide", auth_type="oauth", granted_scopes=["read", "admin"])

    result = aggregate_baseline_runs("t", [*same, wide])

    assert result.valid_runs == 3
    (excluded,) = result.excluded_runs
    assert excluded.session_id == "wide" and "environment differs" in excluded.reason
    assert "granted_scopes: ['admin', 'read'] != ['read']" in excluded.reason, excluded.reason


def test_scope_order_does_not_split_a_cohort(tmp_path):
    paths = [
        _authed(tmp_path, "a", auth_type="oauth", granted_scopes=["read", "write"]),
        _authed(tmp_path, "b", auth_type="oauth", granted_scopes=["write", "read"]),
        _authed(tmp_path, "c", auth_type="oauth", granted_scopes=["read", "write", "read"]),
    ]
    result = aggregate_baseline_runs("t", paths)
    assert result.valid_runs == 3 and result.excluded_runs == []


def test_legacy_and_new_unauthenticated_sessions_share_one_cohort(tmp_path):
    legacy = [_session(tmp_path, f"old{i}") for i in range(2)]  # built without auth: byte-for-byte what 0.2.0 wrote
    new = [_authed(tmp_path, f"new{i}") for i in range(2)]  # `_authed` with no auth kwargs: a 0.3.0 unauthenticated run
    result = aggregate_baseline_runs("t", [*legacy, *new])
    assert result.valid_runs == 4 and result.excluded_runs == []


def test_an_unauthenticated_session_does_not_join_an_authenticated_cohort(tmp_path):
    authed = [_authed(tmp_path, f"a{i}", auth_type="oauth", granted_scopes=["read"]) for i in range(3)]
    bare = _authed(tmp_path, "bare")
    result = aggregate_baseline_runs("t", [*authed, bare])
    assert result.valid_runs == 3
    assert "auth_type: 'none' != 'oauth'" in result.excluded_runs[0].reason, result.excluded_runs[0].reason


def test_an_unknown_grant_does_not_join_a_known_cohort_and_says_unknown(tmp_path):
    known = [_authed(tmp_path, f"k{i}", auth_type="bearer_env", granted_scopes=["read"]) for i in range(3)]
    unknown = _authed(tmp_path, "u", auth_type="bearer_env", granted_scopes=None)
    result = aggregate_baseline_runs("t", [*known, unknown])
    assert result.valid_runs == 3
    assert "granted_scopes: unknown != ['read']" in result.excluded_runs[0].reason, result.excluded_runs[0].reason


def _auth_arm(tmp_path: Path, name: str, n: int = 20, **auth):
    return aggregate_baseline_runs(name, [_authed(tmp_path / name, f"{name}{i}", **auth) for i in range(n)])


def test_arms_recorded_under_different_grants_cannot_be_compared(tmp_path):
    baseline = _auth_arm(tmp_path, "base", auth_type="oauth", granted_scopes=["read"])
    mutated = _auth_arm(tmp_path, "mut", auth_type="oauth", granted_scopes=["read", "admin"])
    effect = compute_behavior_effect_size(baseline, mutated)
    assert effect.verdict == "UNKNOWN"
    assert "different environments" in effect.reason and "granted_scopes" in effect.reason, effect.reason


def test_arms_under_the_same_grant_still_compare(tmp_path):
    baseline = _auth_arm(tmp_path, "base", auth_type="oauth", granted_scopes=["read"])
    mutated = _auth_arm(tmp_path, "mut", auth_type="oauth", granted_scopes=["read"])
    assert compute_behavior_effect_size(baseline, mutated).verdict == "NO_REGRESSION"
