"""F-45: non-interactive service credentials, `type: client_credentials` (docs/FEATURES.md F-45, docs/SPEC.md §5.2).

Written before the implementation. For CI, where nobody can click "Allow": the client authenticates itself
with a secret (`secret_env`) or a signed assertion (`private_key_env`) and is given an access token. There is
no browser, no callback, no stored login and nothing written to disk.

What these tests pin, beyond "it connects":
  - the credentials are only ever sent to the authorization server the config NAMES (`issuer`). Without that,
    whichever server the MCP server's metadata points at would receive the client's secret;
  - a 401 on a request that carried the freshly issued token is reported as such, not retried into a loop;
  - every failure names the environment VARIABLE and the server, never a secret, a key or an assertion;
  - a pasted secret or key in drifter.yaml fails at load without being echoed.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession

from canary_scan import assert_no_leaks, scan_text, scan_tree
from mcp_drifter.auth.errors import UpstreamAuthRejected
from mcp_drifter.cli.config import ConfigError, ServerConfig, load_config
from mcp_drifter.cli.upstream import connect_upstream
from oauth_server import MACHINE_CLIENT_ID, FixtureConfig, serve_oauth_fixture
from test_observe_bearer_e2e import INITIALIZE, _observe_until_exit

SECRET_VAR = "CRM_CLIENT_SECRET"
KEY_VAR = "CRM_CLIENT_KEY"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _server(handle, **auth) -> ServerConfig:
    body = {"type": "client_credentials", "client_id": MACHINE_CLIENT_ID, "issuer": handle.issuer, "secret_env": SECRET_VAR}
    body.update(auth)
    body = {k: v for k, v in body.items() if v is not None}
    return ServerConfig.model_validate({"name": "ci-crm", "url": handle.mcp_url, "auth": body})


def _jwt_server(handle, **auth) -> ServerConfig:
    return _server(handle, secret_env=None, private_key_env=KEY_VAR, **auth)


async def _list_tools(server: ServerConfig):
    async with connect_upstream(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.list_tools()


def _leaves(exc: BaseException):
    pending = [exc]
    while pending:
        item = pending.pop()
        if getattr(item, "exceptions", None):
            pending.extend(item.exceptions)
        else:
            yield item


async def _failure(server: ServerConfig) -> BaseException:
    """The error a connection ends in, however deep the transport's exception group buries it."""
    with anyio.fail_after(20):
        try:
            await _list_tools(server)
        except BaseException as exc:  # noqa: BLE001 - inspected below
            leaves = list(_leaves(exc))
            for leaf in leaves:
                if isinstance(leaf, (UpstreamAuthRejected, ConfigError)):
                    return leaf
            raise AssertionError(f"unexpected failure: {leaves!r}") from exc
    raise AssertionError("the connection succeeded but was expected to fail")


# --- config ------------------------------------------------------------------------------------------------


URL_SERVER = 'version: 1\nservers:\n  - name: ci\n    url: "https://mcp.example.com/mcp"\n    auth:\n'


def _load(tmp_path, body: str):
    path = tmp_path / "drifter.yaml"
    path.write_text(URL_SERVER + "".join(f"      {line}\n" for line in body.strip().splitlines()), encoding="utf-8")
    return load_config(path)


GOOD = "type: client_credentials\nclient_id: c\nissuer: https://auth.example.com/"


def test_a_client_secret_block_and_a_private_key_block_both_load(tmp_path):
    secret = _load(tmp_path, GOOD + "\nsecret_env: CI_SECRET").servers[0].auth
    assert (secret.secret_env, secret.private_key_env, secret.issuer) == ("CI_SECRET", None, "https://auth.example.com/")
    key = _load(tmp_path, GOOD + "\nprivate_key_env: CI_KEY\nsigning_algorithm: ES256").servers[0].auth
    assert (key.secret_env, key.private_key_env, key.signing_algorithm) == (None, "CI_KEY", "ES256")
    assert _load(tmp_path, GOOD + "\nprivate_key_env: CI_KEY").servers[0].auth.signing_algorithm == "RS256"


