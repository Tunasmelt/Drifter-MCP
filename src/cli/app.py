"""`drifter` CLI dispatch (docs/SPEC.md §12).

`observe`, `stats`, `score`, `run`, `replay-serve`, and `doctor`
(connectivity checks only, per docs/PHASES.md Gate 1) are wired up — the
rest of docs/SPEC.md §12's command list (`init`, `tasks mine`, `tasks
approve`, `report`, doctor's classification-sanity checks) lands in
later gates. `run` is F-35's deliberately minimal Gate 3 scope (see
cli/run.py's own docstring) — baseline + one mutation operator +
behavior comparison, not the full v1 orchestration (`--budget`/
`--dry-run`, adaptive scheduling, task/safety verdicts). `replay-serve`
is not itself an F-number — it's the real-agent connection mechanism
`run` needed but never had (see cli/replay_serve.py's own docstring
for why: found blocking the real Gate 0 dogfood run, not planned
in advance). Unregistered subcommands fail with argparse's own
"invalid choice" error rather than a stub pretending to be implemented.
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

    run_parser = subparsers.add_parser("run", help="Baseline + one mutation operator, replay mode (F-35, Gate 3 minimal scope)")
    run_parser.add_argument("--config", type=Path, default=Path("drifter.yaml"), help="Path to drifter.yaml (needs an agent: block)")
    run_parser.add_argument("--fixture", type=Path, required=True, help="Already-recorded session JSONL to replay from")
    run_parser.add_argument("--server", required=True, help="Server name the fixture session was recorded against")
    run_parser.add_argument("--task-id", default="task", help="Label for this task (no task-definition system exists yet — see cli/run.py)")
    run_parser.add_argument("--prompt", default="", help="Substituted into agent.command's {task.prompt} token")
    run_parser.add_argument("--operator", choices=["description_update", "tool_addition"], default="description_update")
    run_parser.add_argument("--runs-dir", type=Path, default=None, help="Where to write new session JSONL, bypassing drifter.yaml")
    run_parser.add_argument("--seed", type=int, default=42, help="Mutation seed (reproducible)")
    run_parser.add_argument("--repeats", type=int, default=None, help="Overrides calibration.yaml's baseline.repeats")
    run_parser.add_argument("--timeout", type=float, default=60.0, help="Per-agent-run timeout in seconds")

    replay_serve_parser = subparsers.add_parser("replay-serve", help="Serve a replayed manifest over real stdio, for a real agent to connect to")
    replay_serve_parser.add_argument("--fixture", type=Path, required=True, help="Already-recorded session JSONL to replay from")
    replay_serve_parser.add_argument("--server", required=True, help="Server name the fixture session was recorded against")
    replay_serve_parser.add_argument("--runs-dir", type=Path, default=Path(".drifter/runs"), help="Where to write the new recorded session JSONL")
    replay_serve_parser.add_argument("--raw-dir", type=Path, default=None, help="Defaults to <runs-dir>/../raw")
    replay_serve_parser.add_argument("--mutate", choices=["description_update", "tool_addition"], default=None, help="Apply a mutation operator before serving (omit for baseline)")
    replay_serve_parser.add_argument("--seed", type=int, default=42, help="Mutation seed (reproducible)")

    return parser


def main() -> None:
    _ensure_utf8_console_streams()
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "observe":
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
    elif args.command == "run":
        from cli.run import run_run

        try:
            run_run(
                config_path=args.config,
                fixture_path=args.fixture,
                server_name=args.server,
                task_id=args.task_id,
                prompt=args.prompt,
                operator=args.operator,
                runs_dir=args.runs_dir,
                seed=args.seed,
                repeats=args.repeats,
                timeout_s=args.timeout,
            )
        except ConfigError as e:
            print(f"drifter run: {e}", file=sys.stderr)
            raise SystemExit(4) from None
    elif args.command == "replay-serve":
        from cli.replay_serve import run_replay_serve

        raw_dir = args.raw_dir if args.raw_dir is not None else args.runs_dir.parent / "raw"
        try:
            run_replay_serve(
                fixture_path=args.fixture,
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
