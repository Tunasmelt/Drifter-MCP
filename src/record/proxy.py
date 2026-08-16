"""Proxy passthrough over stdio (F-01).

Drifter is invoked as the MCP server command in the client's config. It
spawns the real server as a child process and forwards every JSON-RPC frame
bidirectionally, unmodified, between the agent (our own stdin/stdout) and
the real server (the child process's stdin/stdout).

Framing on both sides comes from the official MCP SDK's stdio transport
(`mcp.client.stdio.stdio_client`, `mcp.server.stdio.stdio_server`) rather
than hand-rolled JSON-RPC parsing, per FEATURES.md F-01. No recording, no
transformation of frames — that starts at F-02.
"""

from __future__ import annotations

from collections.abc import AsyncIterable

import anyio
from anyio.abc import CancelScope, ObjectSendStream
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage


async def run_passthrough_proxy(server: StdioServerParameters) -> None:
    """Spawns `server` and pipes frames to/from our own stdio, unmodified.

    Runs until either side closes its connection, then tears the other
    side down too — closing the child's pipes and terminating it if it
    hasn't already exited, via `stdio_client`'s own shutdown sequence.
    """
    async with stdio_client(server) as (server_read, server_write):
        async with stdio_server() as (agent_read, agent_write):
            async with anyio.create_task_group() as tg:
                tg.start_soon(_pump, agent_read, server_write, tg.cancel_scope)
                tg.start_soon(_pump, server_read, agent_write, tg.cancel_scope)


async def _pump(
    source: AsyncIterable[SessionMessage | Exception],
    sink: ObjectSendStream[SessionMessage],
    cancel_scope: CancelScope,
) -> None:
    """Forwards every message from source to sink until source closes.

    A parse error on the source is fatal to the pump (raised, not
    forwarded) since there is no message to pass unmodified. Either way,
    the whole proxy tears down together — one side's connection ending
    isn't a state a passthrough proxy should try to survive.
    """
    try:
        async with source:
            async for message in source:
                if isinstance(message, Exception):
                    raise message
                await sink.send(message)
    finally:
        cancel_scope.cancel()
