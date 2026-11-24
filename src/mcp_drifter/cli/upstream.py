"""Opening the connection to the real server with whatever credentials its config calls for.

The single place `cli/` turns a `ServerConfig` into a connected transport, so `observe`, `doctor`
and `fixture` cannot drift apart on authentication. Kept in `cli/` and out of `record/` on
purpose: `replay/` imports `record/proxy.py`, and replay must never depend on `auth/`
(docs/SPEC.md §5.2). `record/proxy.py` only accepts an optional ready-made `httpx2` client.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import httpx2
from mcp.shared._httpx_utils import create_mcp_http_client

from mcp_drifter.auth.errors import AuthSetupError, UpstreamAuthRejected
from mcp_drifter.auth.upstream import bearer_headers, rejection_hook
from mcp_drifter.cli.config import ConfigError, ServerConfig, server_target
from mcp_drifter.record.proxy import connect_to_server


@asynccontextmanager
async def upstream_http_client(
    server: ServerConfig, on_rejected: Callable[[UpstreamAuthRejected], None] | None = None
) -> AsyncIterator[httpx2.AsyncClient | None]:
    """Yields the `httpx2` client to connect with, or `None` for "use the SDK's default".

    `None` for a stdio server and for a `url` server with no auth: unauthenticated behavior is
    unchanged. For `bearer_env`, a client carrying exactly one extra header, with the SDK's own
    timeouts, closed on exit. An auth type this build cannot log in with yet raises a config
    error instead of quietly connecting with no credentials.

    `on_rejected` (see `auth.upstream.rejection_hook`) is how `observe` gets to exit the
    process when the server refuses the credentials; omit it to just receive the exception.
    """
    auth = server.auth
    if server.url is None or auth is None or auth.type == "none":
        yield None
        return
    if auth.type != "bearer_env":
        raise ConfigError(
            f"server {server.name!r} uses auth type {auth.type!r}. `drifter auth login` can store a login, but "
            "observe, doctor and fixture cannot use a stored login yet; use `type: bearer_env` with a token in an "
            "environment variable, or remove the `auth` block, for now"
        )
    try:
        headers = bearer_headers(auth, server.name, os.environ)
    except AuthSetupError as exc:
        raise ConfigError(str(exc)) from None
    client = create_mcp_http_client(headers=headers)
    client.event_hooks = {"request": [], "response": [rejection_hook(server.name, f"environment variable {auth.token_env}", on_rejected)]}
    async with client:
        yield client


@asynccontextmanager
async def connect_upstream(
    server: ServerConfig, on_rejected: Callable[[UpstreamAuthRejected], None] | None = None
) -> AsyncIterator:
    """`connect_to_server` for a configured server: transport chosen by the config, credentials
    attached when the config asks for them. Yields the `(read, write)` stream pair."""
    async with upstream_http_client(server, on_rejected) as http_client:
        async with connect_to_server(server_target(server), http_client) as streams:
            yield streams


__all__ = ["connect_upstream", "upstream_http_client"]
