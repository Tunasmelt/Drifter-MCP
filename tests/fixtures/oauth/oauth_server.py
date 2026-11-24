"""Local OAuth-protected Streamable HTTP MCP server for the 0.3.0 auth tests
(docs/PHASES.md 0.3.0 P1, docs/SPEC.md §5.2).

One loopback port serves both the resource server (`/mcp`) and an in-memory
authorization server. No network, no third party. Built on the SDK's OWN server-side
auth (`mcp.server.auth`: `create_auth_routes`, bearer middleware, protected-resource
metadata) so Drifter is tested against the same code real Python MCP servers run,
rather than a hand-rolled imitation of the protocol. Only what the SDK does not offer
is added here: the `client_credentials` grant, CIMD client resolution, and the
deliberately-broken metadata modes.

Modes (`FixtureConfig`), one per PHASES.md bullet:
  pre-registered client ...... registration="preregistered"
  CIMD-only .................. registration="cimd"
  DCR-only ................... registration="dcr"
  static bearer .............. protection="static_bearer" (no OAuth metadata at all)
  client credentials ......... client_credentials=True
  no-S256 metadata ........... advertise_s256=False
  issuer mismatch ............ issuer_mismatch=True
  short-lived tokens ......... advertised_expires_in_s=N (the client thinks the token is
                               stale; the server still accepts it, so a slow machine
                               cannot race the server's own clock), or access_token_ttl_s=N
                               (the server really expires it), plus `state.expire_access_tokens()`
  refresh disabled ........... refresh_tokens=False
  scope-dependent tools/list . `FixtureConfig.tools` (the default set already is)

Every secret it mints (access/refresh tokens, authorization codes, client secrets, the
static bearer) starts with `state.canary`, so the F-47 detector (`canary_scan.py`) can
hunt for all of them at once. `state.requests` logs what the server actually received,
so tests can assert on what Drifter sent -- or that it sent nothing at all.

"Browser consent" is auto-approved: `/authorize` answers with a 302 to the client's
redirect URI. `follow_authorization(url)` plays the user's browser for tests.
"""

from __future__ import annotations

import base64
import secrets
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import parse_qs, urlparse

import anyio
import httpx2
import mcp_types as types
import uvicorn
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    ProviderTokenVerifier,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.routes import build_metadata
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.lowlevel import Server
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl, AnyUrl
from sse_starlette.sse import AppStatus
from starlette.responses import JSONResponse
from starlette.routing import Route

from canary_scan import new_canary
from mcp_drifter.cli.http_proxy import _bind_ephemeral_loopback_socket, _wait_until_actually_answering

MCP_PATH = "/mcp"
ALL_SCOPES = ["read", "admin"]

PREREGISTERED_CLIENT_ID = "drifter-fixture-client"
MACHINE_CLIENT_ID = "drifter-fixture-machine"
LOOPBACK_REDIRECT = "http://127.0.0.1/callback"
# Where a CIMD-capable client would publish its document. Never fetched: the fixture's
# authorization server resolves it from `state.published_documents`.
CIMD_CLIENT_URL = "https://drifter.fixture.invalid/oauth/client.json"
ISSUER_MISMATCH_VALUE = "https://issuer-mismatch.invalid"


@dataclass(frozen=True)
class FixtureTool:
    name: str
    scope: str | None  # scope a token must carry for the tool to be listed and callable


DEFAULT_TOOLS = (
    FixtureTool("get_item", "read"),
    FixtureTool("list_items", "read"),
    FixtureTool("delete_item", "admin"),
)


