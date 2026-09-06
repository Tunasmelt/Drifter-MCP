"""Trivial scripted "agent" for cli/subprocess_adapter.py's tests.

Acts as a genuine MCP client entirely over its own process's real
stdin/stdout — it spawns nothing itself. Whatever spawned THIS script
(the adapter under test) is expected to be on the other end of these
same pipes, playing the server role — the roles are inverted from the
usual "client spawns server" convention, matching
cli/subprocess_adapter.py's stdio-wiring design (see that module's
docstring, point 2).

Uses the SDK's real ClientSession rather than hand-rolled JSON-RPC, so
the initialize/notifications-initialized handshake happens exactly as a
real agent framework would produce it — the point of testing against a
*subprocess*, not in-memory streams, is to exercise this for real.

Driven by argv so one script covers both "everything golden-fixture
recorded" and "one call is a deliberate MISS" without near-duplicate
scripts: each argv entry is "tool_name|json_arguments". One JSON
outcome line per call goes to stderr (never stdout — stdout is the
wire) so the test can assert on what actually happened.

Second mode, added for Gate 3's kill-criterion brittle-agent check
(docs/PHASES.md): an argv entry of the form "SELECT:<substring>|
<json_arguments>" makes this script pick its tool by calling
`list_tools()` and selecting the FIRST tool whose *description*
contains `<substring>` (a literal substring, case-sensitive) — a
deliberately fragile, description-text-dependent selection mechanism,
standing in for "a real agent's tool routing that happens to key off
exact wording" per the kill criterion's own text ("construct a known-
brittle test agent"). If no tool's description contains the substring,
this script calls NOTHING for that entry (not an error, not a fallback
guess) — the planted failure mode is "selection silently finds
nothing," which shows up downstream as an empty tool-call path, not a
crash. This is what makes `description_update`'s synonym substitution
a plausible, deterministic way to break it: a substring chosen from a
tool's real, pre-mutation description can be verified (see
tests/cli/test_kill_criterion_brittle_agent.py) to no longer appear
after a specific seed's substitution, with no code change needed here
to prove the harness detects it.

Third mode, added for the Gate 4 pre-handoff dry run (tests/cli/
gate4_dry_run/): an argv entry of the form "LAST_TOOL|<json_arguments>"
calls `list_tools()` and selects whichever tool is LAST in the returned
list -- a deliberately fragile, position-dependent selection mechanism.
`tool_addition` (mutate/tool_addition.py's add_tool) always APPENDS its
new tool to the end of the served manifest, so a position-dependent
agent that worked correctly against the unmutated manifest silently
calls the wrong (newly-injected) tool once that operator is active --
a real, planted regression this operator can cause, distinct from and
never exercised by the SELECT mode above (which only tests
description_update).

Transport mode, added for F-38 (docs/SPEC.md §5.1): this script checks
its own environment for the variable named `DRIFTER_PROXY_URL_ENV_VAR`
(so a test can point it at a custom `AgentConfig.env_var` name — see
`_DEFAULT_ENV_VAR_NAME` below for the fallback), then looks up THAT
variable name and, if set, connects OUT to its value via
`mcp.client.streamable_http.streamable_http_client` instead of treating
its own stdin/stdout as the wire at all. Same argv-driven spec loop
either way (`_run_specs`, shared): the point of this mode split is
proving the transport swap doesn't change anything about how a real
agent's tool-selection logic is written, only how it connects.
"""

from __future__ import annotations

import json
import os
import sys

import anyio
import anyio.to_thread
import mcp_types as types
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.message import SessionMessage

_DEFAULT_ENV_VAR_NAME = "DRIFTER_PROXY_URL"


async def _stdin_reader(write_to) -> None:
    async with write_to:
        while True:
            # abandon_on_cancel=True: sys.stdin.readline() is a plain
            # blocking call with no async-native equivalent here -- by
            # anyio's default (abandon_on_cancel=False), cancelling this
            # task would NOT abandon a thread already parked in that
            # blocking read, so the surrounding task group's
            # cancel_scope.cancel() (see main(), once all calls are
            # done) would never actually complete: it would wait
            # forever for a readline() that only returns once the
            # PARENT closes our stdin, which it doesn't do until it
            # gives up on process.wait() timing out. Confirmed this was
            # a real, reproducing hang (not theoretical) before adding
            # this -- both adapter tests took ~30s each, exactly
            # matching their timeout_s, before this fix.
            line = await anyio.to_thread.run_sync(sys.stdin.readline, abandon_on_cancel=True)
            if not line:
                return
            line = line.strip()
            if not line:
                continue
            try:
                message = types.jsonrpc_message_adapter.validate_json(line, by_name=False)
            except Exception as exc:
                await write_to.send(exc)
                continue
            await write_to.send(SessionMessage(message))


