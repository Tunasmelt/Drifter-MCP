"""Tests for `drifter run` (F-35) — Gate 3's actual orchestration
command, run against the real golden fixture and the same scripted
test-agent used throughout this gate, not new fixtures.

Unit coverage for the CLI wrapper (config resolution, report
rendering) plus two real end-to-end runs (one per operator) through
the full stack: ReplayStore -> run_replay_proxy -> run_agent_subprocess
-> run_baseline (both arms) -> mutation operator -> effect-size
comparison. The individual pieces are already thoroughly tested
elsewhere (tests/cli/test_baseline_integration.py,
tests/replay/test_replay_proxy.py, tests/mutate/); the point here is
confirming they compose correctly end to end via cli/run.py itself.
"""

import io
import json
import sys
from pathlib import Path

import pytest

from cli.config import ConfigError
from cli.run import (
    RunResult,
    _template_command,
    render_run_result,
    run_mutation_comparison,
    run_run,
)
from evaluate.baseline import BaselineResult
from evaluate.effect_size import EffectSizeResult
from mutate.tool_addition import add_tool
from record.reader import read_session
from record.schema import ToolCall
from replay.replay_proxy import tools_served_from_session

GOLDEN_FIXTURE = Path(__file__).parent.parent / "fixtures" / "golden_v0.1.jsonl"
SCRIPTED_AGENT = Path(__file__).parent.parent / "fixtures" / "scripted_agent.py"
GOLDEN_SERVER = "filesystem"


def _golden_calls() -> list[ToolCall]:
    return [r for r in read_session(GOLDEN_FIXTURE) if isinstance(r, ToolCall)]


def _spec(tool_name: str, arguments: dict) -> str:
    return f"{tool_name}|{json.dumps(arguments)}"


# --- unit: templating, rendering --------------------------------------------


def test_template_command_substitutes_task_prompt_per_token():
    command = ["python", "agent.py", "--task", "{task.prompt}"]
    result = _template_command(command, "do the thing")
    assert result == ["python", "agent.py", "--task", "do the thing"]


def test_render_run_result_shows_task_axis_as_unknown_unconditionally():
    """No assertion engine exists (F-24 depends on F-30, task
    definitions, not built) -- TASK must always read UNKNOWN, never
    silently omitted or defaulted to something that looks like a real
    verdict."""
    baseline = BaselineResult(
        task_id="t", total_runs=1, valid_runs=1, dominant_path=("a",),
        variant_frequencies={("a",): 1}, natural_variation=0.0, baseline_spread=0.0,
        baseline_fidelity=1.0, excluded_runs=[],
    )
    result = RunResult(
        task_id="t", operator="description_update", baseline=baseline, mutated=baseline,
        effect=EffectSizeResult(deviation_rate=0.0, effect_size=0.0, verdict="NO_REGRESSION"),
        mutation_log=[],
    )
    output = render_run_result(result)
    assert "TASK      UNKNOWN — no oracle configured" in output


def test_render_run_result_handles_unknown_behavior_verdict_without_crashing():
    empty = BaselineResult(
        task_id="t", total_runs=1, valid_runs=0, dominant_path=None,
        variant_frequencies={}, natural_variation=None, baseline_spread=None,
        baseline_fidelity=None, excluded_runs=[],
    )
    result = RunResult(
        task_id="t", operator="description_update", baseline=empty, mutated=empty,
        effect=EffectSizeResult(deviation_rate=None, effect_size=None, verdict="UNKNOWN"),
        mutation_log=[],
    )
    output = render_run_result(result)
    assert "BEHAVIOR  UNKNOWN" in output
    assert "N/A" in output


# --- run_run: config resolution / actionable errors -------------------------


def _write_config(tmp_path, text):
    path = tmp_path / "drifter.yaml"
    path.write_text(text, encoding="utf-8")
    return path


VALID_YAML_NO_AGENT = """
version: 1
servers:
  - name: filesystem
    command: ["echo", "unused-in-replay-mode"]
"""


def test_run_run_raises_actionable_error_when_agent_block_missing(tmp_path):
    config_path = _write_config(tmp_path, VALID_YAML_NO_AGENT)
    out = io.StringIO()
    with pytest.raises(ConfigError, match="agent:"):
        run_run(config_path=config_path, fixture_path=GOLDEN_FIXTURE, server_name=GOLDEN_SERVER, output_stream=out)


def test_run_run_raises_actionable_error_when_fixture_missing(tmp_path):
    text = VALID_YAML_NO_AGENT + '\nagent:\n  command: ["echo", "hi"]\n'
    config_path = _write_config(tmp_path, text)
    out = io.StringIO()
    with pytest.raises(ConfigError, match="fixture"):
        run_run(config_path=config_path, server_name=GOLDEN_SERVER, output_stream=out)


def test_run_run_raises_actionable_error_when_server_missing(tmp_path):
    text = VALID_YAML_NO_AGENT + '\nagent:\n  command: ["echo", "hi"]\n'
    config_path = _write_config(tmp_path, text)
    out = io.StringIO()
    with pytest.raises(ConfigError, match="server"):
        run_run(config_path=config_path, fixture_path=GOLDEN_FIXTURE, output_stream=out)


