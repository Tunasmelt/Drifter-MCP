from __future__ import annotations

import sys
from pathlib import Path

import yaml

from mcp_drifter.cli.run import run_mutation_comparison
from mcp_drifter.evaluate.assertions import TaskAssertions
from mcp_drifter.record.schema import Environment, SessionStart, ToolCall, ToolDescriptor, ToolsList
from mcp_drifter.record.reader import read_session


AGENT = Path(__file__).parent.parent / "fixtures" / "content_dependent_agent.py"
SERVER = "filesystem"


def _corpus(tmp_path: Path) -> Path:
    tools = [
        ToolDescriptor(name="list_directory", description="Returns the complete listing of a directory. Only works within allowed directories.", input_schema={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}),
        ToolDescriptor(name="read_text_file", description="Read the complete contents of a file as text. Returns an error if the file is missing.", input_schema={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}),
    ]
    records = [
        SessionStart(session_id="source", seq=0, started_at="2026-01-01T00:00:00Z", environment=Environment(tool_manifest_hash="fixture"), raw_frame_offset=0),
        ToolsList(session_id="source", seq=1, timestamp="2026-01-01T00:00:00Z", server=SERVER, tools_raw=tools, tools_served=tools, raw_frame_offset=1),
        ToolCall(session_id="source", seq=2, timestamp="2026-01-01T00:00:01Z", server=SERVER, tool_name="list_directory", arguments={"path": "/project"}, result_shape={"type": "object", "keys": ["content"]}, is_error=False, fault=False, raw_frame_offset=2),
        ToolCall(session_id="source", seq=3, timestamp="2026-01-01T00:00:02Z", server=SERVER, tool_name="read_text_file", arguments={"path": "/project/data/readings.csv"}, result_shape={"type": "object", "keys": ["content"]}, is_error=False, fault=False, raw_frame_offset=3),
    ]
    path = tmp_path / "source.jsonl"
    path.write_text("\n".join(record.model_dump_json() for record in records) + "\n", encoding="utf-8")
    return path


def _responses(tmp_path: Path) -> Path:
    document = {"version": 1, "server": SERVER, "responses": [
        {"tool_name": "list_directory", "arguments": {"path": "/project"}, "result": {"content": [{"type": "text", "text": "/project/data/readings.csv"}], "isError": False}},
        {"tool_name": "read_text_file", "arguments": {"path": "/project/data/readings.csv"}, "result": {"content": [{"type": "text", "text": "sensor,value\na,10\nb,20\n"}], "isError": False}},
    ]}
    path = tmp_path / "responses.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def test_authored_content_closes_navigation_coverage_and_answer_oracle_together(tmp_path):
    result = run_mutation_comparison(
        task_id="content-fixture",
        prompt="Count the data rows",
        fixture=_corpus(tmp_path),
        response_fixture=_responses(tmp_path),
        server_name=SERVER,
        agent_command=[sys.executable, str(AGENT)],
        agent_mode="http",
        operator="description_update",
        session_dir=tmp_path / "runs",
        raw_dir=tmp_path / "raw",
        repeats=1,
        adaptive=False,
        assertions=TaskAssertions(
            calls=("list_directory", "read_text_file"),
            calls_before=(("list_directory", "read_text_file"),),
            answer_matches=r"\b2\b data rows",
        ),
    )

    assert result.baseline.valid_runs == 1
    assert result.mutated.valid_runs == 1
    assert result.baseline.baseline_fidelity == 1.0
    assert result.mutated.baseline_fidelity == 1.0
    assert result.baseline_task.verdict == "PASS"
    assert result.mutated_task.verdict == "PASS"
    for path in (tmp_path / "runs").glob("*/*.jsonl"):
        calls = [record for record in read_session(path) if isinstance(record, ToolCall)]
        assert calls and all(call.result_provenance == "authored_fixture" for call in calls)
