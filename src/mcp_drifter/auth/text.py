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


def scrub(text: str | None, secrets: list[str]) -> str | None:
    """`text` with every secret removed, longest first, an empty one ignored (it would match everywhere).

    Always applied to the RAW text, BEFORE `clean_server_text`: cleaning drops punctuation and caps the length, so a
    secret containing `~ = @` or sitting across the cap would no longer match afterwards and would print mangled or cut.
    """
    if text is None:
        return None
    for secret in sorted((s for s in secrets if s and s.strip()), key=len, reverse=True):
        text = text.replace(secret, "[redacted]")
    return text


# What every caller says instead of the SDK's "Invalid token response: <pydantic error>", whose text quotes the
# response it rejected: the first characters of a token the server just issued.
MALFORMED_TOKEN_RESPONSE = "the token endpoint's answer was not a valid token response"


def is_malformed_token_response(message: str) -> bool:
    return message.startswith("Invalid token response")


def clean_server_text(value: str | None, limit: int = MAX_LENGTH) -> str | None:
    """`value` with everything but ordinary token characters removed, trimmed, capped at `limit`;
    None if nothing is left."""
    if value is None:
        return None
    cleaned = _UNSAFE_RE.sub("", value).strip()[:limit]
    return cleaned or None