@dataclass(frozen=True)
class FixtureConfig:
    protection: Literal["oauth", "static_bearer"] = "oauth"
    registration: Literal["preregistered", "cimd", "dcr"] = "dcr"
    client_credentials: bool = False
    advertise_s256: bool = True
    issuer_mismatch: bool = False
    access_token_ttl_s: int = 3600  # how long the SERVER accepts an access token
    # What the token response's `expires_in` CLAIMS; None means the same as the ttl. Setting it
    # below the ttl makes the client's own clock say "stale" while the server still accepts the
    # token: a deterministic way to force a client-side refresh with no race between two clocks.
    advertised_expires_in_s: int | None = None
    refresh_tokens: bool = True
    rotate_refresh_tokens: bool = True
    loopback_any_port: bool = True  # CIMD -03 / RFC 9700 §4.1.3: loopback redirects may vary the port
    required_scopes: tuple[str, ...] = ("read",)
    tools: tuple[FixtureTool, ...] = DEFAULT_TOOLS


@dataclass
class RequestRecord:
    method: str
    path: str
    authorization: str | None  # the raw header value the server received, or None
    status: int | None = None


class _LoopbackClient(OAuthClientInformationFull):
    """A registered client whose loopback redirect URIs may use any port, per the
    exception in RFC 9700 §4.1.3 that CIMD -03 now cites."""

    def validate_redirect_uri(self, redirect_uri: AnyUrl | None) -> AnyUrl:
        if redirect_uri is not None and self.redirect_uris:
            for registered in self.redirect_uris:
                if (
                    registered.scheme == redirect_uri.scheme == "http"
                    and registered.host == redirect_uri.host
                    and registered.host in ("127.0.0.1", "localhost", "[::1]")
                    and registered.path == redirect_uri.path
                ):
                    return redirect_uri
        return super().validate_redirect_uri(redirect_uri)


@dataclass
class FixtureState:
    config: FixtureConfig
    origin: str
    canary: str = field(default_factory=new_canary)
    clients: dict[str, OAuthClientInformationFull] = field(default_factory=dict)
    published_documents: dict[str, dict[str, Any]] = field(default_factory=dict)
    codes: dict[str, AuthorizationCode] = field(default_factory=dict)
    access_tokens: dict[str, AccessToken] = field(default_factory=dict)
    refresh_tokens: dict[str, RefreshToken] = field(default_factory=dict)
    expired: set[str] = field(default_factory=set)
    issued_secrets: list[str] = field(default_factory=list)
    requests: list[RequestRecord] = field(default_factory=list)
    token_grants: list[str] = field(default_factory=list)  # grant_type of each /token request
    authorize_requests: int = 0
    registrations: int = 0
    forced_statuses: list[int] = field(default_factory=list)
    _counter: int = 0

    @property
    def mcp_url(self) -> str:
        return f"{self.origin}{MCP_PATH}"

    @property
    def issuer(self) -> str:
        """The issuer string the metadata and the protected-resource metadata both advertise.
        A root issuer is written WITH its trailing slash: the SDK's protected-resource
        metadata serializes it that way, and the SDK client then compares the AS metadata's
        `issuer` to that string exactly (RFC 8414 §3.3), so a bare-origin issuer is refused."""
        return f"{self.origin}/"

    @property
    def machine_client_secret(self) -> str:
        return f"{self.canary}-client-secret"

    @property
    def static_bearer(self) -> str:
        return f"{self.canary}-static-bearer"

    def mint(self, kind: str) -> str:
        self._counter += 1
        value = f"{self.canary}-{kind}-{self._counter}"
        self.issued_secrets.append(value)
        return value

    def all_secrets(self) -> list[str]:
        """Every secret this fixture has handed out so far -- what the F-47 detector hunts for.
        The bare canary comes first: every secret starts with it, so a token that was truncated
        or masked after the prefix still contains it and is caught."""
        fixed = [self.machine_client_secret, self.static_bearer]
        return [self.canary, *fixed, *self.issued_secrets]

    def expire_access_tokens(self) -> None:
        """Server-side expiry: every access token issued so far now gets a 401, without
        waiting on a clock the SDK's bearer middleware does not let a test control."""
        self.expired.update(self.access_tokens)

    def force_status(self, status: int, times: int = 1) -> None:
        """The next `times` requests to /mcp get this 401 or 403, whatever their token."""
        assert status in (401, 403)
        self.forced_statuses.extend([status] * times)

    def publish_client_document(self, url: str, document: dict[str, Any]) -> None:
        self.published_documents[url] = document

    def mcp_requests(self) -> list[RequestRecord]:
        return [r for r in self.requests if r.path == MCP_PATH]

    def requests_to(self, path: str) -> list[RequestRecord]:
        return [r for r in self.requests if r.path == path]


