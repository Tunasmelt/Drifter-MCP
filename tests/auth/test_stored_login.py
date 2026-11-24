"""F-44: connecting to an OAuth-protected server with a login stored by `drifter auth login`
(docs/FEATURES.md F-44, docs/SPEC.md §5.2).

Written before the implementation. The connection is NON-INTERACTIVE: no browser, no callback, nothing
on stdout. A usable token is sent; one that is stale by its stored expiry is refreshed silently; anything
else ends in a specific error that says what to run (`drifter auth login <server>`), never in a login flow.

What the P1 characterization of the SDK forces (see F-44's "Two constraints" in FEATURES.md):
  - the SDK never restores a stored token's expiry, so Drifter must, or a long-expired token is sent as-is;
  - when the SERVER rejects a token the SDK starts a full authorization flow, never a refresh, so a 401
    must be turned into the login instruction BEFORE the SDK discovers, registers or authorizes anything.
"""

from __future__ import annotations

import time

import anyio
import pytest
from mcp import ClientSession

from canary_scan import assert_no_leaks, scan_text
from mcp_drifter.auth.errors import UpstreamAuthRejected
from mcp_drifter.auth.session import StoredLoginProvider
from mcp_drifter.cli.upstream import connect_upstream
from oauth_server import FixtureConfig, serve_oauth_fixture
from stub_browser import log_in, oauth_server, store_for


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _list_tools(handle, tmp_path, **kwargs):
    store = store_for(handle, tmp_path / "config")
    async with connect_upstream(oauth_server(handle), store=store, **kwargs) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.list_tools()


def _raised(group_or_exc) -> UpstreamAuthRejected:
    """The rejection, whether it surfaced bare or as a leaf of the transport's exception group."""
    pending = [group_or_exc]
    while pending:
        item = pending.pop()
        if isinstance(item, UpstreamAuthRejected):
            return item
        pending.extend(getattr(item, "exceptions", ()))
    raise AssertionError(f"no UpstreamAuthRejected in {group_or_exc!r}")


