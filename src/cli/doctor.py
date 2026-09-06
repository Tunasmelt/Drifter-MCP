"""`drifter doctor` (F-37), Gate 1 scope only: config parsing + server
connectivity. Classification-sanity checks (unclassified destructive
tools, missing task assertions) are explicitly later-gate scope per
docs/PHASES.md — they need `policy/` (F-26) and `tasks/` (F-33), neither of
which exists yet — and are not attempted here.

The point (CLAUDE.md, F-37's "Done when"): every common misconfiguration
gets a specific, actionable message instead of a raw stack trace. `drifter
observe` already wraps config errors this way (cli/app.py); this extends
the same idea to "the server is misconfigured but the config itself
parses" — a bad command, a command that isn't an MCP server at all, or one
that's simply slow to start.

`agent.mode: http` (F-38, docs/SPEC.md §5.1) check, added here rather than
left undiagnosable: confirms a loopback ephemeral port can actually be
bound (catching a real environment problem — a restrictive sandbox,
firewall, or exhausted local port range — before a real `drifter run`
wastes an agent invocation discovering it the hard way) and warns, without
failing, if `agent.env_var`'s name is already set in the CURRENT
environment — not fatal (`cli.subprocess_adapter.run_agent_subprocess_http`
overlays its own value on top regardless), but worth surfacing so a user
isn't silently surprised that an unrelated pre-existing variable got
overridden for the run.
"""

from __future__ import annotations

import os
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from cli.config import AgentConfig, ConfigError, ServerConfig, load_config
from record.calibration import load_calibration


@dataclass
class ServerCheck:
    name: str
    ok: bool
    detail: str


def _command_str(server: ServerConfig) -> str:
    return " ".join(server.command)


async def _check_server(server: ServerConfig, timeout_seconds: float) -> ServerCheck:
    """Spawns `server` and attempts a real MCP `initialize` handshake.

    Bounded by `timeout_seconds` for the whole attempt — a command that
    spawns but never speaks MCP (wrong executable, a plain shell command,
    a server hung at startup) would otherwise block doctor forever rather
    than reporting "unreachable." `stdio_client`'s own shutdown sequence
    (mcp/client/stdio.py) tears the subprocess down cleanly even when its
    caller is cancelled mid-handshake — every wait inside is bounded and
    shielded — so a `fail_after` timeout here doesn't leak the spawned
    process; verified directly in tests/cli/test_doctor.py with a
    real-subprocess reproduction, not assumed from reading the SDK.
    """
    try:
        with anyio.fail_after(timeout_seconds):
            params = StdioServerParameters(command=server.command[0], args=server.command[1:])
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
    except TimeoutError:
        return ServerCheck(
            server.name,
            False,
            f"no response to initialize within {timeout_seconds:.0f}s "
            f"(command: {_command_str(server)!r}) — is this an MCP server?",
        )
    except OSError as e:
        # stdio_client's own contract (its docstring): OSError if the
        # server process cannot be spawned at all — bad executable path,
        # not found on PATH, no permission to execute, etc.
        return ServerCheck(
            server.name,
            False,
            f"could not start command {_command_str(server)!r}: {e}",
        )
    except Exception as e:
        return ServerCheck(
            server.name,
            False,
            f"unexpected error talking to {server.name!r} ({_command_str(server)!r}): {e}",
        )
    return ServerCheck(server.name, True, "initialize handshake succeeded")


async def _check_all_servers(servers: list[ServerConfig], timeout_seconds: float) -> list[ServerCheck]:
    # Sequential, deliberately: Gate 1's real config has exactly one
    # server, and interleaved subprocess spawns/output would only make a
    # failure's cause harder to read for no real speed benefit here.
    return [await _check_server(server, timeout_seconds) for server in servers]


def _check_http_agent_mode(agent: AgentConfig) -> ServerCheck:
    """`agent.mode: http` (F-38) has no `servers[]`-style connectivity to
    check — there's no URL to dial yet, only a port Drifter itself will
    bind when a real `drifter run` starts. What CAN be verified now: that
    binding a loopback ephemeral port actually works in this environment
    (a real, fast, harmless probe — bind, then close immediately), and
    whether `agent.env_var`'s name is already set (a warning, not a
    failure: `run_agent_subprocess_http` overlays its own value on top of
    the real environment regardless — see that function's own docstring
    for why replacing rather than overlaying was the bug this fix
    corrected — so a collision is never silently broken, only silently
    surprising if the user isn't told).
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", 0))
        finally:
            sock.close()
    except OSError as e:
        return ServerCheck("agent (mode: http)", False, f"could not bind a loopback port: {e}")

    if agent.env_var in os.environ:
        return ServerCheck(
            "agent (mode: http)",
            True,
            f"loopback binding OK; note: ${agent.env_var} is already set in this "
            "environment and will be overridden for the run",
        )
    return ServerCheck("agent (mode: http)", True, "loopback binding OK")


def run_doctor(config_path: Path | None = None, output_stream: TextIO = sys.stdout) -> bool:
    """Runs every Gate 1 doctor check, printing PASS/FAIL per check.

    Returns True iff every check passed — cli/app.py uses this to pick the
    process exit code (docs/SPEC.md §12: exit code 4 = config/connectivity
    error).
    """
    display_path = config_path or Path("drifter.yaml")
    try:
        config = load_config(config_path)
    except ConfigError as e:
        output_stream.write(f"[FAIL] config ({display_path}): {e}\n")
        return False
    output_stream.write(f"[ OK ] config ({display_path}): parses, {len(config.servers)} server(s) declared\n")

    calibration = load_calibration()
    timeout_seconds = calibration.doctor.connectivity_timeout_seconds

    checks = anyio.run(_check_all_servers, config.servers, timeout_seconds)
    all_ok = True
    for check in checks:
        marker = "[ OK ]" if check.ok else "[FAIL]"
        output_stream.write(f"{marker} server {check.name!r}: {check.detail}\n")
        all_ok = all_ok and check.ok

    if config.agent is not None and config.agent.mode == "http":
        agent_check = _check_http_agent_mode(config.agent)
        marker = "[ OK ]" if agent_check.ok else "[FAIL]"
        output_stream.write(f"{marker} {agent_check.name}: {agent_check.detail}\n")
        all_ok = all_ok and agent_check.ok

    return all_ok