def default_client_document(url: str = CIMD_CLIENT_URL) -> dict[str, Any]:
    """A CIMD -03-shaped document: `client_id` exactly its own URL, loopback redirect, and
    NO token-endpoint auth property (-03 reads that as `none`, right for a public client)."""
    return {
        "client_id": url,
        "client_name": "Drifter (fixture)",
        "redirect_uris": [LOOPBACK_REDIRECT],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    }


class FixtureProvider:
    """The in-memory authorization server (the SDK's `OAuthAuthorizationServerProvider`)."""

    def __init__(self, state: FixtureState) -> None:
        self.state = state

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        state = self.state
        if client_id in state.clients:
            return state.clients[client_id]
        if state.config.registration == "cimd" and client_id in state.published_documents:
            return self._client_from_document(client_id, state.published_documents[client_id])
        return None

    def _client_from_document(self, url: str, document: dict[str, Any]) -> OAuthClientInformationFull | None:
        # CIMD -03: a document whose client_id is not exactly its own URL is a terminal failure.
        if document.get("client_id") != url or not document.get("redirect_uris"):
            return None
        info = {
            **document,
            "token_endpoint_auth_method": "none",
            "scope": document.get("scope", " ".join(ALL_SCOPES)),
        }
        return self._wrap(OAuthClientInformationFull.model_validate(info))

    def _wrap(self, info: OAuthClientInformationFull) -> OAuthClientInformationFull:
        if self.state.config.loopback_any_port:
            return _LoopbackClient.model_validate(info.model_dump())
        return info

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self.state.registrations += 1
        if client_info.client_secret is not None:
            # the SDK generated the secret; swap it for a canary-prefixed one so it is detectable
            client_info = client_info.model_copy(update={"client_secret": self.state.mint("client-secret")})
        assert client_info.client_id is not None
        self.state.clients[client_info.client_id] = self._wrap(client_info)

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        state = self.state
        state.authorize_requests += 1
        assert client.client_id is not None
        code = state.mint("code")
        state.codes[code] = AuthorizationCode(
            code=code,
            scopes=params.scopes or ["read"],
            expires_at=time.time() + 300,
            client_id=client.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str):
        return self.state.codes.get(authorization_code)

    def _issue(self, client_id: str, scopes: list[str], resource: str | None, *, with_refresh: bool) -> OAuthToken:
        state = self.state
        ttl = state.config.access_token_ttl_s
        access = state.mint("access")
        state.access_tokens[access] = AccessToken(
            token=access, client_id=client_id, scopes=scopes, expires_at=int(time.time()) + ttl, resource=resource
        )
        refresh = None
        if with_refresh and state.config.refresh_tokens:
            refresh = state.mint("refresh")
            state.refresh_tokens[refresh] = RefreshToken(
                token=refresh, client_id=client_id, scopes=scopes, resource=resource
            )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=state.config.advertised_expires_in_s if state.config.advertised_expires_in_s is not None else ttl,
            scope=" ".join(scopes) or None,
            refresh_token=refresh,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        self.state.codes.pop(authorization_code.code, None)  # single use
        assert client.client_id is not None
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.resource, with_refresh=True)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str):
        found = self.state.refresh_tokens.get(refresh_token)
        return found if found and found.client_id == client.client_id else None

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        state = self.state
        if not state.config.refresh_tokens:
            raise TokenError(error="invalid_grant", error_description="refresh is disabled")
        if state.config.rotate_refresh_tokens:
            state.refresh_tokens.pop(refresh_token.token, None)
        granted = scopes or refresh_token.scopes
        assert client.client_id is not None
        return self._issue(client.client_id, granted, refresh_token.resource, with_refresh=True)

    async def load_access_token(self, token: str) -> AccessToken | None:
        if token in self.state.expired:
            return None
        return self.state.access_tokens.get(token)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self.state.access_tokens.pop(token.token, None)
        self.state.refresh_tokens.pop(token.token, None)

    async def exchange_identity_assertion(self, client, params):  # pragma: no cover - not enabled
        raise TokenError(error="unsupported_grant_type")


