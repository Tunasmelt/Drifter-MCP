"""`drifter replay-serve`: the CLI entrypoint that lets a REAL,
standards-compliant MCP-client agent (Claude Code, or any other real
agent — not just tests/fixtures/scripted_agent.py) connect to Drifter's
replay-serving proxy the normal way: by spawning `drifter replay-serve`
as its own configured MCP server command and talking to it over real
OS stdio, exactly the way it already connects to `drifter observe`.

Gap this closes — found while attempting to actually run `drifter run`
against Claude Code (the real Gate 0 dogfood pairing) rather than the
scripted test-agent, not anticipated in advance: `run_replay_proxy` had
never been exposed over real OS stdio the way `drifter observe` (F-09)
already is for passthrough. Its only existing wiring
(`cli/subprocess_adapter.py`) has *Drifter* spawn the agent and treat
the *agent's own* stdio as the wire — that only works for a purpose-
built script willing to speak raw MCP frames over its own process
stdio (`scripted_agent.py`, built specifically for this). A real agent
CLI always spawns and connects to *its own* configured server
commands; it has no mode where an external process spawns it and feeds
it MCP frames over its own stdin. This module is that missing piece.

Manifest selection: `--fixture` provides the `ReplayStore` + baseline
`tools_served` (docs/SPEC.md's replay-first architecture — an already-
recorded corpus, never a live connection: this command never imports
`mcp.client.stdio` or anything that could dial a real tool server,
same structural guarantee as `replay_proxy.py`/`subprocess_adapter.py`
themselves). `--mutate {description_update,tool_addition}` (optional)
applies that operator to the manifest before serving it, with `--seed`
controlling reproducibility — the exact same two-arm choice `cli/
run.py`'s own orchestration already computes for the scripted-agent
path; this entrypoint exposes the identical choice for a real agent
that has to be spawned externally, by its own client, rather than by
Drifter.

Recording: every session served here is recorded via `SessionRecorder`
(same as `observe.py`), so its output is a real, ordinary session JSONL
— consumable by `evaluate.baseline.aggregate_baseline_runs`/
`drifter score` exactly like any other replay-served session, with no
special-casing needed downstream.

Deliberately NOT wired into `cli/run.py`'s own orchestration in this
prompt: `run_mutation_comparison` always spawns the agent itself via
`subprocess_adapter.py`, which is fine for a scripted/simple agent but
fundamentally can't drive a real, externally-configured agent like
Claude Code (its mcp.json is persistent, global config `drifter run`
has no way to flip per-arm mid-orchestration). Composing this
entrypoint into a single-command real-agent flow is real, separate
scope — this prompt's job is making the connection possible at all,
which is what actually blocked the real dogfood run.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TextIO

import anyio
from mcp.server.stdio import stdio_server

from cli.config import ConfigError
from mutate.description_update import mutate_tool_manifest
from mutate.tool_addition import add_tool
from record.writer import SessionRecorder
from replay.replay_proxy import run_replay_proxy, tools_served_from_session
from replay.replay_store import ReplayStore

OPERATORS = ("description_update", "tool_addition")


def run_replay_serve(
    fixture_path: Path,
    server_name: str,
    session_dir: Path,
    raw_dir: Path,
    operator: str | None = None,
    seed: int = 42,
    status_stream: TextIO = sys.stderr,
) -> None:
    if operator is not None and operator not in OPERATORS:
        raise ConfigError(f"unknown --mutate operator {operator!r} — must be one of {OPERATORS}")

    store = ReplayStore()
    store.index_session(fixture_path)
    original_tools = tools_served_from_session(fixture_path)

    synthetic_tool_names: frozenset[str] = frozenset()
    if operator == "description_update":
        tools_served, _log = mutate_tool_manifest(original_tools, seed=seed)
    elif operator == "tool_addition":
        new_tool, _entry = add_tool(original_tools, seed=seed)
        tools_served = [*original_tools, new_tool]
        synthetic_tool_names = frozenset({new_tool.name})
    else:
        tools_served = original_tools

    recorder = SessionRecorder(session_dir=session_dir, raw_dir=raw_dir, server_name=server_name)

    mutation_note = f"mutated: {operator}, seed={seed}" if operator else "baseline, unmutated"
    status_stream.write(f"drifter replay-serve — serving {server_name!r} from {fixture_path} ({mutation_note})\n")
    status_stream.flush()

    try:
        async def _main() -> None:
            async with stdio_server() as (read_stream, write_stream):
                await run_replay_proxy(
                    read_stream,
                    write_stream,
                    store,
                    server_name,
                    tools_served,
                    recorder.observe,
                    synthetic_tool_names,
                )

        anyio.run(_main)
    finally:
        recorder.close()
        status_stream.write("drifter replay-serve — session ended\n")
        status_stream.flush()
