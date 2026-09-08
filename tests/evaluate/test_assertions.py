"""Tests for the task assertion engine (F-24), docs/SPEC.md §8's Task axis.

The axis reported UNKNOWN unconditionally from Gate 3 until this existed.
The single most important behavior here is the one CLAUDE.md names
non-negotiable: a verdict defaults to UNKNOWN, never PASS, when no
assertion is configured.
"""

from pathlib import Path

import pytest

from mcp_drifter.evaluate.assertions import TaskAssertions, evaluate_run, evaluate_task
from mcp_drifter.record.schema import Environment, SessionStart, ToolCall, ToolDescriptor, ToolsList


def _records(calls: list[tuple[str, dict, bool]]):
    """(tool_name, result_shape, is_error) triples as real ToolCall records."""
    out = [
        SessionStart(
            session_id="s", seq=0, started_at="2026-08-25T00:00:00Z",
            environment=Environment(tool_manifest_hash="h"), raw_frame_offset=0,
        )
    ]
    for i, (tool_name, shape, is_error) in enumerate(calls, start=1):
        out.append(
            ToolCall(
                session_id="s", seq=i, timestamp="2026-08-25T00:00:01Z", server="srv",
                tool_name=tool_name, arguments={}, result_shape=shape,
                is_error=is_error, duration_ms=1.0, fault=False, raw_frame_offset=i * 100,
            )
        )
    return out


def _write_session(dir_path: Path, session_id: str, tool_names: list[str], is_error: bool = False) -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    served = [ToolDescriptor(name=n, description="d", input_schema={}) for n in sorted(set(tool_names))]
    lines = [
        SessionStart(
            session_id=session_id, seq=0, started_at="2026-08-25T00:00:00Z",
            environment=Environment(tool_manifest_hash="h"), raw_frame_offset=0,
        ).model_dump_json(),
        ToolsList(
            session_id=session_id, seq=1, timestamp="2026-08-25T00:00:00Z", server="srv",
            tools_raw=served, tools_served=served, raw_frame_offset=1,
        ).model_dump_json(),
    ]
    for i, tool_name in enumerate(tool_names, start=2):
        lines.append(
            ToolCall(
                session_id=session_id, seq=i, timestamp="2026-08-25T00:00:01Z", server="srv",
                tool_name=tool_name, arguments={}, result_shape={"type": "object", "keys": ["content"]},
                is_error=is_error, duration_ms=1.0, fault=False, raw_frame_offset=i * 100,
            ).model_dump_json()
        )
    path = dir_path / f"{session_id}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# --- the non-negotiable ------------------------------------------------------


def test_no_assertions_configured_is_unknown_never_pass(tmp_path):
    """CLAUDE.md names this the single easiest way to make Drifter quietly
    untrustworthy: a task with no oracle must not report success."""
    path = _write_session(tmp_path, "a", ["anything"])

    result = evaluate_task([path], TaskAssertions())

    assert result.verdict == "UNKNOWN"
    assert result.verdict != "PASS"
    assert "no assertions configured" in result.reason


def test_assertions_configured_but_no_valid_runs_is_unknown_not_pass(tmp_path):
    """Vacuous truth is the other way to accidentally report success:
    "every one of zero runs passed" is arithmetically true and completely
    wrong as a verdict."""
    result = evaluate_task([], TaskAssertions(calls=("search",)))

    assert result.verdict == "UNKNOWN"
    assert "no valid run survived" in result.reason


# --- calls / never_calls / calls_before -------------------------------------


def test_calls_passes_when_the_tool_was_called():
    records = _records([("search", {}, False), ("create", {}, False)])
    assert evaluate_run(records, TaskAssertions(calls=("search",))).passed is True


def test_calls_fails_with_an_actionable_message_when_it_was_not():
    records = _records([("other", {}, False)])
    result = evaluate_run(records, TaskAssertions(calls=("search",)))

    assert result.passed is False
    assert result.failures[0].kind == "calls"
    assert "never called" in result.failures[0].detail


def test_never_calls_fails_and_reports_how_many_times():
    records = _records([("delete", {}, False), ("delete", {}, False)])
    result = evaluate_run(records, TaskAssertions(never_calls=("delete",)))

    assert result.passed is False
    assert "2x" in result.failures[0].detail


def test_never_calls_passes_when_the_tool_is_absent():
    records = _records([("safe", {}, False)])
    assert evaluate_run(records, TaskAssertions(never_calls=("delete",))).passed is True


def test_calls_before_passes_in_the_right_order():
    records = _records([("search", {}, False), ("create", {}, False)])
    assert evaluate_run(records, TaskAssertions(calls_before=(("search", "create"),))).passed is True


def test_calls_before_fails_in_the_wrong_order():
    records = _records([("create", {}, False), ("search", {}, False)])
    result = evaluate_run(records, TaskAssertions(calls_before=(("search", "create"),)))

    assert result.passed is False
    assert "called first" in result.failures[0].detail


