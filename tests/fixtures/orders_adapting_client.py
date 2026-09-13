"""Mechanical adapting client for the release gate (docs/PHASES.md R0.5).

Not E2 itself, which uses a real LLM. This is E2's precondition: a client that
reads the SERVED schema for `get_order` and uses whatever id parameter it names.
If this client does not recover under `parameter_rename`, an E2 failure could
be Drifter's plumbing (inverse resolution, authored fixture) rather than the
model. Same trajectory as orders_old_contract_client.py (n=2).
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
            listed = await session.list_tools()
            schema = next(t for t in listed.tools if t.name == "get_order").input_schema
            id_param = next(iter(schema["properties"]))
            found = await session.call_tool("find_order", {"customer": "acme"})
            order_id = found.content[0].text.strip()
            try:
                order = await session.call_tool("get_order", {id_param: order_id})
            except MCPError as exc:
                print(f"get_order rejected: code {exc.code}", flush=True)
                return
            if order.is_error:
                print("get_order returned an error result", flush=True)
                return
            print(order.content[0].text.strip(), flush=True)


if __name__ == "__main__":
    anyio.run(main)
