"""F-18 mutation audit log.

A durable paper trail of exactly what Drifter changed, so any verdict
can be traced back to the precise edit that produced it.

`MutationLogEntry` (defined in `mutate/description_update.py` and shared
by every operator) already carried most of the facts, but only in
memory for the duration of one `run_mutation_comparison` call. Nothing
survived the process, which is exactly when a paper trail is worth
having. This module adds persistence plus the two fields F-18 names
that the in-memory entry lacked:

`mutation_id` is a deterministic digest over the fields that DEFINE the
mutation -- operator, seed, server, tool, before, after. Deterministic
rather than random on purpose: the same operator at the same seed
against the same manifest must produce the same id, which turns "this
regression came from that exact edit" into a checkable claim. A uuid4
would be unique but would make two identical mutations look like two
different ones, defeating the point.

`target` says what was actually edited, which `tool_name` alone does
not capture -- `parameter_rename` edits one specific PARAMETER, and
without naming it the entry cannot be reproduced by hand from the log.

Nothing here interprets or re-derives a mutation. The log is written
from what the operator itself reported, so a disagreement between the
log and the code is visible rather than silently reconciled.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from mcp_drifter.mutate.description_update import MutationLogEntry

# Long enough to make collision irrelevant at any corpus size this tool
# will ever see, short enough to quote inline in a report without
# wrapping. Truncation is safe here because this is a provenance label,
# never a security boundary.
_MUTATION_ID_LENGTH = 12


def mutation_id_for(entry: MutationLogEntry, server: str) -> str:
    """Deterministic id over the fields that define this mutation.

    `server` is included because the same operator at the same seed
    against two different servers is genuinely two different edits, and
    a corpus spanning several servers would otherwise collide them.
    `injection_flagged` is deliberately NOT included: it is an
    observation ABOUT the mutation, not part of what the mutation is,
    and including it would change the id of an otherwise-identical edit.
    """
    payload = json.dumps(
        {
            "operator": entry.operator,
            "server": server,
            "tool_name": entry.tool_name,
            "before": entry.before,
            "after": entry.after,
            "seed": entry.seed,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:_MUTATION_ID_LENGTH]


def _target_for(entry: MutationLogEntry) -> str:
    """What the operator edited, named precisely enough to reproduce by
    hand. `parameter_rename` carries the old parameter name in `before`,
    so the target names it explicitly rather than leaving a reader to
    infer it from the operator.
    """
    if entry.operator == "parameter_rename":
        # `before is None` is parameter_rename's own "nothing was
        # eligible to rename" outcome (it reports `after` as a sentinel
        # string and `inverse=None`). Recorded as an explicit no-op
        # rather than the misleading `parameter:None`, so a log reader
        # can tell "this tool had no eligible parameter" apart from
        # "a parameter literally named None was renamed".
        if entry.before is None:
            return "parameter:<none eligible>"
        return f"parameter:{entry.before}"
    if entry.operator == "tool_addition":
        return "tool"
    return "description"


@dataclass(frozen=True)
class MutationAuditRecord:
    """One applied mutation, as written to disk."""

    mutation_id: str
    timestamp: str
    task_id: str
    server: str
    operator: str
    tool_name: str
    target: str
    # `None` means nothing existed to change (tool_addition), never an
    # empty description -- see MutationLogEntry.before's own docstring.
    before: str | None
    after: str
    inverse: dict[str, str] | None
    seed: int
    injection_flagged: bool

    @classmethod
    def from_log_entry(
        cls, entry: MutationLogEntry, server: str, task_id: str, timestamp: str | None = None
    ) -> MutationAuditRecord:
        return cls(
            mutation_id=mutation_id_for(entry, server=server),
            timestamp=timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            task_id=task_id,
            server=server,
            operator=entry.operator,
            tool_name=entry.tool_name,
            target=_target_for(entry),
            before=entry.before,
            after=entry.after,
            inverse=entry.inverse,
            seed=entry.seed,
            injection_flagged=entry.injection_flagged,
        )


def write_mutation_audit(
    path: Path,
    entries: list[MutationLogEntry],
    server: str,
    operator: str,
    task_id: str,
    timestamp: str | None = None,
) -> list[MutationAuditRecord]:
    """Writes one JSONL line per applied mutation, returning what it
    wrote.

    `operator` is accepted and unused for per-entry purposes on purpose:
    each entry carries its OWN operator, and writing the caller's value
    over it would hide a disagreement between what the run thinks it
    applied and what the operator reported applying. It stays in the
    signature so the call site reads as a complete description of the
    run, and so a future consistency check has both values available.
    """
    records = [MutationAuditRecord.from_log_entry(e, server=server, task_id=task_id, timestamp=timestamp) for e in entries]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(asdict(record), sort_keys=True) + "\n")
    return records


def read_mutation_audit(path: Path) -> list[MutationAuditRecord]:
    """Reads an audit log back.

    Raises `FileNotFoundError` for a missing file rather than returning
    `[]`: "no log was written" and "a run that applied no mutations" are
    genuinely different states, and collapsing them would let a missing
    paper trail look like a clean one.
    """
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [MutationAuditRecord(**json.loads(line)) for line in lines]
