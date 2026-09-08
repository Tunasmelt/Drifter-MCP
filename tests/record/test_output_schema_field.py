"""F-14: `ToolDescriptor.output_schema`, added under this project's
required schema-evolution procedure (CLAUDE.md).

F-14's stated "Done when" is that synthetic responses pass the tool's
OWN declared schema validation. That is not satisfiable from what
`record/writer.py` captured before this change: `_write_tools_list`
recorded `inputSchema` and `annotations` and dropped `outputSchema`
entirely, so a replayed session had nothing to validate a synthesized
response against. This is real wire data, available only at the moment
a `tools/list` response is observed -- the same class of field as
`annotations`, `is_error` and `fault`, and the fourth time this project
has added one.

Procedure, per CLAUDE.md (this exact sequence has caught a real bug
three times before): the field must be nullable with no non-null
default, its "unknown" state must stay distinguishable from its "empty"
state, and a test against a hand-built PRE-CHANGE corpus is written and
confirmed failing before the field exists.

The nullable distinction carries more weight here than it did for
`annotations`. F-14 has to tell three cases apart downstream:
  - `None`      -- unknown: recorded before this field existed, OR the
                   server declared no outputSchema. Synthesize nothing
                   structured; fall back to a content-empty result.
  - `{}`        -- the server sent an explicitly empty schema.
  - a real dict -- synthesize structuredContent conforming to it.
Collapsing `None` into `{}` would make a pre-change recording look like
a server that declared an empty output contract, and F-14 would then
claim a schema conformance it never actually verified.
"""

from __future__ import annotations

import json
from pathlib import Path

from mcp_drifter.record.reader import read_session
from mcp_drifter.record.schema import ToolDescriptor, ToolsList
from mcp_drifter.record.writer import SessionRecorder


def _write_pre_change_session(path: Path) -> None:
    """A tools_list record exactly as writer.py emitted it BEFORE
    output_schema existed. Hand-built rather than produced by the
    current writer on purpose -- generating it would bake in today's
    format and prove nothing about backward compatibility.
    """
    record = {
        "schema_version": "0.1",
        "record_type": "tools_list",
        "session_id": "pre-change",
        "seq": 1,
        "timestamp": "2026-01-01T00:00:00Z",
        "server": "fs",
        "tools_raw": [
            {"name": "read_file", "description": "Reads a file.", "input_schema": {"type": "object"}},
        ],
        "tools_served": [
            {"name": "read_file", "description": "Reads a file.", "input_schema": {"type": "object"}},
        ],
        "raw_frame_offset": 0,
    }
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")


def test_a_pre_change_record_reads_back_with_output_schema_none_not_empty_dict(tmp_path):
    """The load-bearing assertion: `None`, NOT `{}` -- a pre-change
    recording must stay distinguishable from a server that declared an
    empty output contract. See this module's docstring.
    """
    session = tmp_path / "pre_change.jsonl"
    _write_pre_change_session(session)

    records = list(read_session(session))

    assert len(records) == 1
    tools_list = records[0]
    assert isinstance(tools_list, ToolsList)
    tool = tools_list.tools_raw[0]
    # Everything that existed before still round-trips unchanged.
    assert tool.name == "read_file"
    assert tool.input_schema == {"type": "object"}
    assert tool.output_schema is None


def test_an_explicitly_empty_output_schema_is_not_confused_with_an_absent_one():
    absent = ToolDescriptor(name="a", description="", input_schema={})
    explicit_empty = ToolDescriptor(name="b", description="", input_schema={}, output_schema={})

    assert absent.output_schema is None
    assert explicit_empty.output_schema == {}
    assert absent.output_schema != explicit_empty.output_schema


def test_writer_captures_output_schema_from_the_wire_when_the_server_declares_one(tmp_path):
    """Exact expected value, not mere presence -- this project's
    recurring bug pattern is plausible-but-wrong values, and a schema
    captured from the wrong wire key (`inputSchema`) would still be a
    non-empty dict and still look fine to a presence check.
    """
    declared_output = {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}
    wire = {
        "tools": [
            {
                "name": "read_file",
                "description": "Reads a file.",
                "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}},
                "outputSchema": declared_output,
            },
            {"name": "no_output_contract", "description": "", "inputSchema": {}},
        ]
    }

    writer = SessionRecorder(session_dir=tmp_path / "runs", raw_dir=tmp_path / "raw", server_name="fs")
    writer._write_tools_list(wire, raw_frame_offset=0)
    writer.close()

    tools = [r for r in read_session(writer.jsonl_path) if isinstance(r, ToolsList)][0].tools_raw
    by_name = {t.name: t for t in tools}

    assert by_name["read_file"].output_schema == declared_output
    # Not accidentally the input schema.
    assert by_name["read_file"].output_schema != by_name["read_file"].input_schema
    # A server that declared none stays None, not {}.
    assert by_name["no_output_contract"].output_schema is None


def test_the_committed_golden_fixture_still_reads_back_with_no_output_schema():
    """The strongest available backward-compatibility evidence: the
    golden fixture is a REAL recording made before this field existed
    (a real filesystem MCP server, captured at Gate 1), not a
    hand-built approximation of one. Per CLAUDE.md the golden fixture
    is never modified in place, so it stays a permanent pre-change
    corpus -- every tool in it must read back as `None`, not `{}`.
    """
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "golden_v0.1.jsonl"
    tools_lists = [r for r in read_session(fixture) if isinstance(r, ToolsList)]

    assert tools_lists, "golden fixture has no tools_list record to check"
    tools = [t for tl in tools_lists for t in tl.tools_raw]
    assert tools, "golden fixture's tools_list carried no tools"
    assert all(t.output_schema is None for t in tools)
    # And the pre-existing fields are genuinely still populated -- proving
    # the assertion above means "absent", not "the whole record failed to
    # parse into something empty".
    assert all(t.name for t in tools)
    assert any(t.input_schema for t in tools)
