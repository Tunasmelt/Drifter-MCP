"""Replay-serving proxy mode: answers `tools/call`/`tools/list` from a
`ReplayStore` (F-11) instead of forwarding to a real server. SPEC.md §5's
architecture diagram calls this the "resolve (replay | synthetic | live)"
stage — this module is the `replay` branch; `live` is `record/proxy.py`
(F-01); `synthetic` (F-14) and mutation-serving (Gate 3) don't exist yet.

Design decision, stated explicitly per the task that asked for one: this
does NOT reuse `record/proxy.py`'s `_pump`/`Direction` machinery. `_pump`
is a byte/message RELAY — it forwards an opaque `SessionMessage` unmodified
to a paired stream, and its whole shutdown/sibling-stream contract (see its
own docstring) is built around a *second real process* existing on the
other end. Replay mode has no second process and nothing opaque to
forward: it must parse each request's method/params and construct a
brand-new response. That's a request RESPONDER, not a relay — a different
kind of component, not a variant of the same one.

Because of that, and because replay mode carries none of F-01's byte-
fidelity obligation (there's no real server's bytes to mirror faithfully —
the entire point is that no real server is involved), it's free to use the
MCP SDK's own `mcp.server.lowlevel.Server` framework — the same one
`mcp.server.mcpserver.MCPServer` builds on — instead of hand-rolling raw
JSON-RPC frame construction the way `record/proxy.py` deliberately does.
That framework handles `initialize`/protocol-version negotiation/
capabilities automatically; `record/proxy.py` avoids it specifically
because it needs raw, unmodified frames, a constraint that doesn't apply
here.

What IS shared with `record/proxy.py`: only the transport setup on our own
side (`mcp.server.stdio.stdio_server()`), since both modes present as an
MCP server to the connecting agent. Nothing about *how a request gets
answered* is shared. Mutation-serving (Gate 3) will hit the identical
fork: it also has to construct/rewrite content (a mutated `tools/list`, a
generated response on miss) rather than blindly forward, so it's a
responder too, and should land the same way — a `Server`-framework module,
not a `_pump` extension.

Structural guarantee, not a runtime check: this module never imports
`mcp.client.stdio` or anything else that spawns a subprocess or opens an
outbound connection. There is no code path here that can fall through to
a live server, because the code to do so does not exist in this file —
confirmed by inspection, not by a flag defaulting the "right" way (the
same standard SPEC.md §10/DEC-005 already holds mutation testing to).
"""

from __future__ import annotations

from pathlib import Path

import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.shared.exceptions import MCPError

from record.reader import read_session
from record.schema import ToolDescriptor, ToolsList
from replay.replay_store import RecordedResponse, ReplayStore

# Deliberately OUTSIDE JSON-RPC 2.0's entire reserved band (-32768..-32000
# — "the remainder of the space is available for application defined
# errors" per the spec). An earlier version of this file used -32001/
# -32002, inside "-32000..-32099: reserved for implementation-defined
# server-errors" — which sounds application-safe but isn't: checked
# directly against every negative int constant `mcp_types` actually
# defines (not assumed from the spec text alone) and found MCP itself
# already claims -32000 (CONNECTION_CLOSED), -32001 (REQUEST_TIMEOUT --
# an exact collision with the original REPLAY_MISS_CODE), -32020, -32021,
# -32022, and -32042. A client checking `.code == REQUEST_TIMEOUT` would
# have silently misread a replay MISS as a request timeout. Moved well
# outside the whole reserved band instead of hunting for more currently-
# unclaimed slots within it, since a future MCP SDK version claiming a
# slot I picked today is exactly the same failure mode recurring later.
# test_replay_proxy.py asserts these two never collide with any
# mcp_types-defined code, so this stays true as the SDK evolves, not
# just true today.
REPLAY_MISS_CODE = -31001
REPLAY_FAULT_CODE = -31002


def tools_served_from_session(path: Path) -> list[ToolDescriptor]:
    """Reads the `tools_served` manifest directly from a session JSONL
    file. `ReplayStore` has no manifest concept and isn't touched here —
    its contract (`index_session`/`lookup`) is unchanged; this is the
    "another way" of getting the manifest, read straight from the same
    kind of file `ReplayStore.index_session` already reads. Uses the
    *last* `ToolsList` record if a session somehow has more than one
    (Gate 1 never re-lists mid-session, but nothing in the schema
    forbids it) — the most recent manifest is the most representative.
    """
    tools_lists = [r for r in read_session(path) if isinstance(r, ToolsList)]
    return tools_lists[-1].tools_served if tools_lists else []


