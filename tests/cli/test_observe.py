"""Integration tests for `drifter observe` (F-09).

The safety-critical property this module exists to check: live terminal
feedback must never leak onto stdout, which is the actual MCP wire
protocol channel to the agent. A successful ClientSession exchange is
already fairly strong implicit evidence of that (corrupted JSON-RPC
framing would surface as parse errors), but this test also captures the
raw wire-level stream directly and asserts zero parse-error entries,
rather than relying on that being merely implied by other assertions
passing.
"""

import io
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from cli.config import ConfigError
from cli.observe import LiveStatus, handle_sigint, select_server
from record.reader import read_session
from record.schema import ToolCall, TrajectoryEnd

FIXTURE_SERVER = str(Path(__file__).parent.parent / "fixtures" / "fake_server.py")


@asynccontextmanager
async def _tap_reads(source):
    """Re-exposes `source` unchanged, while recording every parse-error
    entry (a stray non-JSON-RPC line on stdout would surface as one of
    these) seen in arrival order. Sits between the raw transport and
    ClientSession, which must still see every message normally —
    matching tests/record/test_proxy.py's tap of the same shape.
    """
    send, receive = anyio.create_memory_object_stream(0)
    parse_errors: list[Exception] = []

    async def _forward() -> None:
        try:
            async with source:
                async for message in source:
                    if isinstance(message, Exception):
                        parse_errors.append(message)
                    await send.send(message)
        finally:
            await send.aclose()

    async with anyio.create_task_group() as tg:
        tg.start_soon(_forward)
        try:
            yield receive, parse_errors
        finally:
            tg.cancel_scope.cancel()


def _drifter_yaml(tmp_path: Path, runs_dir: Path, servers: list[tuple[str, list[str]]]) -> Path:
    # Single-quoted YAML scalars, deliberately: sys.executable on Windows
    # contains backslashes, which double-quoted YAML strings would try to
    # interpret as escape sequences (\U... etc.) and fail to parse.
    # Single-quoted scalars don't process escapes at all (the only
    # special case is '' for a literal quote, irrelevant to paths here).
    lines = ["version: 1", "servers:"]
    for name, command in servers:
        command_yaml = ", ".join(f"'{part}'" for part in command)
        lines.append(f"  - name: {name}")
        lines.append(f"    command: [{command_yaml}]")
    lines.append("record:")
    lines.append(f"  dir: {runs_dir.as_posix()}")
    config_path = tmp_path / "drifter.yaml"
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return config_path


# --- select_server: pure unit tests, no subprocess needed -----------------


class _FakeServer:
    def __init__(self, name):
        self.name = name


class _FakeConfig:
    def __init__(self, names):
        self.servers = [_FakeServer(n) for n in names]


def test_select_server_infers_the_only_server():
    server = select_server(_FakeConfig(["crm"]), server_name=None)
    assert server.name == "crm"


def test_select_server_by_name_among_several():
    server = select_server(_FakeConfig(["crm", "billing"]), server_name="billing")
    assert server.name == "billing"


def test_select_server_requires_name_when_ambiguous():
    with pytest.raises(ConfigError, match="declares 2 servers"):
        select_server(_FakeConfig(["crm", "billing"]), server_name=None)


def test_select_server_unknown_name_raises_config_error():
    with pytest.raises(ConfigError, match="no server named 'nope'"):
        select_server(_FakeConfig(["crm"]), server_name="nope")


def test_handle_sigint_flushes_open_trajectory_and_exits(tmp_path):
    """Verifies handle_sigint's real behavior at the Python level —
    recorder.close() correctly flushes an IN-PROGRESS trajectory
    (TrajectoryEnd, not just SessionStart), status.finish() runs, and
    exit_fn is called with 0 — without depending on OS signal delivery,
    which this fix's own investigation found unreliable in a sandboxed
    shell environment (see handle_sigint's docstring for the full
    investigation). exit_fn is mocked so this test can't kill itself.
    """
    from mcp.shared.message import SessionMessage
    from mcp_types import JSONRPCRequest, JSONRPCResponse

    from record.proxy import Direction
    from record.writer import SessionRecorder

    runs_dir, raw_dir = tmp_path / "runs", tmp_path / "raw"
    recorder = SessionRecorder(session_dir=runs_dir, raw_dir=raw_dir, server_name="fake")

    # Drive a real tools/call request+response through the recorder
    # directly, opening a trajectory — simulating a session that's live
    # (not idle-empty) when Ctrl+C fires.
    request = JSONRPCRequest(jsonrpc="2.0", id=1, method="tools/call", params={"name": "add", "arguments": {"a": 1, "b": 2}})
    response = JSONRPCResponse(jsonrpc="2.0", id=1, result={"content": [], "structuredContent": {"result": 3}})
    recorder.observe(Direction.AGENT_TO_SERVER, SessionMessage(request))
    recorder.observe(Direction.SERVER_TO_AGENT, SessionMessage(response))

    status_stream = io.StringIO()
    status = LiveStatus(status_stream)
    exit_calls = []

    handle_sigint(recorder, status, status_stream, exit_fn=lambda code: exit_calls.append(code))

    assert exit_calls == [0]  # exit_fn called exactly once, with 0
    assert "stopping (Ctrl+C)" in status_stream.getvalue()

    jsonl_files = list(runs_dir.glob("*.jsonl"))
    assert len(jsonl_files) == 1
    records = list(read_session(jsonl_files[0]))

    call_records = [r for r in records if isinstance(r, ToolCall)]
    assert len(call_records) == 1

    # The in-progress trajectory must be flushed, not silently lost —
    # this is exactly what a hang (the bug this fix addresses) would
    # have prevented, since close_all() would never run.
    trajectory_ends = [r for r in records if isinstance(r, TrajectoryEnd)]
    assert len(trajectory_ends) == 1
    assert trajectory_ends[0].call_seqs == [call_records[0].seq]

    handle_sigint(recorder, status, status_stream, exit_fn=lambda code: exit_calls.append(code))  # rapid double Ctrl+C: must be a no-op, not raise or re-fire
    assert exit_calls == [0] and list(read_session(jsonl_files[0])) == records


