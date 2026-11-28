"""`drifter doctor`'s auth checks (F-46, docs/FEATURES.md, docs/SPEC.md §5.2).

One finding per check, each with its own message and the next command to run, so a broken step is named instead
of hidden inside a generic "could not connect". Which checks apply depends on the credential:

  oauth               login (stored) -> discovery -> PKCE S256 -> grant drift
  client_credentials  credential -> discovery -> issuer -> grant drift        (no PKCE: nothing here is interactive)
  bearer_env          credential -> grant (always unknown)                    (a static token has no discovery or refresh)

Connectivity itself (does `initialize` succeed with this credential, including the silent refresh or the token
exchange) is `doctor`'s existing server check; this module adds what that check cannot say.

The grant check is a DRIFT check, not proof of authorization: MCP tools expose no per-tool required scope, so it
can only say that the scopes granted now differ from those your recorded sessions were made under. An unknown
grant (an opaque bearer token, a token response with no `scope`) is reported as unknown and never as a match.

Nothing here sends a credential: discovery is an unauthenticated probe, and findings carry variable NAMES, URLs
and scope names, never a token, secret or key.
"""

from __future__ import annotations

import json
import time

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import anyio
import httpx2
from mcp.client.auth.utils import (
    build_oauth_authorization_server_metadata_discovery_urls,
    build_protected_resource_metadata_discovery_urls,
    extract_resource_metadata_from_www_auth,
    issuers_match,
)
from mcp.shared.auth import OAuthMetadata, ProtectedResourceMetadata

from mcp_drifter.auth.config import is_loopback_host
from mcp_drifter.auth.errors import AuthSetupError
from mcp_drifter.auth.service import read_client_secret, read_private_key
from mcp_drifter.auth.storage import StorageError, TokenStore
from mcp_drifter.auth.text import clean_server_text
from mcp_drifter.auth.upstream import bearer_headers
from mcp_drifter.cli.config import ServerConfig
from mcp_drifter.record.fingerprint import normalize_scopes

Level = Literal["ok", "warn", "fail"]
_PROBE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "drifter-doctor", "version": "0"}},
}
_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
_MAX_LINES_SCANNED = 20  # SessionStart is line 1 and the tools list follows it; never read a whole recording for this


@dataclass(frozen=True)
class AuthFinding:
    level: Level
    check: str
    detail: str


def _ok(check: str, detail: str) -> AuthFinding:
    return AuthFinding("ok", check, detail)


def _warn(check: str, detail: str) -> AuthFinding:
    return AuthFinding("warn", check, detail)


def _fail(check: str, detail: str) -> AuthFinding:
    return AuthFinding("fail", check, detail)


# -- discovery ---------------------------------------------------------------------------------------------------


@dataclass
class _Discovery:
    finding: AuthFinding
    advertised_servers: list[str]
    metadata: OAuthMetadata | None


def _shown(value: object) -> str:
    return clean_server_text(str(value), 200) or "?"


async def _get(client: httpx2.AsyncClient, url: str) -> httpx2.Response | None:
    try:
        return await client.get(url)
    except httpx2.HTTPError:
        return None


async def _discover(server_url: str, timeout_seconds: float) -> _Discovery:
    """Discovery makes several requests in a row, so ONE deadline covers all of them (a per-request timeout alone
    would multiply against a server that answers the first and goes silent)."""
    try:
        with anyio.fail_after(timeout_seconds):
            return await _discover_within(server_url, timeout_seconds)
    except TimeoutError:
        return _Discovery(
            _fail("discovery", f"{_shown(server_url)} did not answer discovery within {timeout_seconds:g}s"), [], None
        )


