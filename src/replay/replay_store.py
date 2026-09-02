"""Replay store (F-11): docs/SPEC.md §7 tier 1 (exact-key) only.

Indexes every recorded `ToolCall` from one or more session JSONL files under
`sha256(server + tool_name + canonical_json(args))`, so a later request with
identical (server, tool_name, args) resolves to HIT with the originally
recorded `result_shape`/`is_error`/`fault` — no live call needed.

Gate 2 scope, deliberately incomplete: docs/SPEC.md §7's tiers 2 (inverse-
mutation, F-12) and 3 (semantic, F-13) are NOT implemented here — both need
a real mutation's recorded inverse/argument-value multiset to resolve
against, and `mutate/` doesn't exist until Gate 3. Every lookup here either
exact-matches or MISSes; there is no degraded/fuzzy path yet, and MISS is
an ordinary, expected outcome (falls through to synthetic response
generation, F-14 — not built yet either), never an error.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from record.reader import read_session
from record.redact import redact_secrets
from record.schema import ToolCall

MatchTier = Literal["exact"]


@dataclass(frozen=True)
class RecordedResponse:
    """What a HIT resolves to — enough to reconstruct a structurally
    faithful synthetic response (F-14, later) without ever needing the
    live tool again. Never carries the actual result payload:
    `result_shape` is exactly what was recorded (F-02/F-04's shape-only,
    secrets-redacted rule) — replay inherits that redaction boundary,
    it doesn't reopen it.
    """

    result_shape: dict | None
    is_error: bool | None
    fault: bool | None
    match_tier: MatchTier


def replay_key(server: str, tool_name: str, arguments: dict) -> str:
    """`sha256(server + tool + canonical_json(args))` — docs/SPEC.md §7 tier 1.

    Arguments are redacted the same way `record/writer.py` redacts them
    before writing (`redact_secrets`, deterministic, no salt) — a
    recorded `ToolCall.arguments` is already the redacted form, so a
    lookup computed over raw, unredacted live arguments would silently
    and permanently miss every call whose arguments ever contained a
    secret-shaped value. Redacting on both the index and lookup sides
    keeps the two consistent regardless of which one the caller has in
    hand.

    "Canonical" means `sort_keys` + no incidental whitespace: two
    argument dicts built via different code paths (different key
    insertion order) must hash identically when their key/value pairs
    are the same, or an exact match that should hit would miss instead.
    """
    canonical_args = json.dumps(redact_secrets(arguments), sort_keys=True, separators=(",", ":"))
    payload = server + tool_name + canonical_args
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ReplayStore:
    """In-memory exact-key index over one or more recorded sessions."""

    def __init__(self) -> None:
        self._index: dict[str, RecordedResponse] = {}

    def index_session(self, path: Path) -> None:
        """Indexes every `ToolCall` record in one session JSONL file.

        Last-writer-wins on a repeated key (the same call recorded
        twice, e.g. a genuine retry) — the most recent recording is the
        most representative of current tool behavior, matching this
        project's existing precedent for a repeated-value index
        (`record/segment.py`'s data-flow value index uses the same
        rule, for the same reason).
        """
        for record in read_session(path):
            if isinstance(record, ToolCall):
                key = replay_key(record.server, record.tool_name, record.arguments)
                self._index[key] = RecordedResponse(
                    result_shape=record.result_shape,
                    is_error=record.is_error,
                    fault=record.fault,
                    match_tier="exact",
                )

    def lookup(self, server: str, tool_name: str, arguments: dict) -> RecordedResponse | None:
        """HIT (the recorded response) or MISS (`None`) — never raises
        for an unmatched key. See this module's docstring: MISS is
        ordinary here, not an error condition.
        """
        return self._index.get(replay_key(server, tool_name, arguments))
