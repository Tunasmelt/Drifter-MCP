"""`drifter` CLI dispatch (SPEC.md §12).

Only `observe` is wired up so far — the rest of SPEC.md §12's command
list (`init`, `stats`, `tasks mine`, `tasks approve`, `run`, `score`,
`report`, `doctor`) lands in later gates per PHASES.md. Unregistered
subcommands fail with argparse's own "invalid choice" error rather than
a stub pretending to be implemented.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from cli.config import ConfigError


def _ensure_utf8_stderr() -> None:
    """Windows' default console codepage isn't UTF-8. cli/observe.py's
    live status line uses an em dash; without this, that byte encodes as
    whatever the ambient codepage is (observed: cp1252's 0x97) rather
    than UTF-8 — silently wrong when the caller (a real MCP client
    spawning `drifter observe`, or a test) reads stderr expecting UTF-8.
    `errors="replace"` means a future genuinely unencodable character
    degrades to a placeholder instead of crashing observe mid-session.
    """
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="drifter")
    subparsers = parser.add_subparsers(dest="command")

    observe_parser = subparsers.add_parser("observe", help="Passthrough proxy, record only")
    observe_parser.add_argument("--config", type=Path, default=Path("drifter.yaml"), help="Path to drifter.yaml")
    observe_parser.add_argument("--server", default=None, help="Server name from drifter.yaml (required if more than one is defined)")

    return parser


def main() -> None:
    _ensure_utf8_stderr()
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "observe":
        from cli.observe import run_observe

        try:
            run_observe(config_path=args.config, server_name=args.server)
        except ConfigError as e:
            print(f"drifter observe: {e}", file=sys.stderr)
            raise SystemExit(4) from None  # SPEC.md §12: exit code 4 = config/connectivity error
    else:
        parser.print_help(sys.stderr)
        raise SystemExit(1 if args.command else 0)


if __name__ == "__main__":
    main()
