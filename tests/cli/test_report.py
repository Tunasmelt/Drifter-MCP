"""Tests for `drifter report` (F-36's own second half) — re-rendering a
prior `drifter run`'s full report from stored sessions, zero new execution.

Mirrors `test_score.py`'s established pattern: a structural no-live-
connection guarantee (checked by AST, not trusted from the docstring),
plus building/rendering a report from real, on-disk session directories
matching `cli/run.py`'s own `session_dir` layout exactly.
"""

import ast
import io
import json
import sys
from pathlib import Path

import pytest

from cli.config import ConfigError, PolicyConfig
from cli.report import build_report_result, run_report
from cli.report_format import render_run_result
from record.reader import read_session
from record.schema import Environment, SessionStart, ToolCall, ToolDescriptor, ToolsList

GOLDEN_FIXTURE = Path(__file__).parent.parent / "fixtures" / "golden_v0.1.jsonl"
SCRIPTED_AGENT = Path(__file__).parent.parent / "fixtures" / "scripted_agent.py"
GOLDEN_SERVER = "filesystem"


def _write_session(dir_path: Path, session_id: str, tool_names: list[str], tool_manifest_hash: str | None = "h") -> Path:
    """Writes a real ToolsList (every distinct tool_names entry, once each)
    alongside the ToolCalls -- policy.safety.evaluate_safety_for_session
    classifies against `ToolsList.tools_served`, so a call to a tool that
    was never listed there is unclassifiable by construction (correctly --
    see test_a_call_to_a_tool_missing_from_the_served_manifest_does_not_crash
    in tests/policy/test_safety.py for that as its own, separate, intended
    behavior). Real fixture data needs the manifest present to exercise the
    classification path meaningfully.
    """
    dir_path.mkdir(parents=True, exist_ok=True)
    served = [ToolDescriptor(name=n, description="d", input_schema={}) for n in sorted(set(tool_names))]
    lines = [
        SessionStart(
            session_id=session_id, seq=0, started_at="2026-08-25T00:00:00Z",
            environment=Environment(tool_manifest_hash=tool_manifest_hash), raw_frame_offset=0,
        ).model_dump_json(),
        ToolsList(
            session_id=session_id, seq=1, timestamp="2026-08-25T00:00:00Z", server="fake",
            tools_raw=served, tools_served=served, raw_frame_offset=1,
        ).model_dump_json(),
    ]
    for i, tool_name in enumerate(tool_names, start=2):
        lines.append(
            ToolCall(
                session_id=session_id, seq=i, timestamp="2026-08-25T00:00:01Z", server="fake",
                tool_name=tool_name, arguments={}, result_shape={"type": "object", "keys": []},
                is_error=False, duration_ms=1.0, fault=False, raw_frame_offset=i * 100,
            ).model_dump_json()
        )
    path = dir_path / f"{session_id}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# --- structural no-live-connection guarantee, for BOTH new modules ----------


@pytest.mark.parametrize("filename", ["report.py", "report_format.py"])
def test_module_imports_nothing_that_could_reach_a_live_connection(filename):
    """Same discipline as cli/score.py's own guarantee, applied to both new
    F-36 modules: parse the file's own AST and confirm none of its imports
    come from a module that could spawn a process or open a network/MCP
    connection. `cli/report_format.py` gets the SAME check as `cli/
    report.py` itself, not just the latter -- report.py's own cleanliness
    is worthless if the module it depends on for RunResult/render_run_result
    secretly isn't clean too.
    """
    source = (Path(__file__).parent.parent.parent / "src" / "cli" / filename).read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported_modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    forbidden_prefixes = ("mcp.client", "mcp.server", "subprocess", "anyio", "socket", "urllib", "http", "requests")
    violations = [m for m in imported_modules if m.startswith(forbidden_prefixes)]
    assert violations == [], f"cli/{filename} imports something that could reach a live connection: {violations}"

    # Belt-and-suspenders, specific to the actual refactor this guards
    # against: report.py must never import cli.run directly (that's the
    # one path that WOULD transitively reach cli.subprocess_adapter despite
    # neither name appearing in the forbidden-prefix list above).
    assert "cli.run" not in imported_modules, f"cli/{filename} imports cli.run, which is not execution-free"


# --- build_report_result: reconstructing a RunResult from disk --------------


def test_build_report_result_raises_actionable_error_for_an_unrun_task(tmp_path):
    with pytest.raises(ConfigError, match="no recorded `drifter run`"):
        build_report_result("never_run", tmp_path)


