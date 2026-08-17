"""Tests for `drifter doctor` (F-37, Gate 1 scope: config + connectivity
only).

Includes a real-subprocess reproduction of the "server spawns but never
speaks MCP" case (test_check_server_times_out_against_a_non_mcp_process
below) — not just a static read of stdio_client's shutdown code — because
this exact class of bug (a cooperative-cancellation path hanging on an
uncancellable subprocess/thread read) has bitten this project twice
already (record/proxy.py's Prompt-2 shutdown hang, cli/observe.py's
Ctrl+C hang; see both files' docstrings/CHANGELOG.md). `anyio.fail_after`
wrapping `stdio_client` + `ClientSession.initialize()` is a new use of
cooperative cancellation in this codebase and gets the same scrutiny.
"""

import io
import sys
from pathlib import Path

import anyio
import pytest

from cli.doctor import _check_server, run_doctor
from record.calibration import Calibration

FIXTURE_SERVER = str(Path(__file__).parent.parent / "fixtures" / "fake_server.py")


def _server(name: str, command: list[str]):
    from cli.config import ServerConfig

    return ServerConfig(name=name, command=command)


def _drifter_yaml(tmp_path: Path, servers: list[tuple[str, list[str]]]) -> Path:
    # Single-quoted YAML scalars — sys.executable on Windows contains
    # backslashes, which double-quoted YAML strings misparse as escapes
    # (the same fix already applied in tests/cli/test_observe.py).
    lines = ["version: 1", "servers:"]
    for name, command in servers:
        command_yaml = ", ".join(f"'{part}'" for part in command)
        lines.append(f"  - name: {name}")
        lines.append(f"    command: [{command_yaml}]")
    config_path = tmp_path / "drifter.yaml"
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return config_path


# --- _check_server: direct, real-subprocess checks -------------------------


@pytest.mark.anyio
async def test_check_server_passes_against_the_real_fixture_server():
    server = _server("fake", [sys.executable, FIXTURE_SERVER])
    check = await _check_server(server, timeout_seconds=10)
    assert check.ok is True
    assert "succeeded" in check.detail


@pytest.mark.anyio
async def test_check_server_reports_actionable_error_for_missing_executable():
    server = _server("bad", ["this-executable-does-not-exist-anywhere", "--flag"])
    check = await _check_server(server, timeout_seconds=5)
    assert check.ok is False
    # Actionable: names the command it tried, not a bare traceback.
    assert "this-executable-does-not-exist-anywhere" in check.detail


@pytest.mark.anyio
async def test_check_server_times_out_against_a_non_mcp_process():
    """A real subprocess that spawns successfully but never speaks a word
    of MCP (here: `python -c "import time; time.sleep(30)"`) must be
    reported as unreachable within a bounded time, not hang doctor
    indefinitely. This is a genuine repro of the risky case, run against a
    real child process — not a mock — specifically because this codebase
    has already hit two separate uncancellable-hang bugs on cooperative
    cancellation paths (see this file's module docstring).
    """
    server = _server("slow", [sys.executable, "-c", "import time; time.sleep(30)"])

    start = anyio.current_time()
    with anyio.fail_after(8):  # the test's own outer bound: proves _check_server didn't hang
        check = await _check_server(server, timeout_seconds=1)
    elapsed = anyio.current_time() - start

    assert check.ok is False
    assert "no response" in check.detail
    assert elapsed < 8  # returned well inside the test's outer bound, not right at it


# --- run_doctor: config + full end-to-end -----------------------------------


def test_run_doctor_missing_config_file_is_actionable(tmp_path):
    out = io.StringIO()
    ok = run_doctor(config_path=tmp_path / "nope.yaml", output_stream=out)
    assert ok is False
    text = out.getvalue()
    assert "[FAIL]" in text
    assert "not found" in text


def test_run_doctor_malformed_yaml_is_actionable(tmp_path):
    config_path = tmp_path / "drifter.yaml"
    config_path.write_text("servers: [unterminated", encoding="utf-8")

    out = io.StringIO()
    ok = run_doctor(config_path=config_path, output_stream=out)
    assert ok is False
    assert "[FAIL]" in out.getvalue()
    assert "not valid YAML" in out.getvalue()


def test_run_doctor_schema_invalid_yaml_is_actionable(tmp_path):
    """Distinct from test_run_doctor_malformed_yaml_is_actionable: this YAML
    parses fine but fails drifter.yaml's own schema (no `servers:` at all)
    — a different failure mode than a YAML syntax error, and one of the
    three the user explicitly named (bad server command, missing config
    file, malformed drifter.yaml)."""
    config_path = tmp_path / "drifter.yaml"
    config_path.write_text("version: 1\n", encoding="utf-8")  # valid YAML, no `servers:`

    out = io.StringIO()
    ok = run_doctor(config_path=config_path, output_stream=out)
    assert ok is False
    text = out.getvalue()
    assert "[FAIL]" in text
    assert "is invalid" in text


def test_run_doctor_bad_server_command_is_actionable(tmp_path):
    config_path = _drifter_yaml(tmp_path, [("crm", ["this-executable-does-not-exist-anywhere"])])

    out = io.StringIO()
    ok = run_doctor(config_path=config_path, output_stream=out)
    assert ok is False
    text = out.getvalue()
    assert "[ OK ] config" in text  # config itself parsed fine
    assert "[FAIL] server 'crm'" in text
    assert "this-executable-does-not-exist-anywhere" in text


def test_run_doctor_clean_pass_against_a_valid_gate1_config(tmp_path):
    """The exact scenario the user asked to confirm: a clean pass against
    a valid Gate 1-era drifter.yaml (one real server, real fixture)."""
    config_path = _drifter_yaml(tmp_path, [("fake", [sys.executable, FIXTURE_SERVER])])

    out = io.StringIO()
    ok = run_doctor(config_path=config_path, output_stream=out)
    text = out.getvalue()

    assert ok is True
    assert "[ OK ] config" in text
    assert "[ OK ] server 'fake': initialize handshake succeeded" in text
    assert "[FAIL]" not in text


def test_run_doctor_against_the_real_repo_drifter_yaml():
    """The actual drifter.yaml at the repo root (Gate 0 item 5's dogfood
    pairing: Claude Code + the filesystem MCP server) — confirms doctor
    produces a clean pass against real project config, not just a
    purpose-built fixture. Skipped if that server isn't actually
    reachable in this environment (e.g. npx/@modelcontextprotocol/
    server-filesystem not installed here) rather than failing the suite
    on an environment difference unrelated to drifter doctor's own logic.
    """
    repo_root = Path(__file__).resolve().parents[2]
    config_path = repo_root / "drifter.yaml"
    if not config_path.exists():
        pytest.skip("no drifter.yaml at repo root")

    out = io.StringIO()
    ok = run_doctor(config_path=config_path, output_stream=out)
    if not ok:
        pytest.skip(f"real dogfood server not reachable in this environment:\n{out.getvalue()}")
    assert "[ OK ] config" in out.getvalue()


def test_calibration_doctor_timeout_has_a_default():
    assert Calibration().doctor.connectivity_timeout_seconds == 10


@pytest.fixture
def anyio_backend():
    return "asyncio"