@pytest.mark.parametrize(
    "body, fragment",
    [
        ("type: client_credentials\nclient_id: c\nsecret_env: S", "issuer"),  # credentials go only where the config says
        (GOOD, "secret_env"),  # neither a secret nor a key
        (GOOD + "\nsecret_env: S\nprivate_key_env: K", "exactly one"),
        (GOOD + "\nsecret_env: S\nsigning_algorithm: ES256", "signing_algorithm"),  # only meaningful with a key
        (GOOD + "\nprivate_key_env: K\nsigning_algorithm: none", "signing_algorithm"),  # `none` is not a signature
        ("type: client_credentials\nclient_id: c\nissuer: http://auth.example.com/\nsecret_env: S", "issuer"),  # cleartext, remote
        ("type: client_credentials\nclient_id: c\nissuer: https://user:pw@auth.example.com/\nsecret_env: S", "issuer"),
        ("type: client_credentials\nclient_id: c\nissuer: auth.example.com\nsecret_env: S", "issuer"),
        ("type: oauth\nissuer: https://auth.example.com/", "issuer"),  # does not belong to the type
        ("type: oauth\nprivate_key_env: K", "private_key_env"),
        ("type: bearer_env\ntoken_env: T\nsigning_algorithm: RS256", "signing_algorithm"),
    ],
)
def test_service_credential_blocks_that_cannot_work_are_refused_at_load(tmp_path, body, fragment):
    with pytest.raises(ConfigError) as excinfo:
        _load(tmp_path, body)
    assert fragment in str(excinfo.value), str(excinfo.value)


def test_a_loopback_http_issuer_is_allowed_for_local_servers(tmp_path):
    body = "type: client_credentials\nclient_id: c\nissuer: http://127.0.0.1:8080/\nsecret_env: S"
    assert _load(tmp_path, body).servers[0].auth.issuer == "http://127.0.0.1:8080/"


def test_a_pasted_private_key_is_refused_without_being_echoed(tmp_path):
    pem = "-----BEGIN PRIVATE KEY-----MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7-----END PRIVATE KEY-----"
    with pytest.raises(ConfigError) as excinfo:
        _load(tmp_path, GOOD + f'\nprivate_key_env: "{pem}"')
    message = str(excinfo.value)
    assert "private_key_env" in message and "MIIEvQIBADANBgkq" not in message, message


# --- connecting --------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_client_secret_connects_with_no_browser_no_registration_and_no_files(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "cfg"))
    async with serve_oauth_fixture(FixtureConfig(client_credentials=True)) as handle:
        monkeypatch.setenv(SECRET_VAR, handle.state.machine_client_secret)
        tools = await _list_tools(_server(handle))
        assert "get_item" in [t.name for t in tools.tools]

        assert handle.state.token_grants == ["client_credentials"]
        assert handle.state.authorize_requests == 0 and handle.state.registrations == 0
        (token,) = [t for t in handle.state.access_tokens]
        assert {r.authorization for r in handle.state.mcp_requests() if r.authorization} == {f"Bearer {token}"}
        assert not (tmp_path / "cfg").exists(), "nothing is stored: a CI run keeps no login"


@pytest.mark.anyio
async def test_the_secret_goes_to_the_token_endpoint_in_a_basic_header_only(tmp_path, monkeypatch):
    async with serve_oauth_fixture(FixtureConfig(client_credentials=True)) as handle:
        monkeypatch.setenv(SECRET_VAR, handle.state.machine_client_secret)
        await _list_tools(_server(handle))
        assert [r.authorization for r in handle.state.requests_to("/token")] == [
            "Basic " + __import__("base64").b64encode(f"{MACHINE_CLIENT_ID}:{handle.state.machine_client_secret}".encode()).decode()
        ]
        # and never to the MCP server
        assert all(
            handle.state.machine_client_secret not in (r.authorization or "") for r in handle.state.mcp_requests()
        )


@pytest.mark.anyio
async def test_a_signed_assertion_connects_and_names_the_issuer_as_its_audience(tmp_path, monkeypatch):
    async with serve_oauth_fixture(FixtureConfig(client_credentials=True)) as handle:
        monkeypatch.setenv(KEY_VAR, handle.state.machine_private_key_pem)
        await _list_tools(_jwt_server(handle))
        (claims,) = handle.state.assertion_claims
        assert (claims["iss"], claims["sub"], claims["aud"]) == (MACHINE_CLIENT_ID, MACHINE_CLIENT_ID, handle.issuer)
        assert 0 < claims["exp"] - claims["iat"] <= 300, "an assertion is short-lived"
        assert handle.state.token_grants == ["client_credentials"]
        assert handle.state.authorize_requests == 0 and handle.state.registrations == 0


