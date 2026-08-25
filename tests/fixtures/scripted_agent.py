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
(PHASES.md): an argv entry of the form "SELECT:<substring>|
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
"""

from __future__ import annotations

import json
import sys

import anyio
import anyio.to_thread
import mcp_types as types
from mcp import ClientSession
from mcp.shared.message import SessionMessage


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


async def main() -> None:
    read_stream_writer, read_stream = anyio.create_memory_object_stream(0)
    write_stream, write_stream_reader = anyio.create_memory_object_stream(0)

    async with anyio.create_task_group() as tg:
        tg.start_soon(_stdin_reader, read_stream_writer)
        tg.start_soon(_stdout_writer, write_stream_reader)

        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            for spec in sys.argv[1:]:
                if spec.startswith("SELECT:"):
                    substring, _, args_json = spec[len("SELECT:"):].partition("|")
                    arguments = json.loads(args_json) if args_json else {}
                    tools_result = await session.list_tools()
                    matches = [t for t in tools_result.tools if substring in (t.description or "")]
                    if not matches:
                        # The planted failure mode: silently find nothing
                        # and call nothing, rather than error or guess --
                        # see this module's own docstring for why.
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

                tool_name, _, args_json = spec.partition("|")
                arguments = json.loads(args_json) if args_json else {}
                try:
                    result = await session.call_tool(tool_name, arguments)
                    outcome = {"tool": tool_name, "ok": True, "is_error": result.is_error}
                except Exception as exc:
                    outcome = {"tool": tool_name, "ok": False, "error": str(exc)}
                print(json.dumps(outcome), file=sys.stderr, flush=True)

        tg.cancel_scope.cancel()


if __name__ == "__main__":
    anyio.run(main)
