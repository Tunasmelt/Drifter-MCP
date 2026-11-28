"""Findings from the 0.3.0 audit of F-41..F-46 (docs/CHANGELOG.md "0.3.0 audit"): each test here failed before its fix.

A server this tool is pointed at may ECHO what it was sent: "token <token> is expired", "bad secret <secret>". F-47's
promise is that no credential reaches a terminal, a log or a recording, whoever puts it there.

  1. A bearer / OAuth access token reflected in a `WWW-Authenticate` challenge was printed in full.
  2. A client secret reflected in a token-endpoint error survived: the text was cleaned (punctuation dropped,
     capped at 160 characters) BEFORE the secret was looked for, so a secret containing `~ = @` or sitting across the
     cap no longer matched and was printed mangled or cut.
  3. A malformed token response ("Invalid token response: 1 validation error ... input_value={'refresh_token':
     'drifte...") printed the first characters of the token the server had just issued.
  4. The same pydantic error came out of the token STORE for a hand-damaged record, as a raw `ValidationError` (not a
     `StorageError`), so `observe` crashed with a traceback instead of saying "log in again".
  5. A login's token-exchange error could carry the authorization code or PKCE verifier the server was just sent.
"""

from __future__ import annotations

import json
import os

import httpx2
import pytest
from mcp.client.auth import OAuthTokenError

from mcp_drifter.auth.config import AuthConfig
from mcp_drifter.auth.errors import LoginRefused, UpstreamAuthRejected
from mcp_drifter.auth.login import _drive
from mcp_drifter.auth.service import service_provider
from mcp_drifter.auth.storage import StorageError, TokenStore
from mcp_drifter.auth.text import scrub
from mcp_drifter.auth.upstream import rejection_hook
from mcp_drifter.cli.config import ServerConfig

TOKEN = "drifter-canary-9f2c1a7e-token-value"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _rejected(status: int, challenge: str, authorization: str | None) -> httpx2.Response:
    headers = {"Authorization": authorization} if authorization else {}
    request = httpx2.Request("POST", "https://crm.example/mcp", headers=headers)
    return httpx2.Response(status, request=request, headers={"www-authenticate": challenge})


# --- 1. the credential on the request, reflected in the challenge ----------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    "challenge",
    [
        f'Bearer error="invalid_token", error_description="token {TOKEN} is expired"',
        f'Bearer error="{TOKEN}"',
        f'Bearer error="insufficient_scope", scope="{TOKEN}"',
        f'Bearer error="invalid_token", error_description="{TOKEN[:20]} then {TOKEN}"',
    ],
)
async def test_a_token_the_server_echoes_back_never_reaches_the_message(challenge):
    status = 403 if "insufficient_scope" in challenge else 401
    response = _rejected(status, challenge, f"Bearer {TOKEN}")
    with pytest.raises(UpstreamAuthRejected) as caught:
        await rejection_hook("crm", "environment variable CRM_TOKEN")(response)
    assert TOKEN not in str(caught.value)
    assert TOKEN not in repr(caught.value.__dict__)


@pytest.mark.anyio
async def test_the_rest_of_the_challenge_survives_the_scrub():
    response = _rejected(401, f'Bearer error="invalid_token", error_description="token {TOKEN} is expired"', f"Bearer {TOKEN}")
    with pytest.raises(UpstreamAuthRejected) as caught:
        await rejection_hook("crm", "environment variable CRM_TOKEN")(response)
    text = str(caught.value)
    assert "error=invalid_token" in text and "is expired" in text and "redacted" in text


@pytest.mark.anyio
async def test_a_challenge_with_no_request_headers_behaves_as_before():
    response = _rejected(401, 'Bearer error="invalid_token"', None)
    with pytest.raises(UpstreamAuthRejected, match="error=invalid_token"):
        await rejection_hook("crm", "environment variable CRM_TOKEN")(response)


# --- 2. a client secret reflected in a token-endpoint error ---------------------------------------------------------------


def _client_secret_provider(secret: str):
    auth = AuthConfig(type="client_credentials", client_id="ci", secret_env="CI_SECRET", issuer="https://auth.example/")
    return service_provider(auth, "https://crm.example/mcp", "crm", {"CI_SECRET": secret})


async def _refusal_text(provider, response: httpx2.Response) -> str:
    try:
        await provider._handle_token_response(response)
    except OAuthTokenError as exc:
        return str(provider._refused(exc))
    raise AssertionError("the token response was accepted")


@pytest.mark.anyio
@pytest.mark.parametrize("secret", ["pa~ss=w0rd+secret/ok@9", "plain-Secret_value.123456789"])
async def test_a_reflected_client_secret_is_removed_before_the_text_is_cleaned(secret):
    description = f"unknown client with secret {secret}"
    response = httpx2.Response(401, json={"error": "invalid_client", "error_description": description})
    text = await _refusal_text(_client_secret_provider(secret), response)
    assert secret not in text
    assert "w0rd" not in text and "plain-Secret" not in text  # not even a mangled or partial copy
    assert "invalid_client" in text


