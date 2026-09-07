"""`drifter run` (F-35), docs/SPEC.md §12/§13.

Orchestrates the pieces this gate and Gate 2 built into one command:
baseline (`evaluate.baseline.run_baseline` via
`cli.subprocess_adapter.make_run_once`), one mutation operator
(`mutate.description_update`/`mutate.tool_addition`), the replay-
serving proxy, and behavior comparison (`evaluate.effect_size`).

Scope, deliberately minimal — this is Gate 3's actual exit test (one
real fragility found in the real dogfood agent), not the polished v1
command surface: no `--budget`/`--dry-run`, no adaptive repeat
scheduling (F-27), no docs/SPEC.md §13's full calibration-footnote
reporting — only what's needed to run a baseline, apply one operator,
run the mutated arm, and report Behavior-axis
NO_REGRESSION/INCONCLUSIVE/REGRESSION/UNKNOWN. Task axis reports UNKNOWN
unconditionally (no assertion engine exists — F-24 depends on F-30, task
definitions, which isn't scheduled until later; see this prompt's own
STOP-AND-CHECK) — that's the correct, honest default per the
non-negotiable "verdict defaults to UNKNOWN" invariant, not a stub
standing in for something unbuilt.

Safety axis (F-25/F-26, docs/CHANGELOG.md) IS wired, added once both existed:
`policy.safety.evaluate_safety_across_arms` evaluates every recorded session
from both arms (not just "valid" ones — Safety has no fidelity gate,
docs/SPEC.md §8's own text: "evaluated on every run regardless of
configuration") against `config.policy`'s destructive-override/confirmation-
required lists. Lives in `policy/safety.py`, not here — `cli/report.py`
(F-36) needs the identical from-disk logic and `policy/` sits below `cli/`
in this project's module order, so the shared piece has to live on the
`policy/` side.

STOP-AND-CHECK findings, load-bearing for this module's design (not
re-derived here — see the session record for the full investigation):
no `Task` type exists anywhere in this codebase, and docs/SPEC.md §11's
`tasks: [...]` was never expanded into a real schema. Rather than
inventing a `tasks:` YAML block / mining-and-approval workflow (F-30's
job, not this prompt's), a task here is exactly two CLI-level values:
`--task-id` and `--prompt` — matching precisely what
`evaluate.baseline.run_baseline` already accepts (a bare `task_id: str`
label) and what `{task.prompt}` templating needs, nothing richer.

Also per that STOP-AND-CHECK: docs/PHASES.md states "Gate 3 stays in replay
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
from pathlib import Path
from typing import TextIO

from cli.config import ConfigError, PolicyConfig, load_config
from cli.report_format import RunResult, render_run_result
from cli.stats import resolve_runs_dir
from cli.subprocess_adapter import make_run_once
from evaluate.baseline import run_baseline
from evaluate.effect_size import compute_behavior_effect_size
from mutate.description_update import mutate_tool_manifest
from mutate.tool_addition import add_tool
from policy.blast_radius import compute_blast_radius, render_blast_radius
from policy.budget import BudgetTracker, budget_limited
from policy.safety import evaluate_safety_across_arms
from record.calibration import Calibration, load_calibration
from replay.replay_proxy import tools_served_from_session
from replay.replay_store import ReplayStore

OPERATORS = ("description_update", "tool_addition")

DEFAULT_TIMEOUT_S = 60.0


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
    agent_mode: str = "subprocess",
    agent_env_var: str = "DRIFTER_PROXY_URL",
    policy: PolicyConfig | None = None,
    budget: int | None = None,
    max_wall_time_s: float | None = None,
) -> RunResult:
    """Runs the baseline arm, applies `operator` to the manifest, runs
    the mutated arm against the same task and agent, and scores
    Behavior-axis effect size between them. Both arms replay from the
    same `fixture_path`-derived `ReplayStore` — the only difference
    between them is which manifest (`tools_served`) the replay proxy
    serves, exactly matching what docs/SPEC.md §7/§8 mean by "same task,
    mutation active vs. not." Also evaluates the Safety axis (F-25) across
    every recorded run from both arms — see `evaluate_safety_across_arms`.

    `policy` defaults to an empty `PolicyConfig()` (no overrides, nothing
    requires confirmation) when not given, matching `cli.config.
    DrifterConfig.policy`'s own default — callers that already have a
    loaded config pass `config.policy` through; this function never loads
    config itself (same "caller owns configuration" shape as
    `evaluate.baseline.run_baseline`).

    `budget` (F-32, docs/SPEC.md §11/§13) is a TOOL-CALL ceiling, not a literal
    "model call" count — see `policy/budget.py`'s own module docstring for
    why. `max_wall_time_s` is a wall-clock ceiling across the WHOLE
    comparison (both arms combined), not per-run. Both default to `None`
    (unlimited). One `BudgetTracker` is shared across BOTH arms
    deliberately — the budget is for this invocation's real cost as a
    whole, not accounted per-arm.
    """
    if operator not in OPERATORS:
        raise ValueError(f"unknown operator {operator!r} — must be one of {OPERATORS}")

    calibration = calibration or load_calibration()
    store = ReplayStore()
    store.index_session(fixture_path)
    original_tools = tools_served_from_session(fixture_path)
    command = _template_command(agent_command, prompt)
    tracker = BudgetTracker(max_tool_calls=budget, max_wall_time_s=max_wall_time_s)

    baseline_run_once = budget_limited(
        make_run_once(
            command=command,
            replay_store=store,
            server_name=server_name,
            tools_served=original_tools,
            session_dir=session_dir / "baseline",
            raw_dir=raw_dir / "baseline",
            timeout_s=timeout_s,
            agent_mode=agent_mode,
            env_var=agent_env_var,
        ),
        tracker,
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

    mutated_run_once = budget_limited(
        make_run_once(
            command=command,
            replay_store=store,
            server_name=server_name,
            tools_served=mutated_tools,
            session_dir=session_dir / "mutated",
            raw_dir=raw_dir / "mutated",
            timeout_s=timeout_s,
            synthetic_tool_names=synthetic_tool_names,
            agent_mode=agent_mode,
            env_var=agent_env_var,
        ),
        tracker,
    )
    mutated_result = run_baseline(f"{task_id}__mutated_{operator}", mutated_run_once, repeats=repeats, calibration=calibration)

    effect = compute_behavior_effect_size(baseline_result, mutated_result, calibration=calibration)
    effective_policy = policy or PolicyConfig()
    safety = evaluate_safety_across_arms(
        session_dir, effective_policy.destructive, effective_policy.confirmation_required
    )

    return RunResult(
        task_id=task_id,
        operator=operator,
        baseline=baseline_result,
        mutated=mutated_result,
        effect=effect,
        mutation_log=mutation_log,
        safety=safety,
        budget_exceeded=tracker.exceeded(),
    )


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
    input_stream: TextIO = sys.stdin,
    assume_yes: bool = False,
    dry_run: bool = False,
    budget: int | None = None,
    max_wall_time_s: float | None = None,
) -> RunResult | None:
    """F-31's own "Done when" bar, reframed honestly for what this command
    actually does today (no live MCP server mode exists — see
    `policy/blast_radius.py`'s own module docstring): the real, un-deferred
    cost `drifter run` incurs is spawning real agent subprocesses, `repeats`
    times per arm, twice (baseline + mutated) — that path is now
    architecturally unreachable without the blast-radius preview being
    shown and either `assume_yes=True` (the CLI's `--yes` flag) or an
    interactive `y`/`yes` confirmation on `input_stream`.

    `dry_run` (F-32, `--dry-run`) shows the same preview and returns
    immediately — no confirmation prompt, no agent ever spawned. Needed no
    new computation: F-31's blast-radius preview already IS "plan without
    executing." `budget`/`max_wall_time_s` (F-32) are passed straight
    through to `run_mutation_comparison`'s shared `BudgetTracker` — see
    that function's own docstring and `policy/budget.py` for the real,
    stated shape of what "budget" means here (a tool-call ceiling, not a
    literal model-call count, which this codebase cannot observe at all).

    Returns the `RunResult` on a completed comparison, or `None` when
    nothing ran (`--dry-run`, or the interactive confirmation was
    declined) — `cli/app.py` uses this to compute docs/SPEC.md §12's
    verdict-specific exit code via `cli.report_format.compute_exit_code`,
    which needs a real `RunResult` to read.
    """
    config = load_config(config_path)
    if config.agent is None:
        raise ConfigError(
            f"{config_path or 'drifter.yaml'} has no `agent:` block — drifter run needs "
            "`agent.command` to know how to spawn the agent under test (docs/SPEC.md §11)."
        )
    if fixture_path is None:
        raise ConfigError("drifter run needs --fixture: an already-recorded session JSONL to replay from (Gate 3 stays in replay mode).")
    if server_name is None:
        raise ConfigError("drifter run needs --server: the server name the fixture session was recorded against.")

    if runs_dir is None:
        runs_dir = resolve_runs_dir(config)
    session_dir = runs_dir / "run" / task_id
    raw_dir = runs_dir.parent / "raw" / "run" / task_id

    calibration = load_calibration()
    effective_repeats = repeats if repeats is not None else calibration.baseline.repeats
    original_tools = tools_served_from_session(fixture_path)
    preview = compute_blast_radius(fixture_path, original_tools, effective_repeats, config.policy.destructive)
    output_stream.write(render_blast_radius(preview) + "\n")

    if dry_run:
        output_stream.write("Dry run — no agent runs were started.\n")
        return None

    if not assume_yes:
        output_stream.write("Continue? [y/N] ")
        output_stream.flush()
        answer = input_stream.readline().strip().lower()
        if answer not in ("y", "yes"):
            output_stream.write("Aborted — no agent runs were started.\n")
            return None

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
        agent_mode=config.agent.mode,
        agent_env_var=config.agent.env_var,
        policy=config.policy,
        budget=budget,
        max_wall_time_s=max_wall_time_s,
    )
    output_stream.write(render_run_result(result))
    return result
