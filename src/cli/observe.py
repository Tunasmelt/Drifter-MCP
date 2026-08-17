"""`drifter observe` (F-09): the CLI entrypoint that turns on recording.

Wires record/proxy.py (F-01) and record/writer.py (F-02/F-03/F-05/F-06/
F-07/F-08) together with cli/config.py's drifter.yaml (`servers:` block)
into a long-running passthrough session, with live terminal feedback —
trajectory count, call count, error count — instead of a silent process.

Feedback goes to stderr, never stdout. stdout is the actual MCP wire
protocol channel to the agent: `stdio_server()` diverts the real OS-level
stdout away from it specifically to guard against exactly this kind of
accidental corruption (its own docstring: "so handlers and children read
EOF and their stray output misses the wire"), but this module writes to
stderr explicitly rather than relying on that as the only safeguard —
see tests/cli/test_observe.py for the test that verifies stdout stays
clean end-to-end, not just that this file happens to write() correctly.
"""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path
from typing import TextIO

import anyio
from mcp.client.stdio import StdioServerParameters

from cli.config import ConfigError, DrifterConfig, ServerConfig, load_config
from record.proxy import Direction, run_passthrough_proxy
from record.writer import SessionRecorder


def select_server(config: DrifterConfig, server_name: str | None) -> ServerConfig:
    """Picks which drifter.yaml server to proxy for this invocation.

    Drifter stands in for one server at a time over stdio (SPEC.md's
    architecture — it's invoked as *the* server command in a client's
    config slot), so drifter.yaml's `servers:` list is a catalog, not a
    fan-out target. --server disambiguates when there's more than one.
    """
    if server_name is not None:
        for server in config.servers:
            if server.name == server_name:
                return server
        names = [s.name for s in config.servers]
        raise ConfigError(f"no server named {server_name!r} in drifter.yaml (have: {names})")
    if len(config.servers) == 1:
        return config.servers[0]
    names = [s.name for s in config.servers]
    raise ConfigError(f"drifter.yaml declares {len(config.servers)} servers; pass --server NAME to choose one (have: {names})")


