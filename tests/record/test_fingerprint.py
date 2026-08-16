"""Unit tests for record/fingerprint.py (F-05).

FEATURES.md's "Done when": a mismatched fingerprint between baseline and
mutation arms blocks comparison with an explicit error, not a silent wrong
answer. Every mismatch test below checks the error names the specific
sub-field that changed — a bare "fingerprints don't match" wouldn't satisfy
SPEC.md's stated requirement.
"""

import pytest

from record.fingerprint import (
    FingerprintMismatchError,
    build_environment,
    compute_fingerprint,
    compute_tool_manifest_hash,
    diff_environments,
    require_matching_environments,
)
from record.schema import SessionStart

BASE_KWARGS = dict(
    agent_identity="claude-code/1.0",
    model_name="claude-sonnet-5",
    server_versions={"crm": "1.4.2"},
    tool_manifest_hash="sha256:aaaa",
)


def _session(seq: int, session_id: str, **env_overrides) -> SessionStart:
    kwargs = {**BASE_KWARGS, **env_overrides}
    return SessionStart(
        session_id=session_id,
        seq=seq,
        started_at="2026-08-16T00:00:00Z",
        environment=build_environment(**kwargs),
        raw_frame_offset=0,
    )


def test_compute_fingerprint_is_deterministic_regardless_of_dict_order():
    a = compute_fingerprint("agent", "model", {"crm": "1.0", "billing": "2.0"}, "sha256:xx")
    b = compute_fingerprint("agent", "model", {"billing": "2.0", "crm": "1.0"}, "sha256:xx")
    assert a == b


def test_compute_fingerprint_changes_when_any_component_changes():
    base = compute_fingerprint("agent", "model", {"crm": "1.0"}, "sha256:xx")
    assert compute_fingerprint("other-agent", "model", {"crm": "1.0"}, "sha256:xx") != base
    assert compute_fingerprint("agent", "other-model", {"crm": "1.0"}, "sha256:xx") != base
    assert compute_fingerprint("agent", "model", {"crm": "2.0"}, "sha256:xx") != base
    assert compute_fingerprint("agent", "model", {"crm": "1.0"}, "sha256:yy") != base


def test_tool_manifest_hash_is_order_independent():
    tools_a = [{"name": "add"}, {"name": "echo"}]
    tools_b = [{"name": "echo"}, {"name": "add"}]
    assert compute_tool_manifest_hash(tools_a) == compute_tool_manifest_hash(tools_b)


def test_matching_environments_produce_no_diff_and_do_not_raise():
    a = _session(0, "sess-a")
    b = _session(0, "sess-b")
    assert diff_environments(a.environment, b.environment) == []
    require_matching_environments(a, b)  # must not raise


def test_mismatched_tool_manifest_hash_blocks_comparison_with_specific_error():
    baseline = _session(0, "baseline", tool_manifest_hash="sha256:aaaa")
    mutated = _session(0, "mutated", tool_manifest_hash="sha256:bbbb")

    with pytest.raises(FingerprintMismatchError) as exc_info:
        require_matching_environments(baseline, mutated)

    message = str(exc_info.value)
    assert "tool_manifest_hash" in message
    assert "sha256:aaaa" in message
    assert "sha256:bbbb" in message
    # Must name what differed, not just declare a mismatch.
    assert message != "fingerprints don't match"


def test_diff_names_model_mismatch_specifically():
    a = _session(0, "a", model_name="claude-sonnet-5")
    b = _session(0, "b", model_name="claude-opus-5")
    diffs = diff_environments(a.environment, b.environment)
    assert any("model_name" in d and "claude-sonnet-5" in d and "claude-opus-5" in d for d in diffs)
    assert not any("tool_manifest_hash" in d for d in diffs)  # unrelated field, no false alarm


def test_diff_names_server_version_mismatch_specifically():
    a = _session(0, "a", server_versions={"crm": "1.4.2"})
    b = _session(0, "b", server_versions={"crm": "2.0.0"})
    diffs = diff_environments(a.environment, b.environment)
    assert any("crm" in d and "1.4.2" in d and "2.0.0" in d for d in diffs)


def test_multiple_mismatches_are_all_named():
    a = _session(0, "a", model_name="claude-sonnet-5", tool_manifest_hash="sha256:aaaa")
    b = _session(0, "b", model_name="claude-opus-5", tool_manifest_hash="sha256:bbbb")
    diffs = diff_environments(a.environment, b.environment)
    assert len(diffs) == 2
    assert any("model_name" in d for d in diffs)
    assert any("tool_manifest_hash" in d for d in diffs)
