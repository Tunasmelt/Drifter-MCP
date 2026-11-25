"""F-41, connection half: `bearer_env` actually reaches the upstream server, and only as
specified (docs/FEATURES.md F-41, docs/SPEC.md §5.2).

Written before the implementation. Unit tests cover the header builder and the client
factory; the integration tests drive a real Streamable HTTP session against the OAuth
fixture's static-bearer mode (`tests/fixtures/oauth/`), whose logged requests show what the
server actually received -- the only evidence that "sends `Authorization: Bearer <value>` on
every upstream request and nothing else" is true.
"""

from __future__ import annotations

import httpx2
import pytest
from mcp import ClientSession

from canary_scan import assert_no_leaks, new_canary, scan_exception, scan_text
from mcp_drifter.auth.errors import AuthSetupError, UpstreamAuthRejected
from mcp_drifter.auth.upstream import bearer_headers
from mcp_drifter.cli.config import ConfigError, ServerConfig
from mcp_drifter.cli.upstream import connect_upstream, upstream_http_client
from oauth_server import FixtureConfig, serve_oauth_fixture
from sdk_client import leaf_exceptions, tool_names


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _bearer_server(url: str = "https://mcp.example.com/mcp", env: str = "CRM_TOKEN") -> ServerConfig:
    return ServerConfig.model_validate(
        {"name": "remote-crm", "url": url, "auth": {"type": "bearer_env", "token_env": env}}
    )


# --- the header builder ----------------------------------------------------------------------


def test_the_header_is_exactly_authorization_bearer_and_the_variables_value():
    headers = bearer_headers(_bearer_server().auth, "remote-crm", {"CRM_TOKEN": "abc123XYZ"})
    assert headers == {"Authorization": "Bearer abc123XYZ"}


def test_surrounding_whitespace_from_a_shell_substitution_is_stripped():
    # `export CRM_TOKEN=$(cat token.txt)` and `echo "$TOKEN" > f` leave a trailing newline.
    assert bearer_headers(_bearer_server().auth, "s", {"CRM_TOKEN": "  tok_123\n"}) == {"Authorization": "Bearer tok_123"}


def test_an_unset_variable_names_the_variable_and_the_server():
    with pytest.raises(AuthSetupError) as failure:
        bearer_headers(_bearer_server().auth, "remote-crm", {})
    message = str(failure.value)
    assert "CRM_TOKEN" in message and "remote-crm" in message and "not set" in message


@pytest.mark.parametrize("value", ["", "   ", "\n"])
def test_an_empty_variable_is_an_error_not_an_empty_bearer(value):
    with pytest.raises(AuthSetupError, match="empty"):
        bearer_headers(_bearer_server().auth, "s", {"CRM_TOKEN": value})


@pytest.mark.parametrize(
    "value",
    ["abc\r\nX-Injected: 1", "abc def", "abc\x00def", "tök", "abc\tdef"],
    ids=["header-injection", "interior-space", "nul", "non-ascii", "tab"],
)
def test_a_value_that_is_not_a_single_visible_ascii_token_is_refused_without_echoing_it(value):
    with pytest.raises(AuthSetupError) as failure:
        bearer_headers(_bearer_server().auth, "remote-crm", {"CRM_TOKEN": value})
    message = str(failure.value)
    assert "CRM_TOKEN" in message and "single" in message
    assert value.strip() not in message and "X-Injected" not in message


# --- the client factory -----------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_stdio_server_gets_no_http_client():
    server = ServerConfig.model_validate({"name": "local", "command": ["python", "server.py"]})
    async with upstream_http_client(server) as client:
        assert client is None


@pytest.mark.anyio
@pytest.mark.parametrize("auth", [None, {"type": "none"}])
async def test_a_url_server_without_auth_gets_no_http_client_so_behavior_is_unchanged(auth):
    spec = {"name": "open", "url": "https://mcp.example.com/mcp"}
    if auth:
        spec["auth"] = auth
    async with upstream_http_client(ServerConfig.model_validate(spec)) as client:
        assert client is None


@pytest.mark.anyio
async def test_the_bearer_client_adds_exactly_one_header_and_keeps_the_sdks_timeouts(monkeypatch):
    monkeypatch.setenv("CRM_TOKEN", "tok_123")
    async with upstream_http_client(_bearer_server()) as client:
        assert isinstance(client, httpx2.AsyncClient)
        baseline = set(httpx2.AsyncClient().headers.keys())
        assert set(client.headers.keys()) - baseline == {"authorization"}
        assert client.headers["authorization"] == "Bearer tok_123"
        # the same timeouts the SDK would have used for a client it built itself
        assert client.timeout.read == 300.0 and client.timeout.connect == 30.0
    assert client.is_closed  # closed when the context exits