async def _expect_rejected(handle, tmp_path, **kwargs) -> UpstreamAuthRejected:
    with anyio.fail_after(20):
        try:
            await _list_tools(handle, tmp_path, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - inspected below
            return _raised(exc)
    raise AssertionError("the connection succeeded but was expected to be refused")


# --- the success paths -----------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_stored_login_is_sent_and_nothing_else_is_asked_of_the_server(tmp_path):
    async with serve_oauth_fixture(FixtureConfig()) as handle:
        store = await log_in(handle, tmp_path / "config")
        stored = await store.get_tokens()
        handle.state.requests.clear()

        tools = await _list_tools(handle, tmp_path)

        assert {t.name for t in tools.tools} >= {"get_item"}
        mcp = handle.state.mcp_requests()
        assert mcp and {r.authorization for r in mcp} == {f"Bearer {stored.access_token}"}
        # no discovery, registration, authorization or token request: it just used what it had
        assert [r.path for r in handle.state.requests if r.path != "/mcp"] == []


@pytest.mark.anyio
async def test_a_token_stale_by_its_stored_expiry_is_refreshed_silently_and_the_new_one_is_stored(tmp_path):
    async with serve_oauth_fixture(FixtureConfig()) as handle:
        store = await log_in(handle, tmp_path / "config")
        old = await store.get_tokens()
        # the token was issued two hours ago and said it lasts an hour: stale by the stored clock
        await store.set_tokens(old, issued_at=time.time() - 7200)
        handle.state.requests.clear()

        await _list_tools(handle, tmp_path)

        new = await store.get_tokens()
        assert new.access_token != old.access_token
        assert [r.path for r in handle.state.requests if r.path == "/token"], "a refresh request was expected"
        assert {r.authorization for r in handle.state.mcp_requests()} == {f"Bearer {new.access_token}"}
        assert new.refresh_token and new.refresh_token != old.refresh_token  # rotated, and kept
        assert await store.get_expiry() > time.time()  # the stored expiry moved forward
        assert (await store.get_client_info()) is not None  # the registration survived the refresh


# --- every failure says what to run, and none of them starts a login ------------------------------------------


def _no_login_flow_was_started(handle) -> None:
    started = [r.path for r in handle.state.requests if r.path in ("/authorize", "/register")]
    assert started == [], started


@pytest.mark.anyio
async def test_no_stored_login_says_to_log_in_and_contacts_nobody(tmp_path):
    async with serve_oauth_fixture(FixtureConfig()) as handle:
        error = await _expect_rejected(handle, tmp_path)
        assert "drifter auth login remote-crm" in str(error), str(error)
        assert handle.state.requests == []


@pytest.mark.anyio
async def test_an_expired_login_that_cannot_refresh_says_to_log_in_before_asking_the_server(tmp_path):
    async with serve_oauth_fixture(FixtureConfig(refresh_tokens=False)) as handle:
        store = await log_in(handle, tmp_path / "config")
        await store.set_tokens(await store.get_tokens(), issued_at=time.time() - 7200)
        handle.state.requests.clear()

        error = await _expect_rejected(handle, tmp_path)

        assert "drifter auth login remote-crm" in str(error) and "expired" in str(error), str(error)
        assert handle.state.mcp_requests() == []
        _no_login_flow_was_started(handle)


@pytest.mark.anyio
async def test_a_refresh_the_server_refuses_says_to_log_in_and_sends_no_unauthenticated_request(tmp_path):
    async with serve_oauth_fixture(FixtureConfig()) as handle:
        store = await log_in(handle, tmp_path / "config")
        await store.set_tokens(await store.get_tokens(), issued_at=time.time() - 7200)
        handle.state.refresh_tokens.clear()  # the server no longer knows the refresh token (revoked)
        handle.state.requests.clear()

        error = await _expect_rejected(handle, tmp_path)

        assert "drifter auth login remote-crm" in str(error) and "refresh" in str(error), str(error)
        assert handle.state.mcp_requests() == []  # it did not go on to call the server without credentials
        _no_login_flow_was_started(handle)


@pytest.mark.anyio
async def test_a_token_the_server_rejects_early_is_a_login_instruction_not_an_authorization_flow(tmp_path):
    """Revoked, or the clocks disagree: the stored expiry says fine, the server says no. The SDK would now
    discover, register and authorize; Drifter must stop at the instruction."""
    async with serve_oauth_fixture(FixtureConfig()) as handle:
        await log_in(handle, tmp_path / "config")
        handle.state.expire_access_tokens()
        handle.state.requests.clear()

        error = await _expect_rejected(handle, tmp_path)

        assert error.status == 401
        assert "drifter auth login remote-crm" in str(error), str(error)
        assert [r.path for r in handle.state.requests] == ["/mcp"], [r.path for r in handle.state.requests]


@pytest.mark.anyio
async def test_a_wrong_audience_token_is_named_as_such_with_what_the_server_said(tmp_path):
    async with serve_oauth_fixture(FixtureConfig()) as handle:
        await log_in(handle, tmp_path / "config")
        handle.state.force_status(
            401, challenge='Bearer error="invalid_token", error_description="the token audience does not match this server"'
        )

        error = await _expect_rejected(handle, tmp_path)

        text = str(error)
        assert "invalid_token" in text and "audience does not match" in text, text
        assert "drifter auth login remote-crm" in text, text


@pytest.mark.anyio
async def test_insufficient_scope_names_the_missing_scope_and_where_to_configure_it(tmp_path):
    async with serve_oauth_fixture(FixtureConfig()) as handle:
        await log_in(handle, tmp_path / "config")
        handle.state.force_status(403)  # the fixture's default challenge asks for scope "admin"

        error = await _expect_rejected(handle, tmp_path)

        assert error.status == 403
        text = str(error)
        assert "insufficient_scope" in text and "admin" in text, text
        assert "auth.scopes" in text and "drifter auth login remote-crm" in text, text


@pytest.mark.anyio
async def test_an_ordinary_403_is_an_answer_for_the_agent_not_a_reason_to_end_the_session(tmp_path):
    async with serve_oauth_fixture(FixtureConfig()) as handle:
        await log_in(handle, tmp_path / "config")
        handle.state.force_status(403, challenge="Bearer")  # permission denied for THIS request, no insufficient_scope
        with anyio.fail_after(20):
            try:
                await _list_tools(handle, tmp_path)
            except BaseException as exc:  # noqa: BLE001
                with pytest.raises(AssertionError):
                    _raised(exc)  # whatever it was, it was not a credential rejection


# --- the provider itself ---------------------------------------------------------------------------------------


def test_the_provider_has_no_redirect_or_callback_handler_so_it_can_never_start_a_login(tmp_path):
    from mcp_drifter.auth.storage import TokenStore

    provider = StoredLoginProvider("https://example.com/mcp", TokenStore("s", "https://example.com/mcp", base_dir=tmp_path, backend="file"), server_name="s")
    assert provider.context.redirect_handler is None
    assert provider.context.callback_handler is None


@pytest.mark.anyio
async def test_the_stored_expiry_is_restored_into_the_provider(tmp_path):
    async with serve_oauth_fixture(FixtureConfig()) as handle:
        store = await log_in(handle, tmp_path / "config")
        await store.set_tokens(await store.get_tokens(), issued_at=time.time() - 7200)
        provider = StoredLoginProvider(handle.mcp_url, store, server_name="remote-crm")
        await provider._initialize()
        assert provider.context.token_expiry_time == await store.get_expiry()
        assert provider.context.token_expiry_time < time.time()


@pytest.mark.anyio
async def test_nothing_the_connection_printed_or_raised_contains_a_secret(tmp_path):
    async with serve_oauth_fixture(FixtureConfig()) as handle:
        await log_in(handle, tmp_path / "config")
        handle.state.expire_access_tokens()
        error = await _expect_rejected(handle, tmp_path)
        assert_no_leaks(scan_text("the rejection", f"{error}\n{error!r}", handle.state.all_secrets()))
