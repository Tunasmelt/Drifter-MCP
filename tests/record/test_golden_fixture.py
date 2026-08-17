"""Golden fixture test (PHASES.md Gate 1 exit test: "Golden fixture parses
cleanly in CI").

`tests/fixtures/golden_v0.1.jsonl` is one hand-verified session, recorded
via the real `drifter observe` path (SessionRecorder + the real MCP proxy,
not hand-typed JSON) against the real `@modelcontextprotocol/server-
filesystem` package — the actual Gate 1 dogfood server — pointed at a
purpose-built scratch directory rather than the author's real Desktop, so
nothing personal ever entered a permanently-committed file. Reviewed
record-by-record before committing (see CHANGELOG.md).

This file is never modified in place (CLAUDE.md, PHASES.md): a future
schema change produces `golden_v0.2.jsonl` alongside it, with both tested,
never a same-file overwrite.
"""

from pathlib import Path

from record.reader import (
    SchemaVersionError,
    UnknownRecordTypeError,
    read_session,
)
from record.schema import SCHEMA_VERSION, SessionStart, ToolCall, ToolsList, TrajectoryEnd

GOLDEN_FIXTURE = Path(__file__).parent.parent / "fixtures" / "golden_v0.1.jsonl"


def test_golden_fixture_exists():
    assert GOLDEN_FIXTURE.is_file()


def test_golden_fixture_parses_every_record_without_error():
    # The exit test's literal wording: every record must parse. A raised
    # SchemaVersionError/UnknownRecordTypeError/pydantic ValidationError
    # anywhere in the file fails this test outright — no try/except here
    # is deliberate, since read_session is a generator and the failure
    # needs to surface exactly like it would in CI.
    records = list(read_session(GOLDEN_FIXTURE))
    assert len(records) == 10
    for record in records:
        assert record.schema_version == SCHEMA_VERSION


def test_golden_fixture_seq_is_contiguous_from_zero():
    records = list(read_session(GOLDEN_FIXTURE))
    assert [r.seq for r in records] == list(range(len(records)))


def test_golden_fixture_has_every_gate_1_record_type():
    records = list(read_session(GOLDEN_FIXTURE))
    by_type = {type(r) for r in records}
    assert by_type == {SessionStart, ToolsList, ToolCall, TrajectoryEnd}

    assert isinstance(records[0], SessionStart)  # always first, per writer.py's contract
    assert sum(isinstance(r, ToolCall) for r in records) == 7
    assert sum(isinstance(r, ToolsList) for r in records) == 1
    assert sum(isinstance(r, TrajectoryEnd) for r in records) == 1


def test_golden_fixture_session_start_environment_fully_populated():
    session_start = next(r for r in read_session(GOLDEN_FIXTURE) if isinstance(r, SessionStart))
    env = session_start.environment
    assert env.agent_identity == "claude-code/1.0.0"
    assert env.server_versions == {"secure-filesystem-server": "0.2.0"}
    assert env.tool_manifest_hash is not None and env.tool_manifest_hash.startswith("sha256:")
    assert env.fingerprint is not None and env.fingerprint.startswith("sha256:")


def test_golden_fixture_tools_list_matches_the_real_filesystem_server():
    tools_list = next(r for r in read_session(GOLDEN_FIXTURE) if isinstance(r, ToolsList))
    served_names = {t.name for t in tools_list.tools_served}
    # The real @modelcontextprotocol/server-filesystem tool surface at
    # recording time — not a guess, this is what tools/list actually
    # returned (verified by hand before this fixture was committed).
    assert served_names == {
        "read_file", "read_text_file", "read_media_file", "read_multiple_files",
        "write_file", "edit_file", "create_directory", "list_directory",
        "list_directory_with_sizes", "directory_tree", "move_file",
        "search_files", "get_file_info", "list_allowed_directories",
    }
    assert tools_list.tools_raw == tools_list.tools_served  # no mutate/ yet


