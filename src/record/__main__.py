"""CLI entrypoint for the passthrough proxy: `python -m record <command> [args...]`.

This is F-01's invocation shape only — the way Drifter stands in for the
real server in an MCP client's config. `drifter observe` (F-09) is the
user-facing command that wraps this in later gates; this module is
intentionally usable standalone until then.
"""

from __future__ import annotations

import sys

import anyio
from mcp.client.stdio import StdioServerParameters

from record.proxy import run_passthrough_proxy


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: python -m record.proxy <command> [args...]")

    server = StdioServerParameters(command=sys.argv[1], args=sys.argv[2:])
    anyio.run(run_passthrough_proxy, server)


if __name__ == "__main__":
    main()
