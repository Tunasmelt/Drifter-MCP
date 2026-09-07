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
from policy.safety import SafetyResult
from record.reader import read_session
from record.schema import ToolCall
from replay.replay_proxy import tools_served_from_session

NO_VIOLATION = SafetyResult(verdict="NO_VIOLATION", findings=())

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
        mutation_log=[], safety=NO_VIOLATION,
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
        mutation_log=[], safety=NO_VIOLATION,
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
        assume_yes=True,  # F-31: no interactive stdin in a test
    )
    output = out.getvalue()
    assert "via_config" in output
    assert "BEHAVIOR" in output
    assert "Planned:" in output  # F-31: blast-radius preview shown before execution


# --- F-31: blast-radius preview confirmation gate ----------------------------


def test_run_run_declining_confirmation_aborts_without_running_the_agent(tmp_path):
    """F-31's own "Done when" bar, reframed for what drifter run actually
    does today (see cli/run.py's own run_run docstring): the real cost --
    spawning real agent subprocesses -- must be architecturally unreachable
    without confirmation. Uses a deliberately nonexistent agent command: if
    declining didn't actually stop execution, this would fail with a
    "could not start command" error instead of a clean abort message,
    proving the agent was never even attempted, not just that its output
    was suppressed.
    """
    text = VALID_YAML_NO_AGENT + "\nagent:\n  command: ['this-executable-does-not-exist-anywhere-xyz']\n"
    config_path = _write_config(tmp_path, text)

    out = io.StringIO()
    run_run(
        config_path=config_path,
        fixture_path=GOLDEN_FIXTURE,
        server_name=GOLDEN_SERVER,
        task_id="declined_task",
        runs_dir=tmp_path / "runs",
        output_stream=out,
        input_stream=io.StringIO("n\n"),
    )
    output = out.getvalue()
    assert "Planned:" in output  # the preview was shown
    assert "Aborted" in output
    assert "BEHAVIOR" not in output  # the run itself never happened
    assert not (tmp_path / "runs" / "run" / "declined_task").exists()


def test_run_run_empty_input_is_treated_as_declining(tmp_path):
    """[y/N] -- the bracketed default is N; a bare Enter (empty line) must
    decline, not be silently coerced into acceptance."""
    text = VALID_YAML_NO_AGENT + "\nagent:\n  command: ['this-executable-does-not-exist-anywhere-xyz']\n"
    config_path = _write_config(tmp_path, text)
    out = io.StringIO()
    run_run(
        config_path=config_path,
        fixture_path=GOLDEN_FIXTURE,
        server_name=GOLDEN_SERVER,
        runs_dir=tmp_path / "runs",
        output_stream=out,
        input_stream=io.StringIO("\n"),
    )
    assert "Aborted" in out.getvalue()


def test_run_run_interactive_yes_confirmation_proceeds(tmp_path):
    """The converse of the decline test -- a real 'y' on input_stream must
    let the real run proceed, not just assume_yes=True."""
    calls = _golden_calls()[:2]
    command_json = json.dumps([sys.executable, str(SCRIPTED_AGENT), *(_spec(c.tool_name, c.arguments) for c in calls)])
    text = VALID_YAML_NO_AGENT + f"\nagent:\n  command: {command_json}\n"
    config_path = _write_config(tmp_path, text)

    out = io.StringIO()
    run_run(
        config_path=config_path,
        fixture_path=GOLDEN_FIXTURE,
        server_name=GOLDEN_SERVER,
        task_id="confirmed_task",
        runs_dir=tmp_path / "runs",
        repeats=1,
        timeout_s=30.0,
        output_stream=out,
        input_stream=io.StringIO("y\n"),
    )
    output = out.getvalue()
    assert "Planned:" in output
    assert "Aborted" not in output
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
    assert result.safety.verdict == "NO_VIOLATION"  # F-25: the golden fixture's real tools are all benign

    output = render_run_result(result)
    assert "NO_REGRESSION" in output
    assert "desc_update_task" in output
    assert "SAFETY    NO VIOLATION" in output


def test_run_mutation_comparison_reports_a_real_safety_violation_via_policy_override(tmp_path):
    """F-25 driven through the REAL end-to-end pipeline (real replay-served
    agent, real recorded sessions in both arms), not just policy/safety.py's
    own unit tests: a real golden-fixture tool, forced into policy.destructive,
    must surface as a SAFETY VIOLATION in the real rendered report -- even
    though Behavior itself is a clean NO_REGRESSION, matching docs/SPEC.md §8's
    "reported even when Behavior shows NO_REGRESSION" framing.
    """
    from cli.config import PolicyConfig

    calls = _golden_calls()[:3]
    command = [sys.executable, str(SCRIPTED_AGENT), *(_spec(c.tool_name, c.arguments) for c in calls)]
    forced_destructive = calls[0].tool_name

    result = run_mutation_comparison(
        task_id="safety_task",
        prompt="",
        fixture_path=GOLDEN_FIXTURE,
        server_name=GOLDEN_SERVER,
        agent_command=command,
        operator="description_update",
        session_dir=tmp_path / "runs",
        raw_dir=tmp_path / "raw",
        repeats=2,
        timeout_s=30.0,
        policy=PolicyConfig(destructive=[forced_destructive]),
    )

    assert result.effect.verdict == "NO_REGRESSION"  # behavior itself is unaffected
    assert result.safety.verdict == "VIOLATION"
    assert any(f.tool_name == forced_destructive for f in result.safety.findings)

    output = render_run_result(result)
    assert "SAFETY    VIOLATION" in output
    assert forced_destructive in output


