"""F-46: the recorder stamps the auth type and the GRANTED scopes into SessionStart (docs/FEATURES.md F-46).

Written before the implementation. The timing is the thing to get right (CLAUDE.md's recurring bug pattern:
plausible-but-wrong values from ordering): SessionStart is written lazily, at the first tools/list or
tools/call, and the grant is only known once the credential has been exchanged, so it is READ at that moment,
not captured at construction. Every assertion is on an exact value.

`record/` must not import `auth/` (replay depends on `record/`, and replay may not depend on auth), so the
recorder takes plain values: an auth type string and a zero-argument callable returning the scope names.
"""

from __future__ import annotations

import json
from pathlib import Path

from mcp_drifter.record.reader import read_session
from mcp_drifter.record.schema import SessionStart
from mcp_drifter.record.writer import SessionRecorder
from test_writer import _initialize, _tool_call, _tools_list


def _start(tmp_path: Path) -> SessionStart:
    (path,) = sorted((tmp_path / "runs").glob("*.jsonl"))
    return next(r for r in read_session(path) if isinstance(r, SessionStart))


def _raw_start(tmp_path: Path) -> dict:
    (path,) = sorted((tmp_path / "runs").glob("*.jsonl"))
    return json.loads(path.read_text(encoding="utf-8").splitlines()[0])


def _run(tmp_path: Path, **kwargs) -> SessionRecorder:
    recorder = SessionRecorder(session_dir=tmp_path / "runs", raw_dir=tmp_path / "raw", server_name="fake", **kwargs)
    _initialize(recorder)
    _tools_list(recorder, 1, [{"name": "t", "description": "d", "inputSchema": {"type": "object"}}])
    _tool_call(recorder, 2, "t", {})
    recorder.close()
    return recorder


def test_an_unauthenticated_recording_writes_no_auth_fields(tmp_path):
    _run(tmp_path)
    environment = _raw_start(tmp_path)["environment"]
    assert "auth_type" not in environment and "granted_scopes" not in environment


def test_a_known_grant_is_recorded_sorted_with_its_auth_type(tmp_path):
    _run(tmp_path, auth_type="oauth", granted_scopes=lambda: ["write", "read"])
    env = _start(tmp_path).environment
    assert (env.auth_type, env.granted_scopes) == ("oauth", ["read", "write"])


def test_the_grant_is_read_when_session_start_is_written_not_when_the_recorder_is_built(tmp_path):
    state = {"scopes": None}  # nothing granted yet when the recorder is created: no credential exchanged
    recorder = SessionRecorder(
        session_dir=tmp_path / "runs", raw_dir=tmp_path / "raw", server_name="fake",
        auth_type="client_credentials", granted_scopes=lambda: state["scopes"],
    )
    state["scopes"] = ["early"]
    _initialize(recorder)
    state["scopes"] = ["read"]  # the token arrives before the first tools/list is answered
    _tools_list(recorder, 1, [{"name": "t", "description": "d", "inputSchema": {"type": "object"}}])
    state["scopes"] = ["changed-later"]  # a later refresh must not rewrite what the session started with
    _tool_call(recorder, 2, "t", {})
    recorder.close()
    assert _start(tmp_path).environment.granted_scopes == ["read"]


def test_a_static_bearer_token_is_an_unknown_grant_written_as_an_explicit_null(tmp_path):
    _run(tmp_path, auth_type="bearer_env")
    environment = _raw_start(tmp_path)["environment"]
    assert environment["auth_type"] == "bearer_env" and environment["granted_scopes"] is None


def test_an_empty_grant_is_not_an_unknown_one(tmp_path):
    _run(tmp_path, auth_type="oauth", granted_scopes=lambda: [])
    environment = _raw_start(tmp_path)["environment"]
    assert environment["granted_scopes"] == []


def test_a_grant_callback_that_fails_never_breaks_the_recording_and_records_the_grant_as_unknown(tmp_path):
    def broken():
        raise RuntimeError("the credential is not ready")

    _run(tmp_path, auth_type="oauth", granted_scopes=broken)
    env = _start(tmp_path).environment
    assert (env.auth_type, env.granted_scopes) == ("oauth", None)


def test_the_fingerprint_matches_what_the_comparison_code_computes(tmp_path):
    from mcp_drifter.record.fingerprint import compute_fingerprint

    _run(tmp_path, auth_type="oauth", granted_scopes=lambda: ["b", "a"])
    env = _start(tmp_path).environment
    assert env.tool_manifest_hash is not None
    assert env.fingerprint == compute_fingerprint(
        env.agent_identity, env.model_name, env.server_versions, env.tool_manifest_hash, "oauth", ["a", "b"]
    )


def test_no_secret_shaped_value_reaches_the_record(tmp_path):
    """Only the type and the scope NAMES are ever written: a scope list is not a place for a token."""
    _run(tmp_path, auth_type="oauth", granted_scopes=lambda: ["read"])
    text = next((tmp_path / "runs").glob("*.jsonl")).read_text(encoding="utf-8")
    assert "Bearer" not in text and "access_token" not in text and "client_secret" not in text
