"""Text that originates from a server, made safe to print.

A server this tool is pointed at may misbehave, so anything it supplies (a challenge's `error` and
`scope`, an OAuth `error` on a redirect) is reduced to the characters real values use and capped
before it reaches a terminal or a log. Nothing here is a secret-handling function: it is about not
letting a server write control sequences into someone's terminal.
"""

from __future__ import annotations

import re

_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_.:/+\- ]")
MAX_LENGTH = 80  # a structural cap on a printed value, not a tuned threshold


def clean_server_text(value: str | None, limit: int = MAX_LENGTH) -> str | None:
    """`value` with everything but ordinary token characters removed, trimmed, capped at `limit`;
    None if nothing is left."""
    if value is None:
        return None
    cleaned = _UNSAFE_RE.sub("", value).strip()[:limit]
    return cleaned or None
