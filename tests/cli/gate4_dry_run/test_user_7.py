"""Gate 4 pre-handoff dry run — test_user_7: fidelity below the floor
correctly reports UNKNOWN through the full CLI pipeline, not a false
NO_REGRESSION/REGRESSION.

See tests/cli/gate4_dry_run/test_user_1.py's module docstring for the
shared scope note (dry run, not Gate 4 closure).

This is a permanent regression test for Gate 3's single biggest real
finding (docs/PHASES.md's Status section): a real agent whose behavior
doesn't line up cleanly with what was recorded produces low exact-tier
fidelity, and the harness must say UNKNOWN rather than guess. Built
here as a fully deterministic, synthetic reproduction (mostly-missing
calls against the replayed fixture) of that same shape, so it runs in
milliseconds instead of requiring a real, costly live-agent session
every time this behavior needs re-confirming.
"""

from __future__ import annotations

import io
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
            await session.list_tools()
            await session.call_tool("add", {"a": 1, "b": 2})


def test_user_7_low_fidelity_reports_unknown_not_a_false_verdict(tmp_path):
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
    recorded_calls = [r for r in read_session(fixture_path) if isinstance(r, ToolCall)]
    assert [c.tool_name for c in recorded_calls] == ["add"]

    # 1 exact hit (add/{a:1,b:2}, matching what was recorded) + 3 real
    # misses (never-recorded tool/argument combinations against this
    # fixture) = 1/4 = 0.25 fidelity, well below calibration.yaml's 0.70
    # floor -- deterministic, no real agent variance needed to trigger
    # this, matching the real thing's actual failure shape exactly.
    agent_command = json.dumps(
        [
            sys.executable,
            SCRIPTED_AGENT,
            "add|" + json.dumps({"a": 1, "b": 2}),  # real hit
            "add|" + json.dumps({"a": 99, "b": 99}),  # miss: never recorded with these args
            "echo|" + json.dumps({"payload": {"never": "recorded"}}),  # miss
            "fail|" + json.dumps({"message": "never recorded either"}),  # miss
        ]
    )
    config_text_with_agent = config_text + f"\nagent:\n  command: {agent_command}\n"
    config_path.write_text(config_text_with_agent, encoding="utf-8")

    out = io.StringIO()
    run_run(
        config_path=config_path,
        fixture=fixture_path,
        server_name="fake",
        task_id="test_user_7",
        operator="description_update",
        runs_dir=home / "run_sessions",
        repeats=1,
        timeout_s=30.0,
        output_stream=out,
        assume_yes=True,  # F-31: no interactive stdin in a test
    )
    output = out.getvalue()
    assert "test_user_7" in output
    assert "UNKNOWN" in output
    assert "REGRESSION" not in output  # neither NO_REGRESSION nor REGRESSION -- a real UNKNOWN, not disguised
