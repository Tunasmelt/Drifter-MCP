"""`drifter` CLI dispatch (docs/SPEC.md §12).

`init`, `observe`, `stats`, `score`, `report`, `run`, `replay-serve`, and
`doctor` are wired up — the rest of docs/SPEC.md §12's command list (`tasks
mine`, `tasks approve`, doctor's classification-sanity checks) lands in later
gates. `init` is F-33, deliberately narrower than its own spec text (see
cli/init.py's docstring for why — found missing while sanity-checking Gate 4's
own handoff checklist, not planned this way). `run` is F-35's orchestration,
widened since Gate 3 (see cli/run.py's own docstring) with F-31's blast-radius
preview/confirmation gate and F-32's `--budget`/`--dry-run`/`--max-wall-time`,
though still not the full v1 report format (adaptive scheduling, F-27, is
still unbuilt). `report` (F-36's own second half, `cli/report.py`) re-renders
a prior `run`'s full BEHAVIOR/TASK/SAFETY report from stored sessions alone,
zero new execution — `score` already met F-36's own Gate 2 exit test
(re-analyze with zero execution) but never rendered this fuller format.
`replay-serve` is not itself an F-number — it's the real-agent connection
mechanism `run` needed but never had (see cli/replay_serve.py's own docstring
for why: found blocking the real Gate 0 dogfood run, not planned in advance).
Unregistered subcommands fail with argparse's own "invalid choice" error
rather than a stub pretending to be implemented.

docs/SPEC.md §12's verdict-specific exit codes (`1` behavior regression,
`2` assertion failure, `3` safety violation, `5` budget exceeded, on top
of the `0`/`4` clean/config-error split every command already had) are
now wired for `run` and `report` — the two commands that produce a full
`RunResult` with real verdicts to read (`cli.report_format.
compute_exit_code`). `score` still exits `0`/`4` only: it produces a bare
`BaselineResult`, not a `RunResult` — there is no BEHAVIOR/TASK/SAFETY
verdict for it to report an exit code about. Exit code `2` is real,
wired code with nothing that can ever trigger it yet: TASK is
unconditionally UNKNOWN today (no assertion engine reads into
`RunResult`) — see `compute_exit_code`'s own docstring.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from cli.config import ConfigError


def _ensure_utf8_console_streams() -> None:
    """Windows' default console codepage isn't UTF-8. cli/observe.py's
    live status line (stderr) and cli/stats.py's report (stdout) both use
    an em dash; without this, that byte encodes as whatever the ambient
    codepage is (observed: cp1252's 0x97) rather than UTF-8 — silently
    wrong when the caller (a real MCP client spawning `drifter observe`, a
    terminal, or a test) reads the stream expecting UTF-8. Originally
    fixed for stderr only (Prompt 7); extended to stdout in Prompt 8 once
    `stats`/`doctor` started writing user-facing text there too — observe
    itself never touches stdout directly (stdio_server() diverts the real
    OS-level stdout away from it, see cli/observe.py's docstring), so this
    is a no-op for that command, not a risk to the wire protocol.
    `errors="replace"` means a future genuinely unencodable character
    degrades to a placeholder instead of crashing mid-session.
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="drifter")
    subparsers = parser.add_subparsers(dest="command")

    init_parser = subparsers.add_parser("init", help="Scan known MCP client configs and write a starter drifter.yaml")
    init_parser.add_argument("--output", type=Path, default=Path("drifter.yaml"), help="Where to write the generated config")
    init_parser.add_argument("--search-root", type=Path, default=Path("."), help="Directory to look for .mcp.json / .cursor/mcp.json in")
    init_parser.add_argument("--force", action="store_true", help="Overwrite an existing config at --output")

    observe_parser = subparsers.add_parser("observe", help="Passthrough proxy, record only")
    observe_parser.add_argument("--config", type=Path, default=Path("drifter.yaml"), help="Path to drifter.yaml")
    observe_parser.add_argument("--server", default=None, help="Server name from drifter.yaml (required if more than one is defined)")

    stats_parser = subparsers.add_parser("stats", help="Summarize the recorded JSONL corpus")
    stats_parser.add_argument("--config", type=Path, default=Path("drifter.yaml"), help="Path to drifter.yaml")
    stats_parser.add_argument("--runs-dir", type=Path, default=None, help="Corpus directory to read directly, bypassing drifter.yaml")
    stats_parser.add_argument("--server", default=None, help="Restrict the summary to one server's tools")

    doctor_parser = subparsers.add_parser("doctor", help="Config + connectivity pre-flight checks")
    doctor_parser.add_argument("--config", type=Path, default=Path("drifter.yaml"), help="Path to drifter.yaml")

    score_parser = subparsers.add_parser("score", help="Re-analyze recorded sessions, zero new execution")
    score_parser.add_argument("--config", type=Path, default=Path("drifter.yaml"), help="Path to drifter.yaml")
    score_parser.add_argument("--runs-dir", type=Path, default=None, help="Corpus directory to read directly, bypassing drifter.yaml")

    report_parser = subparsers.add_parser(
        "report", help="Re-render a prior `drifter run`'s full report from stored sessions, zero new execution (F-36)"
    )
    report_parser.add_argument("--config", type=Path, default=Path("drifter.yaml"), help="Path to drifter.yaml")
    report_parser.add_argument("--runs-dir", type=Path, default=None, help="Corpus directory to read directly, bypassing drifter.yaml")
    report_parser.add_argument("--task-id", default="task", help="The --task-id a prior `drifter run` was invoked with")

    run_parser = subparsers.add_parser("run", help="Baseline + one mutation operator, replay mode (F-35, Gate 3 minimal scope)")
    run_parser.add_argument("--config", type=Path, default=Path("drifter.yaml"), help="Path to drifter.yaml (needs an agent: block)")
    run_parser.add_argument(
        "--fixture", type=Path, required=True, nargs="+",
        help="Already-recorded session JSONL(s) to replay from, and/or directories of them "
             "(DEC-027: coverage across a corpus is what keeps replay fidelity up)",
    )
    run_parser.add_argument("--server", required=True, help="Server name the fixture session was recorded against")
    run_parser.add_argument("--task-id", default="task", help="Label for this task (no task-definition system exists yet — see cli/run.py)")
    run_parser.add_argument("--prompt", default="", help="Substituted into agent.command's {task.prompt} token")
    run_parser.add_argument(
        "--operator", choices=["description_update", "tool_addition", "parameter_rename"], default="description_update"
    )
    run_parser.add_argument("--runs-dir", type=Path, default=None, help="Where to write new session JSONL, bypassing drifter.yaml")
    run_parser.add_argument("--seed", type=int, default=42, help="Mutation seed (reproducible)")
    run_parser.add_argument("--repeats", type=int, default=None, help="Overrides calibration.yaml's baseline.repeats")
    run_parser.add_argument("--timeout", type=float, default=60.0, help="Per-agent-run timeout in seconds")
    run_parser.add_argument(
        "--yes", "-y", dest="assume_yes", action="store_true",
        help="Skip the blast-radius preview confirmation prompt (F-31, docs/SPEC.md §10)",
    )
    run_parser.add_argument(
        "--no-adaptive", dest="adaptive", action="store_false",
        help="Always run the full --repeats count on the mutated arm instead of stopping "
             "once the verdict is provably settled (F-27). Verdicts are identical either way; "
             "this only spends more real agent runs.",
    )
    run_parser.add_argument(
        "--dry-run", dest="dry_run", action="store_true",
        help="Show the blast-radius preview and exit without running anything (F-32)",
    )
    run_parser.add_argument(
        "--budget", type=int, default=None,
        help="Max total tool calls across both arms before remaining repeats are skipped (F-32) — "
             "not a literal model-call count, see policy/budget.py",
    )
    run_parser.add_argument(
        "--max-wall-time", type=float, default=None, dest="max_wall_time_s",
        help="Max wall-clock seconds across both arms before remaining repeats are skipped (F-32)",
    )

    replay_serve_parser = subparsers.add_parser("replay-serve", help="Serve a replayed manifest over real stdio, for a real agent to connect to")
    replay_serve_parser.add_argument(
        "--fixture", type=Path, required=True, nargs="+",
        help="Already-recorded session JSONL(s) to replay from, and/or directories of them (DEC-027)",
    )
    replay_serve_parser.add_argument("--server", required=True, help="Server name the fixture session was recorded against")
    replay_serve_parser.add_argument("--runs-dir", type=Path, default=Path(".drifter/runs"), help="Where to write the new recorded session JSONL")
    replay_serve_parser.add_argument("--raw-dir", type=Path, default=None, help="Defaults to <runs-dir>/../raw")
    replay_serve_parser.add_argument(
        "--mutate",
        choices=["description_update", "tool_addition", "parameter_rename"],
        default=None,
        help="Apply a mutation operator before serving (omit for baseline)",
    )
    replay_serve_parser.add_argument("--seed", type=int, default=42, help="Mutation seed (reproducible)")

    return parser


def main() -> None:
    _ensure_utf8_console_streams()
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "init":
        from cli.init import run_init

        try:
            run_init(output_path=args.output, search_root=args.search_root, force=args.force)
        except ConfigError as e:
            print(f"drifter init: {e}", file=sys.stderr)
            raise SystemExit(4) from None
    elif args.command == "observe":
        from cli.observe import run_observe

        try:
            run_observe(config_path=args.config, server_name=args.server)
        except ConfigError as e:
            print(f"drifter observe: {e}", file=sys.stderr)
            raise SystemExit(4) from None  # docs/SPEC.md §12: exit code 4 = config/connectivity error
    elif args.command == "stats":
        from cli.stats import run_stats

        try:
            run_stats(config_path=args.config, runs_dir=args.runs_dir, server_name=args.server)
        except ConfigError as e:
            print(f"drifter stats: {e}", file=sys.stderr)
            raise SystemExit(4) from None
    elif args.command == "doctor":
        from cli.doctor import run_doctor

        ok = run_doctor(config_path=args.config)
        raise SystemExit(0 if ok else 4)  # docs/SPEC.md §12: exit code 4 = config/connectivity error
    elif args.command == "score":
        from cli.score import run_score

        try:
            run_score(config_path=args.config, runs_dir=args.runs_dir)
        except ConfigError as e:
            print(f"drifter score: {e}", file=sys.stderr)
            raise SystemExit(4) from None
    elif args.command == "report":
        from cli.report import run_report
        from cli.report_format import compute_exit_code

        try:
            result = run_report(config_path=args.config, runs_dir=args.runs_dir, task_id=args.task_id)
        except ConfigError as e:
            print(f"drifter report: {e}", file=sys.stderr)
            raise SystemExit(4) from None
        raise SystemExit(compute_exit_code(result))  # docs/SPEC.md §12: 0/1/3/5
    elif args.command == "run":
        from cli.run import run_run
        from cli.report_format import compute_exit_code

        try:
            result = run_run(
                config_path=args.config,
                fixture=args.fixture,
                server_name=args.server,
                task_id=args.task_id,
                prompt=args.prompt,
                operator=args.operator,
                runs_dir=args.runs_dir,
                seed=args.seed,
                repeats=args.repeats,
                timeout_s=args.timeout,
                assume_yes=args.assume_yes,
                dry_run=args.dry_run,
                budget=args.budget,
                max_wall_time_s=args.max_wall_time_s,
                adaptive=args.adaptive,
            )
        except ConfigError as e:
            print(f"drifter run: {e}", file=sys.stderr)
            raise SystemExit(4) from None
        # `result` is None for --dry-run or a declined confirmation — no
        # comparison ran, so there's no verdict to report; exit clean (0).
        raise SystemExit(compute_exit_code(result) if result is not None else 0)  # docs/SPEC.md §12: 0/1/3/5
    elif args.command == "replay-serve":
        from cli.replay_serve import run_replay_serve

        raw_dir = args.raw_dir if args.raw_dir is not None else args.runs_dir.parent / "raw"
        try:
            run_replay_serve(
                fixture=args.fixture,
                server_name=args.server,
                session_dir=args.runs_dir,
                raw_dir=raw_dir,
                operator=args.mutate,
                seed=args.seed,
            )
        except ConfigError as e:
            print(f"drifter replay-serve: {e}", file=sys.stderr)
            raise SystemExit(4) from None
    else:
        parser.print_help(sys.stderr)
        raise SystemExit(1 if args.command else 0)


if __name__ == "__main__":
    main()
