"""`drifter auth login`: an interactive OAuth login for one `url` server (F-43, docs/SPEC.md §5.2).

Drifter is its own OAuth client (it never forwards an agent's token), built on the SDK's
`OAuthClientProvider`; what this module adds is what the SDK does not do for a tool like this:

  - a loopback listener that ignores forged callbacks (`callback.py`);
  - a refusal to proceed unless the server's metadata advertises PKCE `S256`. The SDK sends S256
    regardless, so it would happily "log in" to a server that never said it checks the challenge;
  - the identity order: a pre-registered `client_id`, then a client-ID metadata document, then
    dynamic registration, otherwise an actionable error;
  - configured `scopes`, which the SDK would otherwise overwrite with its own choice;
  - saving the result only on success, so a failed login never damages a working one.

The flow runs against in-memory storage and is copied into the real `TokenStore` at the end. That is
also why a second login registers afresh: a dynamically registered redirect URI includes the
loopback port, which changes every time.
"""

from __future__ import annotations

import inspect
import logging
import time
import webbrowser
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

import httpx2
from mcp.client.auth import OAuthClientProvider
from mcp.client.auth.exceptions import OAuthFlowError, OAuthRegistrationError, OAuthTokenError
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from pydantic import AnyUrl, PrivateAttr

from mcp_drifter.auth.callback import CallbackListener
from mcp_drifter.auth.config import is_loopback_host
from mcp_drifter.auth.errors import LoginRefused
from mcp_drifter.auth.storage import TokenStore, canonical_server_url
from mcp_drifter.auth.text import clean_server_text
from mcp_drifter.auth.threads import run_in_daemon_thread

if TYPE_CHECKING:  # auth/ must not depend on cli/ at runtime
    from mcp_drifter.cli.config import ServerConfig

# docs/FEATURES.md F-43: "timing out (default 300 s)". A user-facing default from the spec, not a tuned value.
DEFAULT_LOGIN_TIMEOUT_S = 300.0

_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "drifter", "version": "login"}},
}
_SDK_AUTH_LOGGER = "mcp.client.auth.oauth2"


@dataclass(frozen=True)
class LoginResult:
    identity: str  # "preregistered" | "cimd" | "dcr"
    scope: str | None
    expires_at: float | None
    backend: str  # where the login is stored: "keyring" | "file"


class _LoginStorage:
    """The SDK's `TokenStorage`, in memory, for the duration of one login."""

    def __init__(self, client_info: OAuthClientInformationFull | None = None) -> None:
        self.tokens: OAuthToken | None = None
        self.issued_at: float | None = None
        self.client_info = client_info

    async def get_tokens(self) -> OAuthToken | None:
        return self.tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self.tokens, self.issued_at = tokens, time.time()

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return self.client_info

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self.client_info = client_info


class _PinnedScopeMetadata(OAuthClientMetadata):
    """Client metadata whose `scope` the SDK cannot overwrite.

    The SDK recomputes `client_metadata.scope` from the server's challenge and metadata after
    discovery and before registration, discarding whatever the client asked for. Pinning it makes
    the configured scopes what is registered AND requested (and stops a step-up quietly widening
    them: a server that wants more than the user configured is an error to be reported, not a grant
    to be extended). The SDK assigns the attribute in one place; tests/auth/test_login_flow.py
    fails if that ever stops holding.
    """

    _pinned: str | None = PrivateAttr(default=None)

    def pin_scope(self, scope: str) -> None:
        self._pinned = scope
        super().__setattr__("scope", scope)

    def __setattr__(self, name: str, value) -> None:
        if name == "scope" and getattr(self, "_pinned", None) is not None:
            return
        super().__setattr__(name, value)