def test_golden_fixture_demonstrates_a_data_flow_reference():
    # F-08: at least one call's argument was traced back to an earlier
    # call's result (search_files -> read_text_file, a genuine dependency
    # that arose naturally during recording, not staged).
    calls = [r for r in read_session(GOLDEN_FIXTURE) if isinstance(r, ToolCall)]
    referencing = [c for c in calls if c.references]
    assert referencing
    ref = referencing[0].references[0]
    assert ref.source_seq == 3
    assert ref.target_path == "$.path"


def test_golden_fixture_demonstrates_a_genuine_tool_error():
    # A real is_error: true (reading a file that doesn't exist), not a
    # planted/synthetic one.
    calls = [r for r in read_session(GOLDEN_FIXTURE) if isinstance(r, ToolCall)]
    errored = [c for c in calls if c.is_error]
    assert len(errored) == 1
    assert errored[0].tool_name == "read_text_file"
    assert errored[0].result_shape == {"type": "object", "keys": ["content", "isError"], "array_lengths": {"content": 1}}


def test_golden_fixture_every_call_has_known_fault_status():
    # Recorded on the current schema (post CHANGELOG v1.0.10) — every call
    # has a definite fault value, never the backward-compat None a
    # pre-v1.0.10 recording would show. None of these seven calls actually
    # faulted (a real protocol fault is meant to be rare, and forcing one
    # into a "real recorded session" fixture would make it not real).
    calls = [r for r in read_session(GOLDEN_FIXTURE) if isinstance(r, ToolCall)]
    assert all(c.fault is False for c in calls)


def test_golden_fixture_trajectory_end_covers_every_call():
    records = list(read_session(GOLDEN_FIXTURE))
    trajectory_end = next(r for r in records if isinstance(r, TrajectoryEnd))
    call_seqs = [r.seq for r in records if isinstance(r, ToolCall)]
    assert trajectory_end.call_seqs == call_seqs
    assert trajectory_end.segmentation_method == "heuristic"  # no _meta.traceparent from this client


def test_golden_fixture_arguments_contain_no_secret_placeholder():
    # Sanity check, not a redaction re-test (tests/record/test_redaction.py
    # owns that): this fixture was built from safe scratch content with no
    # planted secrets, so nothing should have been redacted in the first
    # place. A "[REDACTED]"-shaped value here would mean either a real
    # secret leaked into a scratch file used for recording (should never
    # happen) or a false-positive redaction distorting the fixture.
    calls = [r for r in read_session(GOLDEN_FIXTURE) if isinstance(r, ToolCall)]
    for call in calls:
        for value in call.arguments.values():
            if isinstance(value, str):
                assert "REDACTED" not in value.upper()


def test_golden_fixture_rejects_a_bad_schema_version(tmp_path):
    # Confirms read_session's guard actually fires against this fixture's
    # own shape — not just tested in isolation elsewhere (test_schema.py).
    bad = tmp_path / "bad.jsonl"
    first_line = GOLDEN_FIXTURE.read_text(encoding="utf-8").splitlines()[0]
    bad.write_text(first_line.replace('"schema_version":"0.1"', '"schema_version":"9.9"') + "\n", encoding="utf-8")
    try:
        list(read_session(bad))
        assert False, "expected SchemaVersionError"
    except SchemaVersionError:
        pass


def test_golden_fixture_rejects_an_unknown_record_type(tmp_path):
    bad = tmp_path / "bad.jsonl"
    first_line = GOLDEN_FIXTURE.read_text(encoding="utf-8").splitlines()[0]
    bad.write_text(first_line.replace('"record_type":"session_start"', '"record_type":"not_a_real_type"') + "\n", encoding="utf-8")
    try:
        list(read_session(bad))
        assert False, "expected UnknownRecordTypeError"
    except UnknownRecordTypeError:
        pass
