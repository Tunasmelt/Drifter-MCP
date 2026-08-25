"""`drifter` CLI dispatch (SPEC.md §12).

`observe`, `stats`, `score`, and `doctor` (connectivity checks only, per
PHASES.md Gate 1) are wired up — the rest of SPEC.md §12's command list
(`init`, `tasks mine`, `tasks approve`, `run`, `report`, doctor's
classification-sanity checks) lands in later gates. Unregistered
subcommands fail with argparse's own "invalid choice" error rather than
a stub pretending to be implemented.
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
            raise SystemExit(4) from None  # SPEC.md §12: exit code 4 = config/connectivity error
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
        raise SystemExit(0 if ok else 4)  # SPEC.md §12: exit code 4 = config/connectivity error
    elif args.command == "score":
        from cli.score import run_score

        try:
            run_score(config_path=args.config, runs_dir=args.runs_dir)
        except ConfigError as e:
            print(f"drifter score: {e}", file=sys.stderr)
            raise SystemExit(4) from None
    else:
        parser.print_help(sys.stderr)
        raise SystemExit(1 if args.command else 0)


if __name__ == "__main__":
    main()
