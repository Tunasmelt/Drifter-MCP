"""Replay store (F-11 + F-13): docs/SPEC.md §7 tiers 1 (exact-key) and 3
(semantic) — tier 2 (inverse-mutation, F-12) is still not implemented, since
it needs a real mutation's recorded inverse to resolve against, matching
this project's original Gate 2 scoping.

Indexes every recorded `ToolCall` from one or more session JSONL files under
`sha256(server + tool_name + canonical_json(args))`, so a later request with
identical (server, tool_name, args) resolves to HIT with the originally
recorded `result_shape`/`is_error`/`fault` — no live call needed.

F-13 (semantic, docs/SPEC.md §7 tier 3): built after docs/SPEC.md §15
limitation 16's real evidence (Gate 4's real second-user test) reframed this
from "nice to have for later mutation operators" to "possibly blocking
exact-tier replay's real-world viability against ANY real, curious agent" —
a fresh agent invocation naturally diverges from a single recorded
trajectory (different intermediate calls, different parameter naming a
mutation or the agent's own client library introduces) in ways exact-key
matching structurally cannot resolve. `lookup()` now falls back to a
looser, second index keyed on the sorted MULTISET of argument VALUES,
ignoring parameter names entirely, when the exact key misses. Every
lookup still either resolves (exact or semantic) or MISSes; there is no
fuzzy/partial-value matching at either tier, and MISS remains an ordinary,
expected outcome (falls through to synthetic response generation, F-14 —
not built yet either), never an error.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from record.reader import read_session
from record.redact import redact_secrets
from record.schema import ToolCall

MatchTier = Literal["exact", "semantic"]


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


def semantic_key(server: str, tool_name: str, arguments: dict) -> str:
    """`sha256(server + tool + canonical_json(sorted multiset of argument
    VALUES))` — docs/SPEC.md §7 tier 3, ignoring parameter names entirely.

    Redacted the same way and for the same reason `replay_key` already is
    (`record/redact.py`) — a lookup computed over raw, unredacted live
    arguments must still match an index built from already-redacted
    recorded ones.

    "Multiset," not "set": each value is independently canonicalized
    (`sort_keys` + no incidental whitespace, matching `replay_key`'s own
    canonicalization) and the resulting list of canonical value-strings is
    sorted and re-encoded as one JSON array — NOT concatenated as bare
    strings, which would risk an ambiguous boundary between two adjacent
    values. Sorting a `list`, not building a `set`, preserves duplicates:
    `{"a": 5, "b": 5}` (the value 5 twice) must hash differently from
    `{"a": 5}` (5 once), which a set-based multiset would collapse.
    """
    redacted = redact_secrets(arguments)
    canonical_values = sorted(json.dumps(v, sort_keys=True, separators=(",", ":")) for v in redacted.values())
    canonical_multiset = json.dumps(canonical_values, separators=(",", ":"))
    payload = server + tool_name + canonical_multiset
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ReplayStore:
    """In-memory exact-key + semantic (F-11/F-13) index over one or more
    recorded sessions."""

    def __init__(self) -> None:
        self._index: dict[str, RecordedResponse] = {}
        self._semantic_index: dict[str, RecordedResponse] = {}

    def index_session(self, path: Path) -> None:
        """Indexes every `ToolCall` record in one session JSONL file, under
        both its exact key and its semantic key.

        Last-writer-wins on a repeated key (the same call recorded
        twice, e.g. a genuine retry) — the most recent recording is the
        most representative of current tool behavior, matching this
        project's existing precedent for a repeated-value index
        (`record/segment.py`'s data-flow value index uses the same
        rule, for the same reason). Applies independently to each index:
        a repeated EXACT key and a repeated SEMANTIC key are each their
        own last-writer-wins sequence, since the two indexes serve
        different, independently-updated lookups.
        """
        for record in read_session(path):
            if isinstance(record, ToolCall):
                response = RecordedResponse(
                    result_shape=record.result_shape,
                    is_error=record.is_error,
                    fault=record.fault,
                    match_tier="exact",
                )
                self._index[replay_key(record.server, record.tool_name, record.arguments)] = response
                self._semantic_index[semantic_key(record.server, record.tool_name, record.arguments)] = replace(
                    response, match_tier="semantic"
                )

    def lookup(self, server: str, tool_name: str, arguments: dict) -> RecordedResponse | None:
        """HIT (the recorded response) or MISS (`None`) — never raises
        for an unmatched key. See this module's docstring: MISS is
        ordinary here, not an error condition.

        Tries the exact key first, since it's the higher-confidence tier
        (docs/SPEC.md §7's ordering: exact, inverse, semantic, decreasing
        specificity) — only falls back to the semantic key when the exact
        key misses, never the reverse, so a tighter match available is
        never discarded in favor of a looser one.
        """
        exact_hit = self._index.get(replay_key(server, tool_name, arguments))
        if exact_hit is not None:
            return exact_hit
        return self._semantic_index.get(semantic_key(server, tool_name, arguments))
