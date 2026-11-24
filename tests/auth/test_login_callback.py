"""F-43: the loopback listener that receives the browser's redirect (docs/FEATURES.md F-43).

Written before the implementation. What it must be, because a process listening for an
authorization code on a local port is attack surface:
  - bound to 127.0.0.1 only, on an OS-assigned port;
  - it accepts exactly `GET /callback` with the expected `state`. Anything else (another path, a
    wrong or missing state, a foreign Host header, an oversized or slow request) gets an error
    response and does NOT consume the login: a web page that fires forged requests at localhost
    must not be able to cancel it, and the real redirect must still work afterwards;
  - its response is a fixed page that never reflects anything from the request;
  - it times out, and when cancelled it releases the port promptly (CLAUDE.md async-shutdown
    rule: timed, not assumed, because the timeout is 300 s and a hung Ctrl+C would last that long).
"""

from __future__ import annotations

import socket
import time

import anyio
import httpx2
import pytest

from mcp_drifter.auth.callback import CallbackError, CallbackListener, CallbackTimeout
from mcp_drifter.auth.errors import LoginRefused

STATE = "expected-state-VALUE-123"


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _get(listener: CallbackListener, path: str, **kwargs):
    async with httpx2.AsyncClient() as client:
        return await client.get(f"http://127.0.0.1:{listener.port}{path}", **kwargs)


async def _raw(listener: CallbackListener, request: bytes) -> bytes:
    """Sends raw bytes and returns whatever comes back, for requests httpx would not send."""
    async with await anyio.connect_tcp("127.0.0.1", listener.port) as stream:
        await stream.send(request)
        chunks = []
        with anyio.move_on_after(5):
            try:
                while True:
                    chunks.append(await stream.receive(65536))
            except (anyio.EndOfStream, anyio.BrokenResourceError, anyio.ClosedResourceError):
                pass
        return b"".join(chunks)


# --- where it listens ---------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_it_listens_on_loopback_only_on_an_os_assigned_port():
    async with CallbackListener() as listener:
        assert listener.host == "127.0.0.1" and listener.port > 0
        assert listener.redirect_uri == f"http://127.0.0.1:{listener.port}/callback"
        for sock in listener.sockets:
            assert sock.getsockname()[0] == "127.0.0.1"


@pytest.mark.anyio
async def test_two_listeners_get_different_ports():
    async with CallbackListener() as one, CallbackListener() as two:
        assert one.port != two.port


# --- the happy path -------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_real_redirect_returns_code_state_and_issuer_and_gets_a_fixed_page():
    async with CallbackListener() as listener:
        listener.expect(STATE)
        results = []

        async def waiter():
            results.append(await listener.wait(timeout=10))

        async with anyio.create_task_group() as tg:
            tg.start_soon(waiter)
            response = await _get(listener, f"/callback?code=AUTHCODE-9&state={STATE}&iss=https%3A%2F%2Fas.example")
            assert response.status_code == 200
        assert (results[0].code, results[0].state, results[0].iss) == ("AUTHCODE-9", STATE, "https://as.example")
        assert "AUTHCODE-9" not in response.text and STATE not in response.text  # never reflected
        assert response.headers["cache-control"] == "no-store"


@pytest.mark.anyio
async def test_nothing_from_the_request_is_ever_reflected_into_the_page():
    async with CallbackListener() as listener:
        listener.expect(STATE)
        async with anyio.create_task_group() as tg:
            tg.start_soon(lambda: listener.wait(timeout=10))
            payload = "<script>alert(1)</script>"
            response = await _get(listener, "/callback", params={"code": payload, "state": STATE})
        assert "<script>alert(1)</script>" not in response.text and "alert" not in response.text


# --- requests that must not consume the login --------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("query", ["code=C&state=WRONG", "code=C", "code=C&state=", "state=" + STATE.upper() + "&code=C"])
async def test_a_wrong_or_missing_state_is_refused_and_the_login_keeps_waiting(query):
    async with CallbackListener() as listener:
        listener.expect(STATE)
        results = []

        async def waiter():
            results.append(await listener.wait(timeout=10))

        async with anyio.create_task_group() as tg:
            tg.start_soon(waiter)
            forged = await _get(listener, f"/callback?{query}")
            assert forged.status_code == 400
            assert results == []  # the forged request did not end the wait
            real = await _get(listener, f"/callback?code=REAL&state={STATE}")
            assert real.status_code == 200
        assert results[0].code == "REAL"


@pytest.mark.anyio
async def test_any_other_path_or_method_is_refused_and_the_login_keeps_waiting():
    async with CallbackListener() as listener:
        listener.expect(STATE)
        results = []

        async def waiter():
            results.append(await listener.wait(timeout=10))

        async with anyio.create_task_group() as tg:
            tg.start_soon(waiter)
            assert (await _get(listener, f"/favicon.ico?state={STATE}&code=C")).status_code == 404
            assert (await _get(listener, f"/callback/extra?state={STATE}&code=C")).status_code == 404
            async with httpx2.AsyncClient() as client:
                posted = await client.post(f"http://127.0.0.1:{listener.port}/callback?state={STATE}&code=C")
            assert posted.status_code == 405
            assert results == []
            await _get(listener, f"/callback?code=REAL&state={STATE}")
        assert results[0].code == "REAL"


