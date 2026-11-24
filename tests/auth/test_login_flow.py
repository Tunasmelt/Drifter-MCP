"""F-43: `drifter auth login`'s flow, against the OAuth fixture (docs/FEATURES.md F-43, docs/SPEC.md §5.2).

Written before the implementation. The listener is tested on its own (`test_login_callback.py`);
here the whole login runs against each of the fixture's modes, with a stub "browser" that does
what a browser does: open the authorization URL, follow the redirect to the loopback listener.

Client identity is tried in order: pre-registered `client_id`, then a client-ID metadata document
(when the server supports them), then dynamic registration; otherwise an actionable error. Before
anything is sent to the authorization endpoint the server's metadata must advertise PKCE S256.
A failed login must leave an existing login untouched.
"""

from __future__ import annotations

import time
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest

from canary_scan import assert_no_leaks, scan_text, scan_tree
from mcp_drifter.auth.errors import LoginRefused
from mcp_drifter.auth.login import LoginResult, run_login
from mcp_drifter.auth.storage import TokenStore
from mcp_drifter.cli.config import ServerConfig
from oauth_server import (
    CIMD_CLIENT_URL,
    PREREGISTERED_CLIENT_ID,
    FixtureConfig,
    FixtureProvider,
    serve_oauth_fixture,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _server(handle, **auth) -> ServerConfig:
    return ServerConfig.model_validate({"name": "remote-crm", "url": handle.mcp_url, "auth": {"type": "oauth", **auth}})


def _store(tmp_path, handle) -> TokenStore:
    return TokenStore("remote-crm", handle.mcp_url, base_dir=tmp_path / "config", backend="file")


class Browser:
    """What a person's browser does: GET the authorization URL, follow the redirect it answers
    with (which lands on Drifter's loopback listener). `visits` lets a test see whether it was used."""

    def __init__(self, behavior: str = "approve") -> None:
        self.behavior = behavior
        self.visits: list[str] = []

    async def __call__(self, url: str) -> None:
        self.visits.append(url)
        if self.behavior == "ignore":
            return
        query = parse_qs(urlparse(url).query)
        redirect_uri, state = query["redirect_uri"][0], query["state"][0]
        async with httpx2.AsyncClient(follow_redirects=False) as client:
            if self.behavior == "forge-first":
                forged = await client.get(redirect_uri, params={"code": "FORGED", "state": "not-the-state"})
                assert forged.status_code == 400
            if self.behavior == "deny":
                await client.get(redirect_uri, params={"error": "access_denied", "state": state})
                return
            response = await client.get(url)
            await client.get(response.headers["location"])


async def _login(handle, tmp_path, browser=None, *, timeout=20, announce=None, store=None, **kwargs):
    store = store or _store(tmp_path, handle)
    lines: list[str] = []
    result = await run_login(
        kwargs.pop("server", None) or _server(handle, **kwargs.pop("auth", {})),
        store,
        client_metadata_url=kwargs.pop("client_metadata_url", None),
        open_browser=browser or Browser(),
        announce=announce or lines.append,
        timeout=timeout,
    )
    return result, store, lines


# --- the identity order ---------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_dynamic_registration_logs_in_and_stores_the_tokens_and_the_client(tmp_path):
    async with serve_oauth_fixture(FixtureConfig(registration="dcr")) as handle:
        result, store, _ = await _login(handle, tmp_path)
        assert isinstance(result, LoginResult) and result.identity == "dcr"
        assert (handle.state.registrations, handle.state.authorize_requests) == (1, 1)
        fresh = _store(tmp_path, handle)
        tokens = await fresh.get_tokens()
        assert tokens is not None and tokens.access_token.startswith(handle.state.canary) and tokens.refresh_token
        assert (await fresh.get_client_info()) is not None
        assert result.scope == tokens.scope == "read"


@pytest.mark.anyio
async def test_a_preregistered_client_id_is_used_and_nothing_is_registered(tmp_path):
    async with serve_oauth_fixture(FixtureConfig(registration="preregistered")) as handle:
        result, store, _ = await _login(handle, tmp_path, auth={"client_id": PREREGISTERED_CLIENT_ID})
        assert result.identity == "preregistered"
        assert handle.state.registrations == 0 and handle.state.requests_to("/register") == []
        assert (await store.get_client_info()).client_id == PREREGISTERED_CLIENT_ID


@pytest.mark.anyio
async def test_a_client_id_metadata_document_is_used_when_the_server_supports_it(tmp_path):
    async with serve_oauth_fixture(FixtureConfig(registration="cimd")) as handle:
        result, store, _ = await _login(handle, tmp_path, client_metadata_url=CIMD_CLIENT_URL)
        assert result.identity == "cimd" and handle.state.registrations == 0
        assert (await store.get_client_info()).client_id == CIMD_CLIENT_URL


@pytest.mark.anyio
async def test_a_preregistered_id_wins_over_a_metadata_document_even_when_both_are_possible(tmp_path):
    async with serve_oauth_fixture(FixtureConfig(registration="cimd")) as handle:
        handle.state.clients[PREREGISTERED_CLIENT_ID] = FixtureProvider(handle.state)._wrap(_seed_client())
        result, _store_, _ = await _login(
            handle, tmp_path, auth={"client_id": PREREGISTERED_CLIENT_ID}, client_metadata_url=CIMD_CLIENT_URL
        )
        assert result.identity == "preregistered"


def _seed_client():
    from mcp.shared.auth import OAuthClientInformationFull
    from pydantic import AnyUrl

    return OAuthClientInformationFull(
        client_id=PREREGISTERED_CLIENT_ID,
        redirect_uris=[AnyUrl("http://127.0.0.1/callback")],
        token_endpoint_auth_method="none",
        grant_types=["authorization_code", "refresh_token"],
        scope="read admin",
    )


@pytest.mark.anyio
@pytest.mark.parametrize("registration, url", [("preregistered", None), ("cimd", None)])
async def test_a_server_that_offers_no_way_to_register_gets_an_actionable_error_naming_the_options(tmp_path, registration, url):
    """No `client_id`, no registration endpoint, and (here) no metadata-document URL to offer."""
    async with serve_oauth_fixture(FixtureConfig(registration=registration)) as handle:
        browser = Browser()
        with pytest.raises(LoginRefused) as failure:
            await _login(handle, tmp_path, browser, client_metadata_url=url)
        message = str(failure.value)
        assert "client_id" in message and "bearer_env" in message and "remote-crm" in message, message
        assert handle.state.authorize_requests == 0 and browser.visits == []


# --- refusing servers it should not trust --------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_server_that_does_not_advertise_s256_is_refused_before_the_authorization_endpoint_is_touched(tmp_path):
    """The SDK itself sends S256 to such a server (characterized in test_oauth_fixture.py), so this
    refusal is Drifter's own: PKCE support has to be verified, not assumed."""
    async with serve_oauth_fixture(FixtureConfig(advertise_s256=False)) as handle:
        browser = Browser()
        with pytest.raises(LoginRefused) as failure:
            await _login(handle, tmp_path, browser)
        assert "S256" in str(failure.value) and "remote-crm" in str(failure.value)
        assert handle.state.authorize_requests == 0 and browser.visits == []  # never redirected anyone
        assert await _store(tmp_path, handle).get_tokens() is None


@pytest.mark.anyio
async def test_a_server_whose_metadata_names_a_different_issuer_is_refused(tmp_path):
    async with serve_oauth_fixture(FixtureConfig(issuer_mismatch=True)) as handle:
        browser = Browser()
        with pytest.raises(LoginRefused, match="issuer"):
            await _login(handle, tmp_path, browser)
        assert handle.state.authorize_requests == 0 and browser.visits == []


# --- the redirect ------------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_forged_callback_does_not_stop_the_real_login(tmp_path):
    async with serve_oauth_fixture() as handle:
        result, store, _ = await _login(handle, tmp_path, Browser("forge-first"))
        assert (await store.get_tokens()) is not None and result.identity == "dcr"


@pytest.mark.anyio
async def test_a_login_the_user_denies_fails_and_stores_nothing(tmp_path):
    async with serve_oauth_fixture() as handle:
        with pytest.raises(LoginRefused, match="access_denied"):
            await _login(handle, tmp_path, Browser("deny"))
        assert await _store(tmp_path, handle).get_tokens() is None


@pytest.mark.anyio
async def test_a_login_nobody_completes_times_out_and_leaves_an_existing_login_untouched(tmp_path):
    async with serve_oauth_fixture() as handle:
        _, store, _ = await _login(handle, tmp_path)
        before = await store.get_tokens()
        started = time.monotonic()
        with pytest.raises(LoginRefused, match="timed out"):
            await _login(handle, tmp_path, Browser("ignore"), timeout=0.6, store=_store(tmp_path, handle))
        assert time.monotonic() - started < 10
        assert await _store(tmp_path, handle).get_tokens() == before  # the working login survived the failed one


# --- scopes and re-login -------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_configured_scopes_are_what_gets_requested_even_though_the_sdk_would_choose_its_own(tmp_path):
    """The SDK takes scope from the server's challenge and metadata and ignores the client's
    (characterized in test_oauth_fixture.py). The server advertises only `read`; the config asks for
    more, and for dynamic registration that has to hold from the registration onwards."""
    async with serve_oauth_fixture(FixtureConfig(registration="dcr", required_scopes=("read",))) as handle:
        result, store, _ = await _login(handle, tmp_path, auth={"scopes": ["read", "admin"]})
        assert sorted(result.scope.split()) == ["admin", "read"]
        assert sorted((await store.get_tokens()).scope.split()) == ["admin", "read"]
        registered = next(iter(handle.state.clients.values()))
        assert sorted((registered.scope or "").split()) == ["admin", "read"]


@pytest.mark.anyio
async def test_without_configured_scopes_the_servers_own_choice_stands(tmp_path):
    async with serve_oauth_fixture(FixtureConfig(required_scopes=("read",))) as handle:
        result, _store_, _ = await _login(handle, tmp_path)
        assert result.scope == "read"


@pytest.mark.anyio
async def test_logging_in_again_replaces_the_login_and_registers_afresh(tmp_path):
    """The loopback port changes every time, and a dynamically registered redirect URI includes it."""
    async with serve_oauth_fixture() as handle:
        _, store, _ = await _login(handle, tmp_path)
        first = await store.get_tokens()
        _, store, _ = await _login(handle, tmp_path, store=_store(tmp_path, handle))
        second = await store.get_tokens()
        assert first.access_token != second.access_token
        assert handle.state.registrations == 2 and handle.state.authorize_requests == 2


@pytest.mark.anyio
async def test_the_stored_expiry_is_accurate_to_when_the_token_was_issued(tmp_path):
    async with serve_oauth_fixture(FixtureConfig(access_token_ttl_s=1800)) as handle:
        before = time.time()
        result, store, _ = await _login(handle, tmp_path)
        after = time.time()
        expiry = await store.get_expiry()
        assert expiry is not None and before + 1800 - 1 <= expiry <= after + 1800 + 1
        assert result.expires_at == expiry


# --- what it prints and where tokens go -------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_it_announces_the_url_but_never_a_token_and_tokens_land_only_in_the_config_dir(tmp_path):
    async with serve_oauth_fixture() as handle:
        project = tmp_path / "project"
        (project / ".drifter").mkdir(parents=True)
        _, store, lines = await _login(handle, tmp_path)
        secrets = handle.state.all_secrets()
        assert any("authorize" in line for line in lines), lines  # the URL was announced for a person to open
        assert_no_leaks(
            [*scan_text("announcements", "\n".join(lines), secrets), *scan_tree(project, secrets)]
        )
        assert scan_tree(tmp_path / "config", secrets), "the tokens belong in the user config dir"


@pytest.mark.anyio
async def test_stored_tokens_use_the_time_they_were_issued_not_the_time_they_were_saved(tmp_path):
    from mcp.shared.auth import OAuthToken

    store = TokenStore("remote-crm", "https://mcp.example.com/mcp", base_dir=tmp_path / "config", backend="file")
    await store.set_tokens(OAuthToken(access_token="a", expires_in=100), issued_at=1_000_000.0)
    assert await store.get_expiry() == 1_000_100.0


# --- found reviewing P3 before opening the PR ---------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    "override",
    [
        {"authorization_endpoint_override": "http://as.example.invalid/authorize"},
        {"token_endpoint_override": "http://as.example.invalid/token"},
    ],
    ids=["authorization-endpoint", "token-endpoint"],
)
async def test_an_authorization_server_endpoint_that_is_cleartext_on_a_remote_host_is_refused(tmp_path, override):
    """Only the resource URL was checked. The authorization server's own endpoints come from metadata the
    server supplies, and an `http://` token endpoint would carry the code and the tokens in the clear."""
    async with serve_oauth_fixture(FixtureConfig(**override)) as handle:
        browser = Browser()
        with pytest.raises(LoginRefused) as failure:
            await _login(handle, tmp_path, browser)
        message = str(failure.value)
        assert "cleartext" in message and "https" in message and "remote-crm" in message, message
        assert handle.state.authorize_requests == 0 and browser.visits == []
        assert await _store(tmp_path, handle).get_tokens() is None


@pytest.mark.anyio
async def test_a_server_that_never_asks_for_authorization_says_so_instead_of_a_vague_failure(tmp_path):
    async with serve_oauth_fixture(FixtureConfig(protection="open")) as handle:
        with pytest.raises(LoginRefused) as failure:
            await _login(handle, tmp_path)
        message = str(failure.value)
        assert "without asking" in message and "remote-crm" in message and "auth" in message, message
        assert await _store(tmp_path, handle).get_tokens() is None
