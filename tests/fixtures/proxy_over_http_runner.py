"""Standalone runner for `tests/record/test_proxy_http.py`'s real
end-to-end F-39 test: spawned as a subprocess, exactly the way `drifter
observe` runs — real stdio on the agent-facing side (via `record.proxy.
run_passthrough_proxy`'s own `stdio_server()`), a real Streamable HTTP
connection to the URL given on argv on the real-server-facing side.

`argv[1]` is the URL to connect to (the test's own `serve_replay_over_http`
instance). Deliberately minimal — no recording, no config loading — this
exists only to run the real transport-selection code path in a real
separate process, the same way `record/__main__.py` does for the stdio
case (see that module's own docstring for why a standalone entry point
exists at all: F-01/F-02/F-03's invocation shape is meant to be usable
standalone, and this mirrors it for F-39 rather than inventing a new shape).
"""

import sys

import anyio

from mcp_drifter.record.proxy import run_passthrough_proxy

if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: proxy_over_http_runner.py <url>")
    anyio.run(run_passthrough_proxy, sys.argv[1])
