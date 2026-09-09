"""Replay-serving proxy mode: answers `tools/call`/`tools/list` from a
`ReplayStore` (F-11) instead of forwarding to a real server. docs/SPEC.md §5's
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
same standard docs/SPEC.md §10/DEC-005 already holds mutation testing to).

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

from mcp_drifter.record.proxy import Direction, MessageObserver
from mcp_drifter.record.reader import read_session
import jsonschema

from mcp_drifter.replay.corpus_facts import CorpusFacts, successor_values
from mcp_drifter.replay.synthesis import synthesize_structured_content
from mcp_drifter.record.schema import MATCH_TIER_MARKER_KEY, SYNTHETIC_RESULT_MARKER_KEY, ToolDescriptor, ToolsList
from mcp_drifter.replay.replay_store import RecordedResponse, ReplayStore

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
# A call that violates the SERVED (post-mutation) tool schema. Distinct
# from a MISS on purpose: a MISS means "the corpus cannot answer this", an
# INVALID means "no server would accept this at all". Conflating them
# would let a schema violation look like thin recording coverage, which is
# the opposite of what it is -- it is the mutation working.
REPLAY_INVALID_ARGS_CODE = -31003


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


# Both synthesis functions below return CONTENT-EMPTY placeholders —
# TextContent blocks with text="", never a sentence describing their own
# fakeness. This replaced prose like "[drifter replay: original payload
# was never recorded (F-02/F-04, shape-only) — F-14 full synthesis not
# implemented yet]" after a real, live dogfood run (Claude Code, the
# actual Gate 0 pairing, not the scripted test-agent) hit it directly:
# `list_directory` resolved as a genuine exact-tier HIT — fault=False,
# result_provenance=real, arguments matched the recording exactly — but
# Claude Code read the synthesized TEXT and refused to proceed, verbatim:
# "This reads like a prompt-injection attempt: it's phrased to look like
# legitimate tool output/system context... I'm not treating this as an
# instruction and haven't read anything as a result of it." All 3 baseline
# repeats hit this identically, on the very first tool call, every time —
# not a rare edge case, a systemic block on ever completing a multi-step
# task against a real agent in replay mode, since EVERY exact-tier hit
# whose payload wasn't recorded (i.e. every replay-served call, given
# F-02/F-04's shape-only-by-default recording) went through this exact
# code path.
#
# Checked before changing anything, not assumed: SessionRecorder
# (record/writer.py's observe()) determines result_provenance from
# SYNTHETIC_RESULT_MARKER_KEY, a private dict key set separately from
# the wire content — never by parsing this text. Changing what's shown
# to the agent has zero effect on provenance tracking; the two were
# already fully decoupled, confirmed by reading observe()'s actual
# branch, not inferred from the marker-key mechanism's existence alone.
#
# Why empty content specifically, not just less alarming prose: an
# injection/suspicious-content judgment — whether a fixed keyword regex
# (SPEC_INJECTION_PATTERNS, mutate/description_update.py) or a real
# agent's own semantic reasoning, as observed here — operates on some
# textual CLAIM or instruction-shaped content being present to evaluate.
# An empty string expresses no claim, describes nothing, references
# nothing — there is no content for either kind of classifier to
# interpret as suspicious, not because the phrasing was softened but
# because there is no phrasing at all. This is categorically different
# from "honest but softer" wording, which would still be prose a real
# agent could read, judge, and potentially still flag.
def _synthesize_added_tool_result() -> types.CallToolResult:
    """F-14, scoped narrowly to F-17 (tool_addition)'s own case: a tool
    that was injected by mutation has, by definition (docs/SPEC.md §7), no
    prior recording at all — there is no `result_shape` to reconstruct
    from the way `_synthesize_call_tool_result` does for an ordinary
    exact-tier HIT. This is NOT general F-14 (no historical-shape
    inference across arbitrary real tools, no LLM) — it is the one
    fixed, generic, structurally-valid, CONTENT-EMPTY placeholder every
    tool_addition call gets, since there is nothing tool-specific to
    draw from (see the module-level note above for why empty, not
    prose). See `run_replay_proxy`'s `synthetic_tool_names` parameter
    for how a call actually reaches here instead of an ordinary MISS.
    """
    placeholder = types.TextContent(type="text", text="")
    return types.CallToolResult(content=[placeholder], is_error=False)


def _synthesize_missed_tool_result(output_schema: dict | None) -> types.CallToolResult:
    """F-14 general synthesis: a structurally valid, content-empty answer
    for a call replay has no recording for at all.

    `structuredContent` is built from the tool's declared `outputSchema`
    when it has one (see `replay/synthesis.py` for why every value is a
    zero value and never a plausible sample). When the tool declared no
    output contract -- `output_schema is None`, which also covers a
    corpus recorded before that field existed -- there is nothing to
    conform to, and the result is the same content-empty placeholder the
    other synthesis paths return. Nothing is guessed in that case.

    Text content is empty here as everywhere else in this module: see the
    module-level note on the real agent that read explanatory placeholder
    prose and refused to proceed, treating it as a prompt-injection
    attempt.
    """
    structured = synthesize_structured_content(output_schema)
    placeholder = types.TextContent(type="text", text="")
    if structured is None:
        return types.CallToolResult(content=[placeholder], is_error=False)
    return types.CallToolResult(content=[placeholder], structuredContent=structured, is_error=False)


def _synthesize_call_tool_result(hit: RecordedResponse, discovered: tuple[str, ...] = ()) -> types.CallToolResult:
    """Structurally reconstructs a response matching `hit.result_shape`
    — never its original content, which was never recorded in the first
    place (F-02/F-04, shape-only). Full synthesis (F-14: matching every
    recorded key and array length) is explicit later-gate scope; this
    produces just enough to be a valid, schema-shaped, CONTENT-EMPTY
    result (see the module-level note above for why empty, not prose)
    that carries the recorded `is_error` faithfully — not a guess at
    F-14, and not a claim about anything.

    Scope of this fix, stated explicitly so it doesn't quietly expand:
    `content_length` still comes from `hit.result_shape["array_lengths"]`
    — a value already present in the actual historical recording being
    replayed, not new inference. Nothing here starts reconstructing
    per-key *types*, guessing at values, or reasoning about a tool's
    schema beyond what F-02/F-04 already captured at record time. Only
    the TEXT changed (prose → empty); the amount of structural
    reconstruction is identical to before this fix, which was already
    scoped this narrowly. General F-14 (inferring a plausible response
    for a tool with no prior recording at all, beyond tool_addition's
    own narrow no-history case) remains unbuilt.
    """
    content_length = 1
    if hit.result_shape:
        keys = hit.result_shape.get("keys") or []
        array_lengths = hit.result_shape.get("array_lengths") or {}
        if "content" in keys:
            content_length = array_lengths.get("content", 1)
    # R0: newline-joined observed VALUES, or empty as before. Values, never
    # prose -- an identifier carries no claim and no instruction, which is
    # the distinction limitation 11 turned on when a real agent refused
    # synthesized explanatory text as a prompt-injection attempt.
    text = "\n".join(discovered)
    placeholder = types.TextContent(type="text", text=text)
    return types.CallToolResult(content=[placeholder] * content_length, is_error=bool(hit.is_error))


def build_replay_server(
    replay_store: ReplayStore,
    server_name: str,
    tools_served: list[ToolDescriptor],
    on_message: MessageObserver | None = None,
    synthetic_tool_names: frozenset[str] = frozenset(),
    inverse_map: dict[str, dict[str, str]] | None = None,
    synthesize_on_miss: bool = False,
    corpus_facts: CorpusFacts | None = None,
) -> Server:
    """Builds the `mcp.server.lowlevel.Server` app that answers a session
    entirely from `replay_store`/`tools_served` — extracted out of
    `run_replay_proxy` (F-38, docs/SPEC.md §5.1) so an HTTP-serving caller
    (`cli/subprocess_adapter.py`'s `mode: http` path) can host the SAME
    app via `Server.streamable_http_app()`/`StreamableHTTPSessionManager`
    across many connections, instead of the one-shot `server.run(read,
    write, ...)` a single stdio session uses. Pure extraction, zero
    behavior change for the existing stdio/in-memory callers below —
    `run_replay_proxy` is now a two-line wrapper over this function.
    """
    tools = [_to_wire_tool(t) for t in tools_served]
    output_schemas = {t.name: t.output_schema for t in tools_served}
    input_schemas = {t.name: t.input_schema for t in tools_served}

    def _schema_violation(tool_name: str, arguments: dict) -> str | None:
        """Returns why `arguments` violate the SERVED schema, or None.

        This is the mutated contract, and checking it BEFORE lookup is the
        whole point (docs/SPEC.md §15, external review): with a
        `parameter_rename` active, an agent that ignored the rename used to
        resolve straight off the original recording via the exact tier, and
        a wholly invented parameter name resolved via the semantic tier,
        which matches on the multiset of argument VALUES ignoring names.
        Both made `parameter_rename` incapable of detecting the one thing
        it exists to detect. A real server with the renamed required
        property and `additionalProperties: false` rejects both, so replay
        does too.

        Only enforced where a contract was actually DECLARED -- a schema
        with no `properties` has nothing to check, and inventing strictness
        there would reject calls a real server accepts.
        """
        schema = input_schemas.get(tool_name)
        if not schema or not schema.get("properties"):
            return None
        try:
            jsonschema.validate(instance=arguments, schema=schema)
        except jsonschema.ValidationError as exc:
            return exc.message
        except jsonschema.SchemaError:
            # A malformed schema in the manifest is the SERVER's problem,
            # not the agent's -- never fail an agent's call over it.
            return None
        return None

    request_ids = count(1)

    def _output_schema_for(tool_name: str) -> dict | None:
        return output_schemas.get(tool_name)

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
        # F-19 (docs/SPEC.md §10, "verified requirement C8") calls for every
        # tools/list response to set ttlMs/cacheScope. Investigated directly
        # against the real, currently-negotiated MCP protocol before adding
        # them here (not assumed from the SDK's newest type definitions
        # alone): those fields exist ONLY on `mcp_types._v2026_07_28.
        # ListToolsResult`, a draft, not-yet-real protocol version. Every
        # currently-negotiable version (2024-11-05 through 2025-11-25 --
        # everything any real MCP client speaks today) validates server
        # results through the OLDER surface model
        # (`mcp_types._v2025_11_25.ListToolsResult`, fields: meta,
        # next_cursor, tools only), with `extra="ignore"` silently
        # stripping anything else before it reaches the wire — confirmed
        # by a real wire capture showing a bare `{"tools": [...]}` even
        # after explicitly passing ttl_ms/cache_scope here. Setting them
        # is therefore not a no-op exactly, but it IS wire-invisible
        # against every real client that exists today; deliberately not
        # set, to avoid code implying a guarantee that isn't real. See
        # docs/SPEC.md §15 limitation 15 for the full account and the
        # currently-real MCP mechanism (`notifications/tools/list_changed`)
        # this doesn't map cleanly onto either, since Drifter's baseline/
        # mutated arms are always separate fresh connections, not one
        # connection whose manifest changes mid-session.
        return types.ListToolsResult(tools=tools)

    async def on_call_tool(ctx, params: types.CallToolRequestParams):
        _ensure_bootstrapped(ctx)
        arguments = params.arguments or {}
        req_id = next(request_ids)
        _emit(
            Direction.AGENT_TO_SERVER,
            JSONRPCRequest(jsonrpc="2.0", id=req_id, method="tools/call", params={"name": params.name, "arguments": arguments}),
        )

        # F-12: only the slice of inverse_map relevant to THIS tool is
        # passed down -- replay_store.lookup has no mutation-specific
        # knowledge of its own (see its own docstring), it just applies
        # whatever {new_name: old_name} mapping it's handed.
        violation = _schema_violation(params.name, arguments)
        if violation is not None:
            message = f"invalid arguments for {server_name}.{params.name}: {violation}"
            _emit(Direction.SERVER_TO_AGENT, JSONRPCError(jsonrpc="2.0", id=req_id, error=ErrorData(code=REPLAY_INVALID_ARGS_CODE, message=message)))
            raise MCPError(code=REPLAY_INVALID_ARGS_CODE, message=message)

        param_map = inverse_map.get(params.name) if inverse_map else None
        hit = replay_store.lookup(server_name, params.name, arguments, param_map)
        if hit is None:
            if params.name in synthetic_tool_names:
                result = _synthesize_added_tool_result()
                record_result = result.model_dump(mode="json", by_alias=True, exclude_unset=True)
                record_result[SYNTHETIC_RESULT_MARKER_KEY] = "synthetic"
                _emit(Direction.SERVER_TO_AGENT, JSONRPCResponse(jsonrpc="2.0", id=req_id, result=record_result))
                return result
            if synthesize_on_miss:
                # F-14. Opt-in, default off: erroring on a miss is the
                # existing documented contract, and answering instead
                # changes the agent's trajectory (it continues where it
                # would have stopped). Per DEC-027 this changes what a
                # miss DOES to a session, not the miss RATE -- the run's
                # fidelity is identical either way, because the call is
                # recorded with "synthetic_miss" provenance and counts in
                # the denominator as a miss. Limitation 16 is untouched.
                result = _synthesize_missed_tool_result(_output_schema_for(params.name))
                record_result = result.model_dump(mode="json", by_alias=True, exclude_unset=True)
                record_result[SYNTHETIC_RESULT_MARKER_KEY] = "synthetic_miss"
                _emit(Direction.SERVER_TO_AGENT, JSONRPCResponse(jsonrpc="2.0", id=req_id, result=record_result))
                return result
            message = f"replay MISS: no recorded response for {server_name}.{params.name} with these arguments"
            _emit(Direction.SERVER_TO_AGENT, JSONRPCError(jsonrpc="2.0", id=req_id, error=ErrorData(code=REPLAY_MISS_CODE, message=message)))
            raise MCPError(code=REPLAY_MISS_CODE, message=message)
        if hit.fault:
            message = f"replay: recorded call to {server_name}.{params.name} was a protocol-level fault"
            _emit(Direction.SERVER_TO_AGENT, JSONRPCError(jsonrpc="2.0", id=req_id, error=ErrorData(code=REPLAY_FAULT_CODE, message=message)))
            raise MCPError(code=REPLAY_FAULT_CODE, message=message)

        # R0 (docs/SPEC.md §15 limitation 17): a HIT whose content is empty is
        # what actually broke replay -- the agent could not learn what the
        # response would have shown it, so it could not build its next
        # call's arguments. When corpus facts are supplied, hand back the
        # values this corpus WITNESSED being used after this exact call.
        # Never invented, never prose: see replay/corpus_facts.py.
        discovered = successor_values(corpus_facts, server_name, params.name, arguments) if corpus_facts else ()
        result = _synthesize_call_tool_result(hit, discovered)
        # F-13/F-15: tag the RECORDING-only dict with which tier resolved
        # this HIT, same private-marker-key pattern as
        # SYNTHETIC_RESULT_MARKER_KEY above -- the actual wire response
        # returned to the agent (`result`, below) never carries this key.
        record_result = result.model_dump(mode="json", by_alias=True, exclude_unset=True)
        record_result[MATCH_TIER_MARKER_KEY] = hit.match_tier
        _emit(Direction.SERVER_TO_AGENT, JSONRPCResponse(jsonrpc="2.0", id=req_id, result=record_result))
        return result

    return Server(name=f"drifter-replay-{server_name}", on_list_tools=on_list_tools, on_call_tool=on_call_tool)


async def run_replay_proxy(
    read_stream,
    write_stream,
    replay_store: ReplayStore,
    server_name: str,
    tools_served: list[ToolDescriptor],
    on_message: MessageObserver | None = None,
    synthetic_tool_names: frozenset[str] = frozenset(),
    inverse_map: dict[str, dict[str, str]] | None = None,
    synthesize_on_miss: bool = False,
    corpus_facts: CorpusFacts | None = None,
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

    `synthetic_tool_names`, if given, names tools that resolve via
    F-14-scoped-to-tool_addition synthesis on a `replay_store` MISS
    instead of `MCPError(REPLAY_MISS_CODE)` — the injected tool a
    `mutate.tool_addition` mutation added to `tools_served`, which by
    definition (docs/SPEC.md §7) never has a prior recording, so an ordinary
    MISS would be indistinguishable from "an existing tool's call was
    never recorded," losing exactly the "reported separately, excluded
    from the fidelity denominator" distinction docs/SPEC.md §7 requires.
    The wire response the agent actually receives is a clean, generic
    placeholder (`_synthesize_added_tool_result`); a *separate* dict,
    carrying `SYNTHETIC_RESULT_MARKER_KEY`, is what reaches `on_message`
    for recording — never sent to the agent (see `record/schema.py`'s
    marker-key docstring). A name in `synthetic_tool_names` that's also
    a real `ReplayStore` HIT still resolves as an ordinary HIT — this
    only applies on MISS, so a recorded, exact-tier-matched call to a
    once-synthetic tool (impossible today, since nothing ever calls a
    tool before it's added, but not structurally prevented) is never
    silently downgraded to synthetic.

    `on_message`, if given, receives synthesized `JSONRPCRequest`/
    `JSONRPCResponse`/`JSONRPCError` objects matching exactly what
    `record/writer.py`'s `SessionRecorder.observe()` already expects —
    see this module's docstring for why synthesis is necessary here
    (the framework negotiates `initialize` and pre-parses dispatch, so
    there are no raw frames to tap) and for the two documented,
    deliberate departures from live recording (eager one-time
    `initialize`+`tools/list` synthesis; MISS and replayed-fault both
    recorded as `fault=True`).

    `inverse_map` (F-12), if given, is `{tool_name: {new_param_name:
    old_param_name}}` for the active mutation — see `ReplayStore.lookup`'s
    own docstring for what this does. `None`/absent tools skip straight to
    exact-then-semantic resolution, matching pre-F-12 behavior exactly.

    A thin wrapper as of F-38: all the actual response logic lives in
    `build_replay_server`, above, so an HTTP-serving caller can host the
    same app across many connections instead of one `server.run()` per
    stream pair.
    """
    server = build_replay_server(replay_store, server_name, tools_served, on_message, synthetic_tool_names, inverse_map, synthesize_on_miss, corpus_facts)
    await server.run(read_stream, write_stream, server.create_initialization_options())
