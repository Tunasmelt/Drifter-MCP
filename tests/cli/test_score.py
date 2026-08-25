"""Tests for `drifter score` (F-36) — Gate 2's actual exit test.

Unit coverage for the CLI wrapper: config/--runs-dir resolution (reusing
cli.stats.resolve_runs_dir, not reinventing it), output formatting (the
N/A-not-zero and em-dash conventions already established for `drifter
stats`), and the documented "no task-grouping key exists" limitation
behaving exactly as stated (every session in the directory aggregated
as one group). Plus a structural check that this module can never reach
a live connection — same pattern as replay_proxy.py's and
subprocess_adapter.py's own no-live-connection guarantees.

aggregate_baseline_runs itself (the actual analysis logic) is already
thoroughly tested in tests/evaluate/test_baseline.py — not re-tested
here beyond confirming score.py wires it up and renders its output.
"""

import ast
import io
from pathlib import Path

import pytest

from cli.score import render_score, run_score
from evaluate.baseline import aggregate_baseline_runs
from record.schema import Environment, SessionStart, ToolCall

GOLDEN_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures"


def _write_session(dir_path: Path, session_id: str, tool_names: list[str], tool_manifest_hash: str | None = "h") -> Path:
    lines = [
        SessionStart(
            session_id=session_id,
            seq=0,
            started_at="2026-08-25T00:00:00Z",
            environment=Environment(tool_manifest_hash=tool_manifest_hash),
            raw_frame_offset=0,
        ).model_dump_json()
    ]
    for i, tool_name in enumerate(tool_names, start=1):
        lines.append(
            ToolCall(
                session_id=session_id,
                seq=i,
                timestamp="2026-08-25T00:00:01Z",
                server="fake",
                tool_name=tool_name,
                arguments={},
                result_shape={"type": "object", "keys": []},
                is_error=False,
                duration_ms=1.0,
                fault=False,
                raw_frame_offset=i * 100,
            ).model_dump_json()
        )
    path = dir_path / f"{session_id}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# --- structural no-live-connection guarantee -------------------------------


def test_score_module_imports_nothing_that_could_reach_a_live_connection():
    """Same discipline as replay_proxy.py's and subprocess_adapter.py's
    own guarantees: parse cli/score.py's own AST and confirm none of its
    imports come from a module that could spawn a process or open a
    network/MCP connection. Checked by inspecting the actual file's
    imports, not by trusting the module docstring's claim.
    """
    source = (Path(__file__).parent.parent.parent / "src" / "cli" / "score.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported_modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    forbidden_prefixes = ("mcp.client", "mcp.server", "subprocess", "anyio", "socket", "urllib", "http", "requests")
    violations = [m for m in imported_modules if m.startswith(forbidden_prefixes)]
    assert violations == [], f"cli/score.py imports something that could reach a live connection: {violations}"


# --- output rendering: N/A discipline, excluded reasons --------------------


def test_render_score_shows_na_not_zero_when_no_valid_sessions(tmp_path):
    bad_path = _write_session(tmp_path, "s_null", ["a"], tool_manifest_hash=None)
    result = aggregate_baseline_runs("task", [bad_path])
    output = render_score(result, tmp_path)

    assert "N/A" in output
    assert "dominant_path:      N/A" in output
    # Never render the no-data case as if it were a real zero/empty answer.
    assert "0.000" not in output


def test_render_score_shows_real_values_when_data_exists(tmp_path):
    paths = [_write_session(tmp_path, f"s{i}", ["a", "b"]) for i in range(3)]
    result = aggregate_baseline_runs("task", paths)
    output = render_score(result, tmp_path)

    assert "a → b" in output
    assert "natural_variation:  0.000" in output
    assert "baseline_fidelity:  1.000" in output


def test_render_score_lists_excluded_runs_with_reasons(tmp_path):
    good = _write_session(tmp_path, "s_good", ["a"])
    bad = _write_session(tmp_path, "s_null", ["a"], tool_manifest_hash=None)
    result = aggregate_baseline_runs("task", [good, bad])
    output = render_score(result, tmp_path)

    assert "EXCLUDED RUNS:" in output
    assert "s_null" in output
    assert "tool_manifest_hash is null" in output


def test_render_score_reports_no_grouping_limitation():
    """The documented scope limitation must actually be visible in the
    output, not just in source comments -- a user running this command
    should see it, not have to read cli/score.py to learn it."""
    result = aggregate_baseline_runs("task", [])
    output = render_score(result, Path("."))
    assert "no task-grouping key exists" in output


def test_multiple_unrelated_task_sessions_are_aggregated_as_one_group(tmp_path):
    """Explicit confirmation of the stated scope: two sessions that are
    (in reality) two entirely different tasks -- different tool-call
    shapes -- still land in ONE aggregate group, because nothing on disk
    distinguishes them. This is the documented limitation behaving
    exactly as described, not a bug."""
    task_a = _write_session(tmp_path, "s_task_a", ["search", "get_customer"])
    task_b = _write_session(tmp_path, "s_task_b", ["list_directory"])

    result = aggregate_baseline_runs("(all sessions)", [task_a, task_b])

    assert result.total_runs == 2
    assert result.valid_runs == 2
    # Both distinct paths show up as separate variants of the SAME group
    # -- never split into two separate BaselineResults, since there is
    # no signal available to split on.
    assert len(result.variant_frequencies) == 2
    assert ("search", "get_customer") in result.variant_frequencies
    assert ("list_directory",) in result.variant_frequencies


# --- config / --runs-dir resolution -----------------------------------------


def test_run_score_uses_explicit_runs_dir_bypassing_config(tmp_path):
    _write_session(tmp_path, "s1", ["a"])
    out = io.StringIO()
    run_score(runs_dir=tmp_path, output_stream=out)
    output = out.getvalue()
    assert "sessions: 1" in output


def test_run_score_against_empty_directory_does_not_crash(tmp_path):
    out = io.StringIO()
    run_score(runs_dir=tmp_path, output_stream=out)
    assert "No sessions found." in out.getvalue()


def test_run_score_against_nonexistent_directory_does_not_crash(tmp_path):
    out = io.StringIO()
    run_score(runs_dir=tmp_path / "does_not_exist", output_stream=out)
    assert "No sessions found." in out.getvalue()


# --- real fixture smoke test -------------------------------------------------


def test_run_score_against_the_golden_fixture_directory():
    """The actual exit-test shape (PHASES.md), run as a test: an
    already-recorded, already-on-disk session, zero new execution."""
    out = io.StringIO()
    run_score(runs_dir=GOLDEN_FIXTURE_DIR, output_stream=out)
    output = out.getvalue()
    assert "sessions: 1" in output
    assert "valid: 1" in output


@pytest.fixture
def anyio_backend():
    return "asyncio"
