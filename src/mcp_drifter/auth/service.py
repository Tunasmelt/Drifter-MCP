"""Service credentials for unattended runs: `type: client_credentials` (F-45, docs/SPEC.md §5.2).

For CI, where nobody can click "Allow". The client proves who it is with a secret (`secret_env`) or a signed
assertion (`private_key_env`) and the authorization server hands back an access token. Nothing is stored: the
token lives in memory for the run, and the next run asks again.

What this wraps (the SDK's `ClientCredentialsOAuthProvider` / `PrivateKeyJWTOAuthProvider`) and what it adds:

  - `issuer` is required by the config and passed to the SDK, so credentials go ONLY to the authorization
    server the config names. Without it the MCP server's own metadata would decide who receives the secret.
  - the SDK's failures (a refused client, an issuer that does not match, missing metadata) surface as
    `UpstreamAuthRejected` naming the environment VARIABLE and the server, never a credential, and are handed
    to `on_rejected` first so `observe` can end the process (see `upstream.rejection_hook`).
  - a secret or key that cannot possibly work is refused before any request is made.
"""

from __future__ import annotations

import re
from collections.abc import AsyncGenerator, Callable, Mapping
from urllib.parse import urlparse

import httpx2
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from mcp.client.auth import OAuthFlowError, OAuthTokenError, TokenStorage
from mcp.client.auth.extensions.client_credentials import (
    ClientCredentialsOAuthProvider,
    PrivateKeyJWTOAuthProvider,
    SignedJWTParameters,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from mcp_drifter.auth.config import AuthConfig, is_loopback_host
from mcp_drifter.auth.errors import AuthSetupError, UpstreamAuthRejected
from mcp_drifter.auth.text import clean_server_text

_VISIBLE_ASCII_RE = re.compile(r"^[\x21-\x7e]+$")
# A client assertion only has to survive the one request that carries it.
ASSERTION_LIFETIME_SECONDS = 60

_EC_CURVE = {"ES256": ec.SECP256R1, "ES384": ec.SECP384R1, "ES512": ec.SECP521R1}


class MemoryStorage(TokenStorage):
    """Tokens for the life of this process only: a CI run keeps no login, so nothing can be left behind."""

    def __init__(self) -> None:
        self._tokens: OAuthToken | None = None
        self._client_info: OAuthClientInformationFull | None = None

    async def get_tokens(self) -> OAuthToken | None:
        return self._tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self._tokens = tokens

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return self._client_info

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self._client_info = client_info


def _read_variable(name: str, server_name: str, field: str, environ: Mapping[str, str]) -> str:
    where = f"auth.{field} of server {server_name!r}"
    raw = environ.get(name)
    if raw is None:
        raise AuthSetupError(f"environment variable {name} is not set ({where}); export it before running drifter")
    if not raw.strip():
        raise AuthSetupError(f"environment variable {name} is empty ({where})")
    return raw


def read_client_secret(auth: AuthConfig, server_name: str, environ: Mapping[str, str]) -> str:
    """The client secret, trimmed of the newline `export S=$(cat file)` leaves. A secret with a space or an
    embedded newline is refused rather than cleaned: it is almost always a wrongly exported value."""
    name = auth.secret_env
    assert name is not None  # AuthConfig: client_credentials has exactly one of secret_env / private_key_env
    secret = _read_variable(name, server_name, "secret_env", environ).strip()
    if not _VISIBLE_ASCII_RE.match(secret):
        raise AuthSetupError(
            f"environment variable {name} (auth.secret_env of server {server_name!r}) must hold a single secret of "
            "visible ASCII characters: no spaces, newlines or non-ASCII. Check how it was exported"
        )
    return secret


def read_private_key(auth: AuthConfig, server_name: str, environ: Mapping[str, str]) -> str:
    """The PEM private key, checked to be loadable and to suit `signing_algorithm` BEFORE any request. Secret
    stores often flatten a PEM onto one line with the two characters `\\n` for each newline; that is undone."""
    name, algorithm = auth.private_key_env, auth.signing_algorithm
    assert name is not None and algorithm is not None
    where = f"environment variable {name} (auth.private_key_env of server {server_name!r})"
    pem = _read_variable(name, server_name, "private_key_env", environ).strip()
    if "\n" not in pem and "\\n" in pem:
        pem = pem.replace("\\n", "\n")
    try:
        key = serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
    except TypeError:
        raise AuthSetupError(f"{where} holds a password-protected key; Drifter needs an unencrypted PEM") from None
    except Exception:  # noqa: BLE001 - the library's message can quote the input; say only what is wrong
        raise AuthSetupError(f"{where} does not hold a PEM private key Drifter can read (PKCS#8 or traditional PEM)") from None
    if algorithm in _EC_CURVE:
        suits = isinstance(key, ec.EllipticCurvePrivateKey) and isinstance(key.curve, _EC_CURVE[algorithm])
    else:
        suits = isinstance(key, rsa.RSAPrivateKey)
    if not suits:
        raise AuthSetupError(f"{where} holds a key that cannot sign with {algorithm}; set auth.signing_algorithm to match it")
    return pem


def _advice(auth: AuthConfig) -> str:
    credential = auth.secret_env or auth.private_key_env
    return (
        f"check auth.client_id, auth.issuer and the value of {credential}; this is a service credential, "
        "so there is no login to run"
    )


class _Refusals:
    """What both providers add. Placed before the SDK class so these override its steps."""

    _server_name: str
    _source: str
    _advice_text: str
    _on_rejected: Callable[[UpstreamAuthRejected], None] | None
    _last_refusal: tuple[int, str | None, str | None] | None
    _credentials: list[str]

    def _configure(self, server_name: str, source: str, advice: str, on_rejected, credentials: list[str]) -> None:
        self._server_name, self._source, self._advice_text, self._on_rejected = server_name, source, advice, on_rejected
        self._last_refusal = None
        self._credentials = credentials  # the secret, or every assertion minted so far: never to be printed

    def _scrub(self, text: str | None) -> str | None:
        """Server text with this client's own credentials removed: some servers reflect what they received."""
        if text is None:
            return None
        for credential in self._credentials:
            text = text.replace(credential, "[redacted]")
        return text

    def _get_token_endpoint(self) -> str:
        """The SDK posts the credentials here, so a cleartext endpoint on another host is refused first."""
        endpoint = super()._get_token_endpoint()  # type: ignore[misc]
        parts = urlparse(endpoint)
        if parts.scheme != "https" and not (parts.scheme == "http" and is_loopback_host(parts.hostname)):
            raise OAuthFlowError(
                f"the authorization server advertises a cleartext token endpoint ({parts.scheme}://{parts.hostname}); "
                "credentials are only sent over https (or to localhost)"
            )
        return endpoint

    def _refused(self, exc: Exception) -> UpstreamAuthRejected:
        if self._last_refusal is not None:
            status, error, description = self._last_refusal
            error = UpstreamAuthRejected(
                self._server_name, self._source, status, error=error, description=description, advice=self._advice_text
            )
        else:
            detail = clean_server_text(self._scrub(str(exc)), 300) or "the flow could not continue"
            error = UpstreamAuthRejected(
                self._server_name,
                self._source,
                None,
                reason=f"the service credentials could not be exchanged for a token ({detail})",
                advice=self._advice_text,
            )
        if self._on_rejected is not None:
            self._on_rejected(error)  # may not return: `observe` must exit the process
        return error

    async def _handle_token_response(self, response: httpx2.Response) -> None:
        if response.status_code not in (200, 201):
            await response.aread()
            error = description = None
            try:
                body = response.json()
                if isinstance(body, dict):
                    error = clean_server_text(body.get("error") if isinstance(body.get("error"), str) else None)
                    description = clean_server_text(
                        body.get("error_description") if isinstance(body.get("error_description"), str) else None, 160
                    )
            except ValueError:
                pass
            self._last_refusal = (response.status_code, self._scrub(error), self._scrub(description))
        await super()._handle_token_response(response)  # type: ignore[misc]

    async def _auth_flow(self, request: httpx2.Request) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        inner = super()._auth_flow(request)  # type: ignore[misc]
        try:
            message = await inner.__anext__()
            while True:
                response = yield message
                message = await inner.asend(response)
        except StopAsyncIteration:
            return
        except (OAuthFlowError, OAuthTokenError) as exc:
            raise self._refused(exc) from None
        finally:
            # The inner flow holds the SDK's lock while suspended; closing it here, in this task, releases it. Left
            # to the event loop's finalizer it would be closed from another task and fail to release it.
            await inner.aclose()


class ClientSecretProvider(_Refusals, ClientCredentialsOAuthProvider):
    pass


class SignedAssertionProvider(_Refusals, PrivateKeyJWTOAuthProvider):
    pass


def service_provider(
    auth: AuthConfig,
    server_url: str,
    server_name: str,
    environ: Mapping[str, str],
    on_rejected: Callable[[UpstreamAuthRejected], None] | None = None,
):
    """The SDK provider for a `client_credentials` block. Raises `AuthSetupError` (naming the variable, never
    its value) if the secret or key cannot be used."""
    assert auth.type == "client_credentials" and auth.client_id is not None and auth.issuer is not None
    scope = " ".join(auth.scopes) if auth.scopes else None
    credentials: list[str] = []
    if auth.secret_env is not None:
        secret = read_client_secret(auth, server_name, environ)
        credentials.append(secret)
        provider = ClientSecretProvider(
            server_url, MemoryStorage(), client_id=auth.client_id, client_secret=secret, scope=scope, issuer=auth.issuer,
        )
        source = f"environment variable {auth.secret_env}"
    else:
        assert auth.signing_algorithm is not None
        parameters = SignedJWTParameters(
            issuer=auth.client_id,
            subject=auth.client_id,
            signing_key=read_private_key(auth, server_name, environ),
            signing_algorithm=auth.signing_algorithm,
            lifetime_seconds=ASSERTION_LIFETIME_SECONDS,
        )
        sign = parameters.create_assertion_provider()

        async def remembered(audience: str) -> str:
            assertion = await sign(audience)
            credentials.append(assertion)  # a server may reflect it; see `_scrub`
            return assertion

        provider = SignedAssertionProvider(
            server_url, MemoryStorage(), client_id=auth.client_id, assertion_provider=remembered, scope=scope, issuer=auth.issuer,
        )
        source = f"environment variable {auth.private_key_env}"
    provider._configure(server_name, source, _advice(auth), on_rejected, credentials)
    return provider
