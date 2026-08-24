"""Subprocess agent adapter (F-34), SPEC.md §11's `agent.command` shape.

Spawns a real CLI agent as a subprocess, wires it to a replay-serving
proxy (`replay/replay_proxy.py`, F-11/last commit), and produces the
session JSONL `evaluate.baseline.run_baseline`'s `run_once` callable is
waiting for. Module: `cli/` — FEATURES.md's `## Module: cli/ and
adapters` heading, cross-referenced against CLAUDE.md's module list (no
standalone `adapters/`), same correction already applied twice this gate
to `record/proxy.py` and `evaluate/baseline.py`.

Four scope decisions, stated explicitly rather than discovered mid-review:

1. Config-loading is OUT of scope here. `cli/config.py`'s loader only
   reads `servers:`/`record.dir` today; extending it to parse SPEC.md
   §11's `agent:` block is a separate, later concern. This module takes
   an already-resolved argv and an already-substituted task prompt —
   whoever calls `run_agent_subprocess` is responsible for reading
   `agent.command` out of drifter.yaml and doing the `{task.prompt}`
   templating before calling in.

2. Transport is stdio-wired, not "a URL via env var." FEATURES.md's own
   Technical note for F-34 describes injecting a proxy URL by
   environment variable and capturing the agent's stdout as a separate
   "final answer" string — but SPEC.md's architecture diagram is
   explicit that v0 is stdio-only ("MCP (stdio in v0; +HTTP in v1)"),
   and no HTTP/URL-addressable transport exists anywhere in this
   codebase. Raised to the user as a genuine conflict between two locked
   planning documents rather than silently picked; resolved by explicit
   choice: wire the spawned agent's own stdin/stdout directly to an
   in-process `run_replay_proxy` instance, matching every other
   component built this gate, and drop the separate final-answer-string
   capture. Gate 2's baseline/mutation work needs tool-call PATHS (via
   the recorded session JSONL), not a distinct final-answer string —
   scoring an agent's actual textual answer against a task is later
   evaluation territory (F-2x), not F-34's job. A real HTTP transport
   would be new, unplanned v1-scope architecture, not an adapter detail.

3. No live server reachable, structurally: this module imports only
   `anyio` (for process spawning) and this project's own `replay_proxy`/
   `record.writer`/`record.proxy` modules — never `mcp.client.stdio` or
   anything else that could dial a real, network- or subprocess-spawned
   MCP *server*. The spawned agent's only route to any tool response is
   its own stdio, which this module wires to `run_replay_proxy` and
   nothing else. Spawning the agent itself is F-34's whole purpose, not
   a violation of that guarantee — the guarantee is about not reaching a
   live *tool* server, not about not running the thing under test.

4. Process lifecycle: this project has been bitten twice by cooperative-
   shutdown hangs (`record/proxy.py`'s Prompt 6 fix, `cli/observe.py`'s
   Ctrl+C fix). The same risk is real here — if the agent subprocess
   never exits on its own (hangs, or never notices its stdin closed),
   `await process.wait()` can block the whole adapter forever, and there
   is no cooperative-cancellation path an unresponsive child would
   honor. Mitigated the same way as those two prior fixes: never trust a
   graceful exit unconditionally. `run_agent_subprocess` accepts an
   optional `timeout_s` bounding the whole run (`None` = no bound, the
   caller's choice, not this module's default); on timeout, or on any
   other unwind (exception, cancellation), the `finally` block always
   attempts `terminate()` then falls back to `kill()` if the process is
   still alive after a short grace period — the child is never left
   running after this function returns or raises, regardless of why it
   returns or raises.

   This is not a hypothetical: it reproduced while writing this
   adapter's own tests. `tests/fixtures/scripted_agent.py` originally
   read its own stdin via a plain blocking `sys.stdin.readline()` inside
   `anyio.to_thread.run_sync(...)`, whose default is
   `abandon_on_cancel=False` — cancelling the awaiting task does NOT
   abandon a thread already parked in that blocking read, so the
   script's own shutdown (`cancel_scope.cancel()` once its calls are
   done) could never actually complete: the thread stayed blocked until
   the *parent* closed its stdin, which this adapter only did after
   giving up on `process.wait()` timing out. Both adapter tests took
   ~30s each — exactly `timeout_s` — before the fixture was fixed to
   pass `abandon_on_cancel=True`; afterward, ~3s combined. This
   adapter's own pumps (`_pump_stdout_to_proxy`/`_pump_proxy_to_stdin`)
   don't have this problem — they read/write `anyio.open_process`'s
   native async process streams, never a thread-wrapped blocking call —
   but a *real* agent script built the same naive way `scripted_agent.py`
   originally was (a blocking stdin read in a thread, default
   cancellation settings) would hit the identical hang. Worth carrying
   forward if this adapter ever grows agent-side guidance or a reference
   implementation.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import anyio
import mcp_types as types
from anyio.abc import Process
from anyio.streams.text import TextReceiveStream
from mcp.shared.message import SessionMessage

from record.schema import ToolDescriptor
from record.writer import SessionRecorder
from replay.replay_proxy import run_replay_proxy
from replay.replay_store import ReplayStore

DEFAULT_TERMINATE_GRACE_S = 5.0


async def _pump_stdout_to_proxy(process: Process, read_stream_writer) -> None:
    """Parses newline-delimited JSON-RPC from the agent's stdout and
    feeds it to `run_replay_proxy`'s read_stream — the same line-framing
    approach `mcp.server.stdio.stdio_server()` uses for our own process's
    fd 0, pointed at a *child* process's stdout instead, with this module
    in the server role and the spawned agent as the client.
    """
    assert process.stdout is not None
    async with read_stream_writer:
        buffer = ""
        async for chunk in TextReceiveStream(process.stdout, encoding="utf-8", errors="replace"):
            buffer += chunk
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                if not line.strip():
                    continue
                try:
                    message = types.jsonrpc_message_adapter.validate_json(line, by_name=False)
                except Exception as exc:
                    await read_stream_writer.send(exc)
                    continue
                await read_stream_writer.send(SessionMessage(message))


async def _pump_proxy_to_stdin(process: Process, write_stream_reader) -> None:
    """The reverse direction: serializes `run_replay_proxy`'s outgoing
    messages and writes them as lines to the agent's stdin."""
    assert process.stdin is not None
    async with write_stream_reader:
        async for session_message in write_stream_reader:
            json_line = session_message.message.model_dump_json(by_alias=True, exclude_unset=True)
            try:
                await process.stdin.send((json_line + "\n").encode("utf-8"))
            except (anyio.BrokenResourceError, anyio.ClosedResourceError):
                # The agent closed/never opened its stdin for reading —
                # nothing left to deliver to; let the pump end quietly
                # rather than crash the whole run over a dead peer.
                return


