"""The `servers[].auth` block of drifter.yaml (F-41, docs/SPEC.md §11).

Validation here is deliberately strict, because the failure it prevents is silent: before this
block was typed, `ServerConfig` accepted any unknown key (`extra="allow"`), so a user who wrote
`auth:` got NO authentication and no warning. Now:

  - the block rejects unknown keys (a misspelled `token_envv` is an error, not a no-op);
  - fields that do not belong to the chosen `type` are errors, not ignored;
  - `*_env` fields hold the NAME of an environment variable, and a value that looks like a
    secret is rejected so a pasted token fails at load instead of being committed. The error
    never echoes the rejected value (a static message, plus `hide_input_in_errors` on the root
    config model, since pydantic otherwise prints `input_value=...`).

Whether a name is a variable name or a secret cannot be decided perfectly. The rule is: it must
be shaped like a variable name, must not trip F-04's secret patterns (`record.redact`), and must
not be a long run of hex digits (the most common API-key shape that is also a valid name).
A secret that is a plausible variable name and trips none of that gets through, which is why the
documentation tells users to export the secret and name the variable, never the reverse.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from mcp_drifter.record.redact import redact_string

AuthType = Literal["none", "oauth", "bearer_env", "client_credentials"]

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HEX_RUN_RE = re.compile(r"^[0-9A-Fa-f]{32,}$")
# Longest variable name accepted. Real names are well under this; a longer string that fits the
# name shape is far more likely to be a token than a name.
_MAX_ENV_NAME_LENGTH = 64

# Which fields each type uses. A field set on a type that does not use it is an error.
_FIELDS_BY_TYPE: dict[str, frozenset[str]] = {
    "none": frozenset(),
    "oauth": frozenset({"scopes", "client_id"}),
    "bearer_env": frozenset({"token_env"}),
    "client_credentials": frozenset(
        {"scopes", "client_id", "secret_env", "private_key_env", "issuer", "signing_algorithm"}
    ),
}
_REQUIRED_BY_TYPE: dict[str, frozenset[str]] = {
    "bearer_env": frozenset({"token_env"}),
    # `issuer`: the credentials are only ever sent to the authorization server the CONFIG names. Without it
    # the MCP server's own metadata would decide who receives the client secret.
    "client_credentials": frozenset({"client_id", "issuer"}),
}
_OPTIONAL_FIELDS = ("scopes", "client_id", "token_env", "secret_env", "private_key_env", "issuer", "signing_algorithm")
# Asymmetric only: `none` and the HMAC family are not a proof of holding a private key.
SigningAlgorithm = Literal["RS256", "RS384", "RS512", "PS256", "ES256", "ES384", "ES512"]


def is_loopback_host(host: str | None) -> bool:
    """True only for `localhost` and literal loopback addresses. Parsed, not pattern-matched:
    `localhost.evil.com`, `127.0.0.1.evil.com` and `localhost@evil.com` are remote hosts."""
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def looks_like_an_environment_variable_name(value: str) -> bool:
    """True if `value` is plausibly a variable NAME and not a secret pasted in its place."""
    if len(value) > _MAX_ENV_NAME_LENGTH or not _ENV_NAME_RE.match(value):
        return False
    if _HEX_RUN_RE.match(value):
        return False
    return redact_string(value) == value  # F-04's patterns find nothing to redact


class AuthConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    type: AuthType = "none"
    scopes: list[str] | None = None
    client_id: str | None = None
    token_env: str | None = None
    secret_env: str | None = None
    private_key_env: str | None = None
    issuer: str | None = None
    signing_algorithm: SigningAlgorithm | None = None

    @field_validator("token_env", "secret_env", "private_key_env")
    @classmethod
    def _is_a_variable_name_not_a_secret(cls, value: str | None, info) -> str | None:
        if value is not None and not looks_like_an_environment_variable_name(value):
            # Static text: this value may BE the secret, so it is never interpolated.
            raise ValueError(
                f"{info.field_name} must be the NAME of an environment variable (letters, digits and "
                "underscores, not starting with a digit, such as CRM_TOKEN), never the secret itself. "
                "Export the secret in your environment and name that variable here. If this really is a "
                "variable name, rename it (UPPER_SNAKE_CASE is safest): long mixed-case names look like tokens"
            )
        return value

    @field_validator("scopes")
    @classmethod
    def _scopes_are_non_empty_strings(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and (not value or any(not scope.strip() for scope in value)):
            raise ValueError("scopes must be a non-empty list of non-empty scope strings")
        return value

    @field_validator("issuer")
    @classmethod
    def _issuer_is_a_safe_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parts = urlparse(value)
        try:
            host = parts.hostname
        except ValueError:
            host = None
        problem = None
        if parts.scheme not in ("http", "https") or not host:
            problem = "an http(s) URL such as https://auth.example.com/"
        elif parts.username is not None or parts.password is not None:
            problem = "a URL with no user:password@ in it"
        elif parts.scheme == "http" and not is_loopback_host(host):
            problem = "https (a cleartext issuer is only allowed on localhost)"
        if problem:
            raise ValueError(f"issuer must be {problem}")  # never echoes the value
        return value

    @field_validator("client_id")
    @classmethod
    def _client_id_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("client_id must not be blank")
        return value

    @model_validator(mode="after")
    def _fields_match_the_type(self) -> "AuthConfig":
        allowed = _FIELDS_BY_TYPE[self.type]
        stray = [f for f in _OPTIONAL_FIELDS if getattr(self, f) is not None and f not in allowed]
        if stray:
            uses = ", ".join(sorted(allowed)) or "no other fields"
            raise ValueError(
                f"auth type {self.type!r} does not use {', '.join(stray)} (it uses: {uses}); "
                "remove them, or set the type they belong to"
            )
        missing = [f for f in sorted(_REQUIRED_BY_TYPE.get(self.type, frozenset())) if getattr(self, f) is None]
        if missing:
            raise ValueError(f"auth type {self.type!r} requires {', '.join(missing)}")
        if self.type == "client_credentials":
            if (self.secret_env is None) == (self.private_key_env is None):
                raise ValueError(
                    "auth type 'client_credentials' needs exactly one of secret_env (a client secret) or "
                    "private_key_env (a private key that signs a client assertion)"
                )
            if self.signing_algorithm is not None and self.private_key_env is None:
                raise ValueError("signing_algorithm only applies with private_key_env")
            if self.private_key_env is not None and self.signing_algorithm is None:
                self.signing_algorithm = "RS256"
        return self
