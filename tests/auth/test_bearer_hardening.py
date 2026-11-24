"""Findings from the P2 audit (F-41, F-42): each test here failed before its fix.

  1. A bearer token was accepted for a plain `http://` URL on a remote host, i.e. sent in
     cleartext. Refused at config load; loopback stays allowed (the fixture, local development).
  2. Text from the server's `WWW-Authenticate` header was printed to the terminal unbounded and,
     for `error=`, unescaped. Sanitized and capped.
  3. A malformed port in a server URL crashed token storage with a raw ValueError. Refused at
     config load, and storage reports a StorageError.
  4. Any 403 was fatal to the whole `observe` session. An ordinary 403 ("not allowed to do THIS")
     is an answer the agent should see, not a reason to end the recording; only a 401, or a 403
     that says `insufficient_scope` (the credential's grant is wrong), is a credential problem.
  5. `doctor`'s new branches for a rejected token, a missing variable and an unsupported auth
     type had no tests at all.
  6. Nothing showed the bearer cannot follow a redirect to another origin.
"""

from __future__ import annotations

import sys
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.exceptions import MCPError

from canary_scan import new_canary
from mcp_drifter.auth.errors import UpstreamAuthRejected
from mcp_drifter.auth.storage import StorageError, TokenStore
from mcp_drifter.auth.upstream import rejection_hook
from mcp_drifter.cli.config import ConfigError, ServerConfig, load_config
from mcp_drifter.cli.doctor import _check_server
from mcp_drifter.cli.upstream import connect_upstream, upstream_http_client
from oauth_server import FixtureConfig, serve_oauth_fixture
from sdk_client import tool_names


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _yaml(tmp_path: Path, url: str, auth: str = "type: bearer_env\n      token_env: CRM_TOKEN") -> Path:
    path = tmp_path / "drifter.yaml"
    path.write_text(
        f'version: 1\nservers:\n  - name: remote-crm\n    url: "{url}"\n    auth:\n      {auth}\n', encoding="utf-8"
    )
    return path


# --- 1. credentials never go over cleartext HTTP to a remote host ---------------------------------


@pytest.mark.parametrize(
    "url",
    ["http://mcp.example.com/mcp", "http://10.0.0.5/mcp", "http://localhost.evil.com/mcp", "http://127.0.0.1.evil.com/mcp",
     "http://localhost@evil.com/mcp", "http://example.com:80/mcp"],
)
def test_a_credential_is_refused_over_plain_http_to_a_remote_host(tmp_path, url):
    with pytest.raises(ConfigError) as failure:
        load_config(_yaml(tmp_path, url))
    message = str(failure.value)
    assert "remote-crm" in message and "https" in message and "cleartext" in message, message


@pytest.mark.parametrize("url", ["http://127.0.0.1:8000/mcp", "http://localhost/mcp", "http://[::1]:9/mcp", "http://127.0.0.2/mcp"])
def test_loopback_http_is_fine_because_nothing_leaves_the_machine(tmp_path, url):
    assert load_config(_yaml(tmp_path, url)).servers[0].auth is not None


@pytest.mark.parametrize("auth", ["type: oauth", "type: client_credentials\n      client_id: c\n      secret_env: S"])
def test_the_rule_covers_every_auth_type_not_only_bearer(tmp_path, auth):
    with pytest.raises(ConfigError, match="cleartext"):
        load_config(_yaml(tmp_path, "http://mcp.example.com/mcp", auth))


def test_plain_http_without_credentials_is_unchanged(tmp_path):
    path = tmp_path / "drifter.yaml"
    path.write_text('version: 1\nservers:\n  - name: open\n    url: "http://mcp.example.com/mcp"\n', encoding="utf-8")
    assert load_config(path).servers[0].auth is None
    explicit_none = _yaml(tmp_path, "http://mcp.example.com/mcp", "type: none")
    assert load_config(explicit_none).servers[0].auth.type == "none"