def test_build_report_result_reconstructs_a_clean_no_regression_report(tmp_path):
    session_dir = tmp_path / "run" / "my_task"
    for i in range(3):
        _write_session(session_dir / "baseline", f"b{i}", ["a", "b"])
        _write_session(session_dir / "mutated", f"m{i}", ["a", "b"])

    result = build_report_result("my_task", tmp_path)

    assert result.task_id == "my_task"
    assert result.baseline.valid_runs == 3
    assert result.mutated.valid_runs == 3
    assert result.effect.verdict == "NO_REGRESSION"
    assert result.safety.verdict == "NO_VIOLATION"
    assert result.mutation_log == []  # genuinely not reconstructable, see module docstring


def test_build_report_result_reconstructs_a_real_safety_violation(tmp_path):
    session_dir = tmp_path / "run" / "risky_task"
    _write_session(session_dir / "baseline", "b0", ["get_customer"])
    _write_session(session_dir / "mutated", "m0", ["get_customer"])

    result = build_report_result("risky_task", tmp_path, policy=PolicyConfig(destructive=["get_customer"]))

    assert result.safety.verdict == "VIOLATION"
    assert any(f.tool_name == "get_customer" for f in result.safety.findings)


def test_build_report_result_handles_a_missing_arm_gracefully(tmp_path):
    """Only a baseline arm exists (e.g. the mutated arm crashed before
    writing anything real) -- must not crash, and must honestly report the
    mutated arm as having zero valid runs, not silently skip it."""
    session_dir = tmp_path / "run" / "partial_task"
    _write_session(session_dir / "baseline", "b0", ["a"])
    (session_dir / "mutated").mkdir(parents=True)  # exists but empty

    result = build_report_result("partial_task", tmp_path)
    assert result.baseline.valid_runs == 1
    assert result.mutated.has_data is False


def test_render_run_result_output_matches_what_a_real_drifter_run_would_show(tmp_path):
    """The actual point of F-36: this must be indistinguishable in shape
    from drifter run's own live report -- same render_run_result call."""
    session_dir = tmp_path / "run" / "shape_task"
    _write_session(session_dir / "baseline", "b0", ["a"])
    _write_session(session_dir / "mutated", "m0", ["a"])

    result = build_report_result("shape_task", tmp_path)
    output = render_run_result(result)

    assert "DRIFTER RUN — shape_task" in output
    assert "BASELINE  1/1 valid runs" in output
    assert "BEHAVIOR  NO_REGRESSION" in output
    assert "SAFETY    NO VIOLATION" in output
    assert "MUTATION LOG:" not in output  # genuinely nothing to show


# --- run_report: CLI wrapper ---------------------------------------------


def test_run_report_uses_explicit_runs_dir_bypassing_config(tmp_path):
    session_dir = tmp_path / "run" / "cli_task"
    _write_session(session_dir / "baseline", "b0", ["a"])
    _write_session(session_dir / "mutated", "m0", ["a"])

    out = io.StringIO()
    run_report(runs_dir=tmp_path, task_id="cli_task", output_stream=out)
    output = out.getvalue()
    assert "cli_task" in output
    assert "BEHAVIOR" in output


def test_run_report_raises_actionable_config_error_for_an_unrun_task(tmp_path):
    with pytest.raises(ConfigError, match="never_run_task"):
        run_report(runs_dir=tmp_path, task_id="never_run_task", output_stream=io.StringIO())


# --- real end-to-end: reconstructs an ACTUAL drifter run's output -----------


def test_report_reconstructs_the_same_verdict_a_real_drifter_run_produced(tmp_path):
    """The real integration point: run a genuine drifter run (real
    replay-served agent, real recorded sessions), then confirm drifter
    report reconstructs the identical verdict from those same sessions
    alone, with zero new agent execution."""
    from cli.run import run_mutation_comparison

    calls = [r for r in read_session(GOLDEN_FIXTURE) if isinstance(r, ToolCall)][:2]

    def _spec(c: ToolCall) -> str:
        return f"{c.tool_name}|{json.dumps(c.arguments)}"

    command = [sys.executable, str(SCRIPTED_AGENT), *(_spec(c) for c in calls)]

    live_result = run_mutation_comparison(
        task_id="reconstruct_task",
        prompt="",
        fixture_path=GOLDEN_FIXTURE,
        server_name=GOLDEN_SERVER,
        agent_command=command,
        operator="description_update",
        session_dir=tmp_path / "runs" / "run" / "reconstruct_task",
        raw_dir=tmp_path / "raw",
        repeats=1,
        timeout_s=30.0,
    )

    reconstructed = build_report_result("reconstruct_task", tmp_path / "runs")

    assert reconstructed.effect.verdict == live_result.effect.verdict
    assert reconstructed.baseline.dominant_path == live_result.baseline.dominant_path
    assert reconstructed.mutated.dominant_path == live_result.mutated.dominant_path
    assert reconstructed.safety.verdict == live_result.safety.verdict


@pytest.fixture
def anyio_backend():
    return "asyncio"
