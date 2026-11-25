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

    def __init__(
        self,
        server: str,
        source: str,
        status: int | None,
        error: str | None = None,
        scope: str | None = None,
        description: str | None = None,
        advice: str | None = None,
        reason: str | None = None,
    ) -> None:
        self.server = server
        self.source = source
        self.status = status  # None: Drifter itself decided the credentials were unusable, no request was made
        self.error = error
        self.scope = scope
        self.description = description
        self.advice = advice
        self.reason = reason
        super().__init__(self._describe())

    @classmethod
    def login_needed(cls, server: str, reason: str, advice: str | None = None) -> "UpstreamAuthRejected":
        """No usable stored login, decided locally (nothing was sent): `reason` says why, and the message
        says what to run."""
        return cls(
            server,
            "the stored login",
            None,
            reason=reason,
            advice=advice or f"run `drifter auth login {server}`",
        )

    def _describe(self) -> str:
        if self.status is None:
            return f"{self.server!r}: {self.reason}; {self.advice}"
        verb = "rejected" if self.status == 401 else "refused"
        detail = [f"HTTP {self.status}"]
        if self.error:
            detail.append(f"error={self.error}")
        if self.description:
            detail.append(f"the server said {self.description!r}")
        if self.scope:
            detail.append(f"it asks for scope {self.scope!r}")
        if self.advice:
            advice = self.advice
        elif self.status == 401:
            advice = f"the token is missing, expired or not valid for this server; check {self.source}"
        else:
            advice = f"the credentials were understood but are not permitted to do this; check {self.source} and its scopes"
        return f"{self.server!r} {verb} the credentials from {self.source} ({', '.join(detail)}): {advice}"


class LoginRefused(Exception):
    """An interactive login did not complete, or was refused on purpose (an authorization server
    that does not advertise PKCE S256, a wrong issuer, a user who said no, a timeout). The CLI turns
    it into exit 4. Messages name the server and the reason, never a token, a code or a state value."""
