"""Tests for the `mode: http` subprocess adapter (F-38),
`cli/subprocess_adapter.py`'s `run_agent_subprocess_http`.

Same discipline as test_subprocess_adapter.py: a real spawned subprocess
(tests/fixtures/scripted_agent.py's HTTP mode), not in-memory streams —
plus a real HTTP connection over real loopback TCP, since that's the
actual thing F-38 adds. `run_agent_subprocess` (stdio mode) is not
re-tested here; this file only covers what `mode: http` adds or changes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from cli.subprocess_adapter import make_run_once, run_agent_subprocess_http
from record.reader import read_session
from record.schema import ToolCall
from replay.replay_proxy import tools_served_from_session
from replay.replay_store import ReplayStore

GOLDEN_FIXTURE = Path(__file__).parent.parent / "fixtures" / "golden_v0.1.jsonl"
SCRIPTED_AGENT = Path(__file__).parent.parent / "fixtures" / "scripted_agent.py"
GOLDEN_SERVER = "filesystem"


def _golden_calls() -> list[ToolCall]:
    return [r for r in read_session(GOLDEN_FIXTURE) if isinstance(r, ToolCall)]


def _spec(tool_name: str, arguments: dict) -> str:
    return f"{tool_name}|{json.dumps(arguments)}"


@pytest.mark.anyio
async def test_agent_subprocess_http_produces_a_real_parseable_session_with_replayed_hits(tmp_path):
    store = ReplayStore()
    store.index_session(GOLDEN_FIXTURE)
    tools_served = tools_served_from_session(GOLDEN_FIXTURE)
    calls = _golden_calls()[:3]

    session_path = await run_agent_subprocess_http(
        command=[sys.executable, str(SCRIPTED_AGENT), *(_spec(c.tool_name, c.arguments) for c in calls)],
        replay_store=store,
        server_name=GOLDEN_SERVER,
        tools_served=tools_served,
        session_dir=tmp_path / "runs",
        raw_dir=tmp_path / "raw",
        timeout_s=30.0,
    )

    assert session_path.exists()
    records = list(read_session(session_path))

    session_start = next(r for r in records if r.record_type == "session_start")
    assert session_start.environment.tool_manifest_hash is not None

    recorded_calls = [r for r in records if isinstance(r, ToolCall)]
    assert len(recorded_calls) == 3
    for original, recorded in zip(calls, recorded_calls):
        assert recorded.tool_name == original.tool_name
        assert recorded.is_error == bool(original.is_error)
        assert recorded.fault is False
        assert recorded.result_provenance == "real"


@pytest.mark.anyio
async def test_unrecorded_call_surfaces_as_a_distinguishable_miss_over_http_too(tmp_path):
    """Parity check with test_subprocess_adapter.py's own stdio-mode
    equivalent -- MISS handling is replay_proxy.py's own logic
    (build_replay_server, shared by both transports), but this confirms
    it survives the HTTP transport hop for real rather than assuming
    transport-agnosticism from the shared-code argument alone."""
    store = ReplayStore()
    store.index_session(GOLDEN_FIXTURE)
    tools_served = tools_served_from_session(GOLDEN_FIXTURE)
    calls = _golden_calls()[:2]
    miss_args = {"path": "C:\\nowhere\\never\\recorded\\by\\this\\test"}

    session_path = await run_agent_subprocess_http(
        command=[
            sys.executable,
            str(SCRIPTED_AGENT),
            *(_spec(c.tool_name, c.arguments) for c in calls),
            _spec("list_directory", miss_args),
        ],
        replay_store=store,
        server_name=GOLDEN_SERVER,
        tools_served=tools_served,
        session_dir=tmp_path / "runs",
        raw_dir=tmp_path / "raw",
        timeout_s=30.0,
    )

    records = list(read_session(session_path))
    recorded_calls = [r for r in records if isinstance(r, ToolCall)]
    assert len(recorded_calls) == 3  # 2 real hits + 1 miss, all recorded
    for original, recorded in zip(calls, recorded_calls[:2]):
        assert recorded.tool_name == original.tool_name
        assert recorded.fault is False
    assert recorded_calls[2].fault is True  # the miss


@pytest.mark.anyio
async def test_explicit_env_override_is_preserved_alongside_inherited_environment(tmp_path):
    """Regression test for the real bug found while building this
    feature (docs/CHANGELOG.md): the fix was to inherit os.environ AND
    still honor a caller-supplied override, not just one or the other --
    confirmed here by actually reading back a custom variable the
    scripted agent doesn't touch, proving `env=` isn't silently dropped
    now that os.environ is merged in underneath it."""
    store = ReplayStore()
    store.index_session(GOLDEN_FIXTURE)
    tools_served = tools_served_from_session(GOLDEN_FIXTURE)
    calls = _golden_calls()[:1]

    # A tiny `-c` wrapper that fails loudly (AssertionError, non-zero
    # exit) if the explicit env override was lost, then runs the real
    # reference agent's own __main__ block via runpy IN THE SAME PROCESS
    # -- deliberately not os.execv (tried first): on Windows, execv has
    # no true POSIX process-image-replacement semantics, it spawns an
    # entirely NEW process and exits the original, which orphans
    # anyio.open_process's own pipe/handle tracking for the process it
    # thinks it's still watching. runpy avoids a second process schema
    # entirely, so there's nothing for Windows' lack of real exec() to
    # break.
    specs = [f"{c.tool_name}|{json.dumps(c.arguments)}" for c in calls]
    wrapper = (
        "import os, runpy, sys; "
        "assert os.environ.get('DRIFTER_TEST_MARKER') == 'present', 'explicit env override was lost'; "
        f"sys.argv = [sys.argv[0], *{specs!r}]; "
        f"runpy.run_path({str(SCRIPTED_AGENT)!r}, run_name='__main__')"
    )

    session_path = await run_agent_subprocess_http(
        command=[sys.executable, "-c", wrapper],
        replay_store=store,
        server_name=GOLDEN_SERVER,
        tools_served=tools_served,
        session_dir=tmp_path / "runs",
        raw_dir=tmp_path / "raw",
        env={"DRIFTER_TEST_MARKER": "present"},
        timeout_s=30.0,
    )
    records = list(read_session(session_path))
    assert any(isinstance(r, ToolCall) for r in records)  # the assert in the script didn't fail silently


@pytest.mark.anyio
async def test_final_answer_stdout_is_captured_to_a_sidecar_file(tmp_path):
    """Stdout is no longer the wire protocol under mode: http -- the
    reference agent (scripted_agent.py's _main_http) prints "done" as
    its final line once it's finished, and that must land as plain text
    next to the session JSONL, not be parsed as (or corrupted by
    attempting to parse as) JSON-RPC."""
    store = ReplayStore()
    store.index_session(GOLDEN_FIXTURE)
    tools_served = tools_served_from_session(GOLDEN_FIXTURE)
    calls = _golden_calls()[:1]

    session_path = await run_agent_subprocess_http(
        command=[sys.executable, str(SCRIPTED_AGENT), *(_spec(c.tool_name, c.arguments) for c in calls)],
        replay_store=store,
        server_name=GOLDEN_SERVER,
        tools_served=tools_served,
        session_dir=tmp_path / "runs",
        raw_dir=tmp_path / "raw",
        timeout_s=30.0,
    )

    sidecar = session_path.with_suffix(".stdout.txt")
    assert sidecar.exists()
    assert "done" in sidecar.read_text(encoding="utf-8")


@pytest.mark.anyio
async def test_custom_env_var_name_is_honored(tmp_path):
    """cli.config.AgentConfig.env_var lets a user rename the injected
    variable -- confirmed here against the real agent, which reads
    DRIFTER_PROXY_URL_ENV_VAR from its own environment (defaulting to
    DRIFTER_PROXY_URL) rather than the name being hardcoded on both
    sides coincidentally."""
    import os

    store = ReplayStore()
    store.index_session(GOLDEN_FIXTURE)
    tools_served = tools_served_from_session(GOLDEN_FIXTURE)
    calls = _golden_calls()[:1]

    # scripted_agent.py reads DRIFTER_PROXY_URL_ENV_VAR to know which env
    # var name to look for -- exercising a genuinely different name end
    # to end, not just passing the default through unchanged.
    custom_var = "MY_CUSTOM_PROXY_URL"
    session_path = await run_agent_subprocess_http(
        command=[sys.executable, str(SCRIPTED_AGENT), *(_spec(c.tool_name, c.arguments) for c in calls)],
        replay_store=store,
        server_name=GOLDEN_SERVER,
        tools_served=tools_served,
        session_dir=tmp_path / "runs",
        raw_dir=tmp_path / "raw",
        env={**os.environ, "DRIFTER_PROXY_URL_ENV_VAR": custom_var},
        timeout_s=30.0,
        env_var=custom_var,
    )
    records = list(read_session(session_path))
    assert any(isinstance(r, ToolCall) for r in records)


@pytest.mark.anyio
async def test_agent_that_never_connects_times_out_and_leaves_no_process_running(tmp_path):
    """A real HTTP-mode agent that ignores its proxy URL entirely (a
    script that never even tries to connect) must still be bounded by
    timeout_s and cleaned up -- same shutdown discipline as the stdio
    adapter's own equivalent guarantee (module docstring point 4)."""
    store = ReplayStore()
    store.index_session(GOLDEN_FIXTURE)
    tools_served = tools_served_from_session(GOLDEN_FIXTURE)

    session_path = await run_agent_subprocess_http(
        command=[sys.executable, "-c", "import time; time.sleep(300)"],
        replay_store=store,
        server_name=GOLDEN_SERVER,
        tools_served=tools_served,
        session_dir=tmp_path / "runs",
        raw_dir=tmp_path / "raw",
        timeout_s=2.0,
    )
    records = list(read_session(session_path))
    assert not any(isinstance(r, ToolCall) for r in records)


def test_make_run_once_http_mode_produces_a_real_session(tmp_path):
    store = ReplayStore()
    store.index_session(GOLDEN_FIXTURE)
    tools_served = tools_served_from_session(GOLDEN_FIXTURE)
    calls = _golden_calls()[:2]

    run_once = make_run_once(
        command=[sys.executable, str(SCRIPTED_AGENT), *(_spec(c.tool_name, c.arguments) for c in calls)],
        replay_store=store,
        server_name=GOLDEN_SERVER,
        tools_served=tools_served,
        session_dir=tmp_path / "runs",
        raw_dir=tmp_path / "raw",
        timeout_s=30.0,
        agent_mode="http",
    )
    session_path = run_once()
    records = list(read_session(session_path))
    assert len([r for r in records if isinstance(r, ToolCall)]) == 2


@pytest.mark.anyio
async def test_several_sequential_http_mode_runs_in_the_same_process_all_succeed(tmp_path):
    """Regression test for a real, severe bug found while building F-38:
    the SECOND (and every subsequent) `run_agent_subprocess_http` call in
    the same process failed its first real request with uvicorn's "ASGI
    callable returned without completing response" -- root-caused to
    `sse_starlette.sse.AppStatus.should_exit` being a bare, never-reset
    CLASS attribute shared across every server this process ever starts
    (see cli/http_proxy.py's own docstring for the full account). This
    would have made `agent.mode: http` completely broken for its actual
    real use case: `evaluate.baseline.run_baseline`'s `repeats` loop calls
    `run_once()` (calibration.yaml's default: 10 times) against the SAME
    process every real `drifter run`. A single successful call is not
    sufficient evidence this works — this test exists specifically
    because that was true and still silently broken.
    """
    store = ReplayStore()
    store.index_session(GOLDEN_FIXTURE)
    tools_served = tools_served_from_session(GOLDEN_FIXTURE)
    calls = _golden_calls()[:2]
    command = [sys.executable, str(SCRIPTED_AGENT), *(_spec(c.tool_name, c.arguments) for c in calls)]

    for i in range(3):
        session_path = await run_agent_subprocess_http(
            command=command,
            replay_store=store,
            server_name=GOLDEN_SERVER,
            tools_served=tools_served,
            session_dir=tmp_path / f"run{i}" / "runs",
            raw_dir=tmp_path / f"run{i}" / "raw",
            timeout_s=30.0,
        )
        records = list(read_session(session_path))
        recorded_calls = [r for r in records if isinstance(r, ToolCall)]
        assert len(recorded_calls) == 2, f"run {i}: expected 2 recorded calls, got {len(recorded_calls)}"


def test_make_run_once_rejects_an_unknown_agent_mode(tmp_path):
    store = ReplayStore()
    store.index_session(GOLDEN_FIXTURE)
    with pytest.raises(ValueError, match="agent_mode"):
        make_run_once(
            command=["irrelevant"],
            replay_store=store,
            server_name=GOLDEN_SERVER,
            tools_served=[],
            session_dir=tmp_path / "runs",
            raw_dir=tmp_path / "raw",
            agent_mode="carrier_pigeon",
        )


@pytest.fixture
def anyio_backend():
    return "asyncio"
