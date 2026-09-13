"""Release-gate E1 subject: a client mechanically bound to the OLD contract.

docs/PHASES.md R0.5. Deliberately not an LLM: a prompt telling a model to use
old parameter names can be ignored or overridden, and then a failure under
mutation cannot be attributed to the mutation. This client never reads the
served schema. It always sends `order_id`, whatever the manifest says.

Trajectory, fixed and pre-registered (n=2):
  find_order(customer="acme") -> order id
  get_order(order_id=<that id>) -> total

Prints the answer on success. On a protocol error it prints the error code and
exits 0: the session record, not this process's exit status, is the evidence.
"""

from __future__ import annotations

import os

import anyio
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError


async def main() -> None:
    url = os.environ["DRIFTER_PROXY_URL"]
    async with streamable_http_client(url) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            found = await session.call_tool("find_order", {"customer": "acme"})
            order_id = found.content[0].text.strip()
            try:
                order = await session.call_tool("get_order", {"order_id": order_id})
            except MCPError as exc:
                print(f"get_order rejected: code {exc.code}", flush=True)
                return
            if order.is_error:
                print("get_order returned an error result", flush=True)
                return
            print(order.content[0].text.strip(), flush=True)


if __name__ == "__main__":
    anyio.run(main)
