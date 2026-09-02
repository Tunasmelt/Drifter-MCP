"""Secret redaction (F-04).

Pattern-matches common credential shapes and redacts them from argument
values and headers before write — docs/SPEC.md §6, SECURITY.md. Applied in both
the parsed JSONL writer path and the raw frame mirror (record/writer.py);
the mirror gets the SAME redaction, not a weaker pass, since it's a common
place to accidentally leave a gap by treating it as "just a backup copy."

Structural, not free-text: this only ever replaces matched substrings with
a fixed marker. It never generates or alters non-secret content — that
distinction matters for the same reason docs/SPEC.md §10 forbids free-text
mutation of tool descriptions elsewhere in this project.
"""

from __future__ import annotations

import math
import re
from typing import Any

REDACTED = "[REDACTED]"

# Order matters: more specific/structured patterns first, so a JWT embedded
# in a "Bearer <token>" string is fully consumed by the JWT pattern before
# the looser Bearer pattern would otherwise re-match a smaller slice of it.
_JWT_RE = re.compile(r"[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")
_OPENAI_KEY_RE = re.compile(r"sk-[A-Za-z0-9_-]{20,}")
_BEARER_RE = re.compile(r"[Bb]earer\s+[A-Za-z0-9\-_.=]{8,}")

_KNOWN_SECRET_PATTERNS = (_JWT_RE, _OPENAI_KEY_RE, _BEARER_RE)

# Catch-all: a long random-looking token not matching any known format.
# Entropy alone isn't discriminating enough — concatenated natural-language
# identifiers ("customer-42-onboarding-flow") measured 4.1 bits/char in
# testing, comfortably above a naive threshold, and would be false
# positives. What actually separates them from real tokens is character-
# class mix: every mixed-case-and-digit random token tested (API keys,
# bearer tokens) uses all three of upper/lower/digit, while hand-written
# identifiers overwhelmingly don't reach for uppercase. Both conditions —
# entropy AND class mix — are required, so this stays a narrow backstop
# behind the structured patterns above rather than a second guess at them.
"""
Heuristic, not derived: entropy threshold and the require-both
(entropy AND character-class mix) condition were chosen empirically
against test-fixture examples, not against a real corpus of secrets
vs. identifiers. Treat as a tunable default in the spirit of docs/SPEC.md
§9's calibration register, not as a validated boundary. False
negatives (a real secret that's low-entropy or single-case) are
possible and not covered by pattern-matching alone.
"""
_HIGH_ENTROPY_CANDIDATE_RE = re.compile(r"[A-Za-z0-9_\-]{20,}")
_HIGH_ENTROPY_THRESHOLD_BITS_PER_CHAR = 3.5


def _shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    counts: dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    length = len(s)
    return -sum((n / length) * math.log2(n / length) for n in counts.values())


def _has_mixed_character_classes(s: str) -> bool:
    return any(ch.isupper() for ch in s) and any(ch.islower() for ch in s) and any(ch.isdigit() for ch in s)


def _redact_high_entropy(text: str) -> str:
    def _maybe_redact(match: re.Match[str]) -> str:
        candidate = match.group(0)
        if _has_mixed_character_classes(candidate) and _shannon_entropy(candidate) >= _HIGH_ENTROPY_THRESHOLD_BITS_PER_CHAR:
            return REDACTED
        return candidate

    return _HIGH_ENTROPY_CANDIDATE_RE.sub(_maybe_redact, text)


def redact_string(text: str) -> str:
    """Redacts every known secret pattern, then sweeps for high-entropy leftovers."""
    for pattern in _KNOWN_SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return _redact_high_entropy(text)


def redact_secrets(value: Any) -> Any:
    """Recursively redacts secret-shaped strings anywhere inside value.

    Applied to argument values and headers before write (docs/SPEC.md §6). Never
    mutates `value` in place — callers pass in live objects still headed to
    `sink.send()` on the forwarding path, which must stay byte-for-byte
    unmodified (F-01); redaction only ever touches what gets written to disk.
    """
    if isinstance(value, str):
        return redact_string(value)
    if isinstance(value, dict):
        return {k: redact_secrets(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_secrets(v) for v in value]
    return value


def redact_rpc_payload(raw: dict) -> dict:
    """Redacts a raw JSON-RPC message dict's payload fields only.

    Scoped to `params` / `result` / `error.data` — the fields that can
    carry caller-supplied or tool-returned values. Protocol envelope fields
    (`jsonrpc`, `id`, `method`) are structural, never secret-bearing, and
    are left untouched so the raw mirror stays useful for re-parsing.
    """
    redacted = dict(raw)
    if "params" in redacted:
        redacted["params"] = redact_secrets(redacted["params"])
    if "result" in redacted:
        redacted["result"] = redact_secrets(redacted["result"])
    error = redacted.get("error")
    if isinstance(error, dict) and "data" in error:
        redacted["error"] = {**error, "data": redact_secrets(error["data"])}
    return redacted
