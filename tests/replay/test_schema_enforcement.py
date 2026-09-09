"""Replay must enforce the SERVED (mutated) contract before lookup.

Found by external review (Codex) and reproduced exactly at the store
level before fixing. Given a corpus recorded with `customer_id` and an
active `parameter_rename` mutation to `customerId`:

    NEW name   (agent ADAPTED)        -> HIT via inverse   <- intended
    OLD name   (agent did NOT adapt)  -> HIT via exact     <- BUG
    BOGUS name (never valid)          -> HIT via semantic  <- BUG

Both bugs defeat the operator's entire purpose. `parameter_rename` exists
to answer "does the agent adapt when a parameter is renamed?", and replay
was answering YES for an agent that ignored the rename completely, plus
YES for a name no server would ever accept. F-13's semantic tier is the
second mechanism: it hashes the multiset of argument VALUES ignoring
names, so any parameter name carrying the right value resolves.

The fix is not to weaken the tiers -- they are correct for what they do --
but to check the mutated contract FIRST. A real server with the renamed
required property and `additionalProperties: false` would reject both
calls with a validation error, so replay returns one too. The agent then
sees the same failure it would see live, diverges, and the operator can
finally measure whether it adapted.

Ordering matters and is asserted below: validate against the served
schema, THEN resolve through the inverse map. Validating after lookup
would let a schema-invalid call resolve first, which is the bug.
"""

from __future__ import annotations

import anyio
import pytest
from mcp import ClientSession
from mcp.shared.exceptions import MCPError
from mcp.shared.memory import create_client_server_memory_streams

from mcp_drifter.record.schema import Environment, SessionStart, ToolCall, ToolDescriptor, ToolsList
from mcp_drifter.replay.replay_proxy import REPLAY_INVALID_ARGS_CODE, run_replay_proxy
from mcp_drifter.replay.replay_store import ReplayStore

SERVER = "srv"

RENAMED_SCHEMA = {
    "type": "object",
    "properties": {"customerId": {"type": "string"}},
    "required": ["customerId"],
    "additionalProperties": False,
}


@pytest.fixture
def corpus(tmp_path):
    """One recording made with the ORIGINAL parameter name."""
    served = [ToolDescriptor(name="get_customer", description="d", input_schema={"type": "object"})]
    lines = [
        SessionStart(session_id="s", seq=0, started_at="2026-01-01T00:00:00Z",
                     environment=Environment(tool_manifest_hash="h"), raw_frame_offset=0).model_dump_json(),
        ToolsList(session_id="s", seq=1, timestamp="2026-01-01T00:00:00Z", server=SERVER,
                  tools_raw=served, tools_served=served, raw_frame_offset=1).model_dump_json(),
        ToolCall(session_id="s", seq=2, timestamp="2026-01-01T00:00:01Z", server=SERVER,
                 tool_name="get_customer", arguments={"customer_id": "C123"},
                 result_shape={"type": "object", "keys": []}, is_error=False,
                 duration_ms=1.0, fault=False, raw_frame_offset=100).model_dump_json(),
    ]
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    store = ReplayStore()
    store.index_session(p)
    return store


async def _call(store, arguments):
    """Calls the mutated tool through a real replay proxy session."""
    tools_served = [ToolDescriptor(name="get_customer", description="d", input_schema=RENAMED_SCHEMA)]
    inverse = {"get_customer": {"customerId": "customer_id"}}
    outcome: dict = {}
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        async with anyio.create_task_group() as tg:
            tg.start_soon(run_replay_proxy, *server_streams, store, SERVER, tools_served, None, frozenset(), inverse)
            async with ClientSession(*client_streams) as session:
                await session.initialize()
                # Caught here rather than left to propagate: a raise inside
                # the task group surfaces as an ExceptionGroup, which would
                # make `pytest.raises(MCPError)` assert on the wrapper
                # instead of the real error.
                try:
                    outcome["result"] = await session.call_tool("get_customer", arguments)
                except MCPError as exc:
                    outcome["error"] = exc
            tg.cancel_scope.cancel()
    return outcome


@pytest.mark.anyio
async def test_the_adapted_call_still_resolves_through_the_inverse_map(corpus):
    """The fix must not break F-12. An agent that DID adapt uses the new
    name, satisfies the mutated schema, and resolves to the original
    recording via the inverse map.
    """
    outcome = await _call(corpus, {"customerId": "C123"})

    assert "error" not in outcome, f"adapted call was rejected: {outcome.get('error')}"
    assert outcome["result"].is_error is False


@pytest.mark.anyio
async def test_an_unadapted_call_using_the_old_name_is_rejected(corpus):
    """The headline bug. A real server with the renamed required property
    and additionalProperties:false rejects this; replay must too, or the
    operator reports an unadapted agent as healthy.
    """
    outcome = await _call(corpus, {"customer_id": "C123"})

    assert "error" in outcome, "an unadapted call resolved -- the mutation has no teeth"
    assert outcome["error"].error.code == REPLAY_INVALID_ARGS_CODE


@pytest.mark.anyio
async def test_a_bogus_parameter_name_is_rejected_rather_than_semantically_matched(corpus):
    """F-13's semantic tier matches on the multiset of argument VALUES
    ignoring names, so `totally_wrong` carrying the recorded value used to
    resolve. The tier is not wrong -- it just must not be reached by a
    call the served contract already forbids.
    """
    outcome = await _call(corpus, {"totally_wrong": "C123"})

    assert "error" in outcome, "a bogus parameter name resolved via the semantic tier"
    assert outcome["error"].error.code == REPLAY_INVALID_ARGS_CODE


@pytest.mark.anyio
async def test_a_tool_with_no_declared_schema_is_not_gated(corpus):
    """Enforcement applies only where a contract was actually declared.
    A tool whose manifest carries no properties has nothing to validate
    against, and inventing strictness there would reject calls a real
    server accepts.
    """
    tools_served = [ToolDescriptor(name="get_customer", description="d", input_schema={"type": "object"})]
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        async with anyio.create_task_group() as tg:
            tg.start_soon(run_replay_proxy, *server_streams, corpus, SERVER, tools_served)
            async with ClientSession(*client_streams) as session:
                await session.initialize()
                result = await session.call_tool("get_customer", {"customer_id": "C123"})
                tg.cancel_scope.cancel()

    assert result.is_error is False
