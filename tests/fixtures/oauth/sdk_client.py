"""Drives the OAuth fixture with the MCP SDK's OWN client auth, with none of Drifter's
auth code involved (none exists yet).

This is how the fixture earns trust before F-41+ are written: if the SDK's reference
`OAuthClientProvider` can complete a login against each mode, the mode is a faithful
stand-in for a real server. It also records what the SDK does NOT do for itself
(e.g. it sends S256 even to a server that never advertised it), which is exactly the
list of things Drifter must add (docs/SPEC.md §5.2, design constraint 3).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx2
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import AuthorizationCodeResult, OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from pydantic import AnyUrl

from oauth_server import FixtureHandle, follow_authorization

CALLBACK = "http://127.0.0.1:53682/callback"


class MemoryStorage:
    """The SDK's `TokenStorage` protocol, in memory. `set_*_calls` count writes so a test
    can see that a rotated refresh token was actually persisted."""

    def __init__(self, tokens: OAuthToken | None = None, client_info: OAuthClientInformationFull | None = None) -> None:
        self.tokens = tokens
        self.client_info = client_info
        self.set_tokens_calls = 0

    async def get_tokens(self) -> OAuthToken | None:
        return self.tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self.tokens = tokens
        self.set_tokens_calls += 1

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return self.client_info

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self.client_info = client_info


def sdk_provider(
    handle: FixtureHandle,
    storage: MemoryStorage,
    *,
    scope: str = "read",
    client_metadata_url: str | None = None,
) -> OAuthClientProvider:
    """An authorization-code + PKCE provider whose "browser" is `follow_authorization`."""
    captured: dict[str, str | None] = {}

    async def redirect_handler(url: str) -> None:
        captured["code"], captured["state"] = await follow_authorization(url)

    async def callback_handler() -> AuthorizationCodeResult:
        code = captured["code"]
        assert code is not None
        return AuthorizationCodeResult(code=code, state=captured["state"])

    return OAuthClientProvider(
        server_url=handle.mcp_url,
        client_metadata=OAuthClientMetadata(
            client_name="drifter-test",
            redirect_uris=[AnyUrl(CALLBACK)],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
            scope=scope,
        ),
        storage=storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
        client_metadata_url=client_metadata_url,
    )


@asynccontextmanager
async def initialized_session(handle: FixtureHandle, auth: httpx2.Auth | None = None, headers: dict[str, str] | None = None) -> AsyncIterator[ClientSession]:
    """A real, initialized MCP session over Streamable HTTP with `auth` (or `headers`)."""
    async with httpx2.AsyncClient(auth=auth, headers=headers, follow_redirects=True) as http_client:
        async with streamable_http_client(handle.mcp_url, http_client=http_client) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


def leaf_exceptions(exc: BaseException) -> list[BaseException]:
    """The real errors inside however many ExceptionGroups anyio/the SDK wrapped them in.
    Tests assert on these, not on "something raised": a broad `raises(BaseException)` passes for
    ANY failure, including a bug in the test's own setup."""
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for inner in exc.exceptions for leaf in leaf_exceptions(inner)]
    return [exc]


async def tool_names(session: ClientSession) -> list[str]:
    return sorted(tool.name for tool in (await session.list_tools()).tools)
