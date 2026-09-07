"""HTTP-serving side of the replay proxy (F-38), docs/SPEC.md §5.1.

Serves `replay/replay_proxy.py`'s `build_replay_server` app over real
Streamable HTTP instead of stdio, so `cli/subprocess_adapter.py`'s
`agent.mode: http` path can hand a spawned agent a URL instead of piping
its stdin/stdout. Confirmed against the installed SDK before writing this
(not assumed): `mcp.server.lowlevel.Server.streamable_http_app()` already
returns a ready-made Starlette ASGI app defaulting to `host="127.0.0.1"`;
`starlette`/`uvicorn`/`sse-starlette` are transitive dependencies of `mcp`
already present, so no new top-level dependency is needed.

Security (docs/SECURITY.md gap 3, decided pre-code): loopback-only binding
and DNS-rebinding protection are both load-bearing here, not optional
hardening added later. Read `mcp.server.transport_security`'s actual
validation source before configuring this (not assumed from the settings
model's field names alone) — two things aren't obvious from the model
alone: (1) `TransportSecurityMiddleware.__init__` disables protection BY
DEFAULT ("for backwards compatibility") if no `TransportSecuritySettings`
is passed at all, so passing one explicitly, with
`enable_dns_rebinding_protection=True`, is required to get any protection
whatsoever — omitting it silently buys nothing. (2) An absent `Origin`
header always passes validation (real, non-browser MCP clients — this
project's own `streamable_http_client`-based agents included — don't send
one), while any browser-supplied `Origin` must match `allowed_origins`
(kept empty here — no legitimate browser origin exists for this listener,
so nothing needs to be allow-listed, and DNS-rebinding attacks are
specifically a browser-driven threat). `allowed_hosts` uses the
`"host:*"` wildcard the SDK's own matcher supports, since the actual port
is only known after binding (below), not at settings-construction time.

Ephemeral port: bound by this module itself (a real `socket.socket()`,
`bind`, `listen`) BEFORE handing it to uvicorn via `Server.serve(sockets=
[sock])`, rather than letting uvicorn choose and then discovering the
port afterward — this makes the actual bound port known deterministically
before the caller ever needs to construct the URL to inject into a
spawned agent's environment, with no polling or race.
"""

from __future__ import annotations

import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
import httpx2
import uvicorn
from mcp.server.transport_security import TransportSecuritySettings
from sse_starlette.sse import AppStatus

from record.schema import ToolDescriptor
from replay.replay_proxy import MessageObserver, build_replay_server
from replay.replay_store import ReplayStore

STREAMABLE_HTTP_PATH = "/mcp"


def _bind_ephemeral_loopback_socket(host: str) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, 0))
    sock.listen(128)
    return sock


async def _wait_until_actually_answering(url: str, attempts: int = 50, delay_s: float = 0.02) -> None:
    """`uv_server.started` (set once `Server.startup()` returns) is NOT
    sufficient proof the app can answer a real request yet — found
    empirically, not assumed: running `serve_replay_over_http` twice in
    the same process, back to back, made the SECOND run's first real
    request fail with uvicorn's "ASGI callable returned without
    completing response" every time, reproduced in isolation outside
    pytest entirely (so not a test-fixture issue), and the race
    disappeared under DEBUG-level logging (added I/O changes scheduling
    enough to hide it — the classic signature of a real race, not
    flakiness in the test). Root cause not fully isolated inside the SDK
    (`StreamableHTTPSessionManager`'s own internal task-group startup
    lagging behind uvicorn's `.started` flag is the leading candidate),
    but a raw TCP connect can't detect it either, since this module binds
    and listens on the socket itself before uvicorn ever touches it — the
    OS backlog accepts a connection attempt regardless of ASGI-level
    readiness. The only genuinely empirical proof is a real HTTP
    round-trip that gets a real, complete response back, retried with a
    short backoff until one succeeds or `attempts` is exhausted.
    """
    async with httpx2.AsyncClient() as client:
        last_error: Exception | None = None
        for _ in range(attempts):
            try:
                await client.post(
                    url,
                    headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
                    json={"jsonrpc": "2.0", "method": "ping"},
                )
                return  # any complete HTTP response (including a 4xx) proves the app answered for real
            except httpx2.TransportError as exc:
                last_error = exc
                await anyio.sleep(delay_s)
        raise RuntimeError(f"replay HTTP proxy at {url} never answered a real request") from last_error


