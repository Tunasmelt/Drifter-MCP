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
from pathlib import Path

import httpx2
from mcp.shared._httpx_utils import create_mcp_http_client

from mcp_drifter.auth.errors import AuthSetupError, UpstreamAuthRejected
from mcp_drifter.auth.service import service_provider
from mcp_drifter.auth.session import StoredLoginProvider
from mcp_drifter.auth.storage import StorageError, TokenStore, canonical_server_url
from mcp_drifter.auth.upstream import bearer_headers, rejection_hook
from mcp_drifter.cli.config import ConfigError, ServerConfig, server_target
from mcp_drifter.record.proxy import connect_to_server


def _login_advice(server_name: str):
    def advice(status: int, scope: str | None) -> str:
        login = f"run `drifter auth login {server_name}`"
        if status == 401:
            return login
        missing = f"scope {scope!r}" if scope else "the scope it asks for"
        return f"the stored login's scopes do not allow this; add {missing} to `auth.scopes` in drifter.yaml, then {login}"

    return advice


def _same_endpoint(server_url: str):
    wanted = canonical_server_url(server_url)

    def matches(request: httpx2.Request) -> bool:
        return canonical_server_url(str(request.url)) == wanted

    return matches


def _same_endpoint_with_a_token(server_url: str):
    same_endpoint = _same_endpoint(server_url)

    def matches(request: httpx2.Request) -> bool:
        return "authorization" in request.headers and same_endpoint(request)

    return matches


def _service_advice(auth):
    def advice(status: int, scope: str | None) -> str:
        if status == 401:
            return (
                "the server refused the access token it was just issued; check auth.issuer and that this client "
                "is allowed to use the server (there is no login to run for a service credential)"
            )
        missing = f"scope {scope!r}" if scope else "the scope it asks for"
        return f"the token's scopes do not allow this; add {missing} to `auth.scopes` in drifter.yaml"

    return advice


@asynccontextmanager
async def upstream_http_client(
    server: ServerConfig,
    on_rejected: Callable[[UpstreamAuthRejected], None] | None = None,
    *,
    store: TokenStore | None = None,
    project_root: Path | None = None,
) -> AsyncIterator[httpx2.AsyncClient | None]:
    """Yields the `httpx2` client to connect with, or `None` for "use the SDK's default".

    `None` for a stdio server and for a `url` server with no auth: unauthenticated behavior is
    unchanged. For `bearer_env`, a client carrying exactly one extra header, with the SDK's own
    timeouts, closed on exit. For `oauth`, a client whose provider uses the login stored by
    `drifter auth login` and can never start one (F-44); for `client_credentials`, a provider that exchanges the
    configured secret or key for a token, in memory only (F-45); `project_root` is the directory holding
    drifter.yaml, which the token store must never live inside (tests pass `store` instead).
    An auth type this build cannot use yet raises a config error instead of quietly connecting
    with no credentials.

    `on_rejected` (see `auth.upstream.rejection_hook`) is how `observe` gets to exit the
    process when the server refuses the credentials; omit it to just receive the exception.
    """
    auth = server.auth
    if server.url is None or auth is None or auth.type == "none":
        yield None
        return
    if auth.type == "oauth":
        if store is None:
            try:
                store = TokenStore(server.name, server.url, project_root=project_root or server.project_root)
            except StorageError as exc:
                raise ConfigError(str(exc)) from None
        provider = StoredLoginProvider(server.url, store, server_name=server.name, on_rejected=on_rejected)
        await provider.check_usable()
        client = create_mcp_http_client(auth=provider)
        client.event_hooks = {
            "request": [],
            "response": [
                rejection_hook(
                    server.name,
                    "the stored login",
                    on_rejected,
                    applies_to=_same_endpoint(server.url),
                    advice=_login_advice(server.name),
                )
            ],
        }
        async with client:
            yield client
        return
    if auth.type == "client_credentials":
        try:
            provider = service_provider(auth, server.url, server.name, os.environ, on_rejected)
        except AuthSetupError as exc:
            raise ConfigError(str(exc)) from None
        credential = auth.secret_env or auth.private_key_env
        client = create_mcp_http_client(auth=provider)
        client.event_hooks = {
            "request": [],
            "response": [
                rejection_hook(
                    server.name,
                    f"the access token issued for client {auth.client_id!r} (credential: environment variable {credential})",
                    on_rejected,
                    # Only a request that CARRIED a token: the first, unauthenticated request is answered 401 by
                    # design, and that 401 is what starts the exchange for one.
                    applies_to=_same_endpoint_with_a_token(server.url),
                    advice=_service_advice(auth),
                )
            ],
        }
        async with client:
            yield client
        return
    if auth.type != "bearer_env":  # unreachable while AuthType is none | oauth | bearer_env | client_credentials
        raise ConfigError(f"server {server.name!r} uses auth type {auth.type!r}, which this build cannot connect with")
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
    server: ServerConfig,
    on_rejected: Callable[[UpstreamAuthRejected], None] | None = None,
    *,
    store: TokenStore | None = None,
    project_root: Path | None = None,
) -> AsyncIterator:
    """`connect_to_server` for a configured server: transport chosen by the config, credentials
    attached when the config asks for them. Yields the `(read, write)` stream pair."""
    async with upstream_http_client(server, on_rejected, store=store, project_root=project_root) as http_client:
        async with connect_to_server(server_target(server), http_client) as streams:
            yield streams


__all__ = ["connect_upstream", "upstream_http_client"]
