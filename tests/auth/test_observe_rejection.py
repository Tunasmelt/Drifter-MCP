"""`drifter observe` exits 4, promptly, when the upstream rejects its credentials (F-41/F-44).

Why this is not just `except* UpstreamAuthRejected` (it is that too, as the fallback): found
by the first end-to-end run of F-41. `observe` hung forever on a rejected token when the agent
kept its stdin open, as a real agent does. The hook's exception cancels the proxy's task
group, and the SDK's `stdio_server()` is waiting on a stdin read in a worker thread that
cancellation cannot interrupt, so the exception never reaches the handler. It is the same root
cause CLAUDE.md records three times already (`handle_sigint` is the Ctrl+C instance), and gets
the same fix: do the synchronous work (flush the recording, say why) and exit the process
directly, without waiting for the task tree to unwind.

Pre-existing and NOT fixed here: an unreachable `url` upstream hangs the same way. It predates
F-41 and has its own failure path; see docs/CHANGELOG.md.
"""

from __future__ import annotations

import io

import pytest
from mcp.shared.message import SessionMessage
from mcp_types import JSONRPCRequest, JSONRPCResponse

from mcp_drifter.auth.errors import UpstreamAuthRejected
from mcp_drifter.auth.upstream import rejection_hook
from mcp_drifter.cli.config import ServerConfig
from mcp_drifter.cli.observe import LiveStatus, exit_on_upstream_rejection
from mcp_drifter.cli.upstream import upstream_http_client
from mcp_drifter.record.proxy import Direction
from mcp_drifter.record.reader import read_session
from mcp_drifter.record.schema import ToolCall, TrajectoryEnd
from mcp_drifter.record.writer import SessionRecorder

from canary_scan import assert_no_leaks, new_canary, scan_text, scan_tree
from oauth_server import FixtureConfig, serve_oauth_fixture
from sdk_client import leaf_exceptions


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _rejection() -> UpstreamAuthRejected:
    return UpstreamAuthRejected("remote-crm", "environment variable CRM_TOKEN", 401)


def _live_recorder(tmp_path):
    recorder = SessionRecorder(session_dir=tmp_path / "runs", raw_dir=tmp_path / "raw", server_name="remote-crm")
    request = JSONRPCRequest(jsonrpc="2.0", id=1, method="tools/call", params={"name": "get_item", "arguments": {"id": "1"}})
    response = JSONRPCResponse(jsonrpc="2.0", id=1, result={"content": [], "structuredContent": {"ok": True}})
    recorder.observe(Direction.AGENT_TO_SERVER, SessionMessage(request))
    recorder.observe(Direction.SERVER_TO_AGENT, SessionMessage(response))
    return recorder


def test_exit_flushes_the_open_trajectory_says_why_and_exits_4(tmp_path):
    recorder = _live_recorder(tmp_path)
    stream = io.StringIO()
    exit_calls: list[int] = []

    exit_on_upstream_rejection(recorder, LiveStatus(stream), stream, _rejection(), exit_fn=exit_calls.append)

    assert exit_calls == [4]
    text = stream.getvalue()
    assert text.count("drifter observe:") == 1
    assert "CRM_TOKEN" in text and "remote-crm" in text and "401" in text

    records = list(read_session(next((tmp_path / "runs").glob("*.jsonl"))))
    calls = [r for r in records if isinstance(r, ToolCall)]
    ends = [r for r in records if isinstance(r, TrajectoryEnd)]
    assert len(calls) == 1 and len(ends) == 1  # the in-progress trajectory was flushed, not lost


def test_a_second_rejection_while_exiting_is_a_no_op(tmp_path):
    recorder = _live_recorder(tmp_path)
    stream = io.StringIO()
    exit_calls: list[int] = []
    for _ in range(2):
        exit_on_upstream_rejection(recorder, LiveStatus(stream), stream, _rejection(), exit_fn=exit_calls.append)
    assert exit_calls == [4]
    assert stream.getvalue().count("drifter observe:") == 1


@pytest.mark.anyio
async def test_the_hook_reports_to_its_callback_before_it_raises():
    seen: list[UpstreamAuthRejected] = []
    hook = rejection_hook("remote-crm", "environment variable CRM_TOKEN", on_rejected=seen.append)

    class _Response:
        status_code = 401
        headers = {"www-authenticate": 'Bearer error="invalid_token"'}

    with pytest.raises(UpstreamAuthRejected) as failure:
        await hook(_Response())
    assert seen == [failure.value] and seen[0].error == "invalid_token"


@pytest.mark.anyio
async def test_the_hook_ignores_responses_that_are_not_auth_failures():
    seen: list[UpstreamAuthRejected] = []
    hook = rejection_hook("s", "environment variable T", on_rejected=seen.append)

    class _Response:
        headers: dict = {}

        def __init__(self, status):
            self.status_code = status

    for status in (200, 202, 400, 404, 500):
        await hook(_Response(status))
    assert seen == []


@pytest.mark.anyio
async def test_a_client_built_for_observe_reports_a_rejection_through_the_callback(tmp_path, monkeypatch):
    wrong = new_canary() + "-wrong-token"
    monkeypatch.setenv("CRM_TOKEN", wrong)
    seen: list[UpstreamAuthRejected] = []
    async with serve_oauth_fixture(FixtureConfig(protection="static_bearer")) as handle:
        server = ServerConfig.model_validate(
            {"name": "remote-crm", "url": handle.mcp_url, "auth": {"type": "bearer_env", "token_env": "CRM_TOKEN"}}
        )
        from mcp import ClientSession

        from mcp_drifter.cli.upstream import connect_upstream

        with pytest.raises(BaseException) as failure:
            async with connect_upstream(server, on_rejected=seen.append) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
        assert len(seen) >= 1 and all(isinstance(e, UpstreamAuthRejected) for e in seen)
        assert [e for e in leaf_exceptions(failure.value) if isinstance(e, UpstreamAuthRejected)]
        secrets = [wrong, *handle.state.all_secrets()]
        assert_no_leaks([*scan_text("message", str(seen[0]), secrets), *scan_tree(tmp_path, secrets)])


@pytest.mark.anyio
async def test_no_callback_is_installed_when_none_is_given(monkeypatch):
    """doctor and fixture capture pass no callback: they report the exception, they do not exit."""
    monkeypatch.setenv("CRM_TOKEN", "tok_123")
    server = ServerConfig.model_validate(
        {"name": "remote-crm", "url": "https://mcp.example.com/mcp", "auth": {"type": "bearer_env", "token_env": "CRM_TOKEN"}}
    )
    async with upstream_http_client(server) as client:
        assert client is not None and len(client.event_hooks["response"]) == 1
