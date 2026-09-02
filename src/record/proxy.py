"""Proxy passthrough over stdio (F-01).

Drifter is invoked as the MCP server command in the client's config. It
spawns the real server as a child process and forwards every JSON-RPC frame
bidirectionally, unmodified, between the agent (our own stdin/stdout) and
the real server (the child process's stdin/stdout).

Framing on both sides comes from the official MCP SDK's stdio transport
(`mcp.client.stdio.stdio_client`, `mcp.server.stdio.stdio_server`) rather
than hand-rolled JSON-RPC parsing, per docs/FEATURES.md F-01.

`on_message` (optional) is a pure observer hook for record/writer.py to tap
into: it never alters what's forwarded, and its default (None) reproduces
Prompt 2's exact passthrough-only behavior.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterable, Callable
from enum import Enum

import anyio
from anyio.abc import CancelScope, ObjectSendStream
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage

logger = logging.getLogger(__name__)


class Direction(Enum):
    """Which side of the proxy a message is travelling toward."""

    AGENT_TO_SERVER = "agent_to_server"
    SERVER_TO_AGENT = "server_to_agent"


MessageObserver = Callable[[Direction, "SessionMessage | Exception"], None]


async def run_passthrough_proxy(
    server: StdioServerParameters,
    on_message: MessageObserver | None = None,
) -> None:
    """Spawns `server` and pipes frames to/from our own stdio, unmodified.

    Runs until either side closes its connection, then tears the other
    side down too — closing the child's pipes and terminating it if it
    hasn't already exited, via `stdio_client`'s own shutdown sequence.
    """
    async with stdio_client(server) as (server_read, server_write):
        async with stdio_server() as (agent_read, agent_write):
            async with anyio.create_task_group() as tg:
                tg.start_soon(_pump, agent_read, server_write, tg.cancel_scope, Direction.AGENT_TO_SERVER, on_message)
                tg.start_soon(_pump, server_read, agent_write, tg.cancel_scope, Direction.SERVER_TO_AGENT, on_message)


async def _pump(
    source: AsyncIterable[SessionMessage | Exception],
    sink: ObjectSendStream[SessionMessage],
    cancel_scope: CancelScope,
    direction: Direction,
    on_message: MessageObserver | None,
) -> None:
    """Forwards every message from source to sink until source closes.

    A parse error on the source is fatal to the pump (raised, not
    forwarded) since there is no message to pass unmodified. Either way,
    the whole proxy tears down together — one side's connection ending
    isn't a state a passthrough proxy should try to survive.

    Closing `sink` in `finally` is load-bearing, not tidiness: `sink` is
    the other pump's `source`'s *sibling* stream — e.g. `agent_write` here
    is what `stdio_server()`'s own internal `stdout_writer()` task reads
    from via its paired receiver. Nothing else closes it: `stdio_server()`
    hands it to the caller and expects the caller to close it, and unlike
    `stdio_client()` (which defensively closes its own write_stream during
    its shutdown regardless of what the caller did), it has no fallback of
    its own. Without this, that task blocks forever waiting for a message
    or a close that never comes, so `stdio_server()`'s context manager
    never exits, `run_passthrough_proxy` never returns, and the process
    survives only until an external timeout kills it — silently losing
    anything a caller (record/writer.py's close()) would have flushed on
    a clean return.

    `sink.aclose()` is wrapped in its own try/except: if the `try` block
    above is already propagating an exception (a parse error, or a send
    failure) and `sink.aclose()` then *also* raised, Python's `finally`
    semantics would let the aclose() failure silently replace the
    original as what actually propagates out of this function — the real
    cause would only survive as `__context__`, invisible to a plain
    `except SomeType:` upstream. For the concrete stream types this pump
    is actually called with (`MemoryObjectSendStream` via `stdio_client`,
    `ContextSendStream` via `stdio_server`), `aclose()` is a pure,
    idempotent state update with no I/O and cannot currently raise — this
    is a safety net against that ceasing to be true (e.g. a future
    transport swap), not a fix for an observed failure.
    """
    try:
        async with source:
            async for message in source:
                if on_message is not None:
                    on_message(direction, message)
                if isinstance(message, Exception):
                    raise message
                await sink.send(message)
    finally:
        cancel_scope.cancel()
        try:
            await sink.aclose()
        except Exception:
            logger.exception("sink.aclose() failed while tearing down a proxy pump (%s)", direction)
