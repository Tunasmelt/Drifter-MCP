"""`drifter doctor` (F-37): config parsing + server connectivity (Gate 1),
`agent.mode: http` loopback-binding check (F-38), and tool risk
classification (F-26, this round). Missing-task-assertion checks are still
later-gate scope per docs/PHASES.md — `tasks/` (F-33's full spec) doesn't
exist yet.

F-26's own "Done when" bar: `drifter doctor` surfaces every ambiguous
("unknown") classification for one-time user confirmation. Literal
"before any live-mode run is possible" blocking isn't wired here — no
live-mode invocation path exists yet in this codebase at all (F-31/F-32
are still unbuilt) — so this is surfaced as a visible report, not (yet) a
hard gate; a real, narrower-than-spec scope decision, not silently
dropped. `doctor` connects a SECOND time per server (after the existing
connectivity check already succeeded) specifically to call `tools/list`
and classify the result — a real, accepted cost (a `drifter doctor` run
is infrequent, not a hot path) rather than reworking `_check_server`'s
existing, already-tested connect-and-`initialize`-only contract.

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
import httpx2
from mcp import ClientSession

from cli.config import (
    AgentConfig,
    ConfigError,
    DrifterConfig,
    PolicyConfig,
    ServerConfig,
    load_config,
    server_target,
)
from cli.stats import resolve_runs_dir
from policy.classify import Classification, classify_manifest
from record.calibration import Calibration, load_calibration
from record.proxy import connect_to_server
from record.schema import ToolDescriptor
from replay.coverage import estimate_coverage


@dataclass
class ServerCheck:
    name: str
    ok: bool
    detail: str


def _describe_server(server: ServerConfig) -> str:
    """F-39: a `url`-configured server has no `command` to join -- joining
    `None` would raise `TypeError`, not report a useful message. This is
    the one place that distinction is made for `doctor`'s own messages,
    matching `cli/observe.py`'s identical `description` derivation."""
    return server.url if server.url is not None else " ".join(server.command)


async def _check_server(server: ServerConfig, timeout_seconds: float) -> ServerCheck:
    """Connects to `server` (stdio or, F-39, a `url`-configured Streamable
    HTTP endpoint) and attempts a real MCP `initialize` handshake.

    Bounded by `timeout_seconds` for the whole attempt — a command that
    spawns (or a URL that accepts a connection) but never speaks MCP
    (wrong executable, a plain shell command, a server hung at startup, an
    unrelated HTTP service on that URL) would otherwise block doctor
    forever rather than reporting "unreachable." `stdio_client`'s own
    shutdown sequence (mcp/client/stdio.py) tears the subprocess down
    cleanly even when its caller is cancelled mid-handshake — every wait
    inside is bounded and shielded — so a `fail_after` timeout here
    doesn't leak the spawned process; verified directly in
    tests/cli/test_doctor.py with a real-subprocess reproduction, not
    assumed from reading the SDK.
    """
    # `except*` (PEP 654), not plain `except`, throughout this try: an
    # httpx2 connectivity error (the F-39/url case) arrives wrapped in an
    # ExceptionGroup, not bare — confirmed empirically before writing this,
    # not assumed — since streamable_http_client only actually attempts a
    # connection once a real request is sent, deep inside the task group
    # `connect_to_server`/`ClientSession.initialize` opens; `except*`
    # handles both that and a plain (stdio-case) exception uniformly. PEP
    # 654 forbids `return` inside an `except*` block, so each clause sets
    # `failure` instead, returned once, after the try/except finishes.
    failure: ServerCheck | None = None
    try:
        with anyio.fail_after(timeout_seconds):
            async with connect_to_server(server_target(server)) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
    except* TimeoutError:
        failure = ServerCheck(
            server.name,
            False,
            f"no response to initialize within {timeout_seconds:.0f}s "
            f"({_describe_server(server)!r}) — is this an MCP server?",
        )
    except* OSError as eg:
        # stdio_client's own contract (its docstring): OSError if the
        # server process cannot be spawned at all — bad executable path,
        # not found on PATH, no permission to execute, etc.
        failure = ServerCheck(
            server.name,
            False,
            f"could not start command {_describe_server(server)!r}: {eg.exceptions[0]}",
        )
    except* httpx2.TransportError as eg:
        failure = ServerCheck(
            server.name,
            False,
            f"could not connect to {_describe_server(server)!r}: {eg.exceptions[0]}",
        )
    except* Exception as eg:
        failure = ServerCheck(
            server.name,
            False,
            f"unexpected error talking to {server.name!r} ({_describe_server(server)!r}): {eg.exceptions[0]}",
        )
    if failure is not None:
        return failure
    return ServerCheck(server.name, True, "initialize handshake succeeded")


async def _fetch_manifest(server: ServerConfig, timeout_seconds: float) -> list[ToolDescriptor] | None:
    """A real `tools/list` call, wire shape (camelCase `annotations`,
    matching `record/writer.py`'s own `_write_tools_list` capture) fed
    straight into a `ToolDescriptor` — F-26's classification input. `None`
    on any failure: connectivity already has its own, separately-reported
    check (`_check_server`) — this function's caller only invokes it after
    that one already succeeded, so a failure here would be a DIFFERENT,
    rarer problem (e.g. the server dropped between the two connections);
    reported as its own classification-section failure line, not silently
    swallowed, but without re-deriving `_check_server`'s own actionable
    per-failure-type messages a second time.
    """
    try:
        with anyio.fail_after(timeout_seconds):
            async with connect_to_server(server_target(server)) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    return [
                        ToolDescriptor(
                            name=t.name,
                            description=t.description or "",
                            input_schema=t.input_schema or {},
                            annotations=t.annotations.model_dump(mode="json", by_alias=True, exclude_none=True)
                            if t.annotations is not None
                            else None,
                        )
                        for t in result.tools
                    ]
    except Exception:
        return None