@pytest.mark.anyio
@pytest.mark.parametrize("host", ["evil.example", "evil.example:80", "127.0.0.1.evil.com", ""])
async def test_a_foreign_host_header_is_refused_so_dns_rebinding_cannot_deliver_a_callback(host):
    async with CallbackListener() as listener:
        listener.expect(STATE)
        results = []

        async def waiter():
            results.append(await listener.wait(timeout=10))

        async with anyio.create_task_group() as tg:
            tg.start_soon(waiter)
            reply = await _raw(
                listener, f"GET /callback?code=C&state={STATE} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode()
            )
            assert reply.startswith(b"HTTP/1.1 400"), reply[:80]
            assert results == []
            await _get(listener, f"/callback?code=REAL&state={STATE}")
        assert results[0].code == "REAL"


@pytest.mark.anyio
async def test_an_oversized_request_is_refused_and_the_login_keeps_waiting():
    async with CallbackListener() as listener:
        listener.expect(STATE)
        results = []

        async def waiter():
            results.append(await listener.wait(timeout=10))

        async with anyio.create_task_group() as tg:
            tg.start_soon(waiter)
            huge = b"GET /callback?code=C&state=" + STATE.encode() + b" HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Pad: " + b"A" * 20000 + b"\r\n\r\n"
            reply = await _raw(listener, huge)
            assert reply == b"" or reply.startswith((b"HTTP/1.1 431", b"HTTP/1.1 400")), reply[:60]
            assert results == []
            await _get(listener, f"/callback?code=REAL&state={STATE}")
        assert results[0].code == "REAL"


@pytest.mark.anyio
async def test_an_idle_connection_does_not_stop_the_real_redirect_from_getting_through():
    async with CallbackListener() as listener:
        listener.expect(STATE)
        results = []

        async def waiter():
            results.append(await listener.wait(timeout=10))

        async with await anyio.connect_tcp("127.0.0.1", listener.port):  # connects and says nothing
            async with anyio.create_task_group() as tg:
                tg.start_soon(waiter)
                started = time.monotonic()
                await _get(listener, f"/callback?code=REAL&state={STATE}")
        assert results[0].code == "REAL" and time.monotonic() - started < 3


# --- the authorization server said no -----------------------------------------------------------------------------


@pytest.mark.anyio
async def test_an_error_redirect_with_the_right_state_ends_the_wait_with_the_sanitized_reason():
    async with CallbackListener() as listener:
        listener.expect(STATE)
        failures = []

        async def waiter():
            try:
                await listener.wait(timeout=10)
            except CallbackError as exc:
                failures.append(exc)

        async with anyio.create_task_group() as tg:
            tg.start_soon(waiter)
            response = await _get(
                listener, "/callback", params={"error": "access_denied\x1b[31m", "error_description": "the user said no", "state": STATE}
            )
            assert response.status_code == 200  # the browser is told the login ended; no details reflected
        assert isinstance(failures[0], LoginRefused)
        assert "access_denied" in str(failures[0]) and "\x1b" not in str(failures[0])


@pytest.mark.anyio
async def test_an_error_redirect_with_the_wrong_state_is_just_another_forged_request():
    async with CallbackListener() as listener:
        listener.expect(STATE)
        results = []

        async def waiter():
            results.append(await listener.wait(timeout=10))

        async with anyio.create_task_group() as tg:
            tg.start_soon(waiter)
            forged = await _get(listener, "/callback?error=access_denied&state=WRONG")
            assert forged.status_code == 400
            await _get(listener, f"/callback?code=REAL&state={STATE}")
        assert results[0].code == "REAL"


# --- time ---------------------------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_it_times_out():
    async with CallbackListener() as listener:
        listener.expect(STATE)
        started = time.monotonic()
        with pytest.raises(CallbackTimeout) as failure:
            await listener.wait(timeout=0.4)
        assert 0.3 < time.monotonic() - started < 2.0
        assert isinstance(failure.value, LoginRefused) and "timed out" in str(failure.value)


@pytest.mark.anyio
async def test_cancelling_the_wait_is_prompt_and_releases_the_port():
    """CLAUDE.md async-shutdown rule: the timeout is 300 s, so a cancellation that waited on it
    would be a hung Ctrl+C. Timed, with the port checked afterwards."""
    listener = CallbackListener()
    await listener.__aenter__()
    port = listener.port
    listener.expect(STATE)
    started = time.monotonic()
    with anyio.move_on_after(0.3):
        await listener.wait(timeout=300)
    await listener.__aexit__(None, None, None)
    elapsed = time.monotonic() - started
    assert elapsed < 1.5, f"cancelling the callback wait took {elapsed:.2f}s"

    with pytest.raises(OSError):  # nothing is listening any more
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass


@pytest.mark.anyio
async def test_the_port_is_released_when_the_context_exits_normally():
    async with CallbackListener() as listener:
        port = listener.port
    with pytest.raises(OSError):
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass
