"""Proves the local OAuth fixture (`tests/fixtures/oauth/oauth_server.py`) is a faithful
stand-in, mode by mode, BEFORE any Drifter auth code exists (docs/PHASES.md 0.3.0 P1).

Every login here is done by the MCP SDK's own reference client auth, not Drifter's. A
fixture nobody has driven end to end proves nothing about the code later written
against it; these tests are what make a later failure mean "Drifter is wrong", not
"the fixture is".

Where the SDK does NOT protect a user (it sends S256 to a server that never advertised
it), the test says so out loud: that behaviour is Drifter's job in F-43.
"""

from __future__ import annotations

import time

import anyio
import httpx2
import pytest
from mcp.client.auth.extensions.client_credentials import ClientCredentialsOAuthProvider
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from oauth_server import (
    CIMD_CLIENT_URL,
    ISSUER_MISMATCH_VALUE,
    LOOPBACK_REDIRECT,
    MACHINE_CLIENT_ID,
    MCP_PATH,
    PREREGISTERED_CLIENT_ID,
    FixtureConfig,
    serve_oauth_fixture,
)
from sdk_client import MemoryStorage, initialized_session, sdk_provider, tool_names


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _metadata(handle) -> dict:
    async with httpx2.AsyncClient() as client:
        response = await client.get(f"{handle.origin}/.well-known/oauth-authorization-server")
    assert response.status_code == 200, response.text
    return response.json()


# --- registration modes -----------------------------------------------------


@pytest.mark.anyio
async def test_dcr_mode_registers_logs_in_and_lists_tools_for_the_granted_scope():
    async with serve_oauth_fixture(FixtureConfig(registration="dcr")) as handle:
        storage = MemoryStorage()
        async with initialized_session(handle, sdk_provider(handle, storage, scope="read")) as session:
            assert await tool_names(session) == ["get_item", "list_items"]

        state = handle.state
        assert state.registrations == 1
        assert state.authorize_requests == 1
        assert state.token_grants == ["authorization_code"]
        metadata = await _metadata(handle)
        assert "registration_endpoint" in metadata
        assert "client_id_metadata_document_supported" not in metadata
        assert storage.tokens is not None and storage.tokens.access_token.startswith(state.canary)


@pytest.mark.anyio
async def test_a_wider_grant_changes_what_tools_list_returns():
    """The fixture's scope-dependent `tools/list`: the reason F-46 records granted scopes.
    The SDK client requests the scopes the server ADVERTISES (protected-resource metadata),
    not whatever the client's own metadata lists, so the two grants come from two servers."""
    async with serve_oauth_fixture(FixtureConfig(required_scopes=("read",))) as narrow:
        async with initialized_session(narrow, sdk_provider(narrow, MemoryStorage())) as session:
            assert await tool_names(session) == ["get_item", "list_items"]
    async with serve_oauth_fixture(FixtureConfig(required_scopes=("read", "admin"))) as wide:
        storage = MemoryStorage()
        async with initialized_session(wide, sdk_provider(wide, storage)) as session:
            assert await tool_names(session) == ["delete_item", "get_item", "list_items"]
        assert storage.tokens is not None and sorted(storage.tokens.scope.split()) == ["admin", "read"]


@pytest.mark.anyio
async def test_the_sdk_ignores_the_scope_in_its_own_client_metadata():
    """CHARACTERIZATION, and a constraint on F-41/F-43: asking for `read admin` in the
    client's metadata gets only `read` from a server that advertises only `read`. The SDK
    picks scope from the WWW-Authenticate header, then protected-resource metadata, then
    authorization-server metadata (`get_client_metadata_scopes`) -- never from the client's
    own `scope`. So `servers[].auth.scopes` cannot be honored just by passing it through;
    Drifter must apply it itself."""
    async with serve_oauth_fixture(FixtureConfig(required_scopes=("read",))) as handle:
        storage = MemoryStorage()
        async with initialized_session(handle, sdk_provider(handle, storage, scope="read admin")) as session:
            assert await tool_names(session) == ["get_item", "list_items"]
        assert storage.tokens is not None and storage.tokens.scope == "read"


