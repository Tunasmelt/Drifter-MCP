"""Reads a session JSONL file back into the Pydantic models from schema.py.

Deliberately kept separate from writer.py (CLAUDE.md, docs/PHASES.md Gate 1): a
shared read/write module invites silent format drift between what's written
and what's read.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from record.schema import SCHEMA_VERSION, Record, SessionStart, ToolCall, ToolsList, TrajectoryEnd

_RECORD_TYPES: dict[str, type[Record]] = {
    "session_start": SessionStart,
    "tools_list": ToolsList,
    "tool_call": ToolCall,
    "trajectory_end": TrajectoryEnd,
}


class SchemaVersionError(ValueError):
    """A record's schema_version isn't one this reader understands."""


class UnknownRecordTypeError(ValueError):
    """A record's record_type doesn't match any known model."""


def read_session(path: Path) -> Iterator[Record]:
    """Yields one parsed record per line of a session JSONL file, in order."""
    with path.open("r", encoding="utf-8") as f:
        for line_number, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            raw = json.loads(line)

            schema_version = raw.get("schema_version")
            if schema_version != SCHEMA_VERSION:
                raise SchemaVersionError(
                    f"{path}:{line_number}: unsupported schema_version {schema_version!r} "
                    f"(reader supports {SCHEMA_VERSION!r})"
                )

            record_type = raw.get("record_type")
            model = _RECORD_TYPES.get(record_type)
            if model is None:
                raise UnknownRecordTypeError(f"{path}:{line_number}: unknown record_type {record_type!r}")

            yield model.model_validate(raw)
