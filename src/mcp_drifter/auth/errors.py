"""Auth errors. None of them may contain a token, client secret or authorization code (F-47):
they name the environment VARIABLE, the server and the HTTP status, never a credential value."""

from __future__ import annotations


class AuthSetupError(Exception):
    """The configured credential cannot be used at all, before any request is made: the
    environment variable is unset, empty, or not a single header-safe token. The CLI turns it
    into a config error (exit 4)."""


class UpstreamAuthRejected(Exception):
    """The upstream server answered 401 or 403 to credentials Drifter sent.

    The SDK collapses a non-2xx reply with no JSON-RPC body into a generic "Server returned an
    error response" and drops the HTTP status, so without this an expired token would fail
    silently inside an unattended `drifter observe`. Raised from a response hook (see
    `upstream.rejection_hook`), so it reaches the caller as a leaf of whatever exception group
    the transport's task group wraps it in.
    """

    def __init__(self, server: str, source: str, status: int, error: str | None = None, scope: str | None = None) -> None:
        self.server = server
        self.source = source
        self.status = status
        self.error = error
        self.scope = scope
        super().__init__(self._describe())

    def _describe(self) -> str:
        verb = "rejected" if self.status == 401 else "refused"
        detail = [f"HTTP {self.status}"]
        if self.error:
            detail.append(f"error={self.error}")
        if self.scope:
            detail.append(f"it asks for scope {self.scope!r}")
        if self.status == 401:
            advice = f"the token is missing, expired or not valid for this server; check {self.source}"
        else:
            advice = f"the credentials were understood but are not permitted to do this; check {self.source} and its scopes"
        return f"{self.server!r} {verb} the credentials from {self.source} ({', '.join(detail)}): {advice}"
