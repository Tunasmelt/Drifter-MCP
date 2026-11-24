"""F-43: `drifter auth login | status | logout`, as real subprocesses (docs/FEATURES.md F-43).

Written before the implementation. The "browser" is this test: it reads the authorization URL the
command prints, opens it the way a browser would, and follows the redirect to the command's
loopback listener. Keyring use is switched off in the child (`PYTHON_KEYRING_BACKEND`) so the
tests never touch a developer's real keychain, and the config directory is a temporary one.

CLAUDE.md async-shutdown rule: Ctrl+C while waiting for the browser must close the listener and
exit promptly, not after the 300 s timeout. That is timed here with a real signal on a real process.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import anyio
import httpx2
import pytest

from canary_scan import assert_no_leaks, scan_text, scan_tree
from mcp_drifter.auth.storage import TokenStore
from oauth_server import serve_oauth_fixture

CLI = [sys.executable, "-m", "mcp_drifter.cli"]
URL_LINE = re.compile(r"^\s*(https?://\S+/authorize\?\S+)\s*$")


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _env(tmp_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        HOME=str(tmp_path / "home"),
        USERPROFILE=str(tmp_path / "home"),
        XDG_CONFIG_HOME=str(tmp_path / "xdg"),
        LOCALAPPDATA=str(tmp_path / "local"),
        PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring",
    )
    return env


def _config_base(tmp_path: Path) -> Path:
    return (tmp_path / "local" / "drifter") if sys.platform == "win32" else (tmp_path / "xdg" / "drifter")


def _write_config(tmp_path: Path, body: str) -> Path:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    path = project / "drifter.yaml"
    path.write_text("version: 1\nservers:\n" + body, encoding="utf-8")
    return path


def _oauth_config(tmp_path: Path, url: str, extra: str = "") -> Path:
    return _write_config(tmp_path, f'  - name: remote-crm\n    url: "{url}"\n    auth:\n      type: oauth\n{extra}')


def _run(tmp_path: Path, *args: str, config: Path | None = None, timeout: float = 60) -> subprocess.CompletedProcess:
    cmd = [*CLI, "auth", *args]
    if config is not None:
        cmd += ["--config", str(config)]
    return subprocess.run(cmd, capture_output=True, text=True, env=_env(tmp_path), cwd=tmp_path / "project", timeout=timeout)


async def _start_login(tmp_path: Path, config: Path, *extra: str):
    return await anyio.open_process(
        [*CLI, "auth", "login", "remote-crm", "--config", str(config), "--no-browser", *extra],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_env(tmp_path),
        cwd=str(tmp_path / "project"),
    )


async def _read_authorization_url(process, timeout: float = 30) -> tuple[str, list[str]]:
    """Reads stdout lines until the authorization URL appears; returns it and everything seen."""
    seen: list[str] = []
    buffer = b""
    with anyio.fail_after(timeout):
        while True:
            chunk = await process.stdout.receive(4096)
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                text = line.decode(errors="replace").rstrip("\r")
                seen.append(text)
                match = URL_LINE.match(text)
                if match:
                    return match.group(1), seen


async def _browse(url: str) -> None:
    async with httpx2.AsyncClient(follow_redirects=False) as client:
        response = await client.get(url)
        await client.get(response.headers["location"])


async def _finish(process, timeout: float = 30) -> tuple[int, str, str]:
    out, err = bytearray(), bytearray()

    async def drain(stream, sink):
        try:
            async for chunk in stream:
                sink.extend(chunk)
        except (anyio.BrokenResourceError, anyio.ClosedResourceError):
            pass

    async with anyio.create_task_group() as tg:
        tg.start_soon(drain, process.stdout, out)
        tg.start_soon(drain, process.stderr, err)
        with anyio.fail_after(timeout):
            await process.wait()
    return process.returncode, out.decode(errors="replace"), err.decode(errors="replace")


# --- login, status, logout, end to end ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_login_then_status_then_logout(tmp_path):
    async with serve_oauth_fixture() as handle:
        config = _oauth_config(tmp_path, handle.mcp_url)

        before = _run(tmp_path, "status", config=config)
        assert before.returncode == 0, before.stderr
        assert re.search(r"logged in\s*:\s*no", before.stdout) and "drifter auth login remote-crm" in before.stdout

        process = await _start_login(tmp_path, config)
        url, early = await _read_authorization_url(process)
        await _browse(url)
        code, stdout, stderr = await _finish(process)
        assert code == 0, (stdout, stderr)
        assert "Logged in to 'remote-crm'" in stdout and re.search(r"scopes\s*:\s*read", stdout), stdout

        # the tokens are in the user config dir, and nowhere in the project
        store = TokenStore("remote-crm", handle.mcp_url, base_dir=_config_base(tmp_path), backend="file")
        tokens = await store.get_tokens()
        assert tokens is not None and tokens.access_token.startswith(handle.state.canary)
        secrets = handle.state.all_secrets()
        assert_no_leaks(
            [
                *scan_text("login stdout", stdout + "\n".join(early), secrets),
                *scan_text("login stderr", stderr, secrets),
                *scan_tree(tmp_path / "project", secrets),
            ]
        )

        status = _run(tmp_path, "status", config=config)
        assert status.returncode == 0, status.stderr
        for pattern in (r"auth\s*:\s*oauth", r"logged in\s*:\s*yes", r"refreshable\s*:\s*yes", r"scopes\s*:\s*read", r"expires\s*:"):
            assert re.search(pattern, status.stdout), (pattern, status.stdout)
        assert_no_leaks([*scan_text("status", status.stdout + status.stderr, secrets)])

        out = _run(tmp_path, "logout", "remote-crm", config=config)
        assert out.returncode == 0 and "Logged out of 'remote-crm'" in out.stdout, out
        assert "not revoked" in out.stdout  # honest: only the local copy is removed
        assert await store.get_tokens() is None
        again = _run(tmp_path, "logout", "remote-crm", config=config)
        assert again.returncode == 0 and "No stored login" in again.stdout
        assert re.search(r"logged in\s*:\s*no", _run(tmp_path, "status", config=config).stdout)


@pytest.mark.anyio
async def test_status_can_be_limited_to_one_server_and_lists_every_url_server_by_default(tmp_path):
    config = _write_config(
        tmp_path,
        '  - name: alpha\n    url: "https://a.example.com/mcp"\n    auth:\n      type: oauth\n'
        '  - name: beta\n    url: "https://b.example.com/mcp"\n    auth:\n      type: bearer_env\n      token_env: BETA_TOKEN\n'
        '  - name: local\n    command: ["python", "server.py"]\n',
    )
    everything = _run(tmp_path, "status", config=config)
    assert everything.returncode == 0, everything.stderr
    assert "alpha" in everything.stdout and "beta" in everything.stdout
    assert "bearer_env" in everything.stdout and "BETA_TOKEN" in everything.stdout
    assert "local" not in everything.stdout  # a stdio server has no login to describe
    only = _run(tmp_path, "status", "alpha", config=config)
    assert "alpha" in only.stdout and "beta" not in only.stdout


# --- servers that have no interactive login ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body, expected",
    [
        ('  - name: remote-crm\n    command: ["python", "server.py"]\n', ["local command", "url"]),
        ('  - name: remote-crm\n    url: "https://mcp.example.com/mcp"\n    auth:\n      type: bearer_env\n      token_env: CRM_TOKEN\n', ["bearer_env", "CRM_TOKEN", "no login"]),
        ('  - name: remote-crm\n    url: "https://mcp.example.com/mcp"\n', ["no `auth` block", "type: oauth"]),
        ('  - name: remote-crm\n    url: "https://mcp.example.com/mcp"\n    auth:\n      type: none\n', ["type: none", "oauth"]),
        ('  - name: remote-crm\n    url: "https://mcp.example.com/mcp"\n    auth:\n      type: client_credentials\n      client_id: c\n      secret_env: S\n', ["client_credentials", "interactive"]),
    ],
    ids=["stdio", "bearer_env", "no-auth-block", "type-none", "client_credentials"],
)
def test_login_explains_why_a_server_has_nothing_to_log_in_to(tmp_path, body, expected):
    result = _run(tmp_path, "login", "remote-crm", "--no-browser", config=_write_config(tmp_path, body))
    assert result.returncode == 4, (result.stdout, result.stderr)
    for fragment in expected:
        assert fragment in result.stderr, (fragment, result.stderr)


def test_an_unknown_server_name_lists_the_available_ones(tmp_path):
    config = _write_config(tmp_path, '  - name: alpha\n    url: "https://a.example.com/mcp"\n    auth:\n      type: oauth\n')
    for command in ("login", "logout"):
        result = _run(tmp_path, command, "nope", config=config)
        assert result.returncode == 4 and "alpha" in result.stderr and "nope" in result.stderr, result.stderr


def test_a_missing_config_is_exit_4(tmp_path):
    (tmp_path / "project").mkdir()
    result = _run(tmp_path, "status", config=tmp_path / "project" / "missing.yaml")
    assert result.returncode == 4 and "not found" in result.stderr


def test_status_never_prints_credentials_embedded_in_a_servers_url(tmp_path):
    config = _write_config(
        tmp_path, '  - name: alpha\n    url: "https://someone:hunter2-PASSWORD@a.example.com/mcp"\n    auth:\n      type: oauth\n'
    )
    result = _run(tmp_path, "status", config=config)
    assert result.returncode == 0, result.stderr
    assert "hunter2-PASSWORD" not in result.stdout + result.stderr and "someone" not in result.stdout
    assert "a.example.com" in result.stdout


@pytest.mark.parametrize("value", ["0", "-5", "nan", "inf"])
def test_a_timeout_that_cannot_work_is_refused_up_front(tmp_path, value):
    config = _oauth_config(tmp_path, "http://127.0.0.1:9/mcp")
    result = _run(tmp_path, "login", "remote-crm", "--no-browser", "--timeout", value, config=config)
    assert result.returncode == 4 and "--timeout" in result.stderr, (result.stdout, result.stderr)


# --- when the login does not complete ----------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_timeout_flag_ends_a_login_nobody_completes_with_exit_4(tmp_path):
    async with serve_oauth_fixture() as handle:
        config = _oauth_config(tmp_path, handle.mcp_url)
        started = time.monotonic()
        process = await _start_login(tmp_path, config, "--timeout", "1")
        await _read_authorization_url(process)  # printed; nobody opens it
        code, stdout, stderr = await _finish(process)
        assert code == 4 and "timed out" in stderr, (code, stderr)
        assert time.monotonic() - started < 20
        assert handle.state.token_grants == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal delivery; the Windows path is covered by the cancellation test")
@pytest.mark.anyio
async def test_ctrl_c_while_waiting_for_the_browser_exits_promptly_and_releases_the_port(tmp_path):
    """CLAUDE.md async-shutdown rule: the wait is 300 s by default. Timed, with a real SIGINT on a
    real process, and the loopback port checked afterwards."""
    async with serve_oauth_fixture() as handle:
        config = _oauth_config(tmp_path, handle.mcp_url)
        process = await _start_login(tmp_path, config)  # default timeout: 300 s
        url, _ = await _read_authorization_url(process)
        port = int(urlparse(parse_qs(urlparse(url).query)["redirect_uri"][0]).port)

        started = time.monotonic()
        process.send_signal(signal.SIGINT)
        code, stdout, stderr = await _finish(process, timeout=20)
        elapsed = time.monotonic() - started
        assert elapsed < 8, f"Ctrl+C took {elapsed:.1f}s to end the login"
        assert code == 130, (code, stderr)
        assert "cancelled" in (stdout + stderr).lower() and "Traceback" not in stderr, stderr

        import socket

        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=2).close()
        assert handle.state.token_grants == []  # nothing was exchanged