class _DrifterProvider(OAuthClientProvider):
    def __init__(self, *args, server_name: str, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._server_name = server_name

    async def _perform_authorization_code_grant(self):
        """Refuse before anything is sent to the authorization endpoint unless PKCE S256 is advertised."""
        metadata = self.context.oauth_metadata
        if metadata is None:
            raise LoginRefused(
                f"server {self._server_name!r} published no authorization-server metadata, so PKCE S256 "
                "support cannot be verified; refusing to log in"
            )
        if "S256" not in (metadata.code_challenge_methods_supported or []):
            raise LoginRefused(
                f"server {self._server_name!r}'s authorization server does not advertise PKCE S256 "
                "(code_challenge_methods_supported); refusing to log in"
            )
        for label, endpoint in (("authorization", metadata.authorization_endpoint), ("token", metadata.token_endpoint)):
            parts = urlsplit(str(endpoint))
            if parts.scheme == "http" and not is_loopback_host(parts.hostname):
                raise LoginRefused(
                    f"server {self._server_name!r}'s authorization server advertises a cleartext {label} endpoint "
                    f"(http://{parts.hostname}) on a remote host; refusing to log in, because the login code and tokens "
                    "would travel unencrypted. The endpoint must be https"
                )
        return await super()._perform_authorization_code_grant()


async def _open_browser(url: str, open_browser: Callable[[str], Awaitable[None] | None]) -> None:
    result = open_browser(url)
    if inspect.isawaitable(result):
        await result


async def default_open_browser(url: str) -> None:
    """Opens the system browser without being able to block shutdown (`webbrowser.open` blocks on
    terminal browsers). A browser that cannot be opened is not an error: the URL is printed."""
    try:
        await run_in_daemon_thread(lambda: webbrowser.open(url))
    except Exception:
        pass


def _no_way_to_register(server: ServerConfig, *, saw_metadata_documents: bool) -> str:
    what = (
        "supports client-ID metadata documents, but Drifter has no published document to offer yet, and offers no "
        "dynamic client registration"
        if saw_metadata_documents
        else "offers neither client-ID metadata documents nor dynamic client registration"
    )
    return (
        f"server {server.name!r} {what}, so Drifter has no client identity to present. Register a client with it yourself "
        "and set `auth.client_id` in drifter.yaml (redirect URI http://127.0.0.1/callback), or use "
        "`auth: {type: bearer_env, token_env: NAME}` with a token you obtain another way"
    )


async def run_login(
    server: ServerConfig,
    store: TokenStore,
    *,
    client_metadata_url: str | None,
    open_browser: Callable[[str], Awaitable[None] | None],
    announce: Callable[[str], None],
    timeout: float = DEFAULT_LOGIN_TIMEOUT_S,
) -> LoginResult:
    """Log in to `server` and save the result to `store`. Raises `LoginRefused` with a reason that
    names the server and never contains a token, a code or a state value."""
    auth = server.auth
    assert auth is not None and auth.type == "oauth" and server.url is not None

    sdk_logger = logging.getLogger(_SDK_AUTH_LOGGER)
    previous_level = sdk_logger.level
    sdk_logger.setLevel(logging.CRITICAL)  # the SDK logs a traceback for every refusal; we report our own
    try:
        async with CallbackListener() as listener:
            preregistered = None
            if auth.client_id:
                preregistered = OAuthClientInformationFull(
                    client_id=auth.client_id,
                    redirect_uris=[AnyUrl(listener.redirect_uri)],
                    token_endpoint_auth_method="none",
                    grant_types=["authorization_code", "refresh_token"],
                    response_types=["code"],
                    scope=" ".join(auth.scopes) if auth.scopes else None,
                )
            memory = _LoginStorage(preregistered)

            metadata = _PinnedScopeMetadata(
                client_name="Drifter",
                redirect_uris=[AnyUrl(listener.redirect_uri)],
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                token_endpoint_auth_method="none",
            )
            if auth.scopes:
                metadata.pin_scope(" ".join(auth.scopes))

            async def redirect_handler(url: str) -> None:
                parts = urlsplit(url)
                if parts.scheme not in ("http", "https"):  # never hand a browser anything else
                    raise LoginRefused(f"server {server.name!r} produced an authorization URL that is not http(s); refusing")
                state = (parse_qs(parts.query).get("state") or [""])[0]
                listener.expect(state)
                announce(f"Open this URL in your browser to log in to {server.name!r}:")
                announce(f"  {url}")
                await _open_browser(url, open_browser)

            async def callback_handler():
                return await listener.wait(timeout)

            provider = _DrifterProvider(
                server_url=server.url,
                client_metadata=metadata,
                storage=memory,
                redirect_handler=redirect_handler,
                callback_handler=callback_handler,
                client_metadata_url=None if auth.client_id else client_metadata_url,
                server_name=server.name,
            )
            status = await _drive(provider, server)

            tokens = memory.tokens
            if tokens is None or memory.client_info is None:
                if status < 400:
                    raise LoginRefused(
                        f"server {server.name!r} accepted the request without asking for authorization (HTTP {status}), "
                        "so there is nothing to log in to; remove the `auth` block, or check the url"
                    )
                raise LoginRefused(f"server {server.name!r} completed the flow without issuing a token")
            identity = (
                "preregistered"
                if auth.client_id
                else "cimd" if client_metadata_url and memory.client_info.client_id == client_metadata_url else "dcr"
            )
            await store.save_login(memory.client_info, tokens, issued_at=memory.issued_at)
            return LoginResult(identity, tokens.scope, await store.get_expiry(), store.backend_name)
    finally:
        sdk_logger.setLevel(previous_level)


async def _drive(provider: _DrifterProvider, server: ServerConfig) -> int:
    """Sends one request through the provider so the OAuth flow runs, and returns the final HTTP status."""
    server_name = server.name
    try:
        async with create_mcp_http_client(auth=provider) as client:
            response = await client.post(
                server.url, json=_INITIALIZE, headers={"Accept": "application/json, text/event-stream"}
            )
            session_id = response.headers.get("mcp-session-id")
            if session_id:  # a session this probe opened: close it rather than leave it for the server to time out
                try:
                    await client.delete(server.url, headers={"mcp-session-id": session_id})
                except httpx2.HTTPError:
                    pass
    except LoginRefused:
        raise
    except OAuthRegistrationError:
        metadata = provider.context.oauth_metadata
        # A way to register exists only if the server offers dynamic registration, or supports metadata
        # documents AND we have one to present. Supporting them alone is not an option we can use.
        can_use_a_document = bool(provider.context.client_metadata_url) and bool(
            metadata and metadata.client_id_metadata_document_supported
        )
        if metadata is None or (not metadata.registration_endpoint and not can_use_a_document):
            saw_documents = bool(metadata and metadata.client_id_metadata_document_supported)
            raise LoginRefused(_no_way_to_register(server, saw_metadata_documents=saw_documents)) from None
        raise LoginRefused(f"server {server_name!r} refused to register Drifter as a client") from None
    except OAuthTokenError as exc:
        raise LoginRefused(f"server {server_name!r} did not issue a token: {clean_server_text(str(exc), 160) or 'refused'}") from None
    except OAuthFlowError as exc:
        text = str(exc)
        if "State parameter mismatch" in text:  # the message carries both state values; neither is printed
            raise LoginRefused(f"the login callback for {server_name!r} did not match this login (state); refusing") from None
        raise LoginRefused(f"login to {server_name!r} failed: {clean_server_text(text, 160) or 'refused'}") from None
    except httpx2.TransportError as exc:
        raise LoginRefused(f"could not connect to {canonical_server_url(server.url)!r}: {type(exc).__name__}") from None
    if response.status_code in (401, 403):
        raise LoginRefused(f"server {server_name!r} still answers HTTP {response.status_code} after the login; the token was not accepted")
    return response.status_code