def _json(status: int, body: dict[str, Any], headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store", **(headers or {})})


class _TokenEndpoint:
    """Wraps the SDK's `/token` handler to add what it lacks: the `client_credentials`
    grant. Every other grant is replayed, byte for byte, into the SDK's own handler."""

    def __init__(self, inner: Callable, provider: FixtureProvider) -> None:
        self.inner = inner
        self.provider = provider

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.inner(scope, receive, send)
            return
        body = b""
        more = True
        while more:
            message = await receive()
            body += message.get("body", b"")
            more = message.get("more_body", False)
        form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
        grant = form.get("grant_type", "")
        self.provider.state.token_grants.append(grant)
        if grant == "client_credentials":
            headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
            response = self._client_credentials(form, headers.get("authorization"))
            await response(scope, receive, send)
            return

        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        await self.inner(scope, replay, send)

    def _client_credentials(self, form: dict[str, str], authorization: str | None) -> JSONResponse:
        state = self.provider.state
        if not state.config.client_credentials:
            return _json(400, {"error": "unsupported_grant_type"})
        client_id, client_secret = form.get("client_id"), form.get("client_secret")
        if authorization and authorization.lower().startswith("basic "):
            try:
                decoded = base64.b64decode(authorization[6:]).decode()
                client_id, _, client_secret = decoded.partition(":")
            except (ValueError, UnicodeDecodeError):
                return _json(401, {"error": "invalid_client"})
        if client_id != MACHINE_CLIENT_ID or not secrets.compare_digest(
            (client_secret or "").encode(), state.machine_client_secret.encode()
        ):
            return _json(401, {"error": "invalid_client"})
        requested = form.get("scope", "").split() or ["read"]
        if any(s not in ALL_SCOPES for s in requested):
            return _json(400, {"error": "invalid_scope"})
        token = self.provider._issue(MACHINE_CLIENT_ID, requested, form.get("resource"), with_refresh=False)
        return _json(200, token.model_dump(mode="json", exclude_none=True))


class _FixtureMiddleware:
    """Outermost layer: logs what the server received, enforces the static bearer, and
    injects forced 401/403s. Sits outside the SDK's own auth, so it records requests the
    SDK rejects too."""

    def __init__(self, app, state: FixtureState) -> None:
        self.app = app
        self.state = state

    def _challenge(self, error: str, scope_hint: str | None = None) -> str:
        parts = [f'error="{error}"']
        if scope_hint:
            parts.append(f'scope="{scope_hint}"')
        if self.state.config.protection == "oauth":
            parts.append(f'resource_metadata="{self.state.origin}/.well-known/oauth-protected-resource{MCP_PATH}"')
        return "Bearer " + ", ".join(parts)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        state = self.state
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        record = RequestRecord(scope["method"], scope["path"], headers.get("authorization"))
        state.requests.append(record)

        async def recording_send(message):
            if message["type"] == "http.response.start":
                record.status = message["status"]
            await send(message)

        if scope["path"] == MCP_PATH:
            forced = state.forced_statuses.pop(0) if state.forced_statuses else None
            if forced is None and state.config.protection == "static_bearer":
                expected = f"Bearer {state.static_bearer}"
                if not secrets.compare_digest((record.authorization or "").encode(), expected.encode()):
                    forced = 401
            if forced is not None:
                error = "invalid_token" if forced == 401 else "insufficient_scope"
                hint = "admin" if forced == 403 else None
                if forced == 401 and state.config.protection == "static_bearer":
                    www = "Bearer"
                else:
                    www = self._challenge(error, hint)
                response = _json(forced, {"error": error}, {"WWW-Authenticate": www})
                await response(scope, receive, recording_send)
                return
        await self.app(scope, receive, recording_send)


def _build_server(state: FixtureState) -> Server:
    config = state.config

    def _allowed(tool: FixtureTool) -> bool:
        token = get_access_token()
        return tool.scope is None or token is None or tool.scope in token.scopes

    async def on_list_tools(ctx, params):
        tools = [
            types.Tool(
                name=t.name,
                description=f"{t.name} (needs scope {t.scope})" if t.scope else t.name,
                input_schema={"type": "object", "properties": {"id": {"type": "string"}}},
            )
            for t in config.tools
            if _allowed(t)
        ]
        return types.ListToolsResult(tools=tools)

    async def on_call_tool(ctx, params):
        tool = next((t for t in config.tools if t.name == params.name), None)
        if tool is None or not _allowed(tool):
            text, is_error = f"unknown or forbidden tool {params.name}", True
        else:
            text, is_error = f"{params.name} ok", False
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=is_error)

    return Server(name="oauth-fixture", on_list_tools=on_list_tools, on_call_tool=on_call_tool)