@asynccontextmanager
async def serve_replay_over_http(
    replay_store: ReplayStore,
    server_name: str,
    tools_served: list[ToolDescriptor],
    on_message: MessageObserver | None = None,
    synthetic_tool_names: frozenset[str] = frozenset(),
    inverse_map: dict[str, dict[str, str]] | None = None,
    host: str = "127.0.0.1",
) -> AsyncIterator[str]:
    """Serves a replay session over real Streamable HTTP for the
    lifetime of this context manager, yielding the real, loopback-bound
    URL a client should connect to.

    `host` defaults to loopback and is not meant to be widened to
    `0.0.0.0` by a caller — this is a per-run, per-agent listener for
    Drifter's own spawned subprocess, not a shared service (docs/SECURITY.md
    gap 3).

    An exception raised inside the `async with` block using this context
    manager (or from `_wait_until_actually_answering` itself timing out)
    comes back wrapped in an `ExceptionGroup` (PEP 654), not bare — a real
    consequence of the internal `anyio.create_task_group()` this function
    uses, confirmed by a dedicated test
    (`test_an_exception_inside_the_context_manager_still_shuts_down_gracefully`),
    not incidental. A caller that needs to catch a specific exception type
    around this context manager needs `except*`, not a plain `except`.
    """
    # A confirmed, known sse-starlette gotcha, root-caused empirically
    # (not guessed) after this exact symptom reproduced deterministically:
    # running this context manager twice in the same process -- even
    # within one event loop, ruling out any Windows-Proactor-lifecycle
    # theory -- made every run after the first fail its first real
    # request with uvicorn's "ASGI callable returned without completing
    # response." `sse_starlette.sse.AppStatus.should_exit` is a bare
    # CLASS attribute (not per-instance, not per-server): `handle_exit`
    # sets it True in response to an OS-level signal, and every SSE
    # stream (Streamable HTTP's server-to-client channel) this module
    # ever opens afterward checks the SAME shared flag. A first pass at
    # this fix (resetting the flag to False at the start of every run)
    # made the bug intermittent instead of deterministic rather than
    # gone: signal delivery is asynchronous, so a PREVIOUS run's still-
    # in-flight shutdown signal could re-set it True after this run's own
    # reset. The actual fix, using the library's own documented API for
    # exactly this: `disable_automatic_graceful_drain()` stops OS signals
    # from touching this flag at all — appropriate here regardless, since
    # nothing about an OS-level SIGINT/SIGTERM/CTRL_BREAK to THIS process
    # should control an individual per-agent-run server's lifecycle in
    # the first place. This module's own `finally` block (below) sets
    # `AppStatus.should_exit = True` itself, matching the API's own
    # documented warning ("you MUST set AppStatus.should_exit = True ...
    # or streams will never close").
    AppStatus.disable_automatic_graceful_drain()
    AppStatus.should_exit = False

    server = build_replay_server(replay_store, server_name, tools_served, on_message, synthetic_tool_names, inverse_map)
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[f"{host}:*"],
        allowed_origins=[],
    )
    app = server.streamable_http_app(
        streamable_http_path=STREAMABLE_HTTP_PATH, host=host, transport_security=security
    )

    sock = _bind_ephemeral_loopback_socket(host)
    port = sock.getsockname()[1]
    url = f"http://{host}:{port}{STREAMABLE_HTTP_PATH}"

    config = uvicorn.Config(app, log_level="warning")
    uv_server = uvicorn.Server(config)
    serve_done = anyio.Event()

    async def _serve_and_signal() -> None:
        try:
            await uv_server.serve([sock])
        finally:
            serve_done.set()

    async def _graceful_shutdown() -> None:
        # Setting should_exit and WAITING for uv_server.serve() to notice
        # and return on its own (uvicorn's main_loop polls this flag,
        # typically within one tick) -- NOT tg.cancel_scope.cancel() or
        # letting an exception propagate straight out of the task group,
        # which forcibly cancels every task in it, including this one.
        # Found empirically, not assumed: forcibly cancelling while
        # uvicorn's asyncio server may be mid-accept() on our own raw
        # socket raised a raw OSError (WinError 995, "I/O operation
        # aborted") on Windows' ProactorEventLoop -- not cooperative
        # shutdown at all. Explicitly awaiting `serve_done` (bounded, so a
        # genuinely stuck server still can't hang this forever) closes the
        # gap this module's FIRST fix for that bug left open: setting the
        # flag alone doesn't stop the task group's own `__aexit__` from
        # forcibly cancelling if an exception (e.g. from
        # `_wait_until_actually_answering` timing out) is what triggered
        # this shutdown in the first place -- found on re-audit, not by a
        # failing test.
        uv_server.should_exit = True
        # Required by disable_automatic_graceful_drain()'s own documented
        # contract (below): with automatic drain off, WE are responsible
        # for flipping this, or every SSE stream sse_starlette opens
        # would simply never close.
        AppStatus.should_exit = True
        with anyio.move_on_after(5.0):
            await serve_done.wait()

    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(_serve_and_signal)
            try:
                # uvicorn.Server.startup() completes (setting .started =
                # True) before its main_loop begins accepting real
                # traffic -- waited on explicitly rather than assumed
                # instantaneous, so a caller never gets a URL back before
                # the server can actually answer it. Startup here does no
                # real I/O (the socket is already bound/listening; this
                # only registers the asyncio protocol factory), so this
                # loop is expected to run at most once or twice, never a
                # real wait.
                while not uv_server.started:
                    await anyio.sleep(0.01)
                await _wait_until_actually_answering(url)
                yield url
            finally:
                await _graceful_shutdown()
    finally:
        sock.close()
