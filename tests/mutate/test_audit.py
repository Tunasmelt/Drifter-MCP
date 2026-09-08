"""F-18 mutation audit log: `mutate/audit.py`.

F-18's "Done when" is that every mutation applied in a test run is
reconstructable FROM THE AUDIT LOG ALONE, without the mutation code.
`MutationLogEntry` already carried most of the facts, but it lived only
in memory for the duration of one `run_mutation_comparison` call and
was never written anywhere -- so nothing was reconstructable after the
process exited, which is precisely when a paper trail matters.

Two things this adds beyond persistence:

`mutation_id` -- a deterministic digest over the fields that define the
mutation (operator, seed, server, tool, before, after). Deterministic
rather than random on purpose: re-running the same operator at the same
seed against the same manifest must yield the SAME id, which is what
makes "this verdict came from that exact edit" a checkable claim
instead of a hopeful one. A uuid4 would be unique but would make two
identical mutations look like two different ones.

`target` -- what the operator actually edited. For description_update
this is the tool's description; for parameter_rename it is the specific
PARAMETER renamed, which `tool_name` alone does not capture. Without it
a parameter_rename entry cannot be replayed by hand from the log.
"""

from __future__ import annotations

import json

import pytest

from mcp_drifter.mutate.audit import (
    MutationAuditRecord,
    mutation_id_for,
    read_mutation_audit,
    write_mutation_audit,
)
from mcp_drifter.mutate.description_update import MutationLogEntry


def _entry(tool_name: str = "read_file", operator: str = "description_update") -> MutationLogEntry:
    return MutationLogEntry(
        tool_name=tool_name,
        operator=operator,
        before="Reads a file.",
        after="Obtains a file.",
        inverse=None,
        seed=42,
        injection_flagged=False,
    )


class TestMutationId:
    def test_is_deterministic_for_the_same_mutation(self):
        assert mutation_id_for(_entry(), server="fs") == mutation_id_for(_entry(), server="fs")

    def test_differs_when_any_defining_field_differs(self):
        base = mutation_id_for(_entry(), server="fs")

        assert mutation_id_for(_entry(tool_name="write_file"), server="fs") != base
        assert mutation_id_for(_entry(operator="parameter_rename"), server="fs") != base
        assert mutation_id_for(_entry(), server="other_server") != base

    def test_is_short_enough_to_quote_in_a_report(self):
        mutation_id = mutation_id_for(_entry(), server="fs")

        assert len(mutation_id) == 12
        assert all(c in "0123456789abcdef" for c in mutation_id)


def test_a_written_audit_log_round_trips_every_field(tmp_path):
    path = tmp_path / "mutations.jsonl"
    entries = [_entry(), _entry(tool_name="write_file")]

    write_mutation_audit(path, entries, server="fs", operator="description_update", task_id="t1")
    records = read_mutation_audit(path)

    assert len(records) == 2
    first = records[0]
    assert isinstance(first, MutationAuditRecord)
    assert first.tool_name == "read_file"
    assert first.operator == "description_update"
    assert first.before == "Reads a file."
    assert first.after == "Obtains a file."
    assert first.seed == 42
    assert first.server == "fs"
    assert first.task_id == "t1"
    assert first.inverse is None
    assert first.injection_flagged is False
    assert first.mutation_id == mutation_id_for(_entry(), server="fs")


def test_the_target_field_names_what_was_actually_edited():
    """`tool_name` is not enough for parameter_rename -- the log has to
    say WHICH parameter, or the edit cannot be reproduced by hand.
    """
    renamed = MutationLogEntry(
        tool_name="read_file",
        operator="parameter_rename",
        before="file_path",
        after="filePath",
        inverse={"filePath": "file_path"},
        seed=7,
        injection_flagged=False,
    )

    record = MutationAuditRecord.from_log_entry(renamed, server="fs", task_id="t1")

    assert record.target == "parameter:file_path"
    assert record.inverse == {"filePath": "file_path"}


def test_description_update_targets_the_description_not_a_parameter():
    record = MutationAuditRecord.from_log_entry(_entry(), server="fs", task_id="t1")

    assert record.target == "description"


def test_tool_addition_records_no_before_value_rather_than_a_placeholder():
    """`before is None` means "nothing existed to change" -- an injected
    tool has no prior description. It must never be written as `""`,
    which would read as "the description was empty before."
    """
    added = MutationLogEntry(
        tool_name="read_file_fast",
        operator="tool_addition",
        before=None,
        after="Reads a file, quickly.",
        inverse=None,
        seed=1,
        injection_flagged=False,
    )

    record = MutationAuditRecord.from_log_entry(added, server="fs", task_id="t1")

    assert record.before is None
    assert record.target == "tool"


def test_the_log_is_reconstructable_without_the_mutation_code(tmp_path):
    """F-18's literal done-criterion, asserted as a property of the file
    on disk rather than of the objects in memory: reading the raw JSON
    with nothing but `json` must yield every fact needed to describe the
    edit.
    """
    path = tmp_path / "mutations.jsonl"
    write_mutation_audit(path, [_entry()], server="fs", operator="description_update", task_id="t1")

    raw = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    assert len(raw) == 1
    required = {"mutation_id", "operator", "server", "tool_name", "target", "before", "after", "inverse", "seed", "task_id", "timestamp"}
    assert required <= set(raw[0])


def test_reading_a_missing_log_raises_rather_than_silently_returning_nothing(tmp_path):
    """An empty list would be indistinguishable from "a run that applied
    no mutations", which is a real and different state.
    """
    with pytest.raises(FileNotFoundError):
        read_mutation_audit(tmp_path / "absent.jsonl")