@pytest.mark.anyio
async def test_a_missing_variable_is_a_config_error_raised_before_any_connection(monkeypatch):
    monkeypatch.delenv("CRM_TOKEN", raising=False)
    with pytest.raises(ConfigError, match="CRM_TOKEN"):
        async with upstream_http_client(_bearer_server()):
            pytest.fail("must not get as far as connecting")


@pytest.mark.anyio
@pytest.mark.parametrize("auth_type, extra", [("client_credentials", {"client_id": "c", "secret_env": "S"})])
async def test_auth_types_without_a_connection_yet_are_refused_loudly_never_run_unauthenticated(auth_type, extra):
    """F-45 is not built yet (F-43 and F-44 are: `oauth` uses a stored login). A configured-but-unsupported auth type must STOP, not
    quietly connect with no credentials, which is what `extra="allow"` did for every `auth:` key."""
    server = ServerConfig.model_validate(
        {"name": "remote-crm", "url": "https://mcp.example.com/mcp", "auth": {"type": auth_type, **extra}}
    )
    with pytest.raises(ConfigError) as failure:
        async with upstream_http_client(server):
            pytest.fail("must not connect")
    assert auth_type in str(failure.value) and "remote-crm" in str(failure.value)


# --- against the fixture's static-bearer mode ----------------------------------------------------


@pytest.mark.anyio
async def test_a_session_through_connect_upstream_sends_the_bearer_on_every_request_and_only_that(monkeypatch):
    async with serve_oauth_fixture(FixtureConfig(protection="static_bearer")) as handle:
        monkeypatch.setenv("CRM_TOKEN", handle.state.static_bearer)
        server = _bearer_server(handle.mcp_url)
        async with connect_upstream(server) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                assert await tool_names(session) == ["delete_item", "get_item", "list_items"]

        requests = handle.state.mcp_requests()
        assert len(requests) >= 3, requests  # initialize, initialized, tools/list at least
        assert {r.authorization for r in requests} == {f"Bearer {handle.state.static_bearer}"}
        assert all(r.status in (200, 202, 204, 405) for r in requests), [(r.method, r.status) for r in requests]


@pytest.mark.anyio
async def test_a_missing_variable_sends_nothing_to_the_server(monkeypatch):
    monkeypatch.delenv("CRM_TOKEN", raising=False)
    async with serve_oauth_fixture(FixtureConfig(protection="static_bearer")) as handle:
        with pytest.raises(ConfigError):
            async with connect_upstream(_bearer_server(handle.mcp_url)):
                pytest.fail("must not connect")
        assert handle.state.requests == []


@pytest.mark.anyio
async def test_a_rejected_token_surfaces_as_a_specific_401_naming_the_variable_not_the_token(monkeypatch):
    wrong = new_canary() + "-wrong-token"
    monkeypatch.setenv("CRM_TOKEN", wrong)
    async with serve_oauth_fixture(FixtureConfig(protection="static_bearer")) as handle:
        with pytest.raises(BaseException) as failure:
            async with connect_upstream(_bearer_server(handle.mcp_url)) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
        rejections = [e for e in leaf_exceptions(failure.value) if isinstance(e, UpstreamAuthRejected)]
        assert len(rejections) == 1, leaf_exceptions(failure.value)
        rejected = rejections[0]
        assert rejected.status == 401
        assert "CRM_TOKEN" in str(rejected) and "remote-crm" in str(rejected) and "401" in str(rejected)
        assert_no_leaks(scan_exception(failure.value, [wrong, handle.state.static_bearer]))
        assert_no_leaks(scan_text("message", str(rejected), [wrong, handle.state.static_bearer]))


@pytest.mark.anyio
async def test_a_403_carries_the_scope_the_server_asked_for(monkeypatch):
    async with serve_oauth_fixture(FixtureConfig(protection="static_bearer")) as handle:
        monkeypatch.setenv("CRM_TOKEN", handle.state.static_bearer)
        handle.state.force_status(403)
        with pytest.raises(BaseException) as failure:
            async with connect_upstream(_bearer_server(handle.mcp_url)) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
        rejected = [e for e in leaf_exceptions(failure.value) if isinstance(e, UpstreamAuthRejected)]
        assert len(rejected) == 1 and rejected[0].status == 403
        assert "insufficient_scope" in str(rejected[0]) and "admin" in str(rejected[0])


@pytest.mark.anyio
async def test_a_server_without_auth_configured_still_fails_the_way_it_always_did(monkeypatch):
    """No `auth` block means no response hook: an unauthenticated 401 is not turned into an
    UpstreamAuthRejected, so existing unauthenticated behavior is byte-for-byte unchanged."""
    async with serve_oauth_fixture(FixtureConfig(protection="static_bearer")) as handle:
        server = ServerConfig.model_validate({"name": "open", "url": handle.mcp_url})
        with pytest.raises(BaseException) as failure:
            async with connect_upstream(server) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
        assert not [e for e in leaf_exceptions(failure.value) if isinstance(e, UpstreamAuthRejected)]
