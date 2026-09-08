"""F-18 end to end: a real `drifter run` leaves a reconstructable trail.

`tests/mutate/test_audit.py` covers the record format. This covers the
thing that actually failed before F-18 -- the mutation log existed only
in memory for the duration of one call, so after a real run there was
nothing on disk to trace a verdict back to. Asserted through the real
`run_mutation_comparison` pipeline against the golden fixture, not
against a hand-built log.
"""

from __future__ import annotations

import json
import sys

from pathlib import Path

from mcp_drifter.cli.run import run_mutation_comparison
from mcp_drifter.mutate.audit import read_mutation_audit
from mcp_drifter.record.reader import read_session
from mcp_drifter.record.schema import ToolCall

# Defined locally rather than imported from tests/cli/test_run.py: the
# tests/ tree is not an importable package, so a cross-test import fails
# at collection.
GOLDEN_FIXTURE = Path(__file__).parent.parent / "fixtures" / "golden_v0.1.jsonl"
SCRIPTED_AGENT = Path(__file__).parent.parent / "fixtures" / "scripted_agent.py"
GOLDEN_SERVER = "filesystem"


def _golden_calls() -> list[ToolCall]:
    return [r for r in read_session(GOLDEN_FIXTURE) if isinstance(r, ToolCall)]


def _spec(tool_name: str, arguments: dict) -> str:
    return f"{tool_name}|{json.dumps(arguments)}"


def _run(tmp_path, operator: str):
    calls = _golden_calls()[:3]
    command = [sys.executable, str(SCRIPTED_AGENT), *(_spec(c.tool_name, c.arguments) for c in calls)]
    return run_mutation_comparison(
        task_id="audit_task",
        prompt="",
        fixture=GOLDEN_FIXTURE,
        server_name=GOLDEN_SERVER,
        agent_command=command,
        operator=operator,
        session_dir=tmp_path / "runs",
        raw_dir=tmp_path / "raw",
        repeats=3,
        timeout_s=30.0,
    )


def test_a_real_run_writes_a_mutation_audit_log_next_to_its_sessions(tmp_path):
    _run(tmp_path, "description_update")

    audit_path = tmp_path / "runs" / "mutations.jsonl"
    assert audit_path.exists()

    records = read_mutation_audit(audit_path)
    assert records, "a run that applied an operator must record at least one mutation"
    assert all(r.mutation_id for r in records)
    assert all(r.operator == "description_update" for r in records)
    assert all(r.server == GOLDEN_SERVER for r in records)
    assert all(r.task_id == "audit_task" for r in records)
    # A description_update genuinely changed something.
    assert all(r.before != r.after for r in records)


def test_the_written_log_is_readable_with_nothing_but_json(tmp_path):
    """F-18's literal done-criterion: reconstructable from the audit log
    ALONE, without the mutation code that produced it.
    """
    _run(tmp_path, "description_update")

    raw = [
        json.loads(line)
        for line in (tmp_path / "runs" / "mutations.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert raw
    required = {"mutation_id", "operator", "server", "tool_name", "target", "before", "after", "inverse", "seed", "task_id", "timestamp"}
    assert required <= set(raw[0])


def test_parameter_rename_records_the_inverse_map_it_handed_to_replay():
    """The inverse mapping is what F-12 consumes to resolve a renamed
    call back to its recording. If it is not in the log, a
    parameter_rename result cannot be traced without re-running the
    operator -- exactly what F-18 exists to prevent.

    Driven off a purpose-built manifest rather than the golden fixture:
    every tool in that fixture takes single-word parameters (`path`,
    `content`), none of which are eligible for snake_case->camelCase
    renaming, so it produces no real inverse to assert on. Discovered by
    running the operator against it rather than assumed.
    """
    from mcp_drifter.mutate.audit import MutationAuditRecord
    from mcp_drifter.mutate.parameter_rename import rename_tool_parameters
    from mcp_drifter.record.schema import ToolDescriptor

    tools = [
        ToolDescriptor(
            name="read_file",
            description="Reads a file.",
            input_schema={"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]},
        )
    ]
    _, log = rename_tool_parameters(tools, seed=42)
    records = [MutationAuditRecord.from_log_entry(e, server="fs", task_id="t") for e in log]
    renames = [r for r in records if r.inverse]

    assert renames, "a tool with a snake_case parameter must produce a real inverse mapping"
    for record in renames:
        assert record.target == f"parameter:{record.before}"
        # The inverse maps the NEW name back to the old one.
        assert record.inverse == {record.after: record.before}


def test_a_tool_with_nothing_eligible_to_rename_is_logged_as_an_explicit_no_op():
    """`parameter:None` would read as "a parameter named None was
    renamed". The log has to say the tool had nothing eligible.
    """
    from mcp_drifter.mutate.audit import MutationAuditRecord
    from mcp_drifter.mutate.parameter_rename import rename_tool_parameters
    from mcp_drifter.record.schema import ToolDescriptor

    tools = [
        ToolDescriptor(
            name="read_file",
            description="Reads a file.",
            input_schema={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        )
    ]
    _, log = rename_tool_parameters(tools, seed=42)
    record = MutationAuditRecord.from_log_entry(log[0], server="fs", task_id="t")

    assert record.before is None
    assert record.inverse is None
    assert record.target == "parameter:<none eligible>"