@pytest.mark.anyio
async def test_a_private_key_exported_with_literal_backslash_n_still_works(tmp_path, monkeypatch):
    """CI secret stores often flatten a PEM onto one line, newlines written as the two characters `\\n`."""
    async with serve_oauth_fixture(FixtureConfig(client_credentials=True)) as handle:
        monkeypatch.setenv(KEY_VAR, handle.state.machine_private_key_pem.strip().replace("\n", "\\n"))
        await _list_tools(_jwt_server(handle))
        assert len(handle.state.assertion_claims) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("flavour", ["secret", "key"])
async def test_configured_scopes_that_differ_from_what_the_server_would_choose_are_still_the_ones_requested(monkeypatch, flavour):
    """Found by F-46's exact-value test: the SDK REPLACES the configured scope with whatever the 401 challenge or the
    metadata advertises (or drops it), so `auth.scopes` was silently ignored whenever it differed from the server's
    default. `["read"]` hid it because it is the fixture's default."""
    async with serve_oauth_fixture(FixtureConfig(client_credentials=True)) as handle:
        if flavour == "secret":
            monkeypatch.setenv(SECRET_VAR, handle.state.machine_client_secret)
            server = _server(handle, scopes=["read", "admin"])
        else:
            monkeypatch.setenv(KEY_VAR, handle.state.machine_private_key_pem)
            server = _jwt_server(handle, scopes=["read", "admin"])
        await _list_tools(server)
        (token,) = handle.state.access_tokens.values()
        assert sorted(token.scopes) == ["admin", "read"]


@pytest.mark.anyio
async def test_the_requested_scopes_are_the_configured_ones(tmp_path, monkeypatch):
    async with serve_oauth_fixture(FixtureConfig(client_credentials=True)) as handle:
        monkeypatch.setenv(SECRET_VAR, handle.state.machine_client_secret)
        await _list_tools(_server(handle, scopes=["read"]))
        (token,) = handle.state.access_tokens.values()
        assert list(token.scopes) == ["read"]


# --- failing, specifically ---------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_missing_secret_variable_is_a_config_error_naming_it_before_anything_is_sent(monkeypatch):
    monkeypatch.delenv(SECRET_VAR, raising=False)
    async with serve_oauth_fixture(FixtureConfig(client_credentials=True)) as handle:
        error = await _failure(_server(handle))
        assert isinstance(error, ConfigError) and SECRET_VAR in str(error) and "ci-crm" in str(error), error
        assert handle.state.requests == []


@pytest.mark.anyio
@pytest.mark.parametrize("value", ["", "   \n", "has a space", "line1\nline2"])
async def test_an_unusable_secret_value_is_refused_without_being_echoed(monkeypatch, value):
    monkeypatch.setenv(SECRET_VAR, value)
    async with serve_oauth_fixture(FixtureConfig(client_credentials=True)) as handle:
        error = await _failure(_server(handle))
        assert isinstance(error, ConfigError) and SECRET_VAR in str(error), error
        assert "line1" not in str(error) and "has a space" not in str(error)
        assert handle.state.requests == []


@pytest.mark.anyio
async def test_an_unreadable_private_key_is_a_config_error_that_does_not_quote_it(monkeypatch):
    garbage = "-----BEGIN PRIVATE KEY-----\ndrifter-canary-not-a-real-key\n-----END PRIVATE KEY-----"
    monkeypatch.setenv(KEY_VAR, garbage)
    async with serve_oauth_fixture(FixtureConfig(client_credentials=True)) as handle:
        error = await _failure(_jwt_server(handle))
        assert isinstance(error, ConfigError) and KEY_VAR in str(error), error
        assert "drifter-canary-not-a-real-key" not in str(error)
        assert handle.state.requests == []


@pytest.mark.anyio
async def test_a_key_of_the_wrong_type_for_the_algorithm_is_caught_before_any_request(monkeypatch):
    """An RSA key with `signing_algorithm: ES256` would otherwise fail deep inside the token request."""
    async with serve_oauth_fixture(FixtureConfig(client_credentials=True)) as handle:
        monkeypatch.setenv(KEY_VAR, handle.state.machine_private_key_pem)
        error = await _failure(_jwt_server(handle, signing_algorithm="ES256"))
        assert isinstance(error, ConfigError) and "ES256" in str(error) and KEY_VAR in str(error), error
        assert handle.state.requests == []


