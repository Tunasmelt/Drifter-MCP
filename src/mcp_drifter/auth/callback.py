"""The loopback listener that receives the browser's redirect after an interactive login (F-43).

A process waiting for an authorization code on a local port is attack surface, so this is narrow:

  - it binds 127.0.0.1 only, on an OS-assigned port;
  - it accepts exactly `GET /callback` with the expected `state` and a loopback `Host` header
    (so a page that makes the browser talk to localhost, or a DNS-rebinding trick, cannot deliver a
    callback). Anything else gets an error response and does NOT consume the login: a hostile web
    page firing forged requests at localhost must not be able to cancel it, and the real redirect
    still has to work afterwards;
  - requests are read with a size cap and a short deadline, one connection at a time, so an idle or
    oversized connection cannot stall the real redirect;
  - the response is a fixed page that never reflects anything from the request;
  - it times out, and cancelling the wait releases the port at once. The default timeout is 300 s,
    so a cancellation that waited on it would be a hung Ctrl+C (CLAUDE.md: timed, not assumed).

The SDK checks `state` itself too, but only after the callback has been consumed: validating here
is what lets a forged request be ignored instead of failing the login.
"""

from __future__ import annotations

import secrets
from contextlib import AsyncExitStack
from urllib.parse import parse_qs, urlsplit

import anyio
from anyio.abc import SocketAttribute
from mcp.shared.auth import AuthorizationCodeResult

from mcp_drifter.auth.errors import LoginRefused
from mcp_drifter.auth.text import clean_server_text

HOST = "127.0.0.1"
CALLBACK_PATH = "/callback"
# Protocol hygiene limits for one request from a browser, not tuned thresholds: a real redirect is a
# few hundred bytes and arrives at once.
MAX_REQUEST_HEAD_BYTES = 8192
REQUEST_READ_DEADLINE_S = 5.0

_PAGE_OK = (
    b"<!doctype html><meta charset=utf-8><title>Drifter</title>"
    b"<p>Drifter: login complete. You can close this tab and return to the terminal.</p>"
)
_PAGE_ENDED = (
    b"<!doctype html><meta charset=utf-8><title>Drifter</title>"
    b"<p>Drifter: the login did not complete. Return to the terminal for details.</p>"
)
_PAGE_BAD = b"<!doctype html><meta charset=utf-8><title>Drifter</title><p>Drifter: bad request.</p>"


class CallbackTimeout(LoginRefused):
    """Nobody completed the login in time."""


class CallbackError(LoginRefused):
    """The authorization server redirected back with an error (for example the user said no)."""