class LiveStatus:
    """Renders one self-overwriting status line to a stream (stderr)."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self._last_len = 0

    def update(self, recorder: SessionRecorder) -> None:
        text = (
            f"drifter observe — trajectories: {recorder.trajectory_count}  "
            f"calls: {recorder.call_count}  errors: {recorder.error_count}"
        )
        pad = max(0, self._last_len - len(text))
        self._stream.write("\r" + text + " " * pad)
        self._stream.flush()
        self._last_len = len(text)

    def finish(self) -> None:
        self._stream.write("\n")
        self._stream.flush()


def handle_sigint(
    recorder: SessionRecorder,
    status: LiveStatus,
    status_stream: TextIO,
    exit_fn=os._exit,
) -> None:
    """The Ctrl+C response — a plain function, not a closure, specifically
    so it can be unit-tested directly (with exit_fn mocked out) without
    depending on OS signal delivery, which was found unreliable in a
    sandboxed shell environment during this fix's own verification (see
    tests/cli/test_observe.py's test for the substitute it uses instead).

    Verified by hand (commit message / CHANGELOG has the full
    investigation): asyncio.Runner's default SIGINT handling — which
    anyio.run() uses — converts the *first* Ctrl+C into cooperative task
    cancellation (main_task.cancel()), only raising KeyboardInterrupt
    directly on a second press. That cooperative path was proven to hang
    indefinitely here: stdio_server()'s internal stdin_reader() delegates
    its blocking read to a worker thread (anyio.wrap_file's generic
    file-wrapping over a real OS file descriptor), and a blocking OS-level
    read already in flight in a thread cannot be cancelled — it only
    returns when data arrives or the fd closes, neither of which happens
    while an agent is connected but idle (the realistic case: the user
    hits Ctrl+C between tool calls, not mid-call). anyio's task-group
    teardown waits for that thread to finish before considering the task
    done, so the whole process hangs waiting for a thread that will never
    return. A second Ctrl+C press (traced through
    asyncio.runners.Runner._on_sigint's own escalation logic) was also
    tested and did not reliably break this specific hang either —
    confirmed via a deterministic in-process reproduction using
    _thread.interrupt_main() (bypassing flaky OS console-signal delivery
    entirely, which independently could not be confirmed to reach a
    subprocess at all in this sandboxed environment even after the fix —
    a real terminal's console should behave normally) with a control test
    proving this is not a blanket "Windows doesn't support Ctrl+C"
    limitation: a trivial anyio.run() wrapping only a subprocess wait
    handled the identical interrupt correctly and returned in ~1.5s.

    The fix bypasses cooperative cancellation entirely. recorder.close()
    is plain synchronous file I/O against recorder's own state — it has
    no dependency on run_passthrough_proxy's async task tree unwinding
    gracefully at all. The caller installs this (wrapped in a lambda, to
    match signal.signal's (signum, frame) callback shape) as the SIGINT
    handler *before* anyio.run() runs, which pre-empts asyncio.Runner's
    own installation (Runner.run() only installs its handler when
    signal.getsignal(SIGINT) is still signal.default_int_handler), so
    this handler — not Runner's — is what actually fires.

    os._exit() skips stdio_client's own shutdown sequence
    (record/proxy.py, Prompt 6's fix) entirely, so it never sends the
    spawned real server subprocess an explicit terminate. Empirically
    verified this is not actually a problem, not just assumed: killing
    the wrapping process (even via os._exit(), which skips all Python-
    level cleanup) makes the OS unconditionally close that process's file
    descriptors, including the pipe holding the child's stdin open. The
    child — built on the same stdio_server() as this project — sees that
    as EOF and exits on its own; a real subprocess/psutil-tracked
    reproduction (spawn, confirm the child is alive by PID, kill the
    parent via this exact code path, poll the child's PID at t+0/1/2/3/
    5/8s) found it already gone at the very first check, not "eventually
    orphaned."

    This is a genuine protocol-level SHOULD-requirement in the MCP spec
    revision this project targets (2026-07-28, stdio transport page,
    Shutdown section) — verified by fetching the actual spec text, not
    assumed: "Servers SHOULD exit promptly when their standard input is
    closed or reads return end-of-file. This is the primary graceful-
    shutdown signal and the only portable one." That's spec text, not
    merely this SDK's implementation choice — though SHOULD, not MUST,
    so a non-compliant or unusually-written third-party server could
    still choose not to honor it; this was verified for the fixture
    server (built on the same SDK as this project), not tested against
    an arbitrary implementation. The predecessor revision (2025-11-25)
    has no equivalent explicit language — its shutdown section only
    describes the client's sequence (close stdin, wait, SIGTERM, SIGKILL)
    and says a server "MAY initiate shutdown by closing its output
    stream... and exiting" (server-initiated shutdown, not the server's
    expected response to the client closing stdin) — exit-on-EOF was
    implied there, not stated.

    Idempotent under re-entry (a rapid double Ctrl+C, or — in principle —
    a signal arriving while this call is still on the stack, since
    Python signal handlers aren't automatically safe against that): the
    guard is recorder.closed, set at the very start of
    SessionRecorder.close(), not the end, so it covers both a second
    *sequential* call (recorder already fully closed) and a second
    *nested* one (recorder mid-close, closed already True). A second
    call here is a clean no-op — no re-written status line, no second
    recorder.close(), no second exit_fn call.
    """
    if recorder.closed:
        return
    status_stream.write("\ndrifter observe — stopping (Ctrl+C)\n")
    status_stream.flush()
    recorder.close()
    status.finish()
    exit_fn(0)


def run_observe(
    config_path: Path | None = None,
    server_name: str | None = None,
    status_stream: TextIO = sys.stderr,
) -> None:
    config = load_config(config_path)
    server = select_server(config, server_name)

    runs_dir = Path(config.record.dir)
    raw_dir = runs_dir.parent / "raw"  # sibling directories under .drifter/ (SPEC.md architecture diagram)

    recorder = SessionRecorder(session_dir=runs_dir, raw_dir=raw_dir, server_name=server.name)
    status = LiveStatus(status_stream)

    def on_message(direction: Direction, message) -> None:
        recorder.observe(direction, message)
        status.update(recorder)

    server_params = StdioServerParameters(command=server.command[0], args=server.command[1:])

    # See handle_sigint's docstring for why this exists and what was
    # verified by hand before relying on it.
    previous_handler = signal.signal(signal.SIGINT, lambda signum, frame: handle_sigint(recorder, status, status_stream))
    try:
        status_stream.write(f"drifter observe — proxying {server.name!r} ({' '.join(server.command)})\n")
        status_stream.flush()
        anyio.run(run_passthrough_proxy, server_params, on_message)
    finally:
        signal.signal(signal.SIGINT, previous_handler)
        # Only reached on normal completion (agent disconnected) — the
        # Ctrl+C path above terminates the process directly and never
        # gets here, so there's no double-close risk despite both paths
        # calling recorder.close().
        recorder.close()
        status.finish()