def test_run_run_end_to_end_via_real_config(tmp_path):
    """The one test driving the actual public entry point (run_run),
    not just run_mutation_comparison directly -- confirms config
    loading, agent.command extraction, and output rendering all
    compose correctly, not just the orchestration core in isolation."""
    calls = _golden_calls()[:2]
    command_json = json.dumps([sys.executable, str(SCRIPTED_AGENT), *(_spec(c.tool_name, c.arguments) for c in calls)])
    text = VALID_YAML_NO_AGENT + f"\nagent:\n  command: {command_json}\n"
    config_path = _write_config(tmp_path, text)

    out = io.StringIO()
    run_run(
        config_path=config_path,
        fixture_path=GOLDEN_FIXTURE,
        server_name=GOLDEN_SERVER,
        task_id="via_config",
        operator="description_update",
        runs_dir=tmp_path / "runs",
        repeats=1,
        timeout_s=30.0,
        output_stream=out,
    )
    output = out.getvalue()
    assert "via_config" in output
    assert "BEHAVIOR" in output


# --- real end-to-end: description_update ------------------------------------


def test_run_mutation_comparison_description_update_end_to_end(tmp_path):
    """Same tool names/arguments in both arms (description_update never
    touches names/schema) -- exact-tier replay hits identically in both,
    so this is expected, correctly-detected NO_REGRESSION, not a weak
    test: it confirms the whole pipeline (both arms, the operator, the
    comparison) actually works end to end against real data."""
    calls = _golden_calls()[:3]
    command = [sys.executable, str(SCRIPTED_AGENT), *(_spec(c.tool_name, c.arguments) for c in calls)]

    result = run_mutation_comparison(
        task_id="desc_update_task",
        prompt="",
        fixture_path=GOLDEN_FIXTURE,
        server_name=GOLDEN_SERVER,
        agent_command=command,
        operator="description_update",
        session_dir=tmp_path / "runs",
        raw_dir=tmp_path / "raw",
        repeats=2,
        timeout_s=30.0,
    )

    assert result.baseline.has_data is True
    assert result.mutated.has_data is True
    expected_path = tuple(c.tool_name for c in calls)
    assert result.baseline.dominant_path == expected_path
    assert result.mutated.dominant_path == expected_path
    assert result.effect.verdict == "NO_REGRESSION"
    assert result.effect.deviation_rate == 0.0
    assert len(result.mutation_log) == len(tools_served_from_session(GOLDEN_FIXTURE))

    output = render_run_result(result)
    assert "NO_REGRESSION" in output
    assert "desc_update_task" in output


# --- real end-to-end: tool_addition ------------------------------------------


def test_run_mutation_comparison_tool_addition_end_to_end(tmp_path):
    """The injected tool's name is deterministic given (siblings, seed)
    -- predicted here with the identical inputs run_mutation_comparison
    uses internally, so the scripted agent's command can call it by
    name. Confirms: the baseline arm's call to a not-yet-existing tool
    is a genuine miss/fault (diluted below fidelity_floor by 3 real
    hits so the run still counts), the mutated arm's identical call
    resolves as synthetic and is excluded from fidelity accounting
    entirely, and the resulting paths still match -- NO_REGRESSION,
    correctly computed through the operator's own synthesis path, not
    just asserted against replay_proxy.py in isolation.
    """
    siblings = tools_served_from_session(GOLDEN_FIXTURE)
    added_tool, _ = add_tool(siblings, seed=42)  # same seed run_mutation_comparison uses by default

    calls = _golden_calls()[:3]
    command = [
        sys.executable,
        str(SCRIPTED_AGENT),
        *(_spec(c.tool_name, c.arguments) for c in calls),
        _spec(added_tool.name, {}),
    ]

    result = run_mutation_comparison(
        task_id="tool_addition_task",
        prompt="",
        fixture_path=GOLDEN_FIXTURE,
        server_name=GOLDEN_SERVER,
        agent_command=command,
        operator="tool_addition",
        session_dir=tmp_path / "runs",
        raw_dir=tmp_path / "raw",
        seed=42,
        repeats=1,
        timeout_s=30.0,
    )

    assert result.baseline.has_data is True  # 3/4 = 0.75 fidelity, above the 0.70 floor
    assert result.mutated.has_data is True
    assert result.mutated.baseline_fidelity == 1.0  # synthetic call excluded from the denominator

    expected_path = (*[c.tool_name for c in calls], added_tool.name)
    assert result.baseline.dominant_path == expected_path
    assert result.mutated.dominant_path == expected_path
    assert result.effect.verdict == "NO_REGRESSION"

    assert len(result.mutation_log) == 1
    assert result.mutation_log[0].tool_name == added_tool.name
    assert result.mutation_log[0].before is None
    assert result.mutation_log[0].inverse is None
