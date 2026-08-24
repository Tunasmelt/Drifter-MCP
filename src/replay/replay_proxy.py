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

Recording (F-34 prerequisite, closed here rather than left implicit):
`run_replay_proxy` had no recording hook at all until this addition —
confirmed by reading the whole file before writing any adapter code, not
assumed. `run_baseline`'s `run_once` needs a *new* session JSONL per run
(what the agent actually did this run, not the source corpus being
replayed from), so something has to produce one. Fixed by adding an
optional `on_message` parameter using the *same* `Direction`/
`MessageObserver` types `record/proxy.py` already defines (imported, not
redefined), so the existing `record/writer.py`'s `SessionRecorder` —
already built, tested, handling redaction/segmentation/fingerprinting —
plugs in completely unchanged, exactly like `cli/observe.py` already
wires it to passthrough mode. `mcp.server.lowlevel.Server` negotiates
`initialize` internally and dispatches by pre-parsed params, not raw
frames, so there's nothing to literally tap the way `record/proxy.py`
does; instead, the hook synthesizes the equivalent `JSONRPCRequest`/
`JSONRPCResponse`/`JSONRPCError` objects `SessionRecorder.observe()`
already expects, from data already available in the handlers
(`ctx.session.client_params` for agent identity, `tools_served` for the
manifest, the computed result per call).

One deliberate, documented consequence: the `initialize`+`tools/list`
exchange is synthesized once, eagerly, on the first request handled —
regardless of whether the agent's own first move is `tools/list` or
`tools/call` — so `tool_manifest_hash` is *always* populated for a
replay-served session. This sidesteps, by construction, the exact
ordering bug (`ToolCall` observed before `ToolsList`, leaving the hash
null) found and documented while verifying the real permanent-config
round trip. A real agent calling `tools/list` more than once mid-session
still only produces one recorded `ToolsList` — matching the same
"Gate 1 doesn't re-list mid-session" assumption `record/writer.py`
already documents for live recording.

A replay MISS and a replayed `fault=True` are recorded identically, as
`fault=True` — deliberately, not a shortcut: both look the same on the
wire to whatever's connected (a JSON-RPC protocol error, not a
`CallToolResult`), and the recorded schema describes what the agent
observed, not Drifter's internal reason for it. The two stay
distinguishable to Drifter itself via their different `MCPError` codes
(`REPLAY_MISS_CODE` vs `REPLAY_FAULT_CODE`) at the point they're raised;
collapsing that distinction in the *recorded* schema is a scope
decision for this prompt, not an oversight — a real MISS/fault-rate
breakdown for replay-served runs is fidelity-computation territory
(F-15/F-22), explicitly separate, later work.
"""

from __future__ import annotations

from itertools import count
from pathlib import Path

import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.shared.exceptions import MCPError
from mcp.shared.message import SessionMessage
from mcp_types import ErrorData, JSONRPCError, JSONRPCRequest, JSONRPCResponse

from record.proxy import Direction, MessageObserver
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
    on_message: MessageObserver | None = None,
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

    `on_message`, if given, receives synthesized `JSONRPCRequest`/
    `JSONRPCResponse`/`JSONRPCError` objects matching exactly what
    `record/writer.py`'s `SessionRecorder.observe()` already expects —
    see this module's docstring for why synthesis is necessary here
    (the framework negotiates `initialize` and pre-parses dispatch, so
    there are no raw frames to tap) and for the two documented,
    deliberate departures from live recording (eager one-time
    `initialize`+`tools/list` synthesis; MISS and replayed-fault both
    recorded as `fault=True`).
    """
    tools = [_to_wire_tool(t) for t in tools_served]
    request_ids = count(1)
    bootstrapped = False

    def _emit(direction: Direction, message) -> None:
        if on_message is not None:
            on_message(direction, SessionMessage(message))

    def _ensure_bootstrapped(ctx) -> None:
        nonlocal bootstrapped
        if bootstrapped:
            return
        bootstrapped = True

        client_params = ctx.session.client_params
        client_info = client_params.client_info if client_params is not None else None
        init_id = next(request_ids)
        _emit(
            Direction.AGENT_TO_SERVER,
            JSONRPCRequest(
                jsonrpc="2.0",
                id=init_id,
                method="initialize",
                params={"clientInfo": client_info.model_dump(mode="json", by_alias=True) if client_info else {}},
            ),
        )
        _emit(
            Direction.SERVER_TO_AGENT,
            JSONRPCResponse(
                jsonrpc="2.0",
                id=init_id,
                result={"serverInfo": {"name": f"drifter-replay-{server_name}", "version": ""}},
            ),
        )

        # Eager, unconditional — see module docstring: this is what
        # guarantees tool_manifest_hash is never null for a replay-served
        # session, regardless of whether the agent itself calls
        # tools/list before its first tools/call.
        list_id = next(request_ids)
        _emit(Direction.AGENT_TO_SERVER, JSONRPCRequest(jsonrpc="2.0", id=list_id, method="tools/list", params={}))
        _emit(
            Direction.SERVER_TO_AGENT,
            JSONRPCResponse(
                jsonrpc="2.0",
                id=list_id,
                result={"tools": [t.model_dump(mode="json", by_alias=True, exclude_unset=True) for t in tools]},
            ),
        )

    async def on_list_tools(ctx, params):
        _ensure_bootstrapped(ctx)
        return types.ListToolsResult(tools=tools)

    async def on_call_tool(ctx, params: types.CallToolRequestParams):
        _ensure_bootstrapped(ctx)
        arguments = params.arguments or {}
        req_id = next(request_ids)
        _emit(
            Direction.AGENT_TO_SERVER,
            JSONRPCRequest(jsonrpc="2.0", id=req_id, method="tools/call", params={"name": params.name, "arguments": arguments}),
        )

        hit = replay_store.lookup(server_name, params.name, arguments)
        if hit is None:
            message = f"replay MISS: no recorded response for {server_name}.{params.name} with these arguments"
            _emit(Direction.SERVER_TO_AGENT, JSONRPCError(jsonrpc="2.0", id=req_id, error=ErrorData(code=REPLAY_MISS_CODE, message=message)))
            raise MCPError(code=REPLAY_MISS_CODE, message=message)
        if hit.fault:
            message = f"replay: recorded call to {server_name}.{params.name} was a protocol-level fault"
            _emit(Direction.SERVER_TO_AGENT, JSONRPCError(jsonrpc="2.0", id=req_id, error=ErrorData(code=REPLAY_FAULT_CODE, message=message)))
            raise MCPError(code=REPLAY_FAULT_CODE, message=message)

        result = _synthesize_call_tool_result(hit)
        _emit(
            Direction.SERVER_TO_AGENT,
            JSONRPCResponse(jsonrpc="2.0", id=req_id, result=result.model_dump(mode="json", by_alias=True, exclude_unset=True)),
        )
        return result

    server = Server(name=f"drifter-replay-{server_name}", on_list_tools=on_list_tools, on_call_tool=on_call_tool)
    await server.run(read_stream, write_stream, server.create_initialization_options())