def _build_app(state: FixtureState):
    config = state.config
    server = _build_server(state)
    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=["127.0.0.1:*"], allowed_origins=[]
    )
    if config.protection == "static_bearer":
        app = server.streamable_http_app(streamable_http_path=MCP_PATH, host="127.0.0.1", transport_security=transport_security)
        return _FixtureMiddleware(app, state)

    provider = FixtureProvider(state)
    registration = ClientRegistrationOptions(enabled=config.registration == "dcr", valid_scopes=ALL_SCOPES, default_scopes=["read"])
    auth = AuthSettings(
        issuer_url=AnyHttpUrl(state.origin),
        resource_server_url=AnyHttpUrl(state.mcp_url),
        required_scopes=list(config.required_scopes),
        validate_token_resource=True,
        client_registration_options=registration,
        revocation_options=RevocationOptions(enabled=True),
    )
    app = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        host="127.0.0.1",
        transport_security=transport_security,
        auth=auth,
        token_verifier=ProviderTokenVerifier(provider),
        auth_server_provider=provider,
    )

    metadata = build_metadata(AnyHttpUrl(state.origin), None, registration, RevocationOptions(enabled=True))
    document = metadata.model_dump(mode="json", exclude_none=True)
    document["issuer"] = state.issuer  # compared as a plain string; see FixtureState.issuer
    if not config.advertise_s256:
        document["code_challenge_methods_supported"] = ["plain"]
    if config.issuer_mismatch:
        document["issuer"] = ISSUER_MISMATCH_VALUE
    if config.registration == "cimd":
        document["client_id_metadata_document_supported"] = True
    grants = ["authorization_code"] + (["refresh_token"] if config.refresh_tokens else [])
    document["grant_types_supported"] = grants + (["client_credentials"] if config.client_credentials else [])

    async def metadata_endpoint(request):
        return _json(200, document)

    for index, route in enumerate(app.router.routes):
        path = getattr(route, "path", None)
        if path == "/.well-known/oauth-authorization-server":
            app.router.routes[index] = Route(path, endpoint=metadata_endpoint, methods=["GET"])
        elif path == "/token":
            app.router.routes[index] = Route(path, endpoint=_TokenEndpoint(route.endpoint, provider), methods=["POST", "OPTIONS"])

    _seed_clients(state)
    return _FixtureMiddleware(app, state)


