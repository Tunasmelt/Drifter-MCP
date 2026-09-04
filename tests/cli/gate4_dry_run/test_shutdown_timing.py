"""Gate 4 pre-handoff dry run — a timed (not just pass/fail) check that
`drifter observe`'s subprocess shuts down promptly when its client
disconnects abruptly, rather than lingering.

Not a persona (no fake user identity attached) -- a standalone check,
per the project's own explicit, three-times-confirmed recurring bug
pattern (CLAUDE.md): "a blocking call... that doesn't honor
anyio/asyncio cancellation, causing shutdown to hang until something
external force-kills it." CLAUDE.md requires a TIMING test for any code
that spawns a process, reads a stream, or waits on a thread -- "it
passed" is explicitly stated as insufficient evidence for shutdown-path
code in this codebase.

This does NOT re-test SIGINT handling (already covered directly, at the
Python level, by tests/cli/test_observe.py's own
test_handle_sigint_flushes_open_trajectory_and_exits -- deliberately
not via real OS signal delivery, which handle_sigint's own docstring
documents as unreliable in a sandboxed shell). This test instead covers
a scenario neither that test nor any existing test exercises: the
CLIENT disconnecting mid-session (a crashed or force-killed agent, not
a clean SIGINT-driven shutdown) -- confirming the whole spawn-connect-
call-abrupt-disconnect-teardown sequence completes within a small,
explicit bound, not merely that it eventually finishes.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

FIXTURE_SERVER = str(Path(__file__).parent.parent.parent / "fixtures" / "fake_server.py")


def _drifter_yaml(tmp_path: Path, runs_dir: Path) -> Path:
    lines = [
        "version: 1",
        "servers:",
        "  - name: fake",
        f"    command: ['{sys.executable}', '{FIXTURE_SERVER}']",
        "record:",
        f"  dir: {runs_dir.as_posix()}",
    ]
    config_path = tmp_path / "drifter.yaml"
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return config_path


async def _connect_call_then_disconnect_abruptly(config_path: Path) -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "cli", "observe", "--config", str(config_path), "--server", "fake"],
    )
    with anyio.fail_after(15):  # generous but bounded -- a real hang would exceed this by a lot, not a little
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                await session.call_tool("add", {"a": 1, "b": 1})
            # Falling out of the ClientSession/stdio_client `async with`
            # blocks here IS the abrupt disconnect: the underlying
            # subprocess transport is torn down immediately after one
            # real call, with no graceful "goodbye" exchange -- closer to
            # a crashed agent than a cooperative shutdown.


def test_observe_subprocess_shuts_down_promptly_after_an_abrupt_client_disconnect(tmp_path):
    """Timed, per this project's own required discipline for any code
    that spawns a process or reads a stream -- a passing result alone
    (the async block returning at all) would not be sufficient evidence;
    the bound itself is the actual assertion.
    """
    runs_dir = tmp_path / "runs"
    config_path = _drifter_yaml(tmp_path, runs_dir)

    started = time.monotonic()
    anyio.run(_connect_call_then_disconnect_abruptly, config_path)
    elapsed = time.monotonic() - started

    assert elapsed < 15.0
    # A healthy shutdown is fast (well under a second in practice); this
    # is a loose sanity bound so the test isn't flaky on a slow CI
    # runner, while still catching a genuine multi-second-or-worse hang.
    assert elapsed < 5.0, f"observe shutdown took {elapsed:.2f}s after an abrupt disconnect -- investigate a hang"
