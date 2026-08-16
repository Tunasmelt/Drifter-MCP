"""Unit tests for record/proxy.py's _pump exception semantics.

Not fixture/subprocess-based like the rest of record/'s tests — this
targets a narrow, easy-to-get-wrong correctness property directly: when
the try block is already propagating an exception, a failure in the
finally block's own cleanup (sink.aclose()) must never silently replace
it as what actually escapes the function.
"""

import logging

import pytest

from record.proxy import Direction, _pump


class _FakeCancelScope:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class _FakeSource:
    """Minimal async-iterable/context-manager double yielding fixed items."""

    def __init__(self, items):
        self._items = iter(items)
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        self.closed = True
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._items)
        except StopIteration:
            raise StopAsyncIteration from None


class _FailingSink:
    """A sink whose aclose() always raises — the scenario under test."""

    def __init__(self):
        self.sent = []

    async def send(self, item):
        self.sent.append(item)

    async def aclose(self):
        raise RuntimeError("aclose boom")


class _OkSink:
    async def send(self, item):
        pass

    async def aclose(self):
        pass


@pytest.mark.anyio
async def test_original_exception_survives_a_failing_sink_aclose(caplog):
    """The scenario the fix targets: source raises (e.g. a parse error
    surfaced as an Exception item), AND sink.aclose() also raises. The
    ORIGINAL exception must be what escapes _pump, not the aclose failure.
    """
    original_error = ValueError("original parse error")
    source = _FakeSource(["normal message", original_error])
    sink = _FailingSink()
    scope = _FakeCancelScope()

    with caplog.at_level(logging.ERROR):
        with pytest.raises(ValueError) as exc_info:
            await _pump(source, sink, scope, Direction.AGENT_TO_SERVER, None)

    assert exc_info.value is original_error  # the exact object, not a lookalike
    assert sink.sent == ["normal message"]  # the message before the error was forwarded
    assert scope.cancelled  # cleanup still ran despite the double failure
    assert source.closed
    assert "aclose" in caplog.text.lower()  # the masked failure wasn't silently dropped either


@pytest.mark.anyio
async def test_failing_sink_aclose_does_not_raise_when_there_was_no_original_error(caplog):
    """Clean shutdown, but sink.aclose() itself fails: this must not crash
    the pump — it's a benign cleanup failure, logged, not propagated."""
    source = _FakeSource(["a", "b"])
    sink = _FailingSink()
    scope = _FakeCancelScope()

    with caplog.at_level(logging.ERROR):
        await _pump(source, sink, scope, Direction.SERVER_TO_AGENT, None)  # must not raise

    assert sink.sent == ["a", "b"]
    assert scope.cancelled
    assert "aclose" in caplog.text.lower()


@pytest.mark.anyio
async def test_ordinary_shutdown_is_unaffected():
    """Baseline: no failures anywhere — behavior from before this fix is unchanged."""
    source = _FakeSource(["a", "b", "c"])
    sink = _OkSink()
    scope = _FakeCancelScope()

    await _pump(source, sink, scope, Direction.AGENT_TO_SERVER, None)

    assert scope.cancelled
    assert source.closed


@pytest.fixture
def anyio_backend():
    return "asyncio"