def _classify_server(server: ServerConfig, tools: list[ToolDescriptor], policy: PolicyConfig) -> dict[str, Classification]:
    return classify_manifest(tools, destructive_override=policy.destructive)


async def _check_and_classify_all(
    servers: list[ServerConfig], timeout_seconds: float, policy: PolicyConfig
) -> list[tuple[ServerCheck, dict[str, Classification] | None]]:
    """One `anyio.run` for the whole doctor pass (connectivity + F-26
    classification together), rather than a separate `anyio.run` call per
    concern — classification is only even attempted for a server whose
    connectivity check already passed, and `None` distinguishes "not
    attempted, server unreachable" from "attempted, manifest fetch itself
    failed" (an empty dict) for `run_doctor`'s own reporting.

    Sequential, deliberately (kept from the previous connectivity-only
    version of this loop): Gate 1's real config has exactly one server,
    and interleaved subprocess spawns/output would only make a failure's
    cause harder to read for no real speed benefit here.
    """
    results: list[tuple[ServerCheck, dict[str, Classification] | None]] = []
    for server in servers:
        check = await _check_server(server, timeout_seconds)
        if not check.ok:
            results.append((check, None))
            continue
        tools = await _fetch_manifest(server, timeout_seconds)
        classifications = _classify_server(server, tools, policy) if tools is not None else None
        results.append((check, classifications))
    return results


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

    results = anyio.run(_check_and_classify_all, config.servers, timeout_seconds, config.policy)
    all_ok = True
    for check, classifications in results:
        marker = "[ OK ]" if check.ok else "[FAIL]"
        output_stream.write(f"{marker} server {check.name!r}: {check.detail}\n")
        all_ok = all_ok and check.ok

        # F-26: only attempted when connectivity already succeeded (see
        # _check_and_classify_all) — reported as its own line(s), never
        # counted against all_ok (docs/PHASES.md's F-26 note: no live-mode
        # gate exists yet to actually block on this, only a surfaced
        # review, matching the http-agent-mode env_var-collision warning's
        # own precedent just above for "worth surfacing, not fatal").
        if check.ok and classifications is None:
            output_stream.write(f"[WARN] server {check.name!r}: could not fetch tool manifest for classification\n")
        elif classifications is not None:
            unresolved = sorted(name for name, c in classifications.items() if c.source == "unresolved")
            if unresolved:
                output_stream.write(
                    f"[WARN] server {check.name!r}: {len(unresolved)} of {len(classifications)} tool(s) have "
                    f"unresolved risk classification — review before live-mode use: {', '.join(unresolved)}\n"
                )
            else:
                output_stream.write(f"[ OK ] server {check.name!r}: all {len(classifications)} tool(s) classified\n")

    if config.agent is not None and config.agent.mode == "http":
        agent_check = _check_http_agent_mode(config.agent)
        marker = "[ OK ]" if agent_check.ok else "[FAIL]"
        output_stream.write(f"{marker} {agent_check.name}: {agent_check.detail}\n")
        all_ok = all_ok and agent_check.ok

    _report_replay_coverage(config, calibration, output_stream)

    return all_ok


def _report_replay_coverage(config: DrifterConfig, calibration: Calibration, output_stream: TextIO) -> None:
    """DEC-027(c): projected replay coverage per configured server, from the
    recorded corpus alone (docs/CHANGELOG.md).

    Reported as [WARN]/[ OK ]/[INFO] and deliberately NEVER counted against
    `run_doctor`'s own return value: a thin corpus is a real, actionable
    finding but not a broken configuration, and doctor's boolean drives
    docs/SPEC.md §12's exit code 4 ("config/connectivity error"), which this
    is not. Same "surfaced, not fatal" precedent F-26's unresolved-
    classification warning above already set.

    Doctor is where this belongs for standalone use: `drifter run` shows the
    same estimate in its own pre-flight, but only once a user is already
    committed to a run and has a fixture argument in hand. A user asking
    "is my setup ready?" should be able to learn their corpus can't support
    a verdict without constructing a run to find out.
    """
    runs_dir = resolve_runs_dir(config)
    if not runs_dir.exists():
        output_stream.write(f"[INFO] replay corpus: nothing recorded yet at {runs_dir} — run `drifter observe` first\n")
        return

    session_paths = sorted(runs_dir.glob("*.jsonl"))
    for server in config.servers:
        estimate = estimate_coverage(session_paths, server.name)
        if not estimate.estimable:
            output_stream.write(f"[INFO] replay coverage {server.name!r}: {estimate.reason}\n")
            continue
        coverage = estimate.coverage or 0.0
        marker = "[WARN]" if coverage < calibration.fidelity_floor else "[ OK ]"
        detail = (
            f"~{coverage * 100:.0f}% projected from {estimate.sessions} recorded session(s) "
            f"({estimate.misses}/{estimate.total_calls} calls would MISS)"
        )
        if coverage < calibration.fidelity_floor:
            detail += (
                f" — below the {calibration.fidelity_floor:.2f} fidelity floor, so most "
                f"`drifter run` repeats would be excluded and the verdict UNKNOWN"
            )
        output_stream.write(f"{marker} replay coverage {server.name!r}: {detail}\n")