async def _stdout_writer(read_from) -> None:
    async with read_from:
        async for session_message in read_from:
            line = session_message.message.model_dump_json(by_alias=True, exclude_unset=True)
            sys.stdout.write(line + "\n")
            sys.stdout.flush()


async def _run_specs(session: ClientSession) -> None:
    """The shared per-argv-spec loop -- identical regardless of which
    transport got `session` connected (stdio pump vs. real HTTP), which
    is the whole point of factoring this out for F-38: a real agent's
    tool-selection logic doesn't need to know or care which transport
    it's running over."""
    for spec in sys.argv[1:]:
        if spec.startswith("SELECT:"):
            substring, _, args_json = spec[len("SELECT:"):].partition("|")
            arguments = json.loads(args_json) if args_json else {}
            tools_result = await session.list_tools()
            matches = [t for t in tools_result.tools if substring in (t.description or "")]
            if not matches:
                # The planted failure mode: silently find nothing and
                # call nothing, rather than error or guess -- see this
                # module's own docstring for why.
                outcome = {"select": substring, "ok": False, "error": "no tool description matched"}
                print(json.dumps(outcome), file=sys.stderr, flush=True)
                continue
            tool_name = matches[0].name
            try:
                result = await session.call_tool(tool_name, arguments)
                outcome = {"select": substring, "matched_tool": tool_name, "ok": True, "is_error": result.is_error}
            except Exception as exc:
                outcome = {"select": substring, "matched_tool": tool_name, "ok": False, "error": str(exc)}
            print(json.dumps(outcome), file=sys.stderr, flush=True)
            continue

        if spec.startswith("LAST_TOOL|") or spec == "LAST_TOOL":
            _, _, args_json = spec.partition("|")
            arguments = json.loads(args_json) if args_json else {}
            tools_result = await session.list_tools()
            if not tools_result.tools:
                outcome = {"last_tool": True, "ok": False, "error": "server reported zero tools"}
                print(json.dumps(outcome), file=sys.stderr, flush=True)
                continue
            tool_name = tools_result.tools[-1].name
            try:
                result = await session.call_tool(tool_name, arguments)
                outcome = {"last_tool": True, "matched_tool": tool_name, "ok": True, "is_error": result.is_error}
            except Exception as exc:
                outcome = {"last_tool": True, "matched_tool": tool_name, "ok": False, "error": str(exc)}
            print(json.dumps(outcome), file=sys.stderr, flush=True)
            continue

        tool_name, _, args_json = spec.partition("|")
        arguments = json.loads(args_json) if args_json else {}
        try:
            result = await session.call_tool(tool_name, arguments)
            outcome = {"tool": tool_name, "ok": True, "is_error": result.is_error}
        except Exception as exc:
            outcome = {"tool": tool_name, "ok": False, "error": str(exc)}
        print(json.dumps(outcome), file=sys.stderr, flush=True)


async def _main_http(url: str) -> None:
    async with streamable_http_client(url) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            await _run_specs(session)
    # A real "final answer" -- captured by cli/subprocess_adapter.py's
    # run_agent_subprocess_http as plain stdout text now that stdout
    # isn't the wire protocol under this mode.
    print("done", flush=True)


async def _main_stdio() -> None:
    read_stream_writer, read_stream = anyio.create_memory_object_stream(0)
    write_stream, write_stream_reader = anyio.create_memory_object_stream(0)

    async with anyio.create_task_group() as tg:
        tg.start_soon(_stdin_reader, read_stream_writer)
        tg.start_soon(_stdout_writer, write_stream_reader)

        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            await _run_specs(session)

        tg.cancel_scope.cancel()


async def main() -> None:
    env_var_name = os.environ.get("DRIFTER_PROXY_URL_ENV_VAR", _DEFAULT_ENV_VAR_NAME)
    url = os.environ.get(env_var_name)
    if url:
        await _main_http(url)
    else:
        await _main_stdio()


if __name__ == "__main__":
    anyio.run(main)
