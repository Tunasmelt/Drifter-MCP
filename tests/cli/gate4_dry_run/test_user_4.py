"""Gate 4 pre-handoff dry run — test_user_4: real `tool_addition`
regression through the full CLI pipeline.

See tests/cli/gate4_dry_run/test_user_1.py's module docstring for the
shared scope note (dry run, not Gate 4 closure).

`tool_addition` (mutate/tool_addition.py's add_tool) always APPENDS its
injected tool to the end of the served manifest -- confirmed by reading
cli/run.py's own orchestration (`mutated_tools = [*original_tools,
new_tool]`), not assumed. test_user_4's scripted agent uses the new
`LAST_TOOL` mode (tests/fixtures/scripted_agent.py): it always calls
whichever tool is last in `list_tools()`'s response. Pre-mutation, that
is the real `list_items` tool; post-mutation, it's whichever archetype
`add_tool` injected -- a real, agent-observable wrong-tool-called
regression, and the first end-to-end (not unit-level) coverage of
`tool_addition` in this project outside `tests/mutate/`.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from cli.init import run_init
from cli.run import run_run
from record.reader import read_session
from record.schema import ToolCall

ANNOTATED_SERVER = str(Path(__file__).parent.parent.parent / "fixtures" / "fake_server_annotated.py")
SCRIPTED_AGENT = str(Path(__file__).parent.parent.parent / "fixtures" / "scripted_agent.py")


def _write_mcp_json(home: Path) -> None:
    (home / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"annotated": {"command": sys.executable, "args": [ANNOTATED_SERVER]}}}),
        encoding="utf-8",
    )


async def _record_persona_session(config_path: Path) -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "cli", "observe", "--config", str(config_path), "--server", "annotated"],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            # Confirmed, not assumed: list_items really is last in the
            # real server's own tool order.
            assert tools.tools[-1].name == "list_items"
            await session.call_tool("list_items", {})


def test_user_4_tool_addition_regression_via_last_tool_selection(tmp_path):
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
    assert [c.tool_name for c in recorded_calls] == ["list_items"]

    agent_command = json.dumps([sys.executable, SCRIPTED_AGENT, "LAST_TOOL|{}"])
    config_text_with_agent = config_text + f"\nagent:\n  command: {agent_command}\n"
    config_path.write_text(config_text_with_agent, encoding="utf-8")

    out = io.StringIO()
    run_run(
        config_path=config_path,
        fixture_path=fixture_path,
        server_name="annotated",
        task_id="test_user_4",
        operator="tool_addition",
        runs_dir=home / "run_sessions",
        seed=42,
        repeats=1,
        timeout_s=30.0,
        output_stream=out,
    )
    output = out.getvalue()
    assert "test_user_4" in output
    # Baseline correctly calls the real tool; mutated calls the
    # newly-injected one instead -- a real, planted, agent-observable
    # behavior break, not a harness artifact.
    assert "REGRESSION" in output
    assert "NO_REGRESSION" not in output
