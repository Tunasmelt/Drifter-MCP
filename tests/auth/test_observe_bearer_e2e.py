"""F-41's done criteria and F-47's first real run, end to end: a real `drifter observe`
subprocess against the OAuth fixture's static-bearer mode (docs/FEATURES.md F-41, F-47).

What this proves that the unit tests cannot:
  - `bearer_env` "records a real session": the session lands in `.drifter/runs` with the
    tool calls the agent made, and the fixture's own log shows every upstream request
    carried the bearer.
  - the F-47 canary (the fixture's static bearer starts with `drifter-canary-<uuid>`) is
    nowhere Drifter wrote or printed: not `.drifter/`, not stderr, not what the agent saw.
  - a bad token, a missing variable, `auth` on a stdio server and a literal secret in
    `token_env` each end in exit 4 with a specific message, without the secret in it.
  - shutdown is prompt (CLAUDE.md async-shutdown rule: timed, not just passing). The new
    HTTP-client wrapper around the proxy is exactly the kind of code that can hang on exit.

Spawns the CLI the way tests/cli already do (`python -m mcp_drifter.cli`).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from canary_scan import assert_no_leaks, new_canary, scan_text, scan_tree
from mcp_drifter.record.reader import read_session
from mcp_drifter.record.schema import ToolCall
from oauth_server import FixtureConfig, serve_oauth_fixture

OBSERVE = [sys.executable, "-m", "mcp_drifter.cli", "observe"]

INITIALIZE = (
    json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
        }
    )
    + "\n"
).encode()


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _project(tmp_path: Path, server_yaml: str) -> Path:
    config = tmp_path / "drifter.yaml"
    config.write_text(f"version: 1\nservers:\n  - name: remote-crm\n{server_yaml}", encoding="utf-8")
    return config


def _bearer_yaml(url: str, token_env: str = "CRM_TOKEN") -> str:
    return f'    url: "{url}"\n    auth:\n      type: bearer_env\n      token_env: {token_env}\n'


def _env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "CRM_TOKEN"}
    env.update(extra)
    return env


async def _observe_until_exit(config: Path, env: dict[str, str], first_message: bytes | None, timeout: float = 30):
    """Runs observe as a subprocess and waits for it to exit on its own (the agent side stays
    open, as a real agent's would). Async, because the fixture server shares this event loop."""
    out, err = bytearray(), bytearray()

    async def drain(stream, sink: bytearray) -> None:
        async for chunk in stream:
            sink.extend(chunk)

    started = time.monotonic()
    process = await anyio.open_process(
        [*OBSERVE, "--config", str(config)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(config.parent),
    )
    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(drain, process.stdout, out)
            tg.start_soon(drain, process.stderr, err)
            if first_message is not None:
                await process.stdin.send(first_message)
            with anyio.fail_after(timeout):
                await process.wait()
    finally:
        if process.returncode is None:
            process.kill()
        await process.aclose()
    return process.returncode, out.decode(errors="replace"), err.decode(errors="replace"), time.monotonic() - started


# --- the success path --------------------------------------------------------------------------


@pytest.mark.anyio
async def test_observe_records_a_real_session_through_a_bearer_protected_server(tmp_path):
    async with serve_oauth_fixture(FixtureConfig(protection="static_bearer")) as handle:
        config = _project(tmp_path, _bearer_yaml(handle.mcp_url))
        token = handle.state.static_bearer
        errlog = (tmp_path / "observe.stderr").open("w+", encoding="utf-8")
        params = StdioServerParameters(command=OBSERVE[0], args=[*OBSERVE[1:], "--config", str(config)], env=_env(CRM_TOKEN=token), cwd=str(tmp_path))

        seen_by_the_agent = ""
        with anyio.fail_after(60):
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    result = await session.call_tool("get_item", {"id": "42"})
                    seen_by_the_agent = repr(tools) + repr(result)
            closed_at = time.monotonic()
        # the whole stdio_client exit (proxy shutdown included) is timed, with a bound well
        # under its own give-up
        assert time.monotonic() - closed_at < 5

        errlog.flush()
        errlog.seek(0)
        stderr_text = errlog.read()
        errlog.close()

        # the fixture saw the bearer on EVERY upstream request, and nothing else was added
        requests = handle.state.mcp_requests()
        assert len(requests) >= 4, requests
        assert {r.authorization for r in requests} == {f"Bearer {token}"}

        # a real session was recorded, with the call the agent made
        session_files = sorted((tmp_path / ".drifter" / "runs").glob("*.jsonl"))
        assert len(session_files) == 1, list((tmp_path / ".drifter").rglob("*"))
        calls = [r for r in read_session(session_files[0]) if isinstance(r, ToolCall)]
        assert [c.tool_name for c in calls] == ["get_item"]

        # F-47: the canary is nowhere Drifter wrote or printed
        secrets = handle.state.all_secrets()
        assert_no_leaks(
            [
                *scan_tree(tmp_path / ".drifter", secrets),
                *scan_tree(tmp_path, secrets),  # drifter.yaml holds only the variable NAME
                *scan_text("observe stderr", stderr_text, secrets),
                *scan_text("what the agent saw", seen_by_the_agent, secrets),
            ]
        )


# --- failure paths, each exit 4 --------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_rejected_token_exits_4_naming_the_variable_and_the_status_not_the_token(tmp_path):
    wrong = new_canary() + "-wrong-token"
    async with serve_oauth_fixture(FixtureConfig(protection="static_bearer")) as handle:
        config = _project(tmp_path, _bearer_yaml(handle.mcp_url))
        code, stdout, stderr, elapsed = await _observe_until_exit(config, _env(CRM_TOKEN=wrong), INITIALIZE)

        assert code == 4, (code, stdout, stderr)
        assert "CRM_TOKEN" in stderr and "remote-crm" in stderr and "401" in stderr, stderr
        assert elapsed < 10, f"observe took {elapsed:.1f}s to give up on a rejected token"
        secrets = [wrong, *handle.state.all_secrets()]
        assert_no_leaks(
            [
                *scan_text("stdout", stdout, secrets),
                *scan_text("stderr", stderr, secrets),
                *scan_tree(tmp_path, secrets),
            ]
        )


@pytest.mark.anyio
async def test_a_missing_variable_exits_4_before_contacting_the_server(tmp_path):
    async with serve_oauth_fixture(FixtureConfig(protection="static_bearer")) as handle:
        config = _project(tmp_path, _bearer_yaml(handle.mcp_url))
        code, _stdout, stderr, elapsed = await _observe_until_exit(config, _env(), first_message=None)
        assert code == 4, (code, stderr)
        assert "CRM_TOKEN" in stderr and "not set" in stderr, stderr
        assert handle.state.requests == []  # nothing reached the server
        assert elapsed < 10


def test_auth_on_a_stdio_server_exits_4_with_an_actionable_message(tmp_path):
    config = _project(
        tmp_path, '    command: ["python", "server.py"]\n    auth:\n      type: bearer_env\n      token_env: CRM_TOKEN\n'
    )
    result = subprocess.run([*OBSERVE, "--config", str(config)], capture_output=True, text=True, cwd=tmp_path, timeout=60, input="")
    assert result.returncode == 4, (result.stdout, result.stderr)
    assert "remote-crm" in result.stderr and "url" in result.stderr and "command" in result.stderr, result.stderr


def test_a_literal_secret_in_token_env_exits_4_and_the_secret_is_not_printed(tmp_path):
    secret = "sk-AbCdEf0123456789AbCdEf0123456789"
    config = _project(
        tmp_path, f'    url: "https://mcp.example.com/mcp"\n    auth:\n      type: bearer_env\n      token_env: "{secret}"\n'
    )
    result = subprocess.run([*OBSERVE, "--config", str(config)], capture_output=True, text=True, cwd=tmp_path, timeout=60, input="")
    assert result.returncode == 4, (result.stdout, result.stderr)
    assert "token_env" in result.stderr and "environment variable" in result.stderr, result.stderr
    assert secret not in result.stdout + result.stderr
    assert secret[:12] not in result.stdout + result.stderr


def test_an_oauth_server_with_no_stored_login_exits_4_telling_the_user_to_log_in(tmp_path):
    config = _project(tmp_path, '    url: "https://mcp.example.com/mcp"\n    auth:\n      type: oauth\n')
    env = {k: v for k, v in os.environ.items() if k not in ("XDG_CONFIG_HOME", "LOCALAPPDATA")}
    env["XDG_CONFIG_HOME"] = env["LOCALAPPDATA"] = str(tmp_path / "empty-config")
    env["PYTHON_KEYRING_BACKEND"] = "keyring.backends.fail.Keyring"  # never touch a real keyring
    result = subprocess.run([*OBSERVE, "--config", str(config)], capture_output=True, text=True, cwd=tmp_path, timeout=60, input="", env=env)
    assert result.returncode == 4, (result.stdout, result.stderr)
    assert "drifter auth login remote-crm" in result.stderr, result.stderr