# --- 2. server-controlled text is bounded and clean ---------------------------------------------------


def _challenge_failure(www_authenticate: str) -> UpstreamAuthRejected:
    class _Response:
        status_code = 401
        headers = {"www-authenticate": www_authenticate}

    with pytest.raises(UpstreamAuthRejected) as failure:
        anyio.run(rejection_hook("remote-crm", "environment variable CRM_TOKEN"), _Response())
    return failure.value


def test_control_characters_and_escape_sequences_from_the_server_never_reach_the_message():
    rejected = _challenge_failure('Bearer error="inva\x1b[2Jlid\x07_token", scope="re\x1b[31mad"')
    assert not any(ord(c) < 0x20 or ord(c) == 0x7F for c in str(rejected)), repr(str(rejected))


def test_the_server_cannot_make_the_message_arbitrarily_long():
    rejected = _challenge_failure(f'Bearer error="{"A" * 5000}", scope="{"B" * 5000}"')
    assert len(str(rejected)) < 500


def test_ordinary_error_and_scope_values_survive_sanitizing():
    rejected = _challenge_failure('Bearer error="insufficient_scope", scope="crm.read crm:write https://x.example/a-b"')
    assert rejected.error == "insufficient_scope" and rejected.scope == "crm.read crm:write https://x.example/a-b"


# --- 3. a bad port is a clean error --------------------------------------------------------------------


