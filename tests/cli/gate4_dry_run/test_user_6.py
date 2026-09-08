"""Gate 4 pre-handoff dry run — test_user_6: the connectivity-check
artifact (SPEC.md §15 limitation 12) reproduced via a REAL recorded
session, plus `drifter stats`/`drifter score` against a real corpus.

See tests/cli/gate4_dry_run/test_user_1.py's module docstring for the
shared scope note (dry run, not Gate 4 closure).

The existing regression test for limitation 12
(tests/evaluate/test_baseline.py) builds its zero-`ToolCall` artifact
session by hand (constructing SessionStart/ToolCall records directly).
test_user_6 instead reproduces the SAME real-world shape end to end: a
real agent that connects, lists tools (populating
`environment.tool_manifest_hash` via the same eager-bootstrap path a
real `claude mcp get` connectivity check hits), and calls nothing --
confirming this genuinely happens through the real CLI, not only
through a hand-built fixture standing in for it.

SECOND, NEW finding surfaced while building this persona (not assumed,
confirmed via direct repro before writing the regression test below):
`record/writer.py`'s `_ensure_session_start_written()` locks in
`SessionStart` -- `tool_manifest_hash` included -- on whichever event
happens first in a session, a `tools/call` response OR a `tools/list`
response. An agent that calls a tool BEFORE ever calling `list_tools()`
gets a session whose `tool_manifest_hash` is null FOREVER, even if
`list_tools()` is called later in the very same session -- confirmed
directly: calling a tool first, then `list_tools()` second, still
produces `tool_manifest_hash: null`, because `SessionStart` (JSONL's
first, append-only record) was already flushed by the earlier
`tools/call` response, before the later `tools/list` response ever
arrived. This makes a fully legitimate, successful real session
permanently excluded from `aggregate_baseline_runs` (the exact same
null-hash exclusion path limitation 12 also hits, for a completely
different, opposite-direction reason: this is a REAL session wrongly
excluded, not an artifact wrongly included). See SPEC.md §15 limitation
14.
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
from mcp_drifter.cli.score import run_score
from mcp_drifter.cli.stats import run_stats
from mcp_drifter.evaluate.baseline import aggregate_baseline_runs
from mcp_drifter.record.reader import read_session
from mcp_drifter.record.schema import SessionStart, ToolCall

FIXTURE_SERVER = str(Path(__file__).parent.parent.parent / "fixtures" / "fake_server.py")


def _write_mcp_json(home: Path) -> None:
    (home / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"fake": {"command": sys.executable, "args": [FIXTURE_SERVER]}}}),
        encoding="utf-8",
    )


async def _record_real_session(config_path: Path) -> None:
    """A genuine, working call -- `stats`/`score` need at least one real
    session to have something to report on. Calls `list_tools()` BEFORE
    its first `call_tool` deliberately -- see
    test_user_6_a_tool_call_before_the_first_list_tools_permanently_
    nulls_the_hash below for why that ordering is load-bearing, not
    incidental, for whether this session even gets a manifest hash."""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_drifter.cli", "observe", "--config", str(config_path), "--server", "fake"],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            await session.list_tools()
            await session.call_tool("add", {"a": 1, "b": 1})


async def _record_connectivity_check_artifact(config_path: Path) -> None:
    """Connects and lists tools -- exactly what `claude mcp get` does --
    then disconnects without ever calling a tool."""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_drifter.cli", "observe", "--config", str(config_path), "--server", "fake"],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            await session.list_tools()


def test_user_6_connectivity_artifact_reproduced_end_to_end_then_stats_and_score(tmp_path):
    home = tmp_path
    _write_mcp_json(home)

    config_path = home / "drifter.yaml"
    run_init(output_path=config_path, search_root=home)

    runs_dir = home / ".drifter" / "runs"
    config_text = config_path.read_text(encoding="utf-8").replace(".drifter/runs", str(runs_dir.as_posix()))
    config_path.write_text(config_text, encoding="utf-8")

    anyio.run(_record_real_session, config_path)
    anyio.run(_record_connectivity_check_artifact, config_path)

    session_files = sorted(runs_dir.glob("*.jsonl"))
    assert len(session_files) == 2

    real_session, artifact_session = None, None
    for path in session_files:
        records = list(read_session(path))
        call_count = sum(1 for r in records if isinstance(r, ToolCall))
        start = next(r for r in records if isinstance(r, SessionStart))
        assert start.environment.tool_manifest_hash is not None  # both really did connect
        if call_count == 0:
            artifact_session = path
        else:
            real_session = path

    assert real_session is not None
    assert artifact_session is not None

    # This is limitation 12 itself, reproduced live: aggregate_baseline_runs
    # cannot tell these two sessions apart -- the artifact is silently
    # counted as a valid empty-path variant, contaminating the aggregate.
    result = aggregate_baseline_runs("test_user_6", session_files)
    assert result.valid_runs == 2
    assert () in result.variant_frequencies  # the artifact's empty path counted as real

    # drifter stats / drifter score must not crash against this exact
    # mixed-real-and-artifact corpus -- the actual reporting commands a
    # real user would run next.
    stats_out = io.StringIO()
    run_stats(runs_dir=runs_dir, output_stream=stats_out)
    assert "add" in stats_out.getvalue()

    score_out = io.StringIO()
    run_score(runs_dir=runs_dir, output_stream=score_out)
    score_text = score_out.getvalue()
    assert "sessions: 2" in score_text
    assert "valid: 2" in score_text


async def _record_tool_call_before_list_tools(config_path: Path) -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_drifter.cli", "observe", "--config", str(config_path), "--server", "fake"],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            await session.call_tool("add", {"a": 9, "b": 9})  # tool call FIRST
            await session.list_tools()  # list_tools SECOND -- too late, see below


def test_user_6_a_tool_call_before_the_first_list_tools_permanently_nulls_the_hash(tmp_path):
    """New finding, confirmed via direct repro before this test was
    written (this module's own docstring): SessionStart locks in
    whatever tool_manifest_hash is known at the FIRST tools/call-or-
    tools/list response, and is never rewritten -- calling list_tools()
    later in the same session cannot retroactively fix a session whose
    first tracked response was a tools/call. This is a fully legitimate,
    successful real session (a real tool call, a real result) that ends
    up excluded from aggregate_baseline_runs for reasons having nothing
    to do with its own validity -- purely an artifact of call ORDER.
    """
    home = tmp_path
    _write_mcp_json(home)

    config_path = home / "drifter.yaml"
    run_init(output_path=config_path, search_root=home)
    runs_dir = home / ".drifter" / "runs"
    config_text = config_path.read_text(encoding="utf-8").replace(".drifter/runs", str(runs_dir.as_posix()))
    config_path.write_text(config_text, encoding="utf-8")

    anyio.run(_record_tool_call_before_list_tools, config_path)

    session_files = list(runs_dir.glob("*.jsonl"))
    assert len(session_files) == 1
    records = list(read_session(session_files[0]))
    start = next(r for r in records if isinstance(r, SessionStart))
    call_count = sum(1 for r in records if isinstance(r, ToolCall))

    assert call_count == 1  # a completely real, successful call happened
    assert start.environment.tool_manifest_hash is None  # yet the hash is permanently null

    # The direct, real consequence: aggregate_baseline_runs excludes this
    # perfectly legitimate session for the same reason it excludes a
    # genuine connectivity-check artifact -- the schema can't tell them
    # apart from this signal alone.
    result = aggregate_baseline_runs("test_user_6b", session_files)
    assert result.valid_runs == 0
    assert len(result.excluded_runs) == 1