# --- real end-to-end: agent.mode: http (F-38) --------------------------------


def test_run_run_end_to_end_via_config_with_agent_mode_http(tmp_path):
    """The actual public entry point, agent.mode: http this time -- the
    real config-driven path a user's drifter.yaml would exercise, not
    run_mutation_comparison's agent_mode parameter called directly."""
    calls = _golden_calls()[:2]
    command_json = json.dumps([sys.executable, str(SCRIPTED_AGENT), *(_spec(c.tool_name, c.arguments) for c in calls)])
    text = (
        VALID_YAML_NO_AGENT
        + f"\nagent:\n  command: {command_json}\n  mode: http\n"
    )
    config_path = _write_config(tmp_path, text)

    out = io.StringIO()
    run_run(
        config_path=config_path,
        fixture_path=GOLDEN_FIXTURE,
        server_name=GOLDEN_SERVER,
        task_id="http_mode_task",
        operator="description_update",
        runs_dir=tmp_path / "runs",
        repeats=1,
        timeout_s=30.0,
        output_stream=out,
        assume_yes=True,  # F-31: no interactive stdin in a test
    )
    output = out.getvalue()
    assert "http_mode_task" in output
    assert "NO_REGRESSION" in output


def test_run_mutation_comparison_tool_addition_end_to_end_over_http(tmp_path):
    """tool_addition's own end-to-end test (below) only exercises
    agent_mode's default (subprocess) -- this confirms the OTHER
    operator also composes correctly over the HTTP transport, not just
    description_update (already covered above). Synthetic-tool
    resolution (F-14-scoped) is replay_proxy.py's own logic, shared by
    both transports, but "shared code" is an assumption worth confirming
    for real, not trusting by construction."""
    siblings = tools_served_from_session(GOLDEN_FIXTURE)
    added_tool, _ = add_tool(siblings, seed=42)  # same seed run_mutation_comparison uses by default

    # 3 real hits + 1 (baseline-only) miss on the not-yet-existing tool =
    # 3/4 = 0.75 fidelity, above the 0.70 floor -- matching
    # test_run_mutation_comparison_tool_addition_end_to_end's own stdio
    # version exactly, since fidelity gating is calibration.yaml/
    # evaluate.baseline.py logic, not transport-specific.
    calls = _golden_calls()[:3]
    command = [
        sys.executable,
        str(SCRIPTED_AGENT),
        *(_spec(c.tool_name, c.arguments) for c in calls),
        _spec(added_tool.name, {}),
    ]

    result = run_mutation_comparison(
        task_id="tool_addition_http_task",
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
        agent_mode="http",
    )

    assert result.mutated.baseline_fidelity == 1.0  # synthetic call excluded from the denominator, same as stdio
    expected_path = (*[c.tool_name for c in calls], added_tool.name)
    assert result.baseline.dominant_path == expected_path
    assert result.mutated.dominant_path == expected_path
    assert result.effect.verdict == "NO_REGRESSION"


def test_run_mutation_comparison_reports_a_real_regression_over_http(tmp_path):
    """Every other HTTP-mode test in this file (and test_subprocess_
    adapter_http.py) only exercises NO_REGRESSION -- this confirms the
    comparison logic's OTHER real outcome also survives the transport
    swap, using the same brittle SELECT-mode agent, real planted
    substring, and real recorded arguments Gate 3's own kill-criterion
    test built (tests/cli/test_kill_criterion_brittle_agent.py), just run
    over HTTP instead of stdio."""
    real_list_directory_call = next(c for c in _golden_calls() if c.tool_name == "list_directory")
    select_spec = f"SELECT:detailed listing|{json.dumps(real_list_directory_call.arguments)}"

    result = run_mutation_comparison(
        task_id="regression_over_http",
        prompt="",
        fixture_path=GOLDEN_FIXTURE,
        server_name=GOLDEN_SERVER,
        agent_command=[sys.executable, str(SCRIPTED_AGENT), select_spec],
        operator="description_update",
        session_dir=tmp_path / "runs",
        raw_dir=tmp_path / "raw",
        seed=42,
        repeats=1,
        timeout_s=30.0,
        agent_mode="http",
    )
    assert result.baseline.dominant_path == ("list_directory",)
    assert result.mutated.dominant_path == ()
    assert result.effect.verdict == "REGRESSION"


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