@pytest.mark.parametrize("url", ["https://host:abc/mcp", "https://host:99999999/mcp", "https://host:-1/mcp"])
def test_a_malformed_port_is_a_config_error_naming_the_server(tmp_path, url):
    path = tmp_path / "drifter.yaml"
    path.write_text(f'version: 1\nservers:\n  - name: odd\n    url: "{url}"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="odd"):
        load_config(path)


def test_token_storage_reports_a_bad_url_as_a_storage_error_not_a_raw_value_error(tmp_path):
    with pytest.raises(StorageError, match="URL"):
        TokenStore("odd", "https://host:abc/mcp", base_dir=tmp_path / "config", backend="file")


# --- 4. which responses are credential failures ---------------------------------------------------------


class _Response:
    def __init__(self, status: int, challenge: str = ""):
        self.status_code = status
        self.headers = {"www-authenticate": challenge} if challenge else {}


@pytest.mark.anyio
@pytest.mark.parametrize(
    "status, challenge",
    [(401, ""), (401, 'Bearer error="invalid_token"'), (403, 'Bearer error="insufficient_scope", scope="admin"')],
)
async def test_a_401_or_an_insufficient_scope_403_is_a_credential_rejection(status, challenge):
    with pytest.raises(UpstreamAuthRejected):
        await rejection_hook("s", "environment variable T")(_Response(status, challenge))


@pytest.mark.anyio
@pytest.mark.parametrize("challenge", ["", "Bearer", 'Bearer error="access_denied"', 'Bearer realm="crm"'])
async def test_an_ordinary_403_is_left_for_the_agent_to_see(challenge):
    seen = []
    await rejection_hook("s", "environment variable T", on_rejected=seen.append)(_Response(403, challenge))
    assert seen == []  # not raised, and the exit callback was never called


def test_a_rejected_name_says_how_to_fix_a_false_alarm(tmp_path):
    """A realistic mixed-case name ('Prod_Crm_Api_Token_v2_2024_eu') trips F-04's entropy rule. Failing
    is the safe side; the message must say how to move on."""
    with pytest.raises(ConfigError, match="UPPER_SNAKE_CASE"):
        load_config(_yaml(tmp_path, "https://mcp.example.com/mcp", "type: bearer_env\n      token_env: Prod_Crm_Api_Token_v2_2024_eu"))


# --- 5. doctor ----------------------------------------------------------------------------------------------


def _server(url: str, **auth) -> ServerConfig:
    return ServerConfig.model_validate({"name": "remote-crm", "url": url, "auth": {"type": "bearer_env", "token_env": "CRM_TOKEN", **auth}})


@pytest.mark.anyio
async def test_doctor_passes_with_a_good_token_and_names_the_problem_otherwise(monkeypatch):
    async with serve_oauth_fixture(FixtureConfig(protection="static_bearer")) as handle:
        server = _server(handle.mcp_url)

        monkeypatch.setenv("CRM_TOKEN", handle.state.static_bearer)
        good = await _check_server(server, timeout_seconds=15)
        assert good.ok, good

        wrong = new_canary() + "-wrong-token"
        monkeypatch.setenv("CRM_TOKEN", wrong)
        bad = await _check_server(server, timeout_seconds=15)
        assert not bad.ok and "CRM_TOKEN" in bad.detail and "401" in bad.detail, bad
        assert wrong not in bad.detail and handle.state.static_bearer not in bad.detail

        monkeypatch.delenv("CRM_TOKEN")
        missing = await _check_server(server, timeout_seconds=15)
        assert not missing.ok and "CRM_TOKEN" in missing.detail and "not set" in missing.detail, missing


@pytest.mark.anyio
async def test_doctor_reports_an_auth_type_that_cannot_log_in_yet(monkeypatch):
    server = ServerConfig.model_validate({"name": "remote-crm", "url": "https://mcp.example.com/mcp", "auth": {"type": "oauth"}})
    check = await _check_server(server, timeout_seconds=5)
    assert not check.ok and "oauth" in check.detail, check


# --- 4 (end to end). a plain 403 does not end the session ---------------------------------------------------------


@pytest.mark.anyio
async def test_an_ordinary_403_on_one_call_leaves_the_observe_session_running(tmp_path):
    async with serve_oauth_fixture(FixtureConfig(protection="static_bearer")) as handle:
        config = tmp_path / "drifter.yaml"
        config.write_text(
            f'version: 1\nservers:\n  - name: remote-crm\n    url: "{handle.mcp_url}"\n    auth:\n      type: bearer_env\n      token_env: CRM_TOKEN\n',
            encoding="utf-8",
        )
        import os

        env = {**os.environ, "CRM_TOKEN": handle.state.static_bearer}
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "mcp_drifter.cli", "observe", "--config", str(config)], env=env, cwd=str(tmp_path)
        )
        with anyio.fail_after(60):
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    assert await tool_names(session) == ["delete_item", "get_item", "list_items"]

                    handle.state.force_status(403, challenge="Bearer")  # "you may not do THIS"
                    with pytest.raises(MCPError):
                        await session.call_tool("delete_item", {"id": "1"})

                    result = await session.call_tool("get_item", {"id": "1"})  # the session is still alive
                    assert not result.is_error
        statuses = [r.status for r in handle.state.mcp_requests()]
        assert 403 in statuses and statuses[-1] != 401


# --- 6. the bearer cannot follow a redirect to another origin ----------------------------------------------------------


@pytest.mark.anyio
async def test_a_redirect_to_another_origin_is_not_followed_so_the_token_goes_nowhere_else(monkeypatch):
    async with serve_oauth_fixture(FixtureConfig(protection="static_bearer")) as elsewhere:
        async with serve_oauth_fixture(FixtureConfig(protection="static_bearer")) as origin:
            origin.state.redirect_mcp_to = elsewhere.mcp_url
            monkeypatch.setenv("CRM_TOKEN", origin.state.static_bearer)
            server = _server(origin.mcp_url)

            async with upstream_http_client(server) as client:
                assert client is not None and client.follow_redirects is False

            with pytest.raises(BaseException) as failure:
                with anyio.fail_after(15):
                    async with connect_upstream(server) as (read, write):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
            assert failure.value is not None
            assert [r.status for r in origin.state.mcp_requests()][:1] == [307]  # the redirect was seen...
            assert elsewhere.state.requests == []  # ...and nothing, token or otherwise, reached the other origin