class CallbackListener:
    """`async with CallbackListener() as listener`, then `listener.expect(state)` and
    `await listener.wait(timeout)`."""

    host = HOST

    def __init__(self) -> None:
        self._stack: AsyncExitStack | None = None
        self._listener = None
        self._expected_state: str | None = None
        self._outcome: AuthorizationCodeResult | CallbackError | None = None
        self._done = anyio.Event()

    # -- lifecycle ------------------------------------------------------------------------------------

    async def __aenter__(self) -> "CallbackListener":
        stack = AsyncExitStack()
        self._stack = stack
        listener = await anyio.create_tcp_listener(local_host=HOST, local_port=0)
        self._listener = listener
        stack.push_async_callback(listener.aclose)
        task_group = await stack.enter_async_context(anyio.create_task_group())
        stack.callback(task_group.cancel_scope.cancel)  # runs first on exit: stops serving before the port closes
        task_group.start_soon(listener.serve, self._handle)
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> bool:
        assert self._stack is not None
        try:
            await self._stack.__aexit__(exc_type, exc, traceback)
        except BaseExceptionGroup as group:
            # The task group serving the listener wraps whatever the body raised in a one-member
            # group. That is an implementation detail callers should not have to catch around.
            if exc is not None and len(group.exceptions) == 1 and group.exceptions[0] is exc:
                return False  # let the original propagate, unwrapped
            raise
        return False

    @property
    def port(self) -> int:
        assert self._listener is not None
        return self._listener.extra(SocketAttribute.local_port)

    @property
    def sockets(self) -> list:
        assert self._listener is not None
        return [item.extra(SocketAttribute.raw_socket) for item in self._listener.listeners]

    @property
    def redirect_uri(self) -> str:
        return f"http://{HOST}:{self.port}{CALLBACK_PATH}"

    # -- the login -----------------------------------------------------------------------------------------

    def expect(self, state: str) -> None:
        """The `state` value the authorization URL carries; only a callback echoing it is accepted."""
        self._expected_state = state

    async def wait(self, timeout: float) -> AuthorizationCodeResult:
        try:
            with anyio.fail_after(timeout):
                await self._done.wait()
        except TimeoutError:
            raise CallbackTimeout(f"timed out after {timeout:g} s waiting for the browser to finish the login") from None
        outcome = self._outcome
        if isinstance(outcome, CallbackError):
            raise outcome
        assert outcome is not None
        return outcome

    # -- one connection ---------------------------------------------------------------------------------------

    async def _handle(self, stream) -> None:
        async with stream:
            try:
                with anyio.fail_after(REQUEST_READ_DEADLINE_S):
                    head = await self._read_head(stream)
            except (TimeoutError, anyio.EndOfStream, anyio.BrokenResourceError, anyio.ClosedResourceError):
                return
            if head is None:
                await self._respond(stream, 431, "Request Header Fields Too Large", _PAGE_BAD)
                return
            try:
                status, reason, page, outcome = self._judge(head)
            except Exception:  # noqa: BLE001 - nothing a client sends may take the listener, and so the login, down
                status, reason, page, outcome = 400, "Bad Request", _PAGE_BAD, None
            await self._respond(stream, status, reason, page)
            # Only after the browser has its answer: finishing first would let the caller tear the
            # listener down mid-response.
            if outcome is not None and self._outcome is None:
                self._outcome = outcome
                self._done.set()

    @staticmethod
    async def _read_head(stream) -> bytes | None:
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            if len(buffer) > MAX_REQUEST_HEAD_BYTES:
                return None
            buffer += await stream.receive(4096)
        return buffer.split(b"\r\n\r\n", 1)[0]

    def _judge(self, head: bytes):
        """`(status, reason, page, outcome)`: what to answer, and whether this request ends the login."""
        bad = (400, "Bad Request", _PAGE_BAD, None)
        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) != 3 or not parts[2].startswith("HTTP/"):
            return bad
        method, target = parts[0], parts[1]
        headers = {}
        for line in lines[1:]:
            name, sep, value = line.partition(":")
            if sep:
                headers.setdefault(name.strip().lower(), value.strip())
        if not self._host_is_ours(headers.get("host")):
            return bad
        url = urlsplit(target)
        if url.path != CALLBACK_PATH:
            return (404, "Not Found", _PAGE_BAD, None)
        if method != "GET":
            return (405, "Method Not Allowed", _PAGE_BAD, None)
        query = parse_qs(url.query, keep_blank_values=True)
        state = (query.get("state") or [""])[0]
        expected = self._expected_state
        if not expected or not state or not secrets.compare_digest(state.encode(), expected.encode()):
            return bad  # forged, stale or premature: ignored, the real redirect can still arrive
        if "error" in query:
            reason = clean_server_text(query["error"][0]) or "an error"
            return (200, "OK", _PAGE_ENDED, CallbackError(f"the authorization server ended the login: {reason}"))
        code = (query.get("code") or [""])[0]
        if not code:
            return bad
        iss = (query.get("iss") or [None])[0]
        return (200, "OK", _PAGE_OK, AuthorizationCodeResult(code=code, state=state, iss=iss))

    def _host_is_ours(self, host: str | None) -> bool:
        return host in (f"{HOST}:{self.port}", f"localhost:{self.port}")

    @staticmethod
    async def _respond(stream, status: int, reason: str, page: bytes) -> None:
        head = (
            f"HTTP/1.1 {status} {reason}\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            f"Content-Length: {len(page)}\r\n"
            "Cache-Control: no-store\r\n"
            "Referrer-Policy: no-referrer\r\n"
            "X-Content-Type-Options: nosniff\r\n"
            "Connection: close\r\n\r\n"
        )
        try:
            await stream.send(head.encode("ascii") + page)
        except (anyio.BrokenResourceError, anyio.ClosedResourceError):
            pass
