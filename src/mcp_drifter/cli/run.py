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
server. It builds a `ReplayStore`/manifest from ALREADY-RECORDED
sessions (`--fixture`), and spawns only the real agent under test (via
`cli.subprocess_adapter`) against the replay-serving proxy, for both
the baseline and mutated arms. The corpus this reads must already
exist (from a prior `drifter observe` run, or the golden fixture) —
this command does not record one.

`--fixture` takes a whole corpus as of DEC-027(b) (docs/CHANGELOG.md):
any number of session files, directories of them, or a mix, all indexed
into one store. That is limitation 16's only honest lever — coverage,
not looser matching — though explicitly not a claim that it closes it;
see `replay/corpus.py` and docs/SPEC.md §7's own cross-reference.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from mcp_drifter.cli.config import ConfigError, PolicyConfig, assertions_for, find_task, load_config
from mcp_drifter.cli.report_format import RunResult, render_run_result
from mcp_drifter.cli.stats import resolve_runs_dir
from mcp_drifter.cli.subprocess_adapter import make_run_once
from mcp_drifter.evaluate.assertions import TaskAssertions, evaluate_task
from mcp_drifter.evaluate.baseline import run_baseline
from mcp_drifter.evaluate.scheduling import run_mutated_adaptively
from mcp_drifter.evaluate.effect_size import compute_behavior_effect_size
from mcp_drifter.mutate.description_update import mutate_tool_manifest
from mcp_drifter.mutate.parameter_rename import inverse_map_from_log, rename_tool_parameters
from mcp_drifter.mutate.tool_addition import add_tool
from mcp_drifter.policy.blast_radius import compute_blast_radius, render_blast_radius
from mcp_drifter.policy.budget import BudgetTracker, budget_limited
from mcp_drifter.policy.safety import evaluate_safety_across_arms
from mcp_drifter.record.calibration import Calibration, load_calibration
from mcp_drifter.replay.corpus import load_corpus, render_corpus_summary
from mcp_drifter.replay.coverage import estimate_coverage, render_coverage
from mcp_drifter.replay.replay_store import ReplayStore

OPERATORS = ("description_update", "tool_addition", "parameter_rename")

DEFAULT_TIMEOUT_S = 60.0


def _template_command(command: list[str], prompt: str) -> list[str]:
    """`{task.prompt}` substitution per-token — see cli/config.py's
    AgentConfig docstring for why this is list-of-tokens, not a shell
    string requiring shlex parsing."""
    return [token.replace("{task.prompt}", prompt) for token in command]


def _as_corpus_inputs(fixture: Path | Sequence[Path]) -> list[Path]:
    """A single `Path` is a corpus of one — accepted deliberately, not as a
    legacy shim: one recorded session is still a legitimate (if usually
    inadequate, docs/SPEC.md §15 limitation 16) replay source, and the
    golden-fixture tests genuinely want exactly that. `replay/corpus.py`
    handles directories; this only normalizes "one or many" at the API
    boundary.
    """
    if isinstance(fixture, (str, Path)):
        return [Path(fixture)]
    return [Path(p) for p in fixture]