def _to_wire_tool(tool: ToolDescriptor) -> types.Tool:
    return types.Tool(name=tool.name, description=tool.description, input_schema=tool.input_schema)


def _synthesize_call_tool_result(hit: RecordedResponse) -> types.CallToolResult:
    """Structurally reconstructs a response matching `hit.result_shape`
    — never its original content, which was never recorded in the first
    place (F-02/F-04, shape-only). Full synthesis (F-14: matching every
    recorded key and array length) is explicit later-gate scope; this
    produces just enough to be a valid, honestly-labeled result that
    carries the recorded `is_error` faithfully — not a guess at F-14.
    """
    content_length = 1
    if hit.result_shape:
        keys = hit.result_shape.get("keys") or []
        array_lengths = hit.result_shape.get("array_lengths") or {}
        if "content" in keys:
            content_length = array_lengths.get("content", 1)
    placeholder = types.TextContent(
        type="text",
        text=(
            "[drifter replay: original payload was never recorded "
            "(F-02/F-04, shape-only) — F-14 full synthesis not implemented yet]"
        ),
    )
    return types.CallToolResult(content=[placeholder] * content_length, is_error=bool(hit.is_error))


async def run_replay_proxy(
    read_stream,
    write_stream,
    replay_store: ReplayStore,
    server_name: str,
    tools_served: list[ToolDescriptor],
) -> None:
    """Serves one MCP session over `read_stream`/`write_stream` entirely
    from `replay_store` and `tools_served`. Stream-parameterized (matching
    `Server.run()`'s own shape) rather than hardcoding
    `mcp.server.stdio.stdio_server()` internally: real stdio use and
    in-memory test use both just pass different streams in — how this
    gets pointed at a real agent (a stdio-wrapping entry point, the
    subprocess adapter that would launch it) is explicitly a separate,
    later task, not decided here.

    `tools/list` is served from `tools_served` as given — this function
    doesn't read the manifest itself; see `tools_served_from_session` for
    the "read it from the same session file" path.

    `tools/call` resolves via `replay_store.lookup(server_name, ...)`:
    HIT (not a fault) -> a synthesized `CallToolResult` carrying the
    recorded `is_error`. HIT (fault=True) -> `MCPError(REPLAY_FAULT_CODE)`
    — replaying a recorded protocol-level failure faithfully means
    responding with a protocol-level error, not a fake tool result. MISS
    -> `MCPError(REPLAY_MISS_CODE)`. Both are raised from the handler and
    propagate through `mcp.server.lowlevel.Server`'s dispatch as genuine
    wire-level JSON-RPC errors (verified directly against this SDK's
    dispatch code, not assumed: raising a plain exception from a
    lowlevel `Server` handler is not caught and converted to
    `is_error=True` the way `mcp.server.mcpserver.MCPServer`'s
    convenience wrapper does — that swallowing lives in `MCPServer`'s own
    `_handle_call_tool`, not in the lower-level dispatch this module
    uses).
    """
    tools = [_to_wire_tool(t) for t in tools_served]

    async def on_list_tools(ctx, params):
        return types.ListToolsResult(tools=tools)

    async def on_call_tool(ctx, params: types.CallToolRequestParams):
        hit = replay_store.lookup(server_name, params.name, params.arguments or {})
        if hit is None:
            raise MCPError(
                code=REPLAY_MISS_CODE,
                message=f"replay MISS: no recorded response for {server_name}.{params.name} with these arguments",
            )
        if hit.fault:
            raise MCPError(
                code=REPLAY_FAULT_CODE,
                message=f"replay: recorded call to {server_name}.{params.name} was a protocol-level fault",
            )
        return _synthesize_call_tool_result(hit)

    server = Server(name=f"drifter-replay-{server_name}", on_list_tools=on_list_tools, on_call_tool=on_call_tool)
    await server.run(read_stream, write_stream, server.create_initialization_options())
