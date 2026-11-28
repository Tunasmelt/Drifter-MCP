"""F-46 through the real `drifter doctor` command: the auth findings are printed, a FAIL stops the run with exit 4
(docs/SPEC.md §12), and a WARN does not. Written before `cli/doctor.py` called `auth/diagnose.py`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import anyio
import pytest

from mcp_drifter.record.fingerprint import build_environment
from mcp_drifter.record.schema import SessionStart, ToolsList
from oauth_server import MACHINE_CLIENT_ID, FixtureConfig, serve_oauth_fixture

SECRET_VAR = "DOCTOR_CLIENT_SECRET"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in (SECRET_VAR, "XDG_CONFIG_HOME", "LOCALAPPDATA")}
    env["XDG_CONFIG_HOME"] = env["LOCALAPPDATA"] = str(tmp_path / "userconfig")
    env["PYTHON_KEYRING_BACKEND"] = "keyring.backends.fail.Keyring"
    env.update(extra)
    return env


def _config(tmp_path: Path, handle, auth: str) -> Path:
    config = tmp_path / "project" / "drifter.yaml"
    config.parent.mkdir(exist_ok=True)
    config.write_text(f'version: 1\nservers:\n  - name: remote-crm\n    url: "{handle.mcp_url}"\n    auth:\n{auth}', encoding="utf-8")
    return config


def _cc_block(handle, **extra) -> str:
    lines = [f"      type: client_credentials", f"      client_id: {MACHINE_CLIENT_ID}", f'      issuer: "{handle.issuer}"', f"      secret_env: {SECRET_VAR}"]
    lines += [f"      {k}: {v}" for k, v in extra.items()]
    return "\n".join(lines) + "\n"


async def _doctor(config: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    def run():
        return subprocess.run(
            [sys.executable, "-m", "mcp_drifter.cli", "doctor", "--config", str(config)],
            capture_output=True, text=True, cwd=config.parent, env=env, timeout=90,
        )

    return await anyio.to_thread.run_sync(run)


@pytest.mark.anyio
async def test_a_healthy_service_credential_prints_its_checks_and_exits_zero(tmp_path):
    async with serve_oauth_fixture(FixtureConfig(client_credentials=True)) as handle:
        config = _config(tmp_path, handle, _cc_block(handle))
        result = await _doctor(config, _env(tmp_path, **{SECRET_VAR: handle.state.machine_client_secret}))
    out = result.stdout
    assert result.returncode == 0, (out, result.stderr)
    for check in ("credential", "discovery", "issuer", "grant"):
        assert f"auth 'remote-crm' {check}:" in out, (check, out)
    assert "[FAIL]" not in out


@pytest.mark.anyio
async def test_a_missing_login_prints_a_fail_with_the_command_and_exits_4(tmp_path):
    async with serve_oauth_fixture(FixtureConfig()) as handle:
        config = _config(tmp_path, handle, "      type: oauth\n")
        result = await _doctor(config, _env(tmp_path))
    assert result.returncode == 4, (result.stdout, result.stderr)
    assert "[FAIL] auth 'remote-crm' login:" in result.stdout and "drifter auth login remote-crm" in result.stdout, result.stdout


@pytest.mark.anyio
async def test_grant_drift_is_a_warning_that_does_not_change_the_exit_code(tmp_path):
    async with serve_oauth_fixture(FixtureConfig(client_credentials=True)) as handle:
        config = _config(tmp_path, handle, _cc_block(handle, scopes="[read, admin]"))
        runs = config.parent / ".drifter" / "runs"
        runs.mkdir(parents=True)
        # A real recording (the models, so every field the rest of doctor reads is valid), made under ['read'].
        environment = build_environment("a/1", None, {}, "sha256:m", auth_type="client_credentials", granted_scopes=["read"])
        lines = [
            SessionStart(session_id="old", seq=0, started_at="2026-09-15T00:00:00Z", environment=environment, raw_frame_offset=0),
            ToolsList(session_id="old", seq=1, timestamp="2026-09-15T00:00:01Z", server="remote-crm", tools_raw=[], tools_served=[], raw_frame_offset=1),
        ]
        (runs / "old.jsonl").write_text("".join(line.model_dump_json() + "\n" for line in lines), encoding="utf-8")
        result = await _doctor(config, _env(tmp_path, **{SECRET_VAR: handle.state.machine_client_secret}))
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "[WARN] auth 'remote-crm' grant:" in result.stdout and "['read']" in result.stdout and "['admin', 'read']" in result.stdout, result.stdout


@pytest.mark.anyio
async def test_a_wrong_secret_fails_the_connection_check_with_its_own_message_and_leaves_the_credential_check_alone(tmp_path):
    async with serve_oauth_fixture(FixtureConfig(client_credentials=True)) as handle:
        config = _config(tmp_path, handle, _cc_block(handle))
        result = await _doctor(config, _env(tmp_path, **{SECRET_VAR: handle.state.canary + "-wrong"}))
    assert result.returncode == 4, (result.stdout, result.stderr)
    assert "[FAIL] server 'remote-crm':" in result.stdout and "invalid_client" in result.stdout, result.stdout
    assert "[ OK ] auth 'remote-crm' credential:" in result.stdout, "the variable is set and well formed; the SERVER refused it"
    assert handle.state.canary not in result.stdout + result.stderr


@pytest.mark.anyio
async def test_an_unauthenticated_server_prints_no_auth_lines(tmp_path):
    async with serve_oauth_fixture(FixtureConfig(protection="open")) as handle:
        config = tmp_path / "project" / "drifter.yaml"
        config.parent.mkdir()
        config.write_text(f'version: 1\nservers:\n  - name: remote-crm\n    url: "{handle.mcp_url}"\n', encoding="utf-8")
        result = await _doctor(config, _env(tmp_path))
    assert "auth '" not in result.stdout, result.stdout
