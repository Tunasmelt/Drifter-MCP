"""F-44's done criteria end to end: a real `drifter observe` subprocess against the OAuth fixture, using
the login `drifter auth login` stored (docs/FEATURES.md F-44, F-47).

  - "observe records a real session through the fixture with a stored token";
  - expired-but-refreshable succeeds, silently, and the refreshed login is stored;
  - expired-unrefreshable, a server-side rejection and insufficient scope each exit 4 with a specific
    message that says what to run, promptly (CLAUDE.md: shutdown paths are timed, not assumed);
  - the F-47 canary is nowhere Drifter wrote or printed, and stdout carries only the agent's protocol.

The stored login lives in a per-test config directory, found by the subprocess through XDG_CONFIG_HOME
(LOCALAPPDATA on Windows), exactly as a user's would be.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from canary_scan import assert_no_leaks, scan_text, scan_tree
from mcp_drifter.record.reader import read_session
from mcp_drifter.record.schema import ToolCall
from oauth_server import FixtureConfig, serve_oauth_fixture
from stub_browser import log_in, store_for
from test_observe_bearer_e2e import INITIALIZE, _observe_until_exit  # noqa: F401  (shared driver)

OBSERVE = [sys.executable, "-m", "mcp_drifter.cli", "observe"]


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _config_dir(tmp_path: Path) -> Path:
    return tmp_path / "userconfig"


def _env(tmp_path: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("CRM_TOKEN", "XDG_CONFIG_HOME", "LOCALAPPDATA")}
    env["XDG_CONFIG_HOME"] = str(_config_dir(tmp_path))
    env["LOCALAPPDATA"] = str(_config_dir(tmp_path))
    # The login under test is stored in a file in the per-test config directory. Without this a machine with
    # a usable keyring (a Windows runner) would move the refreshed login into the REAL keyring and these
    # tests would both fail to see it and leave entries behind (found by Windows CI).
    env["PYTHON_KEYRING_BACKEND"] = "keyring.backends.fail.Keyring"
    return env


def _project(tmp_path: Path, url: str, scopes: str = "") -> Path:
    config = tmp_path / "project" / "drifter.yaml"
    config.parent.mkdir(exist_ok=True)
    config.write_text(
        f'version: 1\nservers:\n  - name: remote-crm\n    url: "{url}"\n    auth:\n      type: oauth\n{scopes}', encoding="utf-8"
    )
    return config


async def _login(handle, tmp_path):
    store = await log_in(handle, _config_dir(tmp_path) / "drifter")
    handle.state.requests.clear()  # the login's own requests (its unauthenticated probe, registration) are not under test
    return store


@pytest.mark.anyio
async def test_observe_records_a_real_session_through_a_stored_oauth_login(tmp_path):
    async with serve_oauth_fixture(FixtureConfig()) as handle:
        store = await _login(handle, tmp_path)
        token = (await store.get_tokens()).access_token
        config = _project(tmp_path, handle.mcp_url)
        project = config.parent
        errlog = (tmp_path / "observe.stderr").open("w+", encoding="utf-8")
        params = StdioServerParameters(
            command=OBSERVE[0], args=[*OBSERVE[1:], "--config", str(config)], env=_env(tmp_path), cwd=str(project)
        )
        seen = ""
        with anyio.fail_after(60):
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    result = await session.call_tool("get_item", {"id": "42"})
                    seen = repr(tools) + repr(result)
            closed_at = time.monotonic()
        assert time.monotonic() - closed_at < 5

        errlog.flush()
        errlog.seek(0)
        stderr_text = errlog.read()
        errlog.close()

        mcp = handle.state.mcp_requests()
        assert len(mcp) >= 4 and {r.authorization for r in mcp} == {f"Bearer {token}"}
        session_files = sorted((project / ".drifter" / "runs").glob("*.jsonl"))
        assert len(session_files) == 1
        calls = [r for r in read_session(session_files[0]) if isinstance(r, ToolCall)]
        assert [c.tool_name for c in calls] == ["get_item"]

        secrets = handle.state.all_secrets()
        assert_no_leaks(
            [
                *scan_tree(project, secrets),
                *scan_text("observe stderr", stderr_text, secrets),
                *scan_text("what the agent saw", seen, secrets),
            ]
        )
        # and the stored login is not inside the project
        assert not list(project.rglob("*.json")) or all(".drifter" in str(p) for p in project.rglob("*.json"))


@pytest.mark.anyio
async def test_an_expired_but_refreshable_login_is_refreshed_and_stdout_stays_protocol_only(tmp_path):
    async with serve_oauth_fixture(FixtureConfig()) as handle:
        store = await _login(handle, tmp_path)
        old = await store.get_tokens()
        await store.set_tokens(old, issued_at=time.time() - 7200)
        config = _project(tmp_path, handle.mcp_url)

        params = StdioServerParameters(
            command=OBSERVE[0], args=[*OBSERVE[1:], "--config", str(config)], env=_env(tmp_path), cwd=str(config.parent)
        )
        with anyio.fail_after(60):
            async with stdio_client(params, errlog=(tmp_path / "e.log").open("w")) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    await session.list_tools()

        new = await store_for(handle, _config_dir(tmp_path) / "drifter").get_tokens()
        assert new.access_token != old.access_token
        assert {r.authorization for r in handle.state.mcp_requests()} == {f"Bearer {new.access_token}"}
        assert_no_leaks(scan_tree(config.parent, handle.state.all_secrets()))


@pytest.mark.anyio
async def test_no_login_exits_4_telling_the_user_to_log_in_without_contacting_the_server(tmp_path):
    async with serve_oauth_fixture(FixtureConfig()) as handle:
        config = _project(tmp_path, handle.mcp_url)
        code, stdout, stderr, elapsed = await _observe_until_exit(config, _env(tmp_path), INITIALIZE)
        assert code == 4, (code, stdout, stderr)
        assert "drifter auth login remote-crm" in stderr, stderr
        assert stdout == "" or all(line.startswith("{") for line in stdout.splitlines()), stdout
        assert handle.state.requests == []
        assert elapsed < 10


@pytest.mark.anyio
async def test_a_login_the_server_rejects_exits_4_naming_the_command_and_leaks_nothing(tmp_path):
    async with serve_oauth_fixture(FixtureConfig()) as handle:
        await _login(handle, tmp_path)
        handle.state.expire_access_tokens()
        handle.state.requests.clear()  # the login's own requests are not what is under test
        config = _project(tmp_path, handle.mcp_url)
        code, stdout, stderr, elapsed = await _observe_until_exit(config, _env(tmp_path), INITIALIZE)
        assert code == 4, (code, stdout, stderr)
        assert "401" in stderr and "drifter auth login remote-crm" in stderr, stderr
        assert elapsed < 10, f"observe took {elapsed:.1f}s to give up"
        assert [r.path for r in handle.state.requests if r.path in ("/authorize", "/register")] == []
        secrets = handle.state.all_secrets()
        assert_no_leaks([*scan_text("stdout", stdout, secrets), *scan_text("stderr", stderr, secrets), *scan_tree(config.parent, secrets)])


@pytest.mark.anyio
async def test_insufficient_scope_exits_4_naming_the_scope(tmp_path):
    async with serve_oauth_fixture(FixtureConfig()) as handle:
        await _login(handle, tmp_path)
        handle.state.force_status(403)
        config = _project(tmp_path, handle.mcp_url)
        code, _stdout, stderr, elapsed = await _observe_until_exit(config, _env(tmp_path), INITIALIZE)
        assert code == 4, (code, stderr)
        assert "admin" in stderr and "auth.scopes" in stderr, stderr
        assert elapsed < 10


@pytest.mark.anyio
async def test_a_token_store_inside_the_project_is_refused_with_exit_4(tmp_path):
    async with serve_oauth_fixture(FixtureConfig()) as handle:
        config = _project(tmp_path, handle.mcp_url)
        env = _env(tmp_path)
        env["XDG_CONFIG_HOME"] = str(config.parent / ".drifter")
        env["LOCALAPPDATA"] = str(config.parent / ".drifter")
        code, _stdout, stderr, _ = await _observe_until_exit(config, env, None)
        assert code == 4, (code, stderr)
        assert "inside the project" in stderr, stderr
        assert handle.state.requests == []


# --- doctor uses the same connection ------------------------------------------------------------------------


def _doctor(config: Path, env: dict[str, str]):
    import subprocess

    return subprocess.run(
        [sys.executable, "-m", "mcp_drifter.cli", "doctor", "--config", str(config)],
        capture_output=True, text=True, cwd=config.parent, env=env, timeout=60,
    )


@pytest.mark.anyio
async def test_doctor_connects_with_the_stored_login(tmp_path):
    async with serve_oauth_fixture(FixtureConfig()) as handle:
        await _login(handle, tmp_path)
        config = _project(tmp_path, handle.mcp_url)
        result = await anyio.to_thread.run_sync(lambda: _doctor(config, _env(tmp_path)))
        assert "remote-crm" in result.stdout and "[FAIL]" not in result.stdout, (result.stdout, result.stderr)
        assert_no_leaks(scan_text("doctor", result.stdout + result.stderr, handle.state.all_secrets()))


@pytest.mark.anyio
async def test_doctor_without_a_login_fails_that_check_and_says_what_to_run(tmp_path):
    async with serve_oauth_fixture(FixtureConfig()) as handle:
        config = _project(tmp_path, handle.mcp_url)
        result = await anyio.to_thread.run_sync(lambda: _doctor(config, _env(tmp_path)))
        out = result.stdout + result.stderr
        assert "[FAIL]" in out and "drifter auth login remote-crm" in out, out
        # F-46: doctor's discovery check does probe the server, so the old "contacts nothing" no longer holds; what must
        # still hold is that with no login nothing credentialed is sent and no login or registration is started.
        assert all(r.authorization is None for r in handle.state.requests), handle.state.requests
        assert not [r for r in handle.state.requests if r.path in ("/authorize", "/register", "/token")], handle.state.requests
