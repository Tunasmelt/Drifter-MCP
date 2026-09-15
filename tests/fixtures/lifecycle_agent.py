"""HTTP test agent for run-lifecycle tests (docs/PHASES.md R1).

Connects to DRIFTER_PROXY_URL, initializes, lists tools, then behaves per argv[1]:
  crash      exit with code 9 (after tools/list)
  hang       sleep far past any test timeout
  probe      exit 0 with no tool call and no output (a connectivity check)
  answer     exit 0 with no tool call, printing a final answer (a genuine no-tool task)
  call:NAME  call tool NAME with JSON args in argv[2], print an answer, exit 0
  late:NAME  call tool NAME BEFORE listing tools, then list tools, print, exit 0
"""

from __future__ import annotations

import json
import os
import sys
import time

import anyio
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main(mode: str, args: dict) -> int:
    async with streamable_http_client(os.environ["DRIFTER_PROXY_URL"]) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            if mode.startswith("late:"):
                await session.call_tool(mode.split(":", 1)[1], args)
                await session.list_tools()
                print("done after a late tools/list", flush=True)
                return 0
            await session.list_tools()
            if mode == "crash":
                return 9
            if mode == "hang":
                await anyio.sleep(3600)
            if mode == "probe":
                return 0
            if mode == "answer":
                print("No tool is needed: the answer is 4.", flush=True)
                return 0
            if mode.startswith("call:"):
                await session.call_tool(mode.split(":", 1)[1], args)
                print("done", flush=True)
                return 0
    raise SystemExit(f"unknown mode {mode!r}")


if __name__ == "__main__":
    mode = sys.argv[1]
    args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    code = anyio.run(main, mode, args)
    sys.stdout.flush()
    os._exit(code)