@pytest.mark.anyio
async def test_preregistered_mode_has_no_registration_endpoint_and_the_stored_client_id_is_used():
    async with serve_oauth_fixture(FixtureConfig(registration="preregistered")) as handle:
        metadata = await _metadata(handle)
        assert "registration_endpoint" not in metadata
        assert "client_id_metadata_document_supported" not in metadata

        storage = MemoryStorage(
            client_info=OAuthClientInformationFull(
                client_id=PREREGISTERED_CLIENT_ID,
                redirect_uris=[AnyUrl(LOOPBACK_REDIRECT)],
                token_endpoint_auth_method="none",
                scope="read admin",
            )
        )
        async with initialized_session(handle, sdk_provider(handle, storage)) as session:
            assert await tool_names(session) == ["get_item", "list_items"]
        assert handle.state.registrations == 0
        assert handle.state.requests_to("/register") == []


@pytest.mark.anyio
async def test_cimd_mode_uses_the_document_url_as_client_id_and_never_registers():
    async with serve_oauth_fixture(FixtureConfig(registration="cimd")) as handle:
        metadata = await _metadata(handle)
        assert metadata["client_id_metadata_document_supported"] is True
        assert "registration_endpoint" not in metadata

        storage = MemoryStorage()
        provider = sdk_provider(handle, storage, client_metadata_url=CIMD_CLIENT_URL)
        async with initialized_session(handle, provider) as session:
            assert await tool_names(session) == ["get_item", "list_items"]
        assert storage.client_info is not None and storage.client_info.client_id == CIMD_CLIENT_URL
        assert handle.state.registrations == 0


@pytest.mark.anyio
async def test_cimd_mode_refuses_a_document_whose_client_id_is_not_its_own_url():
    """CIMD -03: a malformed document is a terminal failure at the authorization server."""
    async with serve_oauth_fixture(FixtureConfig(registration="cimd")) as handle:
        handle.state.published_documents[CIMD_CLIENT_URL]["client_id"] = CIMD_CLIENT_URL + "/"
        with pytest.raises(BaseException):
            async with initialized_session(handle, sdk_provider(handle, MemoryStorage(), client_metadata_url=CIMD_CLIENT_URL)):
                pass


# --- other protection modes -------------------------------------------------