@pytest.mark.anyio
async def test_a_secret_that_straddles_the_length_cap_is_still_removed():
    secret = "S3cretValue-abcdefghij1234567890XYZ"
    description = "x" * 140 + " " + secret  # the 160-character cap would cut the secret in half
    response = httpx2.Response(400, json={"error": "invalid_client", "error_description": description})
    text = await _refusal_text(_client_secret_provider(secret), response)
    assert "S3cretValue" not in text and "abcdefghij" not in text


# --- 3. a malformed token response ------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_malformed_token_response_prints_none_of_its_contents():
    body = {"refresh_token": "drifter-canary-REFRESH-0123456789", "token_type": "Bearer"}  # no access_token
    text = await _refusal_text(_client_secret_provider("a-secret-value"), httpx2.Response(200, json=body))
    assert "drifte" not in text and "REFRESH" not in text and "input_value" not in text
    assert "not a valid token response" in text


class _FailsWith(httpx2.Auth):
    """An auth flow that fails the way the SDK does, for `_drive`."""

    def __init__(self, error: Exception, sensitive: list[str]) -> None:
        self._error = error
        self._sensitive = sensitive
        self.context = None

    def scrub(self, text: str) -> str:
        return scrub(text, self._sensitive) or ""

    def auth_flow(self, request):
        raise self._error
        yield  # pragma: no cover


def _server() -> ServerConfig:
    return ServerConfig(name="crm", url="https://crm.example/mcp", auth={"type": "oauth"})


@pytest.mark.anyio
async def test_a_login_error_that_echoes_the_code_or_the_verifier_does_not_print_them():
    error = OAuthTokenError("Token exchange failed (400): invalid_grant, code AUTHCODE-77 / verifier VERIFIER-4242")
    with pytest.raises(LoginRefused) as caught:
        await _drive(_FailsWith(error, ["AUTHCODE-77", "VERIFIER-4242"]), _server())
    assert "AUTHCODE-77" not in str(caught.value) and "VERIFIER-4242" not in str(caught.value)
    assert "invalid_grant" in str(caught.value)


@pytest.mark.anyio
async def test_a_login_with_a_malformed_token_response_prints_none_of_its_contents():
    error = OAuthTokenError("Invalid token response: 1 validation error ... input_value={'refresh_token': 'drifte...")
    with pytest.raises(LoginRefused) as caught:
        await _drive(_FailsWith(error, []), _server())
    assert "drifte" not in str(caught.value) and "input_value" not in str(caught.value)


# --- 4. a damaged stored record ---------------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("field", ["tokens", "client_info"])
async def test_a_damaged_stored_record_is_a_storage_error_that_shows_no_token_material(tmp_path, field):
    store = TokenStore("crm", "https://crm.example/mcp", base_dir=tmp_path / "cfg", backend="file")
    (tmp_path / "cfg" / "tokens").mkdir(parents=True, mode=0o700)
    os.chmod(tmp_path / "cfg", 0o700)
    damaged = {"refresh_token": "drifter-canary-REFRESH-0123456789", "token_type": "Bearer", "client_secret": "drifter-canary-S"}
    record = {"format": 1, "server": "crm", "url": store.server_url, "tokens": None, "client_info": None, "expires_at": None}
    record[field] = damaged
    store.file_path.write_text(json.dumps(record), encoding="utf-8")
    os.chmod(store.file_path, 0o600)
    read = store.get_tokens if field == "tokens" else store.get_client_info
    with pytest.raises(StorageError) as caught:
        await read()
    assert "REFRESH" not in str(caught.value) and "canary" not in str(caught.value) and "input_value" not in str(caught.value)
    assert "drifter auth logout crm" in str(caught.value)


# --- the helper -------------------------------------------------------------------------------------------------------------------


def test_scrub_replaces_every_secret_longest_first_and_never_an_empty_one():
    assert scrub("a TOKEN-LONG b TOKEN", ["TOKEN", "TOKEN-LONG"]) == "a [redacted] b [redacted]"
    assert scrub("nothing here", ["", "  "]) == "nothing here"
    assert scrub(None, ["x"]) is None


@pytest.mark.anyio
async def test_observe_style_connect_with_a_damaged_record_says_to_log_in_again_not_a_traceback(tmp_path):
    from mcp_drifter.auth.session import StoredLoginProvider

    store = TokenStore("crm", "https://crm.example/mcp", base_dir=tmp_path / "cfg", backend="file")
    (tmp_path / "cfg" / "tokens").mkdir(parents=True, mode=0o700)
    os.chmod(tmp_path / "cfg", 0o700)
    record = {"format": 1, "server": "crm", "url": store.server_url, "client_info": None, "expires_at": None,
              "tokens": {"refresh_token": "drifter-canary-REFRESH-0123456789", "token_type": "Bearer"}}
    store.file_path.write_text(json.dumps(record), encoding="utf-8")
    os.chmod(store.file_path, 0o600)
    provider = StoredLoginProvider("https://crm.example/mcp", store, server_name="crm")
    with pytest.raises(UpstreamAuthRejected) as caught:
        await provider.check_usable()
    assert "log in again" in str(caught.value) and "REFRESH" not in str(caught.value) and "canary" not in str(caught.value)
