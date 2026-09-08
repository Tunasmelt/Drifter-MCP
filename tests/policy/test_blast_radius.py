"""Tests for the blast-radius preview (F-31), docs/SPEC.md §10."""

from pathlib import Path

from mcp_drifter.record.schema import ToolDescriptor
from mcp_drifter.policy.blast_radius import compute_blast_radius, render_blast_radius

GOLDEN_FIXTURE = Path(__file__).parent.parent / "fixtures" / "golden_v0.1.jsonl"


def _tools_from_names(names: list[str]) -> list[ToolDescriptor]:
    return [ToolDescriptor(name=n, description="d", input_schema={}) for n in names]


def test_workflow_count_is_always_one():
    """drifter run's current scope: exactly one task, one operator (see
    this module's own docstring for why this isn't computed yet)."""
    preview = compute_blast_radius(GOLDEN_FIXTURE, _tools_from_names(["read_file"]), repeats=1)
    assert preview.workflow_count == 1


def test_planned_agent_runs_is_repeats_times_two_arms():
    preview = compute_blast_radius(GOLDEN_FIXTURE, _tools_from_names(["read_file"]), repeats=5)
    assert preview.planned_agent_runs == 10


def test_estimated_tool_calls_scales_with_the_fixtures_own_call_count_and_repeats():
    from mcp_drifter.record.reader import read_session
    from mcp_drifter.record.schema import ToolCall

    fixture_call_count = len([r for r in read_session(GOLDEN_FIXTURE) if isinstance(r, ToolCall)])
    assert fixture_call_count == 7  # matches the golden fixture's known, reviewed content

    preview = compute_blast_radius(GOLDEN_FIXTURE, _tools_from_names(["read_file"]), repeats=3)
    assert preview.estimated_tool_calls == fixture_call_count * 3 * 2  # 3 repeats, 2 arms


def test_risk_breakdown_sums_to_the_total_estimated_calls():
    preview = compute_blast_radius(GOLDEN_FIXTURE, _tools_from_names([]), repeats=2)
    assert sum(preview.estimated_calls_by_risk.values()) == preview.estimated_tool_calls


def test_a_tool_not_in_the_served_manifest_counts_as_unknown():
    """The fixture's real tool names aren't in this deliberately-empty
    manifest -- classify_manifest has no entry for them, so every call
    must fall into "unknown", not silently vanish from the breakdown."""
    preview = compute_blast_radius(GOLDEN_FIXTURE, _tools_from_names([]), repeats=1)
    assert preview.estimated_calls_by_risk["unknown"] == preview.estimated_tool_calls


def test_destructive_override_reclassifies_the_estimate():
    """A tool the fixture actually calls, forced into policy.destructive,
    must shift the estimate's destructive count up by exactly that tool's
    own per-run call frequency times the planned run count."""
    from mcp_drifter.record.reader import read_session
    from mcp_drifter.record.schema import ToolCall

    calls = [r for r in read_session(GOLDEN_FIXTURE) if isinstance(r, ToolCall)]
    target_tool = calls[0].tool_name
    per_run_frequency = sum(1 for c in calls if c.tool_name == target_tool)

    tools = _tools_from_names(sorted({c.tool_name for c in calls}))
    preview = compute_blast_radius(GOLDEN_FIXTURE, tools, repeats=1, destructive_override=[target_tool])
    assert preview.estimated_calls_by_risk["destructive"] == per_run_frequency * 2  # 1 repeat, 2 arms


def test_a_fixture_with_no_tool_calls_produces_an_all_zero_estimate(tmp_path):
    from mcp_drifter.record.schema import Environment, SessionStart

    path = tmp_path / "empty.jsonl"
    path.write_text(
        SessionStart(
            session_id="s", seq=0, started_at="2026-08-25T00:00:00Z", environment=Environment(), raw_frame_offset=0
        ).model_dump_json()
        + "\n",
        encoding="utf-8",
    )
    preview = compute_blast_radius(path, _tools_from_names([]), repeats=4)
    assert preview.estimated_tool_calls == 0
    assert sum(preview.estimated_calls_by_risk.values()) == 0


# --- render_blast_radius ------------------------------------------------------


def test_render_shows_the_header_line():
    preview = compute_blast_radius(GOLDEN_FIXTURE, _tools_from_names([]), repeats=1)
    output = render_blast_radius(preview)
    assert "Planned: 1 workflow(s)" in output
    assert "2 agent run(s)" in output


def test_render_omits_zero_count_risk_levels():
    preview = compute_blast_radius(GOLDEN_FIXTURE, _tools_from_names([]), repeats=1)
    output = render_blast_radius(preview)
    # every call classifies as "unknown" here (empty manifest) -- the other
    # five risk levels are all genuinely zero and must not clutter the line
    assert "0 read_only_local" not in output
    assert "0 destructive" not in output


def test_render_shows_unknown_explicitly_not_folded_into_a_safe_looking_bucket():
    """A real design decision, not SPEC.md's own terse mockup: an unknown-
    classified tool about to be called for real must never be silently
    absorbed into a bucket that reads as safe."""
    preview = compute_blast_radius(GOLDEN_FIXTURE, _tools_from_names([]), repeats=1)
    output = render_blast_radius(preview)
    assert "unknown" in output
