"""Running blocking work off the event loop without being able to hang the process on exit.

Used for the OS keyring (a call can block on an unlock prompt nobody answers) and for opening a
browser (`webbrowser.open` blocks on terminal browsers). Lifted out of `storage.py` when login
needed the same guarantee.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, TypeVar

import anyio
import anyio.from_thread
import anyio.lowlevel

_T = TypeVar("_T")


async def run_in_daemon_thread(function: Callable[[], _T]) -> _T:
    """Run blocking storage work off the event loop, so that cancelling it is prompt AND the
    abandoned thread cannot keep the process alive.

    A keyring call can block indefinitely (an unlock prompt nobody answers). `anyio.to_thread.
    run_sync(abandon_on_cancel=True)` frees the event loop but its worker threads are NOT
    daemons (they inherit the main thread's flag), so the interpreter would wait on the stuck
    call forever at exit: the same shutdown-hang shape CLAUDE.md records for stdin reads.
    A daemon thread of our own is simply dropped at exit. Proven with a real subprocess in
    tests/auth/test_token_storage.py, not assumed. No timeout constant on purpose: the callers
    already bound their waits (doctor's `fail_after`, the agent giving up on `observe`).
    """
    sender, receiver = anyio.create_memory_object_stream[tuple[bool, Any]](1)
    token = anyio.lowlevel.current_token()

    def work() -> None:
        try:
            outcome: tuple[bool, Any] = (True, function())
        except BaseException as exc:  # relayed to the awaiting task, not lost with the thread
            outcome = (False, exc)
        try:
            anyio.from_thread.run_sync(sender.send_nowait, outcome, token=token)
        except Exception:  # the event loop is gone or the awaiter was cancelled: nobody is waiting
            pass

    threading.Thread(target=work, name="drifter-keyring", daemon=True).start()
    with receiver:
        ok, value = await receiver.receive()
    if ok:
        return value
    raise value