async def run_agent_subprocess(
    command: Sequence[str],
    replay_store: ReplayStore,
    server_name: str,
    tools_served: list[ToolDescriptor],
    session_dir: Path,
    raw_dir: Path,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    timeout_s: float | None = None,
) -> Path:
    """Spawns `command` (an already-resolved argv — templating and
    config-loading are the caller's job, see module docstring point 1),
    wires its stdin/stdout to a fresh `run_replay_proxy` instance serving
    `replay_store`, records everything the agent actually did via
    `SessionRecorder`, and returns the path to the resulting session
    JSONL.

    Matches `evaluate.baseline.run_baseline`'s `run_once` contract when
    partially applied down to a zero-arg callable (not done here — that
    wiring is explicitly the next, separate prompt).
    """
    recorder = SessionRecorder(session_dir=session_dir, raw_dir=raw_dir, server_name=server_name)

    read_stream_writer, read_stream = anyio.create_memory_object_stream(0)
    write_stream, write_stream_reader = anyio.create_memory_object_stream(0)

    process = await anyio.open_process(list(command), env=env, cwd=cwd)
    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(_pump_stdout_to_proxy, process, read_stream_writer)
            tg.start_soon(_pump_proxy_to_stdin, process, write_stream_reader)
            tg.start_soon(run_replay_proxy, read_stream, write_stream, replay_store, server_name, tools_served, recorder.observe)

            with anyio.move_on_after(timeout_s):
                await process.wait()
            # Either the agent exited on its own, or timeout_s elapsed.
            # Either way, stop the pumps/proxy — closing these streams
            # unwinds run_replay_proxy's server.run() loop cleanly (it
            # sees end-of-stream, not a raw cancellation mid-handler).
            tg.cancel_scope.cancel()
    finally:
        await _ensure_process_stopped(process)

    recorder.close()

    new_session_files = sorted(session_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    if not new_session_files:
        raise RuntimeError(f"agent subprocess produced no session JSONL under {session_dir}")
    return new_session_files[-1]


async def _ensure_process_stopped(process: Process) -> None:
    """Never trust a cooperative exit (module docstring point 4): close
    stdin first (a real EOF a well-behaved agent notices on its own,
    gentler than a signal), then terminate, then kill if it's still
    alive after each grace period. Always runs from a `finally`, so the
    child is never abandoned regardless of how `run_agent_subprocess`
    unwound."""
    if process.returncode is not None:
        await process.aclose()
        return

    if process.stdin is not None:
        with anyio.move_on_after(DEFAULT_TERMINATE_GRACE_S):
            try:
                await process.stdin.aclose()
                await process.wait()
            except (anyio.BrokenResourceError, anyio.ClosedResourceError):
                pass

    if process.returncode is None:
        process.terminate()
        with anyio.move_on_after(DEFAULT_TERMINATE_GRACE_S):
            await process.wait()
    if process.returncode is None:
        process.kill()
        with anyio.move_on_after(DEFAULT_TERMINATE_GRACE_S):
            await process.wait()
    await process.aclose()
