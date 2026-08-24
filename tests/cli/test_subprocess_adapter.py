"""Tests for the subprocess agent adapter (F-34).

Deliberately uses a real subprocess (tests/fixtures/scripted_agent.py),
not in-memory streams — F-34's whole point is process boundaries, so an
in-memory test wouldn't exercise the thing being built. Same discipline
as Gate 1's fixture-server tests and this gate's replay-proxy tests: a
real component driving the real thing under test, not a hand-rolled
fake.
"""

import json
import sys
from pathlib import Path

import pytest

from cli.subprocess_adapter import run_agent_subprocess
from record.reader import read_session
from record.schema import ToolCall
from replay.replay_proxy import tools_served_from_session
from replay.replay_store import ReplayStore

GOLDEN_FIXTURE = Path(__file__).parent.parent / "fixtures" / "golden_v0.1.jsonl"
SCRIPTED_AGENT = Path(__file__).parent.parent / "fixtures" / "scripted_agent.py"
GOLDEN_SERVER = "filesystem"


def _golden_calls() -> list[ToolCall]:
    return [r for r in read_session(GOLDEN_FIXTURE) if isinstance(r, ToolCall)]


def _spec(tool_name: str, arguments: dict) -> str:
    return f"{tool_name}|{json.dumps(arguments)}"


@pytest.mark.anyio
async def test_agent_subprocess_produces_a_real_parseable_session_with_replayed_hits(tmp_path):
    store = ReplayStore()
    store.index_session(GOLDEN_FIXTURE)
    tools_served = tools_served_from_session(GOLDEN_FIXTURE)
    calls = _golden_calls()[:3]

    session_path = await run_agent_subprocess(
        command=[sys.executable, str(SCRIPTED_AGENT), *(_spec(c.tool_name, c.arguments) for c in calls)],
        replay_store=store,
        server_name=GOLDEN_SERVER,
        tools_served=tools_served,
        session_dir=tmp_path / "runs",
        raw_dir=tmp_path / "raw",
        timeout_s=30.0,
    )

    assert session_path.exists()
    records = list(read_session(session_path))  # raises on unparseable JSONL

    session_start = next(r for r in records if r.record_type == "session_start")
    assert session_start.environment.tool_manifest_hash is not None

    recorded_calls = [r for r in records if isinstance(r, ToolCall)]
    assert len(recorded_calls) == 3
    for original, recorded in zip(calls, recorded_calls):
        assert recorded.tool_name == original.tool_name
        assert recorded.is_error == bool(original.is_error)
        assert recorded.fault is False  # genuine hits from ReplayStore, not a miss

    trajectory_ends = [r for r in records if r.record_type == "trajectory_end"]
    assert len(trajectory_ends) == 1
    assert trajectory_ends[0].call_seqs == [c.seq for c in recorded_calls]


@pytest.mark.anyio
async def test_unrecorded_call_surfaces_as_a_distinguishable_miss_not_a_hang_or_crash(tmp_path):
    store = ReplayStore()
    store.index_session(GOLDEN_FIXTURE)
    tools_served = tools_served_from_session(GOLDEN_FIXTURE)
    calls = _golden_calls()[:2]
    miss_args = {"path": "C:\\nowhere\\never\\recorded\\by\\this\\test"}

    session_path = await run_agent_subprocess(
        command=[
            sys.executable,
            str(SCRIPTED_AGENT),
            *(_spec(c.tool_name, c.arguments) for c in calls),
            _spec("list_directory", miss_args),
        ],
        replay_store=store,
        server_name=GOLDEN_SERVER,
        tools_served=tools_served,
        session_dir=tmp_path / "runs",
        raw_dir=tmp_path / "raw",
        timeout_s=30.0,
    )

    records = list(read_session(session_path))
    recorded_calls = [r for r in records if isinstance(r, ToolCall)]
    assert len(recorded_calls) == 3  # 2 real hits + 1 miss, all recorded

    for original, recorded in zip(calls, recorded_calls[:2]):
        assert recorded.tool_name == original.tool_name
        assert recorded.fault is False

    miss_record = recorded_calls[2]
    assert miss_record.tool_name == "list_directory"
    assert miss_record.arguments == miss_args
    assert miss_record.fault is True  # distinguishable from a real hit — never confusable
    # with a recorded is_error=True result, matching replay_proxy.py's own
    # MISS-and-fault-both-recorded-as-fault=True decision.


@pytest.fixture
def anyio_backend():
    return "asyncio"
