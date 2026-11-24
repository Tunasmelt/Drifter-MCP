"""Building the credentials Drifter sends upstream, and noticing when they are refused (F-41).

`bearer_env`: `Authorization: Bearer <value of the named environment variable>` on every upstream
request and nothing else. The value is read at connect time, never at config load (a config can
be loaded where the secret is not available, e.g. `drifter stats`), and never stored.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping

import httpx2

from mcp_drifter.auth.config import AuthConfig
from mcp_drifter.auth.errors import AuthSetupError, UpstreamAuthRejected

_VISIBLE_ASCII_RE = re.compile(r"^[\x21-\x7e]+$")  # no spaces, controls, or non-ASCII


def bearer_headers(auth: AuthConfig, server_name: str, environ: Mapping[str, str]) -> dict[str, str]:
    """The single header `bearer_env` adds. Raises `AuthSetupError` (naming the variable and the
    server, never its value) if the variable is unset, empty, or not a lone header-safe token.

    Surrounding whitespace is stripped (`export T=$(cat token.txt)` leaves a trailing newline).
    Anything else a header cannot carry is refused rather than "cleaned", because a value with
    an embedded CR/LF would be a header-injection hole and a value with a space is not a token.
    """
    name = auth.token_env
    assert name is not None  # AuthConfig guarantees it for type bearer_env
    where = f"auth.token_env of server {server_name!r}"
    raw = environ.get(name)
    if raw is None:
        raise AuthSetupError(f"environment variable {name} is not set ({where}); export it before running drifter")
    token = raw.strip()
    if not token:
        raise AuthSetupError(f"environment variable {name} is empty ({where})")
    if not _VISIBLE_ASCII_RE.match(token):
        raise AuthSetupError(
            f"environment variable {name} ({where}) must hold a single token of visible ASCII "
            "characters: no spaces, newlines, quotes-and-text or non-ASCII. Check how it was exported"
        )
    return {"Authorization": f"Bearer {token}"}


_CHALLENGE_PARAM_RE = re.compile(r'(\w+)="([^"]*)"')
# What a challenge's `error` or `scope` may contain once it reaches a terminal: the characters real
# values use (scope names, URLs as scopes), nothing a terminal would interpret, and a hard length cap.
# The text comes from the server, which this tool is pointed at precisely because it may misbehave.
_UNSAFE_CHALLENGE_CHARS_RE = re.compile(r"[^A-Za-z0-9_.:/+\- ]")
_MAX_CHALLENGE_VALUE_LENGTH = 80


def _clean_challenge_value(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = _UNSAFE_CHALLENGE_CHARS_RE.sub("", value).strip()[:_MAX_CHALLENGE_VALUE_LENGTH]
    return cleaned or None


def rejection_hook(
    server_name: str,
    source: str,
    on_rejected: Callable[[UpstreamAuthRejected], None] | None = None,
) -> Callable[[httpx2.Response], Awaitable[None]]:
    """An httpx response hook that turns a credential failure into `UpstreamAuthRejected`.

    A credential failure is a 401, or a 403 whose challenge says `insufficient_scope` (the
    credential's grant is wrong). An ordinary 403 ("not allowed to do THIS") is an answer the
    agent should see, like any tool error, not a reason to end the session: raising on it would
    kill an `observe` recording over one forbidden call (found by the P2 audit).

    `on_rejected` is called with the exception just before it is raised, and may not return:
    `drifter observe` passes a function that flushes the recording and exits the process,
    because the raised exception alone cannot end a proxy whose agent-side stdin read is
    blocked in a thread (see tests/auth/test_observe_rejection.py). Callers that only want the
    exception (`doctor`) pass nothing.

    Installed only when `auth` is configured, so an unauthenticated server fails exactly as it
    always did. Reads the status and the `error` and `scope` parameters of the `WWW-Authenticate`
    challenge (what the server asks for), never the request headers and never the body.
    """

    async def hook(response: httpx2.Response) -> None:
        if response.status_code not in (401, 403):
            return
        params = dict(_CHALLENGE_PARAM_RE.findall(response.headers.get("www-authenticate", "")))
        error, scope = _clean_challenge_value(params.get("error")), _clean_challenge_value(params.get("scope"))
        if response.status_code == 403 and error != "insufficient_scope":
            return
        rejection = UpstreamAuthRejected(server_name, source, response.status_code, error=error, scope=scope)
        if on_rejected is not None:
            on_rejected(rejection)
        raise rejection

    return hook
