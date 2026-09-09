"""Replay store (F-11 + F-13 + F-12): docs/SPEC.md §7's full three-tier
scheme — exact-key, inverse-mutation, and semantic. F-12 was deferred at
Gate 2 for exactly the reason its own docs/FEATURES.md entry states ("needs a
real mutation's recorded inverse to resolve against") — built once
`mutate.parameter_rename` (F-40) gave it one: `lookup`'s
`inverse_param_map` parameter, populated by `replay/replay_proxy.py` from
the active mutation's own `MutationLogEntry.inverse` mappings.

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
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from mcp_drifter.record.reader import read_session
from mcp_drifter.record.redact import redact_secrets
from mcp_drifter.record.schema import ToolCall

MatchTier = Literal["exact", "semantic", "inverse"]


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
                # Only genuinely OBSERVED calls become evidence. A session
                # produced BY replay carries synthesized placeholders --
                # `synthetic` (F-17 tool_addition) and, since F-14,
                # `synthetic_miss` -- and such a session can legitimately be
                # handed back as a --fixture. Indexing those would resolve
                # fabricated content as exact historical hits and inflate
                # the very fidelity number the floor gates on, which is the
                # failure DEC-027 rejected fuzzy matching to avoid. Found
                # by external review; see tests/replay/test_replay_store.py.
                if record.result_provenance != "real":
                    continue
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

    def index_sessions(self, paths: Sequence[Path]) -> None:
        """Indexes every session in `paths` into this one store — DEC-027(b)'s
        corpus replay (docs/CHANGELOG.md). Nothing here is new behavior:
        `index_session` was always additive across files (last-writer-wins
        per key, see its own docstring), and `tests/replay/test_replay_store.
        py` already covered multi-file merging. This exists so the intent is
        named at the call site rather than left as a bare loop, and so
        `replay/corpus.py`'s resolved path list has an obvious destination.
        """
        for path in paths:
            self.index_session(path)

    def lookup(
        self,
        server: str,
        tool_name: str,
        arguments: dict,
        inverse_param_map: dict[str, str] | None = None,
    ) -> RecordedResponse | None:
        """HIT (the recorded response) or MISS (`None`) — never raises
        for an unmatched key. See this module's docstring: MISS is
        ordinary here, not an error condition.

        docs/SPEC.md §7's full three-tier ordering, decreasing specificity:
        exact, inverse, semantic. Tries the exact key first (`arguments`
        as given); only falls back to the next tier on a miss, never the
        reverse, so a tighter match available is never discarded in favor
        of a looser one.

        `inverse_param_map` (F-12), if given, is `{new_param_name:
        old_param_name}` for THIS tool under the currently-active
        mutation (`mutate.parameter_rename`'s own inverse — the caller,
        `replay/replay_proxy.py`, holds the per-tool mapping and passes
        in only the slice relevant to `tool_name`; this class has no
        mutation-specific knowledge itself, matching this project's
        module dependency order — `replay/` is upstream of `mutate/`
        and must not import from it). When given, a live call using the
        NEW parameter name is translated back to the OLD name and looked
        up again under the exact index — recovering the original,
        pre-mutation key exactly, not a fuzzy approximation, which is
        why an inverse hit gets the same full fidelity weight as an
        exact one (`evaluate.baseline._run_fidelity`) despite being
        reported as its own tier. A key in `arguments` that isn't in
        `inverse_param_map` passes through unchanged — this only
        reverses parameters that were actually renamed, not every
        argument on the call.
        """
        exact_hit = self._index.get(replay_key(server, tool_name, arguments))
        if exact_hit is not None:
            return exact_hit
        if inverse_param_map:
            de_mutated_args = {inverse_param_map.get(k, k): v for k, v in arguments.items()}
            inverse_hit = self._index.get(replay_key(server, tool_name, de_mutated_args))
            if inverse_hit is not None:
                return replace(inverse_hit, match_tier="inverse")
        return self._semantic_index.get(semantic_key(server, tool_name, arguments))