def run_mutation_comparison(
    task_id: str,
    prompt: str,
    fixture: Path | Sequence[Path],
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
    assertions: TaskAssertions | None = None,
    adaptive: bool = True,
) -> RunResult:
    """Runs the baseline arm, applies `operator` to the manifest, runs
    the mutated arm against the same task and agent, and scores
    Behavior-axis effect size between them. Both arms replay from the
    same `fixture`-derived `ReplayStore` — the only difference
    between them is which manifest (`tools_served`) the replay proxy
    serves, exactly matching what docs/SPEC.md §7/§8 mean by "same task,
    mutation active vs. not." Also evaluates the Safety axis (F-25) across
    every recorded run from both arms — see `evaluate_safety_across_arms`.

    `fixture` is one session file, a directory of them, or any mix of both
    (DEC-027(b), docs/CHANGELOG.md) — every resolved session is indexed into
    one `ReplayStore`, because coverage is the only honest lever against the
    MISS rate docs/SPEC.md §15 limitation 16 measured. The served manifest
    comes from the most recent contributing session; see
    `replay/corpus.py` for how the corpus is resolved and what it reports.

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
    corpus = load_corpus(_as_corpus_inputs(fixture), server_name)
    store = ReplayStore()
    store.index_sessions(corpus.session_paths)
    original_tools = list(corpus.tools_served)
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
    elif operator == "parameter_rename":
        mutated_tools, mutation_log = rename_tool_parameters(original_tools, seed=seed)
        synthetic_tool_names = frozenset()
    else:
        new_tool, entry = add_tool(original_tools, seed=seed)
        mutated_tools = [*original_tools, new_tool]
        mutation_log = [entry]
        synthetic_tool_names = frozenset({new_tool.name})

    # F-12: only parameter_rename ever produces a real inverse mapping
    # (description_update/tool_addition's own MutationLogEntry.inverse is
    # always None) — inverse_map_from_log handles that generically, so
    # this line doesn't need an operator-specific branch. Only the MUTATED
    # arm's replay resolution needs it: the baseline arm serves the
    # ORIGINAL, un-renamed manifest, so its calls already match the
    # recording under their original parameter names with no translation
    # needed.
    inverse_map = inverse_map_from_log(mutation_log) or None

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
            inverse_map=inverse_map,
        ),
        tracker,
    )
    mutated_task_id = f"{task_id}__mutated_{operator}"
    effective_repeats = repeats if repeats is not None else calibration.baseline.repeats
    if adaptive:
        # F-27: stops as soon as no remaining run could change the verdict.
        # Provably verdict-preserving, so this is on by default -- see
        # evaluate/scheduling.py for the bound and why it is a proof rather
        # than a peek at an interim result.
        scheduled = run_mutated_adaptively(
            mutated_task_id, mutated_run_once, baseline_result, effective_repeats, calibration=calibration
        )
        mutated_result = scheduled.result
        scheduling_note = (
            f"{scheduled.attempts_made}/{scheduled.attempts_allowed} mutated run(s) spent"
            f"{f' ({scheduled.runs_saved} saved)' if scheduled.runs_saved else ''} — {scheduled.stop_reason}"
        )
    else:
        mutated_result = run_baseline(
            mutated_task_id, mutated_run_once, repeats=repeats, calibration=calibration
        )
        scheduling_note = None

    effect = compute_behavior_effect_size(baseline_result, mutated_result, calibration=calibration)
    effective_policy = policy or PolicyConfig()
    safety = evaluate_safety_across_arms(
        session_dir, effective_policy.destructive, effective_policy.confirmation_required
    )

    # F-24: evaluated over each arm's VALID sessions only -- a run excluded
    # for low replay fidelity is one where the agent couldn't do the task
    # for harness reasons, and asserting on it would report a task failure
    # that is really a fidelity failure (see evaluate/assertions.py).
    effective_assertions = assertions or TaskAssertions()
    baseline_task = evaluate_task(baseline_result.valid_session_paths, effective_assertions)
    mutated_task = evaluate_task(mutated_result.valid_session_paths, effective_assertions)

    return RunResult(
        task_id=task_id,
        operator=operator,
        baseline=baseline_result,
        mutated=mutated_result,
        effect=effect,
        mutation_log=mutation_log,
        safety=safety,
        budget_exceeded=tracker.exceeded(),
        baseline_task=baseline_task,
        mutated_task=mutated_task,
        scheduling_note=scheduling_note,
    )


def run_run(
    config_path: Path | None = None,
    fixture: Path | Sequence[Path] | None = None,
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
    adaptive: bool = True,
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
    if fixture is None:
        raise ConfigError(
            "drifter run needs --fixture: already-recorded session JSONL to replay from, "
            "or a directory of them (Gate 3 stays in replay mode)."
        )
    if server_name is None:
        raise ConfigError("drifter run needs --server: the server name the fixture session was recorded against.")

    if runs_dir is None:
        runs_dir = resolve_runs_dir(config)
    session_dir = runs_dir / "run" / task_id
    raw_dir = runs_dir.parent / "raw" / "run" / task_id

    calibration = load_calibration()
    effective_repeats = repeats if repeats is not None else calibration.baseline.repeats

    # F-24: `--task-id` selects an authored task from `drifter.yaml`'s
    # `tasks:` when one matches, supplying both its prompt and its
    # assertions. An unmatched --task-id stays a bare label, exactly as it
    # behaved before authored tasks existed -- so this widens what --task-id
    # can mean without breaking the ad-hoc `--task-id X --prompt Y` usage
    # every existing caller and test relies on.
    task = find_task(config.tasks, task_id)
    assertions = assertions_for(config.tasks, task_id)
    if task is not None and not prompt:
        prompt = task.prompt

    # DEC-027(b): resolve the corpus once, up front, and SHOW what it holds
    # before anything is spent -- a thin or wrong-server corpus is the single
    # most likely reason a verdict later comes back UNKNOWN, and the user can
    # only act on that if they learn it here rather than afterwards.
    corpus = load_corpus(_as_corpus_inputs(fixture), server_name)
    output_stream.write(render_corpus_summary(corpus, server_name) + "\n")

    # DEC-027(c): the projected MISS rate, BEFORE anything is spent. This is
    # the number limitation 16's real test only learned after twenty real
    # agent runs. Leave-one-out over the corpus, so it estimates how well
    # these recordings answer a session they have never seen rather than
    # self-congratulating on the calls that built the index.
    coverage = estimate_coverage(corpus.session_paths, server_name)
    output_stream.write(render_coverage(coverage, fidelity_floor=calibration.fidelity_floor) + "\n\n")

    original_tools = list(corpus.tools_served)
    # Blast radius estimates the cost of ONE agent run, so it samples a
    # single session rather than the whole corpus -- summing every session's
    # calls would overstate a single run's cost by the corpus size. Of the
    # available single-session samples it takes the HEAVIEST, because this
    # preview is the gate a user authorizes real spending through and must
    # not understate it (see `Corpus.heaviest_path` for the real bug that
    # settled this).
    representative = corpus.heaviest_path or corpus.session_paths[0]
    preview = compute_blast_radius(representative, original_tools, effective_repeats, config.policy.destructive)
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
        assertions=assertions,
        fixture=fixture,
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
        adaptive=adaptive,
    )
    output_stream.write(render_run_result(result))
    return result