def _seed_clients(state: FixtureState) -> None:
    provider = FixtureProvider(state)
    registration = state.config.registration
    if registration == "preregistered":
        info = OAuthClientInformationFull(
            client_id=PREREGISTERED_CLIENT_ID,
            redirect_uris=[AnyUrl(LOOPBACK_REDIRECT)],
            token_endpoint_auth_method="none",
            grant_types=["authorization_code", "refresh_token"],
            scope=" ".join(ALL_SCOPES),
        )
        state.clients[PREREGISTERED_CLIENT_ID] = provider._wrap(info)
    if registration == "cimd":
        state.publish_client_document(CIMD_CLIENT_URL, default_client_document())
    if state.config.client_credentials:
        state.clients[MACHINE_CLIENT_ID] = OAuthClientInformationFull(
            client_id=MACHINE_CLIENT_ID,
            client_secret=state.machine_client_secret,
            redirect_uris=None,
            token_endpoint_auth_method="client_secret_basic",
            grant_types=["client_credentials"],
            scope=" ".join(ALL_SCOPES),
        )


@dataclass
class FixtureHandle:
    state: FixtureState

    @property
    def mcp_url(self) -> str:
        return self.state.mcp_url

    @property
    def origin(self) -> str:
        return self.state.origin

    @property
    def issuer(self) -> str:
        return self.state.issuer


@asynccontextmanager
async def serve_oauth_fixture(config: FixtureConfig | None = None) -> AsyncIterator[FixtureHandle]:
    """Serves the fixture on an ephemeral loopback port for the life of the context.

    Shutdown follows `cli/http_proxy.py`'s hard-won pattern (its docstrings explain the
    sse-starlette `AppStatus` gotcha and why shutdown waits on `serve_done`).
    """
    config = config or FixtureConfig()
    AppStatus.disable_automatic_graceful_drain()
    AppStatus.should_exit = False

    sock = _bind_ephemeral_loopback_socket("127.0.0.1")
    port = sock.getsockname()[1]
    state = FixtureState(config=config, origin=f"http://127.0.0.1:{port}")
    app = _build_app(state)

    uv_server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    serve_done = anyio.Event()

    async def _serve() -> None:
        try:
            await uv_server.serve([sock])
        finally:
            serve_done.set()

    async def _shutdown() -> None:
        uv_server.should_exit = True
        AppStatus.should_exit = True
        with anyio.move_on_after(5.0):
            await serve_done.wait()

    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(_serve)
            try:
                while not uv_server.started:
                    await anyio.sleep(0.01)
                await _wait_until_actually_answering(state.mcp_url)
                state.requests.clear()  # the readiness probe is not a test's request
                yield FixtureHandle(state)
            finally:
                await _shutdown()
    finally:
        sock.close()


class AuthorizationRefused(Exception):
    """The authorization endpoint answered with an error instead of redirecting to the client.
    Typed so a test can assert it got THIS refusal (status, error code), not just "something raised"."""

    def __init__(self, status: int, error: str | None, description: str | None) -> None:
        super().__init__(f"authorization refused ({status}): {error}: {description}")
        self.status = status
        self.error = error
        self.description = description


async def follow_authorization(authorization_url: str) -> tuple[str, str | None]:
    """Plays the user's browser: GETs the authorization URL, does not follow the redirect,
    and returns `(code, state)` from the redirect target's query string. Raises
    `AuthorizationRefused` if the server refuses instead of redirecting."""
    async with httpx2.AsyncClient(follow_redirects=False) as client:
        response = await client.get(authorization_url)
    if response.status_code not in (302, 303, 307):
        try:
            body = response.json()
        except ValueError:
            body = {}
        raise AuthorizationRefused(response.status_code, body.get("error"), body.get("error_description"))
    query = parse_qs(urlparse(response.headers["location"]).query)
    return query["code"][0], (query.get("state") or [None])[0]