@pytest.mark.anyio
async def test_static_bearer_mode_has_no_oauth_surface_and_accepts_only_its_token():
    async with serve_oauth_fixture(FixtureConfig(protection="static_bearer")) as handle:
        async with httpx2.AsyncClient() as client:
            anonymous = await client.post(handle.mcp_url, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
            assert anonymous.status_code == 401
            assert anonymous.headers["www-authenticate"] == "Bearer"
            for path in ("/.well-known/oauth-protected-resource/mcp", "/.well-known/oauth-authorization-server"):
                assert (await client.get(f"{handle.origin}{path}")).status_code == 404

        async with initialized_session(handle, headers={"Authorization": f"Bearer {handle.state.static_bearer}"}) as session:
            assert await tool_names(session) == ["delete_item", "get_item", "list_items"]


@pytest.mark.anyio
async def test_client_credentials_mode_issues_a_token_with_no_browser_step():
    async with serve_oauth_fixture(FixtureConfig(client_credentials=True)) as handle:
        provider = ClientCredentialsOAuthProvider(
            server_url=handle.mcp_url,
            storage=MemoryStorage(),
            client_id=MACHINE_CLIENT_ID,
            client_secret=handle.state.machine_client_secret,
            scope="read",
            issuer=handle.issuer,
        )
        async with initialized_session(handle, provider) as session:
            assert await tool_names(session) == ["get_item", "list_items"]
        assert handle.state.authorize_requests == 0
        assert handle.state.token_grants == ["client_credentials"]


@pytest.mark.anyio
async def test_client_credentials_with_a_wrong_secret_is_rejected():
    async with serve_oauth_fixture(FixtureConfig(client_credentials=True)) as handle:
        provider = ClientCredentialsOAuthProvider(
            server_url=handle.mcp_url,
            storage=MemoryStorage(),
            client_id=MACHINE_CLIENT_ID,
            client_secret="not-the-secret",
            issuer=handle.issuer,
        )
        with pytest.raises(BaseException):
            async with initialized_session(handle, provider):
                pass


# --- deliberately broken metadata -------------------------------------------


@pytest.mark.anyio
async def test_no_s256_mode_advertises_only_plain_and_the_sdk_does_not_notice():
    """CHARACTERIZATION, not a Drifter requirement: the SDK sends S256 unconditionally, so
    this login SUCCEEDS against a server that never advertised S256. Drifter must refuse
    such a server itself, before redirecting (docs/SPEC.md §5.2) -- F-43's own test."""
    async with serve_oauth_fixture(FixtureConfig(advertise_s256=False)) as handle:
        assert (await _metadata(handle))["code_challenge_methods_supported"] == ["plain"]
        async with initialized_session(handle, sdk_provider(handle, MemoryStorage())) as session:
            assert await tool_names(session)
        assert handle.state.authorize_requests == 1


@pytest.mark.anyio
async def test_issuer_mismatch_mode_makes_the_sdk_stop_before_authorizing():
    async with serve_oauth_fixture(FixtureConfig(issuer_mismatch=True)) as handle:
        assert (await _metadata(handle))["issuer"] == ISSUER_MISMATCH_VALUE
        with pytest.raises(BaseException):
            async with initialized_session(handle, sdk_provider(handle, MemoryStorage())):
                pass
        assert handle.state.authorize_requests == 0
        assert handle.state.registrations == 0


# --- token lifetime ---------------------------------------------------------


@pytest.mark.anyio
async def test_a_locally_expired_access_token_is_refreshed_and_the_rotated_refresh_token_is_stored():
    """Short-lived tokens: `expires_in` is 1 s, so the client's OWN clock says expired."""
    async with serve_oauth_fixture(FixtureConfig(access_token_ttl_s=1)) as handle:
        storage = MemoryStorage()
        async with initialized_session(handle, sdk_provider(handle, storage)) as session:
            first = storage.tokens
            assert first is not None and first.refresh_token is not None
            assert first.expires_in == 1

            await anyio.sleep(1.5)
            assert await tool_names(session) == ["get_item", "list_items"]

            assert handle.state.token_grants == ["authorization_code", "refresh_token"]
            second = storage.tokens
            assert second is not None
            assert second.access_token != first.access_token
            assert second.refresh_token != first.refresh_token  # rotated, and persisted
            assert handle.state.authorize_requests == 1  # no second login


@pytest.mark.anyio
async def test_a_server_side_401_makes_the_sdk_log_in_again_instead_of_refreshing():
    """CHARACTERIZATION of the SDK, and a constraint on F-44: the SDK refreshes only when
    its own stored expiry says the token is stale. A token the SERVER rejects early
    (revoked, clock skew) sends it through the full authorization flow, never a refresh.
    F-44's provider has no browser handler, so there that path must end in exit 4 with the
    `drifter auth login` instruction -- not a silent refresh the SDK would never attempt."""
    async with serve_oauth_fixture() as handle:
        storage = MemoryStorage()
        async with initialized_session(handle, sdk_provider(handle, storage)) as session:
            handle.state.expire_access_tokens()
            assert await tool_names(session) == ["get_item", "list_items"]
            assert handle.state.token_grants == ["authorization_code", "authorization_code"]
            assert handle.state.authorize_requests == 2


@pytest.mark.anyio
async def test_refresh_disabled_mode_issues_no_refresh_token_so_expiry_forces_a_new_login():
    async with serve_oauth_fixture(FixtureConfig(refresh_tokens=False)) as handle:
        assert "refresh_token" not in (await _metadata(handle))["grant_types_supported"]
        storage = MemoryStorage()
        async with initialized_session(handle, sdk_provider(handle, storage)) as session:
            assert storage.tokens is not None and storage.tokens.refresh_token is None
            handle.state.expire_access_tokens()
            assert await tool_names(session)
            assert handle.state.authorize_requests == 2
            assert "refresh_token" not in handle.state.token_grants


# --- protected-resource behaviour Drifter's exit-4 paths depend on -----------


@pytest.mark.anyio
async def test_an_unauthenticated_request_gets_a_401_pointing_at_the_resource_metadata():
    async with serve_oauth_fixture() as handle:
        async with httpx2.AsyncClient() as client:
            response = await client.post(handle.mcp_url, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
            assert response.status_code == 401
            challenge = response.headers["www-authenticate"]
            assert f'resource_metadata="{handle.origin}/.well-known/oauth-protected-resource{MCP_PATH}"' in challenge
            prm = (await client.get(f"{handle.origin}/.well-known/oauth-protected-resource{MCP_PATH}")).json()
            assert prm["resource"] == handle.mcp_url
            assert prm["authorization_servers"] == [handle.issuer]


@pytest.mark.anyio
async def test_forced_401_and_403_carry_the_challenges_a_client_must_react_to():
    async with serve_oauth_fixture() as handle:
        handle.state.force_status(401)
        handle.state.force_status(403)
        async with httpx2.AsyncClient() as client:
            first = await client.post(handle.mcp_url, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
            second = await client.post(handle.mcp_url, json={"jsonrpc": "2.0", "id": 2, "method": "ping"})
            third = await client.post(handle.mcp_url, json={"jsonrpc": "2.0", "id": 3, "method": "ping"})
        assert first.status_code == 401 and 'error="invalid_token"' in first.headers["www-authenticate"]
        assert second.status_code == 403
        assert 'error="insufficient_scope"' in second.headers["www-authenticate"]
        assert 'scope="admin"' in second.headers["www-authenticate"]
        assert third.status_code == 401  # forced statuses are consumed; the real check resumes


@pytest.mark.anyio
async def test_a_token_issued_for_another_resource_is_refused():
    """RFC 8707: tokens are audience-bound. This is why Drifter can never forward an
    agent's token (docs/SPEC.md §5.2, design constraint 1)."""
    async with serve_oauth_fixture() as handle:
        from mcp.server.auth.provider import AccessToken

        handle.state.access_tokens["foreign"] = AccessToken(
            token="foreign",
            client_id="someone-else",
            scopes=["read"],
            expires_at=int(time.time()) + 600,
            resource="http://localhost:9999/other",
        )
        async with httpx2.AsyncClient() as client:
            response = await client.post(
                handle.mcp_url,
                headers={"Authorization": "Bearer foreign"},
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            )
        assert response.status_code == 401


# --- the canary contract the F-47 detector relies on --------------------------


@pytest.mark.anyio
async def test_every_secret_the_fixture_mints_starts_with_the_canary():
    async with serve_oauth_fixture(FixtureConfig(client_credentials=True)) as handle:
        storage = MemoryStorage()
        async with initialized_session(handle, sdk_provider(handle, storage)):
            pass
        state = handle.state
        minted = state.all_secrets()
        assert len(minted) >= 5  # static bearer, machine secret, DCR client secret, code, access, refresh
        assert all(secret.startswith(state.canary) for secret in minted), minted
        assert len(set(minted)) == len(minted)


@pytest.mark.anyio
async def test_each_fixture_instance_has_its_own_canary():
    async with serve_oauth_fixture() as one, serve_oauth_fixture() as two:
        assert one.state.canary != two.state.canary
        assert one.origin != two.origin


# --- CLAUDE.md async-shutdown rule: timed, not just passing ------------------


@pytest.mark.anyio
async def test_fixture_shutdown_is_prompt_after_a_real_session():
    """CLAUDE.md: this project has hit the async-shutdown-hang shape three times. A
    fixture that serves SSE streams is exactly where a fourth would hide, so teardown
    is timed, with a bound far below the fixture's own 5 s give-up."""
    async with serve_oauth_fixture() as handle:
        async with initialized_session(handle, sdk_provider(handle, MemoryStorage())) as session:
            assert await tool_names(session)
        started = time.monotonic()
    elapsed = time.monotonic() - started
    assert elapsed < 2.0, f"fixture teardown took {elapsed:.2f}s"


@pytest.mark.anyio
async def test_fixture_shutdown_is_prompt_even_if_a_client_is_still_connected():
    started = None
    with pytest.raises(BaseException):  # the still-open client is cut off mid-session; that is the point
        async with serve_oauth_fixture() as handle:
            async with initialized_session(handle, sdk_provider(handle, MemoryStorage())) as session:
                assert await tool_names(session)
                started = time.monotonic()
                raise RuntimeError("leave while connected")
    assert started is not None
    elapsed = time.monotonic() - started
    assert elapsed < 4.0, f"teardown with a live client took {elapsed:.2f}s"