@pytest.mark.anyio
async def test_a_wrong_secret_is_reported_as_the_authorization_server_refusing_this_client(monkeypatch):
    monkeypatch.setenv(SECRET_VAR, "drifter-canary-wrong-secret")
    async with serve_oauth_fixture(FixtureConfig(client_credentials=True)) as handle:
        started = time.monotonic()
        error = await _failure(_server(handle))
        assert time.monotonic() - started < 10
        assert isinstance(error, UpstreamAuthRejected), error
        text = str(error)
        assert "ci-crm" in text and "invalid_client" in text and "401" in text, text
        assert SECRET_VAR in text and "client_id" in text, text  # says what to check
        assert "drifter-canary-wrong-secret" not in text
        assert "drifter auth login" not in text, "a service credential has no login to run"
        assert handle.state.token_grants == ["client_credentials"], "one attempt, not a retry loop"
        assert handle.state.mcp_requests()[-1].authorization is None  # the MCP server never saw a token


@pytest.mark.anyio
async def test_credentials_are_never_sent_to_an_authorization_server_the_config_did_not_name(monkeypatch):
    """The MCP server's metadata decides where discovery leads; the config decides where credentials go."""
    monkeypatch.setenv(SECRET_VAR, "drifter-canary-must-not-be-sent")
    async with serve_oauth_fixture(FixtureConfig(client_credentials=True)) as handle:
        error = await _failure(_server(handle, issuer="https://auth.elsewhere.example.com/"))
        assert isinstance(error, UpstreamAuthRejected), error
        assert "issuer" in str(error) and "auth.elsewhere.example.com" in str(error), str(error)
        assert handle.state.token_grants == [], "no token request was made, so no secret was sent"
        assert handle.state.requests_to("/token") == []


@pytest.mark.anyio
async def test_the_same_holds_for_a_signed_assertion(monkeypatch):
    async with serve_oauth_fixture(FixtureConfig(client_credentials=True)) as handle:
        monkeypatch.setenv(KEY_VAR, handle.state.machine_private_key_pem)
        error = await _failure(_jwt_server(handle, issuer="https://auth.elsewhere.example.com/"))
        assert isinstance(error, UpstreamAuthRejected), error
        assert handle.state.token_grants == [] and handle.state.assertion_claims == []


@pytest.mark.anyio
async def test_a_token_the_server_rejects_right_after_issuing_it_ends_the_connection_without_looping(monkeypatch):
    async with serve_oauth_fixture(FixtureConfig(client_credentials=True)) as handle:
        monkeypatch.setenv(SECRET_VAR, handle.state.machine_client_secret)
        handle.state.force_status(401, times=1)  # the first request is answered 401, as a server asking for a token does
        handle.state.force_status(401, times=5, challenge='Bearer error="invalid_token", error_description="audience mismatch"')
        error = await _failure(_server(handle))
        assert isinstance(error, UpstreamAuthRejected) and error.status == 401, error
        assert "invalid_token" in str(error) and "audience mismatch" in str(error), str(error)
        assert len(handle.state.requests_to("/token")) <= 1, "the same credentials are not asked for again"
        assert "drifter auth login" not in str(error)


@pytest.mark.anyio
async def test_insufficient_scope_names_the_scope_and_the_config_key(monkeypatch):
    async with serve_oauth_fixture(FixtureConfig(client_credentials=True)) as handle:
        monkeypatch.setenv(SECRET_VAR, handle.state.machine_client_secret)
        error = None
        with anyio.fail_after(20):
            try:
                async with connect_upstream(_server(handle)) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        await session.list_tools()  # a token is now held and in use
                        handle.state.force_status(403)
                        await session.list_tools()
            except BaseException as exc:  # noqa: BLE001 - the transport buries it in an exception group
                error = next((leaf for leaf in _leaves(exc) if isinstance(leaf, UpstreamAuthRejected)), None)
        assert error is not None, "the 403 was not reported"
        assert error.status == 403, error
        assert "admin" in str(error) and "auth.scopes" in str(error), str(error)


# --- observe, doctor: a CI-style run with no TTY -----------------------------------------------------------


def _project(tmp_path: Path, handle, auth_lines: str) -> Path:
    config = tmp_path / "project" / "drifter.yaml"
    config.parent.mkdir(exist_ok=True)
    config.write_text(
        f'version: 1\nservers:\n  - name: ci-crm\n    url: "{handle.mcp_url}"\n    auth:\n      type: client_credentials\n'
        f"      client_id: {MACHINE_CLIENT_ID}\n      issuer: \"{handle.issuer}\"\n{auth_lines}",
        encoding="utf-8",
    )
    return config


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in (SECRET_VAR, KEY_VAR, "XDG_CONFIG_HOME", "LOCALAPPDATA")}
    env["XDG_CONFIG_HOME"] = env["LOCALAPPDATA"] = str(tmp_path / "userconfig")
    env["PYTHON_KEYRING_BACKEND"] = "keyring.backends.fail.Keyring"
    env.update(extra)
    return env


