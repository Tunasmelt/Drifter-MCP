"""F-46: the environment fingerprint learns the auth type and the GRANTED scopes (docs/FEATURES.md F-46,
docs/SPEC.md §5.2 "Recording consequence" and §6 "Fingerprint evolution").

Written before the implementation, in the order CLAUDE.md requires for a new record field: a hand-built
PRE-CHANGE corpus first (what 0.2.0 wrote), proving old records keep their hashes and compare as before;
then the new fields. A server may list different tools depending on the scopes it granted, so two corpora
recorded under different grants must not be silently mixed.

Three states, kept apart on purpose (the second recurring bug pattern, docs/CLAUDE.md):
  - NONE      no auth fields at all: every pre-0.3.0 session, and every session against an unauthenticated
              server. Provably unauthenticated, so absent means `none`, not unknown.
  - KNOWN     `auth_type` set and `granted_scopes` a list, possibly EMPTY (a grant of no scopes).
  - UNKNOWN   `auth_type` set and `granted_scopes` null: an opaque credential (a static bearer token).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_drifter.record.fingerprint import build_environment, compute_fingerprint, diff_environments
from mcp_drifter.record.reader import read_session
from mcp_drifter.record.schema import Environment, SessionStart

GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "golden_v0.1.jsonl"

LEGACY = dict(
    agent_identity="claude-code/1.0.0",
    model_name=None,
    server_versions={"crm": "1.4.2"},
    tool_manifest_hash="sha256:aaaa",
)

# What 0.2.0 wrote for LEGACY, computed by hand from the documented formula and PINNED here as a literal: if
# the formula ever drifts for an unauthenticated session, this fails before any recorded corpus does.
LEGACY_FINGERPRINT = "sha256:" + __import__("hashlib").sha256(
    json.dumps(
        {
            "agent_identity": "claude-code/1.0.0",
            "model_name": None,
            "server_versions": {"crm": "1.4.2"},
            "tool_manifest_hash": "sha256:aaaa",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
).hexdigest()


def _env(**auth) -> Environment:
    return build_environment(**LEGACY, **auth)


# --- the pre-change corpus ------------------------------------------------------------------------------------


def test_the_golden_fixture_keeps_its_recorded_fingerprint_and_reads_as_unauthenticated():
    start = next(r for r in read_session(GOLDEN) if isinstance(r, SessionStart))
    env = start.environment
    assert (env.auth_type, env.granted_scopes) == (None, None)
    recomputed = compute_fingerprint(env.agent_identity, env.model_name, env.server_versions, env.tool_manifest_hash)
    assert recomputed == env.fingerprint == "sha256:f72c60c27f856c0fbf632c5c74671cc11108699f8f85f1c2e8a8e2aa33eb76eb"


def test_an_unauthenticated_fingerprint_is_byte_identical_to_the_0_2_0_formula():
    assert compute_fingerprint(**LEGACY) == LEGACY_FINGERPRINT
    assert _env().fingerprint == LEGACY_FINGERPRINT


def test_an_unauthenticated_environment_writes_no_auth_keys_at_all():
    """Absent, not null: a 0.3.0 recording of an unauthenticated server must be indistinguishable on disk from a 0.2.0 one."""
    written = json.loads(_env().model_dump_json())
    assert set(written) == {"agent_identity", "model_name", "server_versions", "tool_manifest_hash", "fingerprint"}
    start = SessionStart(session_id="s", seq=0, started_at="2026-09-15T00:00:00Z", environment=_env(), raw_frame_offset=0)
    assert "auth_type" not in start.model_dump_json() and "granted_scopes" not in start.model_dump_json()


def test_a_hand_built_0_2_0_record_reads_compares_and_hashes_as_before(tmp_path):
    legacy_line = json.dumps(
        {
            "schema_version": "0.1",
            "record_type": "session_start",
            "session_id": "legacy",
            "seq": 0,
            "started_at": "2026-09-15T00:00:00Z",
            "environment": {**LEGACY, "fingerprint": LEGACY_FINGERPRINT},
            "raw_frame_offset": 0,
        }
    )
    path = tmp_path / "legacy.jsonl"
    path.write_text(legacy_line + "\n", encoding="utf-8")
    (start,) = [r for r in read_session(path) if isinstance(r, SessionStart)]
    assert (start.environment.auth_type, start.environment.granted_scopes) == (None, None)
    # a new recording of the same unauthenticated environment is identical, so the two still compare and aggregate
    assert diff_environments(start.environment, _env()) == []
    assert start.environment.fingerprint == _env().fingerprint


def test_a_legacy_session_is_not_unknown_auth_it_compares_equal_to_a_new_unauthenticated_one():
    legacy = Environment(**LEGACY, fingerprint=LEGACY_FINGERPRINT)
    assert diff_environments(legacy, _env()) == []
    assert diff_environments(_env(), legacy) == []


def test_a_legacy_session_does_not_match_an_authenticated_one_and_the_difference_is_named():
    legacy = Environment(**LEGACY, fingerprint=LEGACY_FINGERPRINT)
    diffs = diff_environments(legacy, _env(auth_type="oauth", granted_scopes=["read"]))
    assert diffs and any("auth_type" in d and "none" in d and "oauth" in d for d in diffs), diffs


# --- the new fields -------------------------------------------------------------------------------------------


def test_the_granted_scopes_are_stored_sorted_and_unique_and_the_fingerprint_ignores_their_order():
    a = _env(auth_type="oauth", granted_scopes=["write", "read", "read"])
    b = _env(auth_type="oauth", granted_scopes=["read", "write"])
    assert a.granted_scopes == b.granted_scopes == ["read", "write"]
    assert a.fingerprint == b.fingerprint


def test_different_scopes_give_different_fingerprints_and_name_the_scopes():
    narrow = _env(auth_type="oauth", granted_scopes=["read"])
    wide = _env(auth_type="oauth", granted_scopes=["read", "admin"])
    assert narrow.fingerprint != wide.fingerprint
    (diff,) = diff_environments(narrow, wide)
    assert "granted_scopes" in diff and "['read']" in diff and "['admin', 'read']" in diff, diff


def test_the_same_scopes_under_a_different_auth_type_do_not_match():
    oauth = _env(auth_type="oauth", granted_scopes=["read"])
    service = _env(auth_type="client_credentials", granted_scopes=["read"])
    assert oauth.fingerprint != service.fingerprint
    assert any("auth_type" in d for d in diff_environments(oauth, service))


def test_the_three_states_have_three_different_fingerprints():
    none = _env()
    known_empty = _env(auth_type="oauth", granted_scopes=[])
    unknown = _env(auth_type="oauth", granted_scopes=None)
    known = _env(auth_type="oauth", granted_scopes=["read"])
    assert len({none.fingerprint, known_empty.fingerprint, unknown.fingerprint, known.fingerprint}) == 4


def test_an_unknown_grant_is_written_explicitly_as_null_and_an_empty_grant_as_an_empty_list():
    unknown = json.loads(_env(auth_type="bearer_env", granted_scopes=None).model_dump_json())
    assert unknown["auth_type"] == "bearer_env" and "granted_scopes" in unknown and unknown["granted_scopes"] is None
    empty = json.loads(_env(auth_type="oauth", granted_scopes=[]).model_dump_json())
    assert empty["granted_scopes"] == []


def test_a_grant_survives_a_write_and_read_round_trip_exactly(tmp_path):
    for env in (
        _env(auth_type="oauth", granted_scopes=["a", "b"]),
        _env(auth_type="oauth", granted_scopes=[]),
        _env(auth_type="bearer_env", granted_scopes=None),
        _env(),
    ):
        path = tmp_path / "s.jsonl"
        path.write_text(
            SessionStart(session_id="s", seq=0, started_at="2026-09-15T00:00:00Z", environment=env, raw_frame_offset=0).model_dump_json() + "\n",
            encoding="utf-8",
        )
        (back,) = [r for r in read_session(path) if isinstance(r, SessionStart)]
        assert (back.environment.auth_type, back.environment.granted_scopes, back.environment.fingerprint) == (
            env.auth_type, env.granted_scopes, env.fingerprint,
        )


def test_a_grant_without_an_auth_type_is_a_programming_error_not_a_silently_dropped_field():
    with pytest.raises(ValueError, match="auth_type"):
        build_environment(**LEGACY, auth_type=None, granted_scopes=["read"])


# --- comparison: unknown never silently matches a known grant ---------------------------------------------------


def test_an_unknown_grant_does_not_match_a_known_one_in_either_direction():
    unknown = _env(auth_type="oauth", granted_scopes=None)
    known = _env(auth_type="oauth", granted_scopes=["read"])
    for a, b in ((unknown, known), (known, unknown)):
        diffs = diff_environments(a, b)
        assert diffs and any("unknown" in d for d in diffs), diffs


def test_an_empty_grant_does_not_match_an_unknown_one():
    assert diff_environments(_env(auth_type="oauth", granted_scopes=[]), _env(auth_type="oauth", granted_scopes=None))


def test_two_unknown_grants_of_the_same_type_compare_as_they_always_did():
    """Two sessions that both used an opaque static bearer token share the same blindness; blocking every
    bearer_env comparison forever would make the feature unusable. Documented as a limitation (SPEC §15)."""
    a = _env(auth_type="bearer_env", granted_scopes=None)
    b = _env(auth_type="bearer_env", granted_scopes=None)
    assert diff_environments(a, b) == []


def test_the_grant_is_compared_even_when_the_tool_manifest_is_exempt():
    """`compare_manifest=False` compares a baseline arm with a MUTATED arm; a mutation changes the manifest,
    never the grant, so a grant difference still invalidates the comparison."""
    narrow = _env(auth_type="oauth", granted_scopes=["read"])
    wide = _env(auth_type="oauth", granted_scopes=["read", "admin"])
    assert diff_environments(narrow, wide, compare_manifest=False)
