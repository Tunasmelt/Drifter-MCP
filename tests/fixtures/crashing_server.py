"""A real MCP server that crashes abruptly right after responding to
`initialize` -- used by tests/record/test_proxy.py's F-01 edge case:
does `record/proxy.py`'s passthrough shut down cleanly (not hang) when
the REAL server it spawned dies mid-session, as opposed to the agent
disconnecting cleanly (already covered by every other proxy test)?

Deliberately NOT using `mcp.server.mcpserver.MCPServer` (fake_server.py's
own approach) -- that framework doesn't offer a clean way to crash
mid-handshake on command. Hand-rolled, minimal JSON-RPC instead: read one
line (the `initialize` request), write back a valid response, then
`os._exit(1)` -- a real, immediate process termination, not a clean
`sys.exit()` a signal handler or `atexit` hook could intercept.
"""

from __future__ import annotations

import json
import os
import sys


def main() -> None:
    line = sys.stdin.readline()
    request = json.loads(line)
    response = {
        "jsonrpc": "2.0",
        "id": request["id"],
        "result": {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "crashing-server", "version": "0.0.1"},
        },
    }
    sys.stdout.write(json.dumps(response) + "\n")
    sys.stdout.flush()
    os._exit(1)  # a real crash, not a cooperative exit


if __name__ == "__main__":
    main()
