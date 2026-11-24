"""Using a stored login to connect, with no one there to answer a question (F-44, docs/SPEC.md §5.2).

`observe`, `doctor` and `fixture` talk to the server for as long as an agent is attached, and `observe`'s
stdout is the agent's protocol channel, so nothing here may open a browser, wait for a callback or print.
The SDK's `OAuthClientProvider` already sends a stored token and refreshes it; this subclass makes the
parts that do not fit an unattended process fail in a useful way instead:

  - the SDK loads a stored token WITHOUT its expiry, so a token that lapsed hours ago would be sent as-is
    and a stale one would trigger a login rather than a refresh. The expiry is restored from the store.
  - an expired login that cannot be refreshed, a refresh the server refuses, and any attempt to start a new
    authorization all end in `UpstreamAuthRejected.login_needed`, which says to run `drifter auth login`.
    No redirect or callback handler is ever given to the provider, so a login cannot start by accident.

A token the SERVER rejects while the stored expiry still says it is fine (revoked, clock skew) never
reaches this class: the SDK would discover, register and authorize, so the response hook installed by
`cli/upstream.py` turns that 401 into the same instruction first.
"""

from __future__ import annotations

from collections.abc import Callable

from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import OAuthClientMetadata
from pydantic import AnyUrl

from mcp_drifter.auth.errors import UpstreamAuthRejected
from mcp_drifter.auth.storage import StorageError, TokenStore


class StoredLoginProvider(OAuthClientProvider):
    def __init__(
        self,
        server_url: str,
        store: TokenStore,
        *,
        server_name: str,
        on_rejected: Callable[[UpstreamAuthRejected], None] | None = None,
    ) -> None:
        metadata = OAuthClientMetadata(
            redirect_uris=[AnyUrl("http://127.0.0.1/callback")],  # required by the model, never used: no login happens here
            grant_types=["authorization_code", "refresh_token"],
            token_endpoint_auth_method="none",
            client_name="Drifter",
        )
        super().__init__(server_url, metadata, store)  # deliberately no redirect_handler or callback_handler
        self._server_name = server_name
        self._store = store
        self._on_rejected = on_rejected

    def _login_needed(self, reason: str, advice: str | None = None) -> UpstreamAuthRejected:
        error = UpstreamAuthRejected.login_needed(self._server_name, reason, advice)
        if self._on_rejected is not None:
            self._on_rejected(error)  # may not return: `observe` must exit the process, see rejection_hook
        return error

    async def _initialize(self) -> None:
        try:
            await super()._initialize()
            expiry = await self._store.get_expiry()
        except StorageError as exc:
            raise self._login_needed(
                f"the stored login cannot be read ({exc})",
                f"fix that, or run `drifter auth logout {self._server_name}` and log in again",
            ) from None
        context = self.context
        if context.current_tokens is None:
            raise self._login_needed("there is no stored login")
        context.token_expiry_time = expiry
        if not context.is_token_valid() and not context.can_refresh_token():
            raise self._login_needed("the stored login has expired and cannot be refreshed")

    async def _handle_refresh_response(self, response) -> bool:
        refreshed = await super()._handle_refresh_response(response)
        if not refreshed:
            # The SDK would now send the request with no credentials; say what happened instead.
            raise self._login_needed("the server refused to refresh the stored login")
        return refreshed

    async def _perform_authorization(self):
        raise self._login_needed("the server asked for a new authorization")
