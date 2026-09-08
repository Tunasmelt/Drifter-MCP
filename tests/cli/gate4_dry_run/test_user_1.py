"""Gate 4 pre-handoff dry run — test_user_1: happy path, multi-server.

IMPORTANT SCOPE NOTE, same for every file in this package: these are
synthetic personas used to stress-test the real CLI end to end BEFORE
handing the build to an actual second person (docs/PHASES.md Gate 4).
They do NOT satisfy Gate 4's exit test (which requires a real,
unassisted human) and must never be cited as having closed Gate 4 --
see docs/CHANGELOG.md's dry-run entry and `.drifter/GATE_STATUS`.

test_user_1 has an ordinary, working setup: two servers registered in
`.mcp.json` (one real, spawnable stdio server; one non-stdio `http`
entry Drifter's v0 can't drive), and an agent robust to
`description_update` (it calls tools by exact name/arguments, never by
description text). Exercises the full real pipeline: `drifter init` ->
`drifter observe` (a real recorded session, including one error call)
-> `drifter run` via the actual init-generated config, confirming a
clean comparison correctly reports NO_REGRESSION rather than a false
alarm.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from mcp_drifter.cli.init import run_init
from mcp_drifter.cli.run import run_run
from mcp_drifter.record.reader import read_session
from mcp_drifter.record.schema import ToolCall

FIXTURE_SERVER = str(Path(__file__).parent.parent.parent / "fixtures" / "fake_server.py")
SCRIPTED_AGENT = str(Path(__file__).parent.parent.parent / "fixtures" / "scripted_agent.py")


def _write_mcp_json(home: Path) -> None:
    (home / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "fake": {"command": sys.executable, "args": [FIXTURE_SERVER]},
                    "remote": {"type": "http", "url": "https://example.com/mcp"},
                }
            }
        ),
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
            await session.call_tool("add", {"a": 1, "b": 2})
            await session.call_tool("echo", {"payload": {"x": 1}})
            await session.call_tool("fail", {})  # exercises F-10's is_error path for real


def test_user_1_happy_path_end_to_end(tmp_path):
    home = tmp_path
    _write_mcp_json(home)

    # --- drifter init: real scan, real skip-reporting ----------------------
    config_path = home / "drifter.yaml"
    runs_dir = home / ".drifter" / "runs"
    run_init(output_path=config_path, search_root=home)

    written = config_path.read_text(encoding="utf-8")
    assert "fake" in written
    assert "remote" not in written  # the http entry must never be written as a server

    # Point the generated config's record dir at a scratch location and
    # append an agent block for the later `drifter run` step -- editing
    # the REAL init-generated file, not writing a fresh one from scratch,
    # since the point is proving init's own output is directly usable.
    config_text = written.replace(".drifter/runs", str(runs_dir.as_posix()))
    config_path.write_text(config_text, encoding="utf-8")

    # --- drifter observe: real subprocess, real recording -------------------
    anyio.run(_record_persona_session, config_path)

    session_files = list(runs_dir.glob("*.jsonl"))
    assert len(session_files) == 1
    fixture_path = session_files[0]
    recorded_calls = [r for r in read_session(fixture_path) if isinstance(r, ToolCall)]
    assert [c.tool_name for c in recorded_calls] == ["add", "echo", "fail"]
    assert recorded_calls[2].is_error is True

    # --- drifter run: via the real, edited, init-derived config -------------
    agent_command = json.dumps(
        [
            sys.executable,
            SCRIPTED_AGENT,
            "add|" + json.dumps({"a": 1, "b": 2}),
            "echo|" + json.dumps({"payload": {"x": 1}}),
            "fail|" + json.dumps({}),
        ]
    )
    config_text_with_agent = config_text + f"\nagent:\n  command: {agent_command}\n"
    config_path.write_text(config_text_with_agent, encoding="utf-8")

    import io

    out = io.StringIO()
    run_run(
        config_path=config_path,
        fixture=fixture_path,
        server_name="fake",
        task_id="test_user_1",
        operator="description_update",
        runs_dir=home / "run_sessions",
        repeats=3,  # 3 = calibration.min_valid_runs: the minimum-evidence gate (SPEC §15 limitation 16) refuses a verdict below it
        timeout_s=30.0,
        output_stream=out,
        assume_yes=True,  # F-31: no interactive stdin in a test
    )
    output = out.getvalue()
    assert "test_user_1" in output
    assert "NO_REGRESSION" in output
