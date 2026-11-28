"""F-46 end to end: a real `drifter observe` subprocess stamps the auth type and the GRANTED scopes into the
recorded SessionStart (docs/FEATURES.md F-46: "two sessions recorded under different scope sets carry
different fingerprints"). Written before the wiring in `cli/observe.py` / `cli/upstream.py`.

The values asserted are exact and independent: the expected scopes come from the credential the TEST holds
(the stored token, the configured request, the fixture's own record), never from reading Drifter's output.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from mcp_drifter.record.fingerprint import compute_fingerprint
from mcp_drifter.record.reader import read_session
from mcp_drifter.record.schema import SessionStart
from oauth_server import MACHINE_CLIENT_ID, FixtureConfig, serve_oauth_fixture
from stub_browser import log_in

SECRET_VAR = "CRM_CLIENT_SECRET"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in (SECRET_VAR, "CRM_TOKEN", "XDG_CONFIG_HOME", "LOCALAPPDATA")}
    env["XDG_CONFIG_HOME"] = env["LOCALAPPDATA"] = str(tmp_path / "userconfig")
    env["PYTHON_KEYRING_BACKEND"] = "keyring.backends.fail.Keyring"
    env.update(extra)
    return env


def _config(tmp_path: Path, url: str, auth_block: str) -> Path:
    config = tmp_path / "project" / "drifter.yaml"
    config.parent.mkdir(exist_ok=True)
    config.write_text(f'version: 1\nservers:\n  - name: remote-crm\n    url: "{url}"\n{auth_block}', encoding="utf-8")
    return config


async def _observe(config: Path, env: dict[str, str], tmp_path: Path) -> SessionStart:
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "mcp_drifter.cli", "observe", "--config", str(config)], env=env, cwd=str(config.parent)
    )
    with anyio.fail_after(60):
        async with stdio_client(params, errlog=(tmp_path / "observe.stderr").open("w")) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                await session.list_tools()
                await session.call_tool("get_item", {"id": "1"})
    (path,) = sorted((config.parent / ".drifter" / "runs").glob("*.jsonl"))
    return next(r for r in read_session(path) if isinstance(r, SessionStart))


def _written_environment(config: Path) -> dict:
    (path,) = sorted((config.parent / ".drifter" / "runs").glob("*.jsonl"))
    return json.loads(path.read_text(encoding="utf-8").splitlines()[0])["environment"]


def _check_fingerprint(env) -> None:
    assert env.fingerprint == compute_fingerprint(
        env.agent_identity, env.model_name, env.server_versions, env.tool_manifest_hash, env.auth_type, env.granted_scopes
    )


@pytest.mark.anyio
async def test_an_oauth_login_is_recorded_with_the_scopes_the_server_granted(tmp_path):
    async with serve_oauth_fixture(FixtureConfig()) as handle:
        store = await log_in(handle, tmp_path / "userconfig" / "drifter")
        granted = (await store.get_tokens()).scope.split()
        config = _config(tmp_path, handle.mcp_url, "    auth:\n      type: oauth\n")
        start = await _observe(config, _env(tmp_path), tmp_path)
    assert granted, "the fixture must have granted something for this test to mean anything"
    assert (start.environment.auth_type, start.environment.granted_scopes) == ("oauth", sorted(set(granted)))
    _check_fingerprint(start.environment)


@pytest.mark.anyio
async def test_a_client_credentials_run_is_recorded_with_its_granted_scopes(tmp_path):
    async with serve_oauth_fixture(FixtureConfig(client_credentials=True)) as handle:
        block = (
            f"    auth:\n      type: client_credentials\n      client_id: {MACHINE_CLIENT_ID}\n"
            f'      issuer: "{handle.issuer}"\n      secret_env: {SECRET_VAR}\n      scopes: [read]\n'
        )
        config = _config(tmp_path, handle.mcp_url, block)
        start = await _observe(config, _env(tmp_path, **{SECRET_VAR: handle.state.machine_client_secret}), tmp_path)
    assert (start.environment.auth_type, start.environment.granted_scopes) == ("client_credentials", ["read"])
    _check_fingerprint(start.environment)


@pytest.mark.anyio
async def test_a_static_bearer_token_is_recorded_as_an_unknown_grant(tmp_path):
    async with serve_oauth_fixture(FixtureConfig(protection="static_bearer")) as handle:
        config = _config(tmp_path, handle.mcp_url, "    auth:\n      type: bearer_env\n      token_env: CRM_TOKEN\n")
        start = await _observe(config, _env(tmp_path, CRM_TOKEN=handle.state.static_bearer), tmp_path)
        written = _written_environment(config)
    assert written["auth_type"] == "bearer_env" and "granted_scopes" in written and written["granted_scopes"] is None
    assert start.environment.granted_scopes is None
    _check_fingerprint(start.environment)


@pytest.mark.anyio
async def test_an_unauthenticated_server_records_no_auth_fields(tmp_path):
    async with serve_oauth_fixture(FixtureConfig(protection="open")) as handle:
        config = _config(tmp_path, handle.mcp_url, "")
        await _observe(config, _env(tmp_path), tmp_path)
        written = _written_environment(config)
    assert "auth_type" not in written and "granted_scopes" not in written


@pytest.mark.anyio
async def test_two_sessions_under_different_scopes_carry_different_fingerprints(tmp_path):
    async with serve_oauth_fixture(FixtureConfig(client_credentials=True)) as handle:
        starts = []
        for scopes in ("[read]", "[read, admin]"):
            work = tmp_path / scopes.strip("[]").replace(", ", "-")
            work.mkdir()
            block = (
                f"    auth:\n      type: client_credentials\n      client_id: {MACHINE_CLIENT_ID}\n"
                f'      issuer: "{handle.issuer}"\n      secret_env: {SECRET_VAR}\n      scopes: {scopes}\n'
            )
            config = _config(work, handle.mcp_url, block)
            starts.append(await _observe(config, _env(work, **{SECRET_VAR: handle.state.machine_client_secret}), work))
    narrow, wide = (s.environment for s in starts)
    assert narrow.granted_scopes == ["read"] and wide.granted_scopes == ["admin", "read"]
    assert narrow.fingerprint != wide.fingerprint