async def _discover_within(server_url: str, timeout_seconds: float) -> _Discovery:
    nothing: list[str] = []
    async with httpx2.AsyncClient(timeout=timeout_seconds, follow_redirects=False) as client:
        try:
            probe = await client.post(server_url, json=_PROBE, headers=_HEADERS)
        except httpx2.HTTPError as exc:
            return _Discovery(_fail("discovery", f"could not reach {_shown(server_url)}: {type(exc).__name__}"), nothing, None)
        if probe.status_code != 401:
            return _Discovery(
                _warn(
                    "discovery",
                    f"the server did not ask for authorization (HTTP {probe.status_code}) on an unauthenticated request, "
                    "so there is nothing to discover; the configured `auth` may be unnecessary for this URL",
                ),
                nothing,
                None,
            )

        challenge_url = extract_resource_metadata_from_www_auth(probe)
        resource: ProtectedResourceMetadata | None = None
        for url in build_protected_resource_metadata_discovery_urls(challenge_url, server_url):
            response = await _get(client, url)
            if response is not None and response.status_code == 200:
                try:
                    resource = ProtectedResourceMetadata.model_validate_json(response.content)
                    break
                except ValueError:
                    continue
        advertised = [str(s) for s in resource.authorization_servers] if resource is not None else nothing
        auth_server = advertised[0] if advertised else None

        metadata: OAuthMetadata | None = None
        for url in build_oauth_authorization_server_metadata_discovery_urls(auth_server, server_url):
            response = await _get(client, url)
            if response is not None and response.status_code == 200:
                try:
                    metadata = OAuthMetadata.model_validate_json(response.content)
                    break
                except ValueError:
                    continue

    if metadata is None:
        what = (
            "the protected-resource metadata names an authorization server that publishes no metadata"
            if resource is not None
            else "the server publishes neither protected-resource metadata (RFC 9728) nor authorization-server metadata"
        )
        return _Discovery(_fail("discovery", f"{what}, so OAuth cannot be discovered for {_shown(server_url)}"), advertised, None)
    if auth_server is not None and not issuers_match(str(metadata.issuer), auth_server):
        return _Discovery(
            _fail(
                "discovery",
                f"the authorization server's metadata names issuer {_shown(metadata.issuer)} but the server pointed at "
                f"{_shown(auth_server)}; the issuer must match exactly (RFC 8414 §3.3), so Drifter will not trust it",
            ),
            advertised,
            None,
        )
    for label, endpoint in (("authorization", metadata.authorization_endpoint), ("token", metadata.token_endpoint)):
        parts = urlsplit(str(endpoint))
        if parts.scheme == "http" and not is_loopback_host(parts.hostname):
            return _Discovery(
                _fail(
                    "discovery",
                    f"the authorization server advertises a cleartext {label} endpoint (http://{_shown(parts.hostname)}); "
                    "credentials and tokens would travel unencrypted, so Drifter refuses it. The endpoint must be https",
                ),
                advertised,
                metadata,
            )
    return _Discovery(
        _ok("discovery", f"authorization server {_shown(metadata.issuer)} found for {_shown(server_url)}"), advertised, metadata
    )


# -- the individual checks -----------------------------------------------------------------------------------------


