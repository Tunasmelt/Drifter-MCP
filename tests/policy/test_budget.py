"""Tests for budget and hard limits (F-32), docs/SPEC.md §11/§13."""

from pathlib import Path

import pytest

from mcp_drifter.evaluate.baseline import run_baseline
from mcp_drifter.policy.budget import BudgetExceededError, BudgetTracker, budget_limited
from mcp_drifter.record.schema import Environment, SessionStart, ToolCall


def _write_session_with_n_calls(dir_path: Path, session_id: str, n_calls: int) -> Path:
    lines = [
        SessionStart(
            session_id=session_id, seq=0, started_at="2026-08-25T00:00:00Z",
            environment=Environment(tool_manifest_hash="h"), raw_frame_offset=0,
        ).model_dump_json()
    ]
    for i in range(1, n_calls + 1):
        lines.append(
            ToolCall(
                session_id=session_id, seq=i, timestamp="2026-08-25T00:00:01Z", server="fake",
                tool_name="a", arguments={}, result_shape={"type": "object", "keys": []},
                is_error=False, duration_ms=1.0, fault=False, raw_frame_offset=i * 10,
            ).model_dump_json()
        )
    path = dir_path / f"{session_id}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# --- BudgetTracker ------------------------------------------------------------


def test_no_limits_set_never_raises(tmp_path):
    tracker = BudgetTracker()
    tracker.check()  # must not raise
    tracker.record(_write_session_with_n_calls(tmp_path, "s1", 100))
    tracker.check()  # still must not raise, however much was spent


def test_tool_call_budget_exhausted_raises(tmp_path):
    tracker = BudgetTracker(max_tool_calls=5)
    tracker.record(_write_session_with_n_calls(tmp_path, "s1", 5))
    with pytest.raises(BudgetExceededError, match="tool-call budget exhausted: 5/5"):
        tracker.check()


def test_tool_call_budget_not_yet_exhausted_does_not_raise(tmp_path):
    tracker = BudgetTracker(max_tool_calls=10)
    tracker.record(_write_session_with_n_calls(tmp_path, "s1", 5))
    tracker.check()  # 5/10 spent -- must not raise


def test_wall_time_budget_exhausted_raises():
    tracker = BudgetTracker(max_wall_time_s=0.0)  # already "elapsed" the instant it's checked
    with pytest.raises(BudgetExceededError, match="wall-time budget exhausted"):
        tracker.check()


def test_record_accumulates_across_multiple_calls(tmp_path):
    tracker = BudgetTracker()
    tracker.record(_write_session_with_n_calls(tmp_path, "s1", 3))
    tracker.record(_write_session_with_n_calls(tmp_path, "s2", 4))
    assert tracker.spent_tool_calls == 7


# --- budget_limited -----------------------------------------------------------


def test_budget_limited_wraps_run_once_and_records_its_calls(tmp_path):
    tracker = BudgetTracker(max_tool_calls=100)
    paths = iter([_write_session_with_n_calls(tmp_path, "s1", 3)])
    wrapped = budget_limited(lambda: next(paths), tracker)

    result = wrapped()
    assert result.name == "s1.jsonl"
    assert tracker.spent_tool_calls == 3


def test_budget_limited_raises_before_calling_run_once_once_exhausted(tmp_path):
    """The core "stops starting NEW work" guarantee: once the budget is
    spent, the underlying run_once must never even be invoked -- confirmed
    by counting real invocations, not just checking the final tally."""
    call_count = 0

    def real_run_once() -> Path:
        nonlocal call_count
        call_count += 1
        return _write_session_with_n_calls(tmp_path, f"s{call_count}", 5)

    tracker = BudgetTracker(max_tool_calls=5)
    wrapped = budget_limited(real_run_once, tracker)

    wrapped()  # spends the whole budget (5/5)
    assert call_count == 1

    with pytest.raises(BudgetExceededError):
        wrapped()
    assert call_count == 1  # NOT incremented -- real_run_once was never called the second time


# --- end-to-end with run_baseline: partial results, matching F-32's own "Done when" --


def test_a_budget_that_runs_out_mid_baseline_still_produces_a_partial_report(tmp_path):
    """F-32's own docs/FEATURES.md 'Done when' bar: a run exceeding budget stops
    cleanly mid-execution and still produces a report on the partial data
    collected. Each real run makes 4 tool calls; a budget of 10 is checked
    BEFORE each repeat starts (module docstring's own documented shape), so
    the 3rd repeat still runs (8 < 10 at the time it starts, pushing spend
    to 12) before the 4th and 5th are skipped outright."""
    call_index = 0

    def run_once() -> Path:
        nonlocal call_index
        call_index += 1
        return _write_session_with_n_calls(tmp_path, f"run{call_index}", 4)

    tracker = BudgetTracker(max_tool_calls=10)
    wrapped = budget_limited(run_once, tracker)

    result = run_baseline("budget_task", wrapped, repeats=5)

    assert call_index == 3  # only 3 of 5 repeats ever actually ran
    assert result.has_data is True
    assert result.valid_runs == 3
    assert len(result.excluded_runs) == 2
    assert all("budget exhausted" in e.reason for e in result.excluded_runs)
