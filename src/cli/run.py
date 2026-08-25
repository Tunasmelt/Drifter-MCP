"""`drifter run` (F-35), SPEC.md §12/§13.

Orchestrates the pieces this gate and Gate 2 built into one command:
baseline (`evaluate.baseline.run_baseline` via
`cli.subprocess_adapter.make_run_once`), one mutation operator
(`mutate.description_update`/`mutate.tool_addition`), the replay-
serving proxy, and behavior comparison (`evaluate.effect_size`).

Scope, deliberately minimal — this is Gate 3's actual exit test (one
real fragility found in the real dogfood agent), not the polished v1
command surface: no `--budget`/`--dry-run`, no adaptive repeat
scheduling (F-27), no SPEC.md §13's full report-format mockup (task/
safety verdict lines, calibration footnotes) — only what's needed to
run a baseline, apply one operator, run the mutated arm, and report
Behavior-axis NO_REGRESSION/INCONCLUSIVE/REGRESSION/UNKNOWN. Task axis
reports UNKNOWN unconditionally (no assertion engine exists — F-24
depends on F-30, task definitions, which isn't scheduled until later;
see this prompt's own STOP-AND-CHECK) — that's the correct, honest
default per the non-negotiable "verdict defaults to UNKNOWN" invariant,
not a stub standing in for something unbuilt. Safety axis is out of
scope entirely this prompt (F-25/F-26 risk classification).

STOP-AND-CHECK findings, load-bearing for this module's design (not
re-derived here — see the session record for the full investigation):
no `Task` type exists anywhere in this codebase, and SPEC.md §11's
`tasks: [...]` was never expanded into a real schema. Rather than
inventing a `tasks:` YAML block / mining-and-approval workflow (F-30's
job, not this prompt's), a task here is exactly two CLI-level values:
`--task-id` and `--prompt` — matching precisely what
`evaluate.baseline.run_baseline` already accepts (a bare `task_id: str`
label) and what `{task.prompt}` templating needs, nothing richer.

Also per that STOP-AND-CHECK: PHASES.md states "Gate 3 stays in replay
mode throughout" — this command never spawns or connects to a live
server. It builds a `ReplayStore`/manifest from an ALREADY-RECORDED
session (`--fixture`), and spawns only the real agent under test (via
`cli.subprocess_adapter`) against the replay-serving proxy, for both
the baseline and mutated arms. The corpus this reads must already
exist (from a prior `drifter observe` run, or the golden fixture) —
this command does not record one.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from cli.config import ConfigError, load_config
from cli.stats import resolve_runs_dir
from cli.subprocess_adapter import make_run_once
from evaluate.baseline import BaselineResult, run_baseline
from evaluate.effect_size import EffectSizeResult, compute_behavior_effect_size
from mutate.description_update import MutationLogEntry, mutate_tool_manifest
from mutate.tool_addition import add_tool
from record.calibration import Calibration, load_calibration
from replay.replay_proxy import tools_served_from_session
from replay.replay_store import ReplayStore

OPERATORS = ("description_update", "tool_addition")

DEFAULT_TIMEOUT_S = 60.0


@dataclass(frozen=True)
class RunResult:
    task_id: str
    operator: str
    baseline: BaselineResult
    mutated: BaselineResult
    effect: EffectSizeResult
    mutation_log: list[MutationLogEntry]


def _template_command(command: list[str], prompt: str) -> list[str]:
    """`{task.prompt}` substitution per-token — see cli/config.py's
    AgentConfig docstring for why this is list-of-tokens, not a shell
    string requiring shlex parsing."""
    return [token.replace("{task.prompt}", prompt) for token in command]


def run_mutation_comparison(
    task_id: str,
    prompt: str,
    fixture_path: Path,
    server_name: str,
    agent_command: list[str],
    operator: str,
    session_dir: Path,
    raw_dir: Path,
    seed: int = 42,
    repeats: int | None = None,
    calibration: Calibration | None = None,
    timeout_s: float | None = DEFAULT_TIMEOUT_S,
) -> RunResult:
    """Runs the baseline arm, applies `operator` to the manifest, runs
    the mutated arm against the same task and agent, and scores
    Behavior-axis effect size between them. Both arms replay from the
    same `fixture_path`-derived `ReplayStore` — the only difference
    between them is which manifest (`tools_served`) the replay proxy
    serves, exactly matching what SPEC.md §7/§8 mean by "same task,
    mutation active vs. not."
    """
    if operator not in OPERATORS:
        raise ValueError(f"unknown operator {operator!r} — must be one of {OPERATORS}")

    calibration = calibration or load_calibration()
    store = ReplayStore()
    store.index_session(fixture_path)
    original_tools = tools_served_from_session(fixture_path)
    command = _template_command(agent_command, prompt)

    baseline_run_once = make_run_once(
        command=command,
        replay_store=store,
        server_name=server_name,
        tools_served=original_tools,
        session_dir=session_dir / "baseline",
        raw_dir=raw_dir / "baseline",
        timeout_s=timeout_s,
    )
    baseline_result = run_baseline(task_id, baseline_run_once, repeats=repeats, calibration=calibration)

    if operator == "description_update":
        mutated_tools, mutation_log = mutate_tool_manifest(original_tools, seed=seed)
        synthetic_tool_names: frozenset[str] = frozenset()
    else:
        new_tool, entry = add_tool(original_tools, seed=seed)
        mutated_tools = [*original_tools, new_tool]
        mutation_log = [entry]
        synthetic_tool_names = frozenset({new_tool.name})

    mutated_run_once = make_run_once(
        command=command,
        replay_store=store,
        server_name=server_name,
        tools_served=mutated_tools,
        session_dir=session_dir / "mutated",
        raw_dir=raw_dir / "mutated",
        timeout_s=timeout_s,
        synthetic_tool_names=synthetic_tool_names,
    )
    mutated_result = run_baseline(f"{task_id}__mutated_{operator}", mutated_run_once, repeats=repeats, calibration=calibration)

    effect = compute_behavior_effect_size(baseline_result, mutated_result, calibration=calibration)

    return RunResult(
        task_id=task_id,
        operator=operator,
        baseline=baseline_result,
        mutated=mutated_result,
        effect=effect,
        mutation_log=mutation_log,
    )


def _path_str(path: tuple[str, ...] | None) -> str:
    if path is None:
        return "N/A"
    return " → ".join(path) if path else "(no tool calls)"


def render_run_result(result: RunResult) -> str:
    lines: list[str] = []
    lines.append(f"DRIFTER RUN — {result.task_id}  (mutation: {result.operator})")
    lines.append("")
    lines.append(f"BASELINE  {result.baseline.valid_runs}/{result.baseline.total_runs} valid runs")
    lines.append(f"          dominant path: {_path_str(result.baseline.dominant_path)}")
    lines.append(f"MUTATED   {result.mutated.valid_runs}/{result.mutated.total_runs} valid runs")
    lines.append(f"          dominant path: {_path_str(result.mutated.dominant_path)}")
    lines.append("")

    lines.append(f"BEHAVIOR  {result.effect.verdict}")
    if result.effect.deviation_rate is not None:
        lines.append(f"          deviation from baseline: {result.effect.deviation_rate * 100:.0f}%")
    if result.effect.effect_size is not None:
        lines.append(f"          effect size: {result.effect.effect_size:.2f}×")
    elif result.effect.verdict != "UNKNOWN":
        lines.append("          effect size: undefined (baseline had zero natural variation)")

    lines.append("")
    lines.append("TASK      UNKNOWN — no oracle configured")
    lines.append("")

    for run, label in ((result.baseline, "baseline"), (result.mutated, "mutated")):
        if run.excluded_runs:
            lines.append(f"{label.upper()} EXCLUSIONS:")
            for excluded in run.excluded_runs:
                who = excluded.session_id or (str(excluded.path) if excluded.path is not None else "<no session>")
                lines.append(f"  {who}: {excluded.reason}")
            lines.append("")

    if result.mutation_log:
        lines.append("MUTATION LOG:")
        for entry in result.mutation_log:
            lines.append(f"  {entry.tool_name} ({entry.operator}), seed={entry.seed}, inverse={entry.inverse}")

    return "\n".join(lines) + "\n"


def run_run(
    config_path: Path | None = None,
    fixture_path: Path | None = None,
    server_name: str | None = None,
    task_id: str = "task",
    prompt: str = "",
    operator: str = "description_update",
    runs_dir: Path | None = None,
    seed: int = 42,
    repeats: int | None = None,
    timeout_s: float | None = DEFAULT_TIMEOUT_S,
    output_stream: TextIO = sys.stdout,
) -> None:
    config = load_config(config_path)
    if config.agent is None:
        raise ConfigError(
            f"{config_path or 'drifter.yaml'} has no `agent:` block — drifter run needs "
            "`agent.command` to know how to spawn the agent under test (SPEC.md §11)."
        )
    if fixture_path is None:
        raise ConfigError("drifter run needs --fixture: an already-recorded session JSONL to replay from (Gate 3 stays in replay mode).")
    if server_name is None:
        raise ConfigError("drifter run needs --server: the server name the fixture session was recorded against.")

    if runs_dir is None:
        runs_dir = resolve_runs_dir(config)
    session_dir = runs_dir / "run" / task_id
    raw_dir = runs_dir.parent / "raw" / "run" / task_id

    result = run_mutation_comparison(
        task_id=task_id,
        prompt=prompt,
        fixture_path=fixture_path,
        server_name=server_name,
        agent_command=config.agent.command,
        operator=operator,
        session_dir=session_dir,
        raw_dir=raw_dir,
        seed=seed,
        repeats=repeats,
        timeout_s=timeout_s,
    )
    output_stream.write(render_run_result(result))