# --- integration: real subprocess, real recording, stdout safety ----------


@pytest.mark.anyio
async def test_observe_records_a_session_and_keeps_status_off_stdout(tmp_path):
    runs_dir = tmp_path / "runs"
    config_path = _drifter_yaml(tmp_path, runs_dir, [("fake", [sys.executable, FIXTURE_SERVER])])
    stderr_path = tmp_path / "stderr.log"

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "cli", "observe", "--config", str(config_path), "--server", "fake"],
    )

    with stderr_path.open("w", encoding="utf-8") as errlog:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with _tap_reads(read) as (tapped_read, parse_errors):
                async with ClientSession(tapped_read, write) as session:
                    await session.initialize()
                    await session.call_tool("add", {"a": 1, "b": 2})
                    await session.call_tool("echo", {"payload": {"x": 1}})

    # A stray status line landing on stdout would surface here as a
    # parse-error entry — not just as a failed call above.
    assert parse_errors == []

    # Recording actually happened.
    jsonl_files = list(runs_dir.glob("*.jsonl"))
    assert len(jsonl_files) == 1
    records = list(read_session(jsonl_files[0]))
    call_records = [r for r in records if isinstance(r, ToolCall)]
    assert {c.tool_name for c in call_records} == {"add", "echo"}

    # Live feedback landed on stderr, with the counts F-09 asks for.
    stderr_text = stderr_path.read_text(encoding="utf-8")
    assert "trajectories:" in stderr_text
    assert "calls: 2" in stderr_text  # final state: both calls counted
    assert "errors: 0" in stderr_text


@pytest.mark.anyio
async def test_observe_env_override_keeps_the_real_dot_drifter_untouched(tmp_path):
    """DRIFTER_RUNS_DIR/DRIFTER_RAW_DIR must take precedence over
    drifter.yaml's record.dir, exactly like record/__main__.py already
    does — found missing during Gate 0 item 5's dogfood-pairing
    verification, where a smoke test's session data landed in the repo's
    real .drifter/ instead of a scratch directory because run_observe()
    had no override at all.

    drifter.yaml here deliberately does NOT set record.dir, so it falls
    back to the real ".drifter/runs" default relative to cwd (repo root,
    since the subprocess isn't given an explicit cwd) — the exact
    scenario that leaked before. The real .drifter/runs and .drifter/raw
    are snapshotted before and compared after, rather than assumed empty
    or nonexistent: by the time this test runs, real trial data may
    already be sitting there, and this test must neither be confused by
    it nor, especially, ever delete or overwrite it.
    """
    repo_root = Path(__file__).resolve().parents[2]
    real_runs_dir = repo_root / ".drifter" / "runs"
    real_raw_dir = repo_root / ".drifter" / "raw"

    def _snapshot(path: Path) -> set[str]:
        return {p.name for p in path.iterdir()} if path.exists() else set()

    before_runs, before_raw = _snapshot(real_runs_dir), _snapshot(real_raw_dir)

    config_lines = [
        "version: 1",
        "servers:",
        "  - name: fake",
        f"    command: ['{sys.executable}', '{FIXTURE_SERVER}']",
        # No `record:` block at all — must fall back to the real
        # ".drifter/runs" default, which the env vars below must override.
    ]
    config_path = tmp_path / "drifter.yaml"
    config_path.write_text("\n".join(config_lines) + "\n", encoding="utf-8")

    scratch_runs, scratch_raw = tmp_path / "scratch_runs", tmp_path / "scratch_raw"
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "cli", "observe", "--config", str(config_path), "--server", "fake"],
        env={"DRIFTER_RUNS_DIR": str(scratch_runs), "DRIFTER_RAW_DIR": str(scratch_raw)},
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            await session.call_tool("add", {"a": 1, "b": 2})

    # Positive check: the override actually worked.
    assert len(list(scratch_runs.glob("*.jsonl"))) == 1
    assert len(list(scratch_raw.glob("*.frames"))) == 1

    # Negative check: the real path is exactly as it was before — no
    # new files, and nothing pre-existing (e.g. real trial data)
    # disturbed.
    assert _snapshot(real_runs_dir) == before_runs
    assert _snapshot(real_raw_dir) == before_raw


@pytest.fixture
def anyio_backend():
    return "asyncio"
