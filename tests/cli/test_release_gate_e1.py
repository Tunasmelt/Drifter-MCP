"""Release gate E1, compatibility detection (docs/PHASES.md R0.5), end to end.

Pre-registered before this test existed:
- server `tests/fixtures/orders_server.py`, operator `parameter_rename`,
  which renames exactly `get_order.order_id` -> `orderId`;
- subject `tests/fixtures/orders_old_contract_client.py`, a scripted client
  bound to the old contract, trajectory n=2;
- control: 3/3 valid, TASK PASS;
- mutated: k=1 of n=2 rejected -> coverage 0.50 < 0.70 floor -> 3/3 runs
  excluded, TASK UNKNOWN, and each run's get_order rejected with -31003,
  read from the raw mirror.

Deterministic: any other outcome is a defect, not noise.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import anyio
import yaml
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from mcp_drifter.cli.run import run_mutation_comparison
from mcp_drifter.evaluate.assertions import TaskAssertions
from mcp_drifter.record.reader import read_session
from mcp_drifter.record.schema import ToolCall
from mcp_drifter.replay.replay_proxy import REPLAY_INVALID_ARGS_CODE

FIXTURES = Path(__file__).parent.parent / "fixtures"
ORDERS_SERVER = str(FIXTURES / "orders_server.py")
OLD_CONTRACT_CLIENT = str(FIXTURES / "orders_old_contract_client.py")
SERVER = "orders"
ORDER_ID = "ord-7f3a91"
ANSWER = f"order {ORDER_ID} total: 1240 dollars"


def _record_live_corpus(runs_dir: Path, raw_dir: Path) -> None:
    """One real session through `drifter observe` against the live server --
    the path a user takes, not hand-built records."""
    workdir = runs_dir.parent
    config = workdir / "drifter.yaml"
    # Single-quoted YAML scalars: Windows paths contain backslashes.
    config.write_text(
        "version: 1\nservers:\n"
        f"  - name: {SERVER}\n    command: ['{sys.executable}', '{ORDERS_SERVER}']\n"
        f"record:\n  dir: '{runs_dir.as_posix()}'\n",
        encoding="utf-8",
    )

    async def _session() -> None:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "mcp_drifter.cli", "observe", "--config", str(config), "--server", SERVER],
            env={"DRIFTER_RUNS_DIR": str(runs_dir), "DRIFTER_RAW_DIR": str(raw_dir)},
            cwd=str(workdir),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                await session.list_tools()
                found = await session.call_tool("find_order", {"customer": "acme"})
                await session.call_tool("get_order", {"order_id": found.content[0].text.strip()})

    anyio.run(_session)


def _authored_fixture(path: Path) -> Path:
    def text(body: str) -> dict:
        return {"content": [{"type": "text", "text": body}], "isError": False}

    document = {
        "version": 1,
        "server": SERVER,
        "responses": [
            {"tool_name": "find_order", "arguments": {"customer": "acme"}, "result": text(ORDER_ID)},
            {"tool_name": "get_order", "arguments": {"order_id": ORDER_ID}, "result": text(ANSWER)},
        ],
    }
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def _raw_frame(raw_path: Path, offset: int) -> dict:
    with raw_path.open("rb") as fh:
        fh.seek(offset)
        return json.loads(fh.readline().decode("utf-8"))


def test_e1_old_contract_client_is_detected_by_exclusion_with_the_expected_reason(tmp_path):
    corpus_dir = tmp_path / "corpus"
    _record_live_corpus(corpus_dir, tmp_path / "corpus_raw")
    corpus = sorted(corpus_dir.glob("*.jsonl"))
    assert len(corpus) == 1
    recorded = [c.tool_name for c in read_session(corpus[0]) if isinstance(c, ToolCall)]
    assert recorded == ["find_order", "get_order"]

    runs, raw = tmp_path / "runs", tmp_path / "raw"
    result = run_mutation_comparison(
        task_id="e1",
        prompt="What is acme's latest order total?",
        fixture=corpus_dir,
        response_fixture=_authored_fixture(tmp_path / "responses.yaml"),
        server_name=SERVER,
        agent_command=[sys.executable, OLD_CONTRACT_CLIENT],
        agent_mode="http",
        operator="parameter_rename",
        session_dir=runs,
        raw_dir=raw,
        repeats=3,
        adaptive=False,
        timeout_s=60.0,
        assertions=TaskAssertions(
            calls=("find_order", "get_order"),
            answer_matches=r"\btotal: 1240 dollars\b",
        ),
    )

    # The mutation landed on a tool this task calls, and only there.
    renamed = {e.tool_name: e.inverse for e in result.mutation_log if e.inverse}
    assert renamed == {"get_order": {"orderId": "order_id"}}

    # Control: the unmutated contract works.
    assert result.baseline.valid_runs == 3
    assert result.baseline.baseline_fidelity == 1.0
    assert result.baseline_task.verdict == "PASS"

    # Mutated: detected by exclusion, as the arithmetic predicted.
    assert result.mutated.total_runs == 3
    assert result.mutated.valid_runs == 0
    assert len(result.mutated.excluded_runs) == 3
    assert all("0.50" in run.reason for run in result.mutated.excluded_runs)
    assert result.mutated_task.verdict == "UNKNOWN"

    # ...and for the expected reason, from the raw mirror.
    sessions = sorted((runs / "mutated").glob("*.jsonl"))
    assert len(sessions) == 3
    for session in sessions:
        calls = [c for c in read_session(session) if isinstance(c, ToolCall)]
        assert [(c.tool_name, c.fault) for c in calls] == [("find_order", False), ("get_order", True)]
        assert calls[1].arguments == {"order_id": ORDER_ID}
        frame = _raw_frame(raw / "mutated" / f"{session.stem}.frames", calls[1].raw_frame_offset)
        assert frame["error"]["code"] == REPLAY_INVALID_ARGS_CODE
        assert session.with_suffix(".stdout.txt").read_text(encoding="utf-8").strip() == (
            f"get_order rejected: code {REPLAY_INVALID_ARGS_CODE}"
        )


ADAPTING_CLIENT = str(FIXTURES / "orders_adapting_client.py")


def test_e2_precondition_a_schema_reading_client_recovers_under_the_same_mutation(tmp_path):
    """Before E2 spends live LLM runs: the plumbing must let an adapting client
    recover. It sends `orderId` on the mutated arm; inverse resolution maps it
    back to the recorded `order_id` key and the authored fixture answers."""
    corpus_dir = tmp_path / "corpus"
    _record_live_corpus(corpus_dir, tmp_path / "corpus_raw")

    runs = tmp_path / "runs"
    result = run_mutation_comparison(
        task_id="e2-precondition",
        prompt="What is acme's latest order total?",
        fixture=corpus_dir,
        response_fixture=_authored_fixture(tmp_path / "responses.yaml"),
        server_name=SERVER,
        agent_command=[sys.executable, ADAPTING_CLIENT],
        agent_mode="http",
        operator="parameter_rename",
        session_dir=runs,
        raw_dir=tmp_path / "raw",
        repeats=3,
        adaptive=False,
        timeout_s=60.0,
        assertions=TaskAssertions(
            calls=("find_order", "get_order"),
            answer_matches=r"\btotal: 1240 dollars\b",
        ),
    )

    assert result.baseline.valid_runs == 3
    assert result.baseline_task.verdict == "PASS"
    assert result.mutated.valid_runs == 3
    assert result.mutated.baseline_fidelity == 1.0
    assert result.mutated_task.verdict == "PASS"
    for session in sorted((runs / "mutated").glob("*.jsonl")):
        get_order = [c for c in read_session(session) if isinstance(c, ToolCall) and c.tool_name == "get_order"]
        assert [(c.arguments, c.fault, c.match_tier, c.result_provenance) for c in get_order] == [
            # Tier "inverse": the renamed argument resolved through the
            # mutation's recorded inverse map, not by an exact key match.
            ({"orderId": ORDER_ID}, False, "inverse", "authored_fixture")
        ]
