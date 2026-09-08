"""Gate 4 pre-handoff dry run — test_user_5: the full init -> observe ->
replay-serve loop, closed end to end with the persona's OWN recorded
session as the fixture -- not the pre-existing golden fixture every
other `replay-serve` test uses (tests/cli/test_replay_serve.py).

See tests/cli/gate4_dry_run/test_user_1.py's module docstring for the
shared scope note (dry run, not Gate 4 closure).

`drifter replay-serve` is a structurally different code path from
`drifter run` (cli/replay_serve.py's own docstring: real OS stdio via
`mcp.server.stdio.stdio_server()`, the NORMAL "client spawns its
configured server" MCP relationship a real agent's `mcp.json` would
use -- not `run`'s in-process wiring via subprocess_adapter). This
persona is the first test anywhere (existing coverage in
tests/cli/test_replay_serve.py always replays the pre-built golden
fixture) to prove a session THIS persona just recorded live can be
served back to a second, independent real client connection.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from mcp_drifter.cli.init import run_init
from mcp_drifter.record.reader import read_session
from mcp_drifter.record.schema import ToolCall

FIXTURE_SERVER = str(Path(__file__).parent.parent.parent / "fixtures" / "fake_server.py")


def _write_mcp_json(home: Path) -> None:
    (home / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"fake": {"command": sys.executable, "args": [FIXTURE_SERVER]}}}),
        encoding="utf-8",
    )


async def _record_persona_session(config_path: Path) -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_drifter.cli", "observe", "--config", str(config_path), "--server", "fake"],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            await session.call_tool("add", {"a": 5, "b": 7})
            await session.call_tool("echo", {"payload": {"ok": True}})


async def _replay_and_verify(fixture_path: Path, replayed_runs_dir: Path) -> list[str]:
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m", "mcp_drifter.cli", "replay-serve",
            "--fixture", str(fixture_path),
            "--server", "fake",
            "--runs-dir", str(replayed_runs_dir),
        ],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            add_result = await session.call_tool("add", {"a": 5, "b": 7})
            echo_result = await session.call_tool("echo", {"payload": {"ok": True}})
            assert add_result.is_error is False
            assert echo_result.is_error is False
            return sorted(t.name for t in tools.tools)


def test_user_5_replay_serve_closes_the_loop_on_a_session_this_persona_just_recorded(tmp_path):
    home = tmp_path
    _write_mcp_json(home)

    config_path = home / "drifter.yaml"
    run_init(output_path=config_path, search_root=home)

    runs_dir = home / ".drifter" / "runs"
    config_text = config_path.read_text(encoding="utf-8").replace(".drifter/runs", str(runs_dir.as_posix()))
    config_path.write_text(config_text, encoding="utf-8")

    anyio.run(_record_persona_session, config_path)

    session_files = list(runs_dir.glob("*.jsonl"))
    assert len(session_files) == 1
    fixture_path = session_files[0]

    replayed_runs_dir = home / "replayed"
    served_tool_names = anyio.run(_replay_and_verify, fixture_path, replayed_runs_dir)
    assert served_tool_names == sorted(["add", "echo", "fail"])

    # The replay itself produced its own, separate recorded session --
    # replay-serve is not a read-only viewer, it's a real server a
    # second client connected to.
    replayed_files = list(replayed_runs_dir.glob("*.jsonl"))
    assert len(replayed_files) == 1
    replayed_calls = [r for r in read_session(replayed_files[0]) if isinstance(r, ToolCall)]
    assert [c.tool_name for c in replayed_calls] == ["add", "echo"]
    assert all(c.result_provenance == "real" for c in replayed_calls)  # exact-tier hits, not synthesized