def _login_finding(server: ServerConfig, store: TokenStore) -> tuple[AuthFinding, list[str] | None, bool]:
    """(finding, granted scopes of the stored login, whether a usable login exists)."""
    login = f"run `drifter auth login {server.name}`"
    try:
        summary = store.summary()
    except StorageError as exc:
        return _fail("login", f"the stored login cannot be read ({_shown(exc)}); {login}"), None, False
    if not summary.has_tokens:
        return _fail("login", f"there is no stored login for {server.name!r}; {login}"), None, False
    scopes = None if summary.scope is None else sorted(set(summary.scope.split()))
    expires = summary.expires_at
    if expires is not None and expires < time.time():
        ago = int((time.time() - expires) // 60)
        if summary.refreshable:
            return _ok("login", f"the stored access token expired {ago} min ago and will be refreshed silently on next use"), scopes, True
        return (
            _fail("login", f"the stored login expired {ago} min ago and cannot be refreshed; {login}"),
            scopes,
            False,
        )
    when = "no expiry reported" if expires is None else f"expires in {int((expires - time.time()) // 60)} min"
    return _ok("login", f"logged in ({when}; {'refreshable' if summary.refreshable else 'not refreshable'})"), scopes, True


def _pkce_finding(server: ServerConfig, metadata: OAuthMetadata) -> AuthFinding:
    if "S256" in (metadata.code_challenge_methods_supported or []):
        return _ok("pkce", "the authorization server advertises PKCE S256")
    return _fail(
        "pkce",
        f"the authorization server of {server.name!r} does not advertise PKCE S256 (code_challenge_methods_supported); "
        "Drifter refuses to log in without it. Ask the server's operator to enable S256, or use another auth type",
    )


def _issuer_finding(server: ServerConfig, discovery: _Discovery) -> AuthFinding | None:
    auth = server.auth
    assert auth is not None and auth.issuer is not None
    if discovery.metadata is None and not discovery.advertised_servers:
        return None  # discovery itself failed and said why; this check cannot add to it
    if discovery.advertised_servers and not any(issuers_match(a, auth.issuer) for a in discovery.advertised_servers):
        named = ", ".join(_shown(a) for a in discovery.advertised_servers)
        return _fail(
            "issuer",
            f"auth.issuer is {_shown(auth.issuer)} but the server points at {named}; credentials are only ever sent to "
            "the configured issuer, so nothing will be sent. Set auth.issuer to the authorization server that issued "
            "this client's credentials, if the server's list is right",
        )
    grants = discovery.metadata.grant_types_supported if discovery.metadata is not None else None
    if grants is not None and "client_credentials" not in grants:
        return _warn(
            "issuer",
            f"auth.issuer matches, but its metadata does not list the client_credentials grant (it lists "
            f"{', '.join(_shown(g) for g in grants)}); the token request may be refused",
        )
    return _ok("issuer", f"auth.issuer {_shown(auth.issuer)} is the authorization server the server points at")


# -- recorded sessions -----------------------------------------------------------------------------------------------

# A recorded grant: ("none", None) for no auth; ("oauth", ["read"]) known; ("oauth", None) unknown.
_Recorded = tuple[str, list[str] | None]


def _recorded_grants(runs_dir: Path, server_name: str) -> list[_Recorded]:
    grants: list[_Recorded] = []
    try:
        files = sorted(runs_dir.glob("*.jsonl"))
    except OSError:
        return grants
    for path in files:
        environment: dict | None = None
        belongs = False
        try:
            with path.open(encoding="utf-8") as handle:
                for line_number, line in enumerate(handle):
                    if line_number >= _MAX_LINES_SCANNED:
                        break
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(record, dict):
                        continue
                    if record.get("record_type") == "session_start" and isinstance(record.get("environment"), dict):
                        environment = record["environment"]
                    elif record.get("record_type") == "tools_list":
                        belongs = record.get("server") == server_name
                        break
        except (OSError, UnicodeDecodeError):
            continue
        if environment is None or not belongs:
            continue
        auth_type = environment.get("auth_type")
        if auth_type is None:
            grants.append(("none", None))  # absent means unauthenticated, not unknown: 0.2.0 could not authenticate at all
        else:
            scopes = environment.get("granted_scopes")
            grants.append((str(auth_type), normalize_scopes(scopes) if isinstance(scopes, list) else None))
    return grants


def _describe(grant: _Recorded) -> str:
    auth_type, scopes = grant
    if auth_type == "none":
        return "no auth"
    return f"{auth_type} with unknown scopes" if scopes is None else f"{auth_type} {scopes!r}"


def _grant_finding(server: ServerConfig, runs_dir: Path, current: _Recorded) -> AuthFinding:
    auth_type, scopes = current
    if auth_type == "bearer_env":
        return _ok(
            "grant",
            "unknown: a static bearer token carries no scope information, so recorded sessions cannot be compared with it",
        )
    recorded = _recorded_grants(runs_dir, server.name)
    if not recorded:
        return _ok("grant", "no recorded sessions for this server to compare with")
    if scopes is None:
        return _warn(
            "grant",
            f"the current grant is unknown (the token carries no scope), so {len(recorded)} recorded session(s) cannot "
            "be compared with it; do not treat them as matching",
        )
    same = sum(1 for r in recorded if r == (auth_type, scopes))
    if same == len(recorded):
        return _ok("grant", f"{len(recorded)} recorded session(s) were recorded under the current grant ({_describe(current)})")
    different: dict[str, int] = {}
    for r in recorded:
        if r != (auth_type, scopes):
            different[_describe(r)] = different.get(_describe(r), 0) + 1
    breakdown = ", ".join(f"{count} under {text}" for text, count in sorted(different.items()))
    fix = f"`drifter auth login {server.name}` with the scopes you want, then " if auth_type == "oauth" else ""
    return _warn(
        "grant",
        f"{len(recorded) - same} of {len(recorded)} recorded session(s) were made under a different grant than the current one "
        f"({_describe(current)}): {breakdown}. Sessions under different grants do not aggregate into one baseline "
        f"(a server may list different tools per scope). Re-record: {fix}run `drifter observe`, or restore the scopes "
        "with `auth.scopes` in drifter.yaml. This compares grants; it does not prove any tool is authorized",
    )


# -- entry point -----------------------------------------------------------------------------------------------------


async def diagnose_auth(
    server: ServerConfig,
    *,
    runs_dir: Path,
    environ: Mapping[str, str],
    timeout_seconds: float,
    store: TokenStore | None = None,
    granted_scopes: list[str] | None = None,
    connected: bool = True,
) -> list[AuthFinding]:
    """The auth findings for one server, in the order they should be fixed. `granted_scopes` and `connected` come
    from the connectivity check: a service credential's grant only exists once it has been exchanged for a token."""
    auth = server.auth
    if server.url is None or auth is None or auth.type == "none":
        return []
    findings: list[AuthFinding] = []

    if auth.type == "bearer_env":
        try:
            bearer_headers(auth, server.name, environ)
        except AuthSetupError as exc:
            findings.append(_fail("credential", str(exc)))
        else:
            findings.append(_ok("credential", f"environment variable {auth.token_env} holds a usable token"))
        findings.append(_grant_finding(server, runs_dir, ("bearer_env", None)))
        return findings

    current: _Recorded | None = None
    if auth.type == "client_credentials":
        try:
            if auth.secret_env is not None:
                read_client_secret(auth, server.name, environ)
                findings.append(_ok("credential", f"environment variable {auth.secret_env} holds a usable client secret"))
            else:
                read_private_key(auth, server.name, environ)
                findings.append(
                    _ok("credential", f"environment variable {auth.private_key_env} holds a private key that can sign with {auth.signing_algorithm}")
                )
        except AuthSetupError as exc:
            findings.append(_fail("credential", str(exc)))
        discovery = await _discover(server.url, timeout_seconds)
        findings.append(discovery.finding)
        issuer = _issuer_finding(server, discovery)
        if issuer is not None:
            findings.append(issuer)
        if connected:
            current = ("client_credentials", normalize_scopes(granted_scopes))
    else:  # oauth
        if store is None:
            try:
                store = TokenStore(server.name, server.url, project_root=server.project_root)
            except StorageError as exc:
                findings.append(_fail("login", str(exc)))
        login_ok = False
        scopes: list[str] | None = None
        if store is not None:
            login, scopes, login_ok = _login_finding(server, store)
            findings.append(login)
        discovery = await _discover(server.url, timeout_seconds)
        findings.append(discovery.finding)
        if discovery.metadata is not None:
            findings.append(_pkce_finding(server, discovery.metadata))
        if login_ok:
            current = ("oauth", scopes)

    if current is not None:
        findings.append(_grant_finding(server, runs_dir, current))
    return findings