@pytest.mark.anyio
@pytest.mark.parametrize("flavour", ["secret", "key"])
async def test_observe_records_a_session_with_no_tty_and_no_login_step(tmp_path, flavour):
    from mcp.client.stdio import StdioServerParameters, stdio_client

    from mcp_drifter.record.reader import read_session
    from mcp_drifter.record.schema import ToolCall

    async with serve_oauth_fixture(FixtureConfig(client_credentials=True)) as handle:
        if flavour == "secret":
            config = _project(tmp_path, handle, f"      secret_env: {SECRET_VAR}\n")
            env = _env(tmp_path, **{SECRET_VAR: handle.state.machine_client_secret})
        else:
            config = _project(tmp_path, handle, f"      private_key_env: {KEY_VAR}\n")
            env = _env(tmp_path, **{KEY_VAR: handle.state.machine_private_key_pem})
        project = config.parent
        errlog = (tmp_path / "observe.stderr").open("w+", encoding="utf-8")
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "mcp_drifter.cli", "observe", "--config", str(config)], env=env, cwd=str(project)
        )
        seen = ""
        with anyio.fail_after(60):
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    result = await session.call_tool("get_item", {"id": "42"})
                    seen = repr(tools) + repr(result)
        errlog.flush()
        errlog.seek(0)
        stderr_text = errlog.read()
        errlog.close()

        assert handle.state.authorize_requests == 0 and handle.state.registrations == 0
        assert handle.state.token_grants == ["client_credentials"]
        files = sorted((project / ".drifter" / "runs").glob("*.jsonl"))
        assert len(files) == 1
        assert [c.tool_name for c in read_session(files[0]) if isinstance(c, ToolCall)] == ["get_item"]
        assert not (tmp_path / "userconfig").exists(), "no login or token was stored"
        secrets = handle.state.all_secrets()
        assert_no_leaks(
            [
                *scan_tree(project, secrets),
                *scan_text("observe stderr", stderr_text, secrets),
                *scan_text("what the agent saw", seen, secrets),
            ]
        )


@pytest.mark.anyio
async def test_observe_with_a_wrong_secret_exits_4_promptly_and_leaks_nothing(tmp_path):
    async with serve_oauth_fixture(FixtureConfig(client_credentials=True)) as handle:
        config = _project(tmp_path, handle, f"      secret_env: {SECRET_VAR}\n")
        env = _env(tmp_path, **{SECRET_VAR: handle.state.canary + "-wrong"})
        code, stdout, stderr, elapsed = await _observe_until_exit(config, env, INITIALIZE)
        assert code == 4, (code, stdout, stderr)
        assert "invalid_client" in stderr and SECRET_VAR in stderr, stderr
        assert elapsed < 15, f"observe took {elapsed:.1f}s to give up"
        assert stdout == "" or all(line.startswith("{") for line in stdout.splitlines()), stdout
        assert_no_leaks(
            [*scan_text("stdout", stdout, [handle.state.canary]), *scan_text("stderr", stderr, [handle.state.canary]),
             *scan_tree(config.parent, [handle.state.canary])]
        )


@pytest.mark.anyio
async def test_observe_with_the_variable_unset_exits_4_before_contacting_the_server(tmp_path):
    async with serve_oauth_fixture(FixtureConfig(client_credentials=True)) as handle:
        config = _project(tmp_path, handle, f"      secret_env: {SECRET_VAR}\n")
        code, _stdout, stderr, elapsed = await _observe_until_exit(config, _env(tmp_path), INITIALIZE)
        assert code == 4 and SECRET_VAR in stderr, (code, stderr)
        assert handle.state.requests == [] and elapsed < 10


