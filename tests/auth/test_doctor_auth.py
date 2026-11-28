"""F-46: `drifter doctor`'s auth checks (docs/FEATURES.md F-46: "each applicable doctor check fails in isolation
against a deliberately broken fixture with its own message"). Written before `auth/diagnose.py`.

One scenario, one broken thing, one finding with its own message and the next command to run. Each test also
asserts the OTHER checks stay quiet, because a doctor that reports everything as broken at once hides which
step to fix first.

Which checks apply to which credential:
  oauth               login (stored) -> discovery -> PKCE S256 -> grant drift
  client_credentials  credential -> discovery -> issuer/token endpoint -> grant drift   (no PKCE: nothing interactive)
  bearer_env          credential only; a static token has no discovery, no refresh, and an UNKNOWN grant

The grant check is a DRIFT check, not authorization proof: MCP tools expose no per-tool required scope, so it only
says "the scopes granted now differ from the ones your recorded sessions were made under".
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from mcp_drifter.auth.diagnose import AuthFinding, diagnose_auth
from mcp_drifter.cli.config import ServerConfig
from oauth_server import MACHINE_CLIENT_ID, FixtureConfig, serve_oauth_fixture
from stub_browser import log_in, store_for

SECRET_VAR = "DOCTOR_CLIENT_SECRET"
TOKEN_VAR = "DOCTOR_TOKEN"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _oauth(handle) -> ServerConfig:
    return ServerConfig.model_validate({"name": "remote-crm", "url": handle.mcp_url, "auth": {"type": "oauth"}})


def _cc(handle, **auth) -> ServerConfig:
    body = {"type": "client_credentials", "client_id": MACHINE_CLIENT_ID, "issuer": handle.issuer, "secret_env": SECRET_VAR}
    body.update(auth)
    return ServerConfig.model_validate({"name": "remote-crm", "url": handle.mcp_url, "auth": body})


def _bearer(handle) -> ServerConfig:
    return ServerConfig.model_validate(
        {"name": "remote-crm", "url": handle.mcp_url, "auth": {"type": "bearer_env", "token_env": TOKEN_VAR}}
    )


async def _run(server, tmp_path, *, environ=None, store=None, granted_scopes=None, connected=True, runs=None) -> dict[str, AuthFinding]:
    findings = await diagnose_auth(
        server,
        runs_dir=runs or (tmp_path / "runs"),
        environ=environ or {},
        timeout_seconds=15,
        store=store,
        granted_scopes=granted_scopes,
        connected=connected,
    )
    names = [f.check for f in findings]
    assert len(names) == len(set(names)), f"a check was reported twice: {names}"
    return {f.check: f for f in findings}


def _levels(found: dict[str, AuthFinding]) -> dict[str, str]:
    return {name: f.level for name, f in found.items()}


def _record(runs: Path, sid: str, *, server="remote-crm", auth_type=None, scopes="absent", extra=()) -> None:
    """A recorded session as a 0.3.0 `observe` writes it (or, with no auth, as 0.2.0 did)."""
    runs.mkdir(parents=True, exist_ok=True)
    env = {"agent_identity": "a/1", "model_name": None, "server_versions": {}, "tool_manifest_hash": "sha256:m", "fingerprint": "sha256:f"}
    if auth_type is not None:
        env["auth_type"] = auth_type
        env["granted_scopes"] = scopes
    lines = [
        {"schema_version": "0.1", "record_type": "session_start", "session_id": sid, "seq": 0, "environment": env},
        {"schema_version": "0.1", "record_type": "tools_list", "session_id": sid, "seq": 1, "server": server},
        *extra,
    ]
    (runs / f"{sid}.jsonl").write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")


# --- oauth: each step broken in isolation ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_healthy_oauth_setup_passes_every_check(tmp_path):
    async with serve_oauth_fixture(FixtureConfig()) as handle:
        store = await log_in(handle, tmp_path / "cfg")
        found = await _run(_oauth(handle), tmp_path, store=store)
    assert _levels(found) == {"login": "ok", "discovery": "ok", "pkce": "ok", "grant": "ok"}, {k: v.detail for k, v in found.items()}


@pytest.mark.anyio
async def test_no_stored_login_fails_only_the_login_check_and_says_what_to_run(tmp_path):
    async with serve_oauth_fixture(FixtureConfig()) as handle:
        found = await _run(_oauth(handle), tmp_path, store=store_for(handle, tmp_path / "cfg"))
    assert found["login"].level == "fail" and "drifter auth login remote-crm" in found["login"].detail
    assert [n for n, f in found.items() if f.level == "fail"] == ["login"], _levels(found)


@pytest.mark.anyio
async def test_an_expired_login_that_cannot_refresh_fails_the_login_check(tmp_path):
    async with serve_oauth_fixture(FixtureConfig(refresh_tokens=False)) as handle:
        store = await log_in(handle, tmp_path / "cfg")
        tokens = await store.get_tokens()
        await store.set_tokens(tokens.model_copy(update={"refresh_token": None}), issued_at=time.time() - 7200)
        found = await _run(_oauth(handle), tmp_path, store=store)
    assert found["login"].level == "fail"
    assert "expired" in found["login"].detail and "cannot be refreshed" in found["login"].detail
    assert "drifter auth login remote-crm" in found["login"].detail


@pytest.mark.anyio
async def test_an_expired_but_refreshable_login_is_fine_and_says_it_will_refresh(tmp_path):
    async with serve_oauth_fixture(FixtureConfig()) as handle:
        store = await log_in(handle, tmp_path / "cfg")
        await store.set_tokens(await store.get_tokens(), issued_at=time.time() - 7200)
        found = await _run(_oauth(handle), tmp_path, store=store)
    assert found["login"].level == "ok" and "refresh" in found["login"].detail


@pytest.mark.anyio
async def test_an_authorization_server_without_s256_fails_only_the_pkce_check(tmp_path):
    async with serve_oauth_fixture(FixtureConfig(advertise_s256=False)) as handle:
        store = store_for(handle, tmp_path / "cfg")
        found = await _run(_oauth(handle), tmp_path, store=store)
    assert found["pkce"].level == "fail" and "S256" in found["pkce"].detail
    assert found["discovery"].level == "ok"


@pytest.mark.anyio
async def test_a_server_that_publishes_no_authorization_metadata_fails_only_discovery(tmp_path):
    async with serve_oauth_fixture(FixtureConfig(protection="static_bearer")) as handle:  # 401, but no OAuth metadata
        found = await _run(_oauth(handle), tmp_path, store=store_for(handle, tmp_path / "cfg"))
    assert found["discovery"].level == "fail" and "metadata" in found["discovery"].detail
    assert "pkce" not in found or found["pkce"].level != "ok", "PKCE cannot be judged without metadata"


@pytest.mark.anyio
async def test_a_server_that_never_asks_for_authorization_is_a_warning_not_a_failure(tmp_path):
    async with serve_oauth_fixture(FixtureConfig(protection="open")) as handle:
        found = await _run(_oauth(handle), tmp_path, store=store_for(handle, tmp_path / "cfg"))
    assert found["discovery"].level == "warn" and "did not ask for authorization" in found["discovery"].detail


@pytest.mark.anyio
async def test_an_authorization_server_that_names_a_different_issuer_fails_discovery(tmp_path):
    async with serve_oauth_fixture(FixtureConfig(issuer_mismatch=True)) as handle:
        found = await _run(_oauth(handle), tmp_path, store=store_for(handle, tmp_path / "cfg"))
    assert found["discovery"].level == "fail" and "issuer" in found["discovery"].detail


@pytest.mark.anyio
async def test_a_cleartext_token_endpoint_on_a_remote_host_fails_the_pkce_check_family(tmp_path):
    async with serve_oauth_fixture(FixtureConfig(token_endpoint_override="http://auth.example.com/token")) as handle:
        found = await _run(_oauth(handle), tmp_path, store=store_for(handle, tmp_path / "cfg"))
    failing = [f for f in found.values() if f.level == "fail" and "cleartext" in f.detail]
    assert failing, {k: v.detail for k, v in found.items()}


# --- client_credentials ---------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_healthy_client_credentials_setup_skips_interactive_checks(tmp_path):
    async with serve_oauth_fixture(FixtureConfig(client_credentials=True, advertise_s256=False)) as handle:
        found = await _run(
            _cc(handle), tmp_path, environ={SECRET_VAR: handle.state.machine_client_secret}, granted_scopes=["read"]
        )
    assert _levels(found) == {"credential": "ok", "discovery": "ok", "issuer": "ok", "grant": "ok"}, {k: v.detail for k, v in found.items()}
    assert "pkce" not in found and "login" not in found  # nothing interactive applies, so S256 being absent is no problem


@pytest.mark.anyio
async def test_an_unset_secret_variable_fails_only_the_credential_check_and_names_it(tmp_path):
    async with serve_oauth_fixture(FixtureConfig(client_credentials=True)) as handle:
        found = await _run(_cc(handle), tmp_path, environ={}, connected=False)
    assert found["credential"].level == "fail" and SECRET_VAR in found["credential"].detail
    assert [n for n, f in found.items() if f.level == "fail"] == ["credential"], _levels(found)


@pytest.mark.anyio
async def test_a_configured_issuer_the_server_does_not_use_fails_the_issuer_check(tmp_path):
    async with serve_oauth_fixture(FixtureConfig(client_credentials=True)) as handle:
        found = await _run(
            _cc(handle, issuer="https://auth.elsewhere.example.com/"),
            tmp_path, environ={SECRET_VAR: handle.state.machine_client_secret}, connected=False,
        )
    assert found["issuer"].level == "fail" and "auth.elsewhere.example.com" in found["issuer"].detail
    assert "auth.issuer" in found["issuer"].detail


@pytest.mark.anyio
async def test_an_authorization_server_that_does_not_offer_client_credentials_is_a_warning(tmp_path):
    async with serve_oauth_fixture(FixtureConfig(client_credentials=False)) as handle:
        found = await _run(_cc(handle), tmp_path, environ={SECRET_VAR: "x"}, connected=False)
    assert found["issuer"].level == "warn" and "client_credentials" in found["issuer"].detail


# --- bearer_env -----------------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_static_bearer_setup_checks_the_variable_only_and_reports_the_grant_as_unknown(tmp_path):
    async with serve_oauth_fixture(FixtureConfig(protection="static_bearer")) as handle:
        found = await _run(_bearer(handle), tmp_path, environ={TOKEN_VAR: handle.state.static_bearer})
    assert set(found) == {"credential", "grant"}, "no discovery, PKCE or refresh for an opaque token"
    assert found["credential"].level == "ok"
    assert found["grant"].level == "ok" and "unknown" in found["grant"].detail


@pytest.mark.anyio
async def test_an_unset_token_variable_fails_the_credential_check_and_names_it(tmp_path):
    async with serve_oauth_fixture(FixtureConfig(protection="static_bearer")) as handle:
        found = await _run(_bearer(handle), tmp_path, environ={})
    assert found["credential"].level == "fail" and TOKEN_VAR in found["credential"].detail


# --- grant drift against recorded sessions --------------------------------------------------------------------------


@pytest.mark.anyio
async def test_sessions_recorded_under_other_scopes_warn_naming_both_grants_and_the_fix(tmp_path):
    runs = tmp_path / "runs"
    for i in range(2):
        _record(runs, f"narrow{i}", auth_type="client_credentials", scopes=["read"])
    _record(runs, "same", auth_type="client_credentials", scopes=["admin", "read"])
    async with serve_oauth_fixture(FixtureConfig(client_credentials=True)) as handle:
        found = await _run(
            _cc(handle), tmp_path, environ={SECRET_VAR: handle.state.machine_client_secret},
            granted_scopes=["admin", "read"], runs=runs,
        )
    grant = found["grant"]
    assert grant.level == "warn", grant.detail
    assert "2 of 3" in grant.detail and "['read']" in grant.detail and "['admin', 'read']" in grant.detail, grant.detail
    assert "drifter observe" in grant.detail and "auth.scopes" in grant.detail, grant.detail


@pytest.mark.anyio
async def test_matching_recorded_sessions_are_ok_and_say_how_many(tmp_path):
    runs = tmp_path / "runs"
    for i in range(3):
        _record(runs, f"s{i}", auth_type="client_credentials", scopes=["read"])
    async with serve_oauth_fixture(FixtureConfig(client_credentials=True)) as handle:
        found = await _run(
            _cc(handle), tmp_path, environ={SECRET_VAR: handle.state.machine_client_secret},
            granted_scopes=["read"], runs=runs,
        )
    assert found["grant"].level == "ok" and "3" in found["grant"].detail


@pytest.mark.anyio
async def test_a_legacy_recording_is_unauthenticated_not_unknown_and_differs_from_an_authenticated_grant(tmp_path):
    runs = tmp_path / "runs"
    _record(runs, "legacy")  # written by 0.2.0: no auth fields
    async with serve_oauth_fixture(FixtureConfig(client_credentials=True)) as handle:
        found = await _run(
            _cc(handle), tmp_path, environ={SECRET_VAR: handle.state.machine_client_secret},
            granted_scopes=["read"], runs=runs,
        )
    assert found["grant"].level == "warn" and "no auth" in found["grant"].detail, found["grant"].detail


@pytest.mark.anyio
async def test_an_unknown_grant_on_either_side_is_reported_as_not_comparable_never_as_a_match(tmp_path):
    runs = tmp_path / "runs"
    _record(runs, "opaque", auth_type="bearer_env", scopes=None)
    async with serve_oauth_fixture(FixtureConfig(protection="static_bearer")) as handle:
        found = await _run(_bearer(handle), tmp_path, environ={TOKEN_VAR: handle.state.static_bearer}, runs=runs)
        assert found["grant"].level == "ok" and "unknown" in found["grant"].detail
    _record(runs, "known", auth_type="oauth", scopes=["read"])
    async with serve_oauth_fixture(FixtureConfig(client_credentials=True)) as handle:
        found = await _run(
            _cc(handle), tmp_path, environ={SECRET_VAR: handle.state.machine_client_secret}, granted_scopes=None, runs=runs,
        )
    assert found["grant"].level == "warn" and "current grant is unknown" in found["grant"].detail, found["grant"].detail
    assert "cannot be compared" in found["grant"].detail, found["grant"].detail


@pytest.mark.anyio
async def test_a_recorded_unknown_grant_does_not_match_a_known_current_one(tmp_path):
    runs = tmp_path / "runs"
    _record(runs, "opaque", auth_type="client_credentials", scopes=None)
    async with serve_oauth_fixture(FixtureConfig(client_credentials=True)) as handle:
        found = await _run(
            _cc(handle), tmp_path, environ={SECRET_VAR: handle.state.machine_client_secret}, granted_scopes=["read"], runs=runs,
        )
    assert found["grant"].level == "warn" and "1 of 1" in found["grant"].detail and "unknown scopes" in found["grant"].detail


@pytest.mark.anyio
async def test_other_servers_sessions_and_unreadable_files_are_ignored(tmp_path):
    runs = tmp_path / "runs"
    _record(runs, "elsewhere", server="some-other-server", auth_type="oauth", scopes=["zzz"])
    (runs / "garbage.jsonl").write_text("not json\n{\n", encoding="utf-8")
    (runs / "empty.jsonl").write_text("", encoding="utf-8")
    _record(runs, "mine", auth_type="client_credentials", scopes=["read"])
    async with serve_oauth_fixture(FixtureConfig(client_credentials=True)) as handle:
        found = await _run(
            _cc(handle), tmp_path, environ={SECRET_VAR: handle.state.machine_client_secret},
            granted_scopes=["read"], runs=runs,
        )
    assert found["grant"].level == "ok" and "1 recorded" in found["grant"].detail, found["grant"].detail


@pytest.mark.anyio
async def test_no_recorded_sessions_is_fine(tmp_path):
    async with serve_oauth_fixture(FixtureConfig(client_credentials=True)) as handle:
        found = await _run(
            _cc(handle), tmp_path, environ={SECRET_VAR: handle.state.machine_client_secret}, granted_scopes=["read"],
        )
    assert found["grant"].level == "ok" and "no recorded sessions" in found["grant"].detail


# --- nothing leaks ---------------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_no_finding_contains_a_secret_a_token_or_a_key(tmp_path):
    from canary_scan import assert_no_leaks, scan_text

    async with serve_oauth_fixture(FixtureConfig(client_credentials=True)) as handle:
        store = await log_in(handle, tmp_path / "cfg")
        found = {}
        found.update(await _run(_oauth(handle), tmp_path, store=store))
        found.update(await _run(_cc(handle), tmp_path, environ={SECRET_VAR: handle.state.machine_client_secret}, granted_scopes=["read"]))
        text = "\n".join(f"{f.level} {f.check} {f.detail}" for f in found.values())
        assert_no_leaks(scan_text("doctor findings", text, handle.state.all_secrets()))


# --- bounded: a server that never answers cannot hang doctor (CLAUDE.md: blocking paths are TIMED) ----------------


async def _serve_then_go_silent(answered_first: bool):
    """A loopback server. With `answered_first`, the first connection gets a real 401 and every later one hangs:
    discovery makes several requests in a row, so a per-request timeout alone would multiply."""
    import anyio

    state = {"connections": 0}

    async def handle(stream):
        state["connections"] += 1
        if answered_first and state["connections"] == 1:
            await stream.receive(65536)
            await stream.send(b"HTTP/1.1 401 Unauthorized\r\nWWW-Authenticate: Bearer\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await stream.aclose()
            return
        await anyio.sleep(60)

    listener = await anyio.create_tcp_listener(local_host="127.0.0.1")
    return listener, listener.extra(anyio.abc.SocketAttribute.local_port), handle


@pytest.mark.anyio
@pytest.mark.parametrize("answered_first", [False, True])
async def test_discovery_against_a_server_that_goes_silent_ends_within_the_timeout(tmp_path, answered_first):
    import anyio

    timeout = 1.5
    listener, port, handle = await _serve_then_go_silent(answered_first)
    async with anyio.create_task_group() as tg:
        tg.start_soon(listener.serve, handle)
        server = ServerConfig.model_validate({"name": "remote-crm", "url": f"http://127.0.0.1:{port}/mcp", "auth": {"type": "oauth"}})
        started = time.monotonic()
        findings = await diagnose_auth(server, runs_dir=tmp_path / "runs", environ={}, timeout_seconds=timeout, store=_NoLogin())
        elapsed = time.monotonic() - started
        tg.cancel_scope.cancel()
    found = {f.check: f for f in findings}
    assert found["discovery"].level == "fail" and "did not answer" in found["discovery"].detail, found["discovery"].detail
    # One overall bound, not one per request: the second case makes 2+ requests that would each wait the full timeout.
    assert elapsed < timeout + 1.5, f"discovery took {elapsed:.1f}s against a silent server with a {timeout}s timeout"


class _NoLogin:
    def summary(self):
        from mcp_drifter.auth.storage import StoredSummary

        return StoredSummary(backend="file", has_tokens=False, has_client_info=False, refreshable=False, expires_at=None, scope=None)
