"""Integration tests for `drifter replay-serve` — the real-agent
connection mechanism `drifter run` needed but never had (see
cli/replay_serve.py's own docstring).

Deliberately uses `mcp.client.stdio.stdio_client` + a real
`ClientSession`, spawning `drifter replay-serve` as a genuine
subprocess — the NORMAL "client spawns its configured server command"
MCP relationship, matching exactly how a real agent (Claude Code) would
actually connect. This is the opposite direction from
tests/cli/test_subprocess_adapter.py's tests (which spawn the AGENT
and treat ITS stdio as the wire) — proving this specific, missing
direction actually works is the whole point of this module existing.
"""

from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from record.reader import read_session
from record.schema import ToolCall

GOLDEN_FIXTURE = Path(__file__).parent.parent / "fixtures" / "golden_v0.1.jsonl"
GOLDEN_SERVER = "filesystem"


def _golden_calls() -> list[ToolCall]:
    return [r for r in read_session(GOLDEN_FIXTURE) if isinstance(r, ToolCall)]


@pytest.mark.anyio
async def test_replay_serve_baseline_over_a_real_subprocess_connection(tmp_path):
    """A real ClientSession, spawning drifter replay-serve as its own
    child (the standards-compliant MCP relationship a real agent's
    mcp.json uses) -- not run_replay_proxy called in-process, and not
    subprocess_adapter's inverted agent-spawns-nothing/gets-spawned
    role. This is the actual connection shape that was missing."""
    import sys

    runs_dir = tmp_path / "runs"
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m", "cli", "replay-serve",
            "--fixture", str(GOLDEN_FIXTURE),
            "--server", GOLDEN_SERVER,
            "--runs-dir", str(runs_dir),
        ],
    )

    calls = _golden_calls()[:3]
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            from replay.replay_proxy import tools_served_from_session

            expected_names = {t.name for t in tools_served_from_session(GOLDEN_FIXTURE)}
            assert {t.name for t in tools.tools} == expected_names  # the real fixture's manifest, served for real
            assert len(tools.tools) == 14
            for call in calls:
                result = await session.call_tool(call.tool_name, call.arguments)
                assert result.is_error == bool(call.is_error)

    session_files = list(runs_dir.glob("*.jsonl"))
    assert len(session_files) == 1
    records = list(read_session(session_files[0]))
    recorded_calls = [r for r in records if isinstance(r, ToolCall)]
    assert len(recorded_calls) == 3
    for original, recorded in zip(calls, recorded_calls):
        assert recorded.tool_name == original.tool_name
        assert recorded.result_provenance == "real"


@pytest.mark.anyio
async def test_replay_serve_with_description_update_serves_a_changed_manifest(tmp_path):
    import sys

    runs_dir = tmp_path / "runs"
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m", "cli", "replay-serve",
            "--fixture", str(GOLDEN_FIXTURE),
            "--server", GOLDEN_SERVER,
            "--runs-dir", str(runs_dir),
            "--mutate", "description_update",
            "--seed", "1",
        ],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()

    from replay.replay_proxy import tools_served_from_session

    original_descriptions = {t.name: t.description for t in tools_served_from_session(GOLDEN_FIXTURE)}
    changed = [t for t in tools.tools if original_descriptions.get(t.name) != t.description]
    assert changed  # at least one description actually differs from the original manifest


@pytest.mark.anyio
async def test_replay_serve_with_tool_addition_serves_an_extra_tool_and_it_resolves_synthetic(tmp_path):
    import sys

    from mutate.tool_addition import add_tool
    from replay.replay_proxy import tools_served_from_session

    siblings = tools_served_from_session(GOLDEN_FIXTURE)
    added_tool, _ = add_tool(siblings, seed=1)

    runs_dir = tmp_path / "runs"
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m", "cli", "replay-serve",
            "--fixture", str(GOLDEN_FIXTURE),
            "--server", GOLDEN_SERVER,
            "--runs-dir", str(runs_dir),
            "--mutate", "tool_addition",
            "--seed", "1",
        ],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert added_tool.name in {t.name for t in tools.tools}
            result = await session.call_tool(added_tool.name, {})
            assert result.is_error is False

    session_files = list(runs_dir.glob("*.jsonl"))
    records = list(read_session(session_files[0]))
    recorded = [r for r in records if isinstance(r, ToolCall)][0]
    assert recorded.tool_name == added_tool.name
    assert recorded.result_provenance == "synthetic"


@pytest.fixture
def anyio_backend():
    return "asyncio"