def test_drifter_auth_login_says_there_is_no_login_for_client_credentials(tmp_path):
    import subprocess

    config = tmp_path / "drifter.yaml"
    config.write_text(URL_SERVER + f"      {GOOD.replace(chr(10), chr(10) + '      ')}\n      secret_env: S\n", encoding="utf-8")
    run = subprocess.run(
        [sys.executable, "-m", "mcp_drifter.cli", "auth", "login", "ci", "--config", str(config)],
        capture_output=True, text=True, env=_env(tmp_path), timeout=30,
    )
    assert run.returncode == 4, run.stderr
    assert "no login to perform" in run.stderr and "not built yet" not in run.stderr, run.stderr


# --- adversarial probes (written after the first implementation passed; each one failed or was unproven) ------


@pytest.mark.anyio
async def test_a_token_endpoint_that_echoes_the_secret_back_does_not_get_it_printed(monkeypatch):
    """Some servers put what they received in `error_description`. That text is shown to the user, so the
    secret (and an assertion) must be scrubbed from it."""
    import httpx2

    from mcp_drifter.auth.service import service_provider
    from mcp_drifter.auth.config import AuthConfig

    secret = "drifter-canary-echoed-secret-value"
    auth = AuthConfig(type="client_credentials", client_id="c", issuer="https://auth.example.com/", secret_env=SECRET_VAR)
    provider = service_provider(auth, "https://mcp.example.com/mcp", "ci-crm", {SECRET_VAR: secret})
    response = httpx2.Response(
        401,
        json={"error": "invalid_client", "error_description": f"bad secret {secret} for client c"},
        request=httpx2.Request("POST", "https://auth.example.com/token"),
    )
    with pytest.raises(Exception):
        await provider._handle_token_response(response)
    error = provider._refused(Exception("x"))
    assert secret not in str(error) and "invalid_client" in str(error), str(error)


@pytest.mark.anyio
@pytest.mark.parametrize("flavour", ["secret", "key"])
async def test_a_cleartext_token_endpoint_never_receives_the_credentials(monkeypatch, flavour):
    async with serve_oauth_fixture(
        FixtureConfig(client_credentials=True, token_endpoint_override="http://auth.example.com/token")
    ) as handle:
        if flavour == "secret":
            monkeypatch.setenv(SECRET_VAR, handle.state.machine_client_secret)
            server = _server(handle)
        else:
            monkeypatch.setenv(KEY_VAR, handle.state.machine_private_key_pem)
            server = _jwt_server(handle)
        error = await _failure(server)
        assert isinstance(error, UpstreamAuthRejected), error
        assert "cleartext" in str(error) and "token" in str(error), str(error)
        assert handle.state.token_grants == [] and handle.state.requests_to("/token") == []


def _pem(key) -> str:
    from cryptography.hazmat.primitives import serialization

    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def _key_auth(algorithm: str):
    from mcp_drifter.auth.config import AuthConfig

    return AuthConfig(
        type="client_credentials", client_id="c", issuer="https://auth.example.com/", private_key_env=KEY_VAR, signing_algorithm=algorithm
    )


def test_keys_are_checked_against_the_algorithm_they_will_sign_with():
    from cryptography.hazmat.primitives.asymmetric import ec, rsa

    from mcp_drifter.auth.errors import AuthSetupError
    from mcp_drifter.auth.service import read_private_key

    ec256 = _pem(ec.generate_private_key(ec.SECP256R1()))
    ec384 = _pem(ec.generate_private_key(ec.SECP384R1()))
    rsa_pem = _pem(rsa.generate_private_key(public_exponent=65537, key_size=2048))
    assert read_private_key(_key_auth("ES256"), "s", {KEY_VAR: ec256}) == ec256.strip()
    assert read_private_key(_key_auth("RS256"), "s", {KEY_VAR: rsa_pem}) == rsa_pem.strip()
    assert read_private_key(_key_auth("PS256"), "s", {KEY_VAR: rsa_pem}) == rsa_pem.strip()
    for algorithm, pem in (("RS256", ec256), ("ES256", rsa_pem), ("ES384", ec256), ("ES256", ec384)):
        with pytest.raises(AuthSetupError, match="cannot sign with"):
            read_private_key(_key_auth(algorithm), "s", {KEY_VAR: pem})


def test_a_password_protected_key_is_refused_with_a_clear_reason():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from mcp_drifter.auth.errors import AuthSetupError
    from mcp_drifter.auth.service import read_private_key

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    locked = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.BestAvailableEncryption(b"pw")
    ).decode()
    with pytest.raises(AuthSetupError, match="password-protected") as excinfo:
        read_private_key(_key_auth("RS256"), "s", {KEY_VAR: locked})
    assert "BEGIN" not in str(excinfo.value)