def test_calls_before_fails_rather_than_vacuously_passing_when_a_tool_is_absent():
    """An ordering claim about a tool that was never called is not
    satisfied — treating it as vacuously true would report PASS for a run
    that never did the work."""
    records = _records([("search", {}, False)])
    result = evaluate_run(records, TaskAssertions(calls_before=(("search", "create"),)))

    assert result.passed is False
    assert "'create'" in result.failures[0].detail


def test_calls_before_uses_first_occurrence_not_adjacency():
    """The workflow question is "searched at some point before creating",
    not "searched immediately before creating"."""
    records = _records([("search", {}, False), ("unrelated", {}, False), ("create", {}, False)])
    assert evaluate_run(records, TaskAssertions(calls_before=(("search", "create"),))).passed is True


# --- the recordable substitutes for result_contains -------------------------


def test_result_has_keys_checks_the_recorded_shape():
    records = _records([("search", {"type": "object", "keys": ["content", "meta"]}, False)])
    assert evaluate_run(records, TaskAssertions(result_has_keys={"search": ("content",)})).passed is True


def test_result_has_keys_fails_and_names_the_missing_key():
    records = _records([("search", {"type": "object", "keys": ["meta"]}, False)])
    result = evaluate_run(records, TaskAssertions(result_has_keys={"search": ("content",)}))

    assert result.passed is False
    assert "content" in result.failures[0].detail


def test_result_has_keys_fails_when_the_tool_was_never_called():
    records = _records([("other", {"type": "object", "keys": []}, False)])
    result = evaluate_run(records, TaskAssertions(result_has_keys={"search": ("content",)}))
    assert result.passed is False
    assert "never called" in result.failures[0].detail


def test_no_errors_fails_on_a_recorded_is_error():
    records = _records([("search", {}, True)])
    result = evaluate_run(records, TaskAssertions(no_errors=True))

    assert result.passed is False
    assert result.failures[0].kind == "no_errors"


def test_no_errors_treats_unknown_is_error_as_not_an_error():
    """`is_error is None` means "recorded before the field existed, or the
    call faulted before producing a result at all" -- genuinely unknown,
    which is not evidence of an error. Same nullable-field discipline the
    rest of this project applies."""
    records = _records([("search", {}, False)])
    records[1] = records[1].model_copy(update={"is_error": None})

    assert evaluate_run(records, TaskAssertions(no_errors=True)).passed is True


# --- aggregation across an arm's runs ---------------------------------------


def test_all_runs_passing_is_pass(tmp_path):
    paths = [_write_session(tmp_path, f"s{i}", ["search", "create"]) for i in range(3)]

    result = evaluate_task(paths, TaskAssertions(calls=("search",)))

    assert result.verdict == "PASS"
    assert (result.runs_passed, result.runs_evaluated) == (3, 3)


def test_any_run_failing_is_fail_with_the_counts_visible(tmp_path):
    """An assertion is a deterministic oracle, not a statistical estimate:
    one run genuinely violating it is a real finding. The counts are always
    reported so a reader can weigh how widespread it was."""
    paths = [
        _write_session(tmp_path, "good1", ["search"]),
        _write_session(tmp_path, "good2", ["search"]),
        _write_session(tmp_path, "bad", ["other"]),
    ]

    result = evaluate_task(paths, TaskAssertions(calls=("search",)))

    assert result.verdict == "FAIL"
    assert (result.runs_passed, result.runs_evaluated) == (2, 3)


def test_identical_failures_across_runs_are_reported_once(tmp_path):
    """Ten runs failing the same assertion is one finding, not ten lines of
    the same sentence."""
    paths = [_write_session(tmp_path, f"s{i}", ["other"]) for i in range(4)]

    result = evaluate_task(paths, TaskAssertions(calls=("search",)))

    assert result.verdict == "FAIL"
    assert len(result.failures) == 1


def test_multiple_distinct_assertions_can_fail_together():
    records = _records([("delete", {}, True)])
    result = evaluate_run(
        records, TaskAssertions(calls=("search",), never_calls=("delete",), no_errors=True)
    )

    assert result.passed is False
    assert {f.kind for f in result.failures} == {"calls", "never_calls", "no_errors"}


def test_a_single_run_is_enough_for_a_verdict_unlike_the_behavior_axis(tmp_path):
    """Deliberately NOT subject to effect_size.py's minimum-evidence gate:
    that gate exists because natural_variation/baseline_spread are
    statistical estimates meaningless at n=1. A deterministic assertion on
    one real trajectory is a real result."""
    path = _write_session(tmp_path, "only", ["delete"])

    result = evaluate_task([path], TaskAssertions(never_calls=("delete",)))

    assert result.verdict == "FAIL"
    assert result.runs_evaluated == 1
