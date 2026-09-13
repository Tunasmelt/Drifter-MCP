from __future__ import annotations

from pathlib import Path

import anyio
import pytest
import yaml
from mcp import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from mcp_drifter.record.schema import ToolCall
from mcp_drifter.record.reader import read_session
from mcp_drifter.replay.authored_responses import AuthoredResponseError, load_authored_responses
from mcp_drifter.replay.replay_proxy import run_replay_proxy, tools_served_from_session
from mcp_drifter.replay.replay_store import ReplayStore

GOLDEN = Path(__file__).parent.parent / "fixtures" / "golden_v0.1.jsonl"
SERVER = "filesystem"


def _first_call() -> ToolCall:
    return next(r for r in read_session(GOLDEN) if isinstance(r, ToolCall))


def _write_fixture(tmp_path: Path, call: ToolCall, text: str = "two rows") -> Path:
    path = tmp_path / "responses.yaml"
    document = {"version": 1, "server": SERVER, "responses": [{
        "tool_name": call.tool_name, "arguments": call.arguments,
        "result": {"content": [{"type": "text", "text": text}], "isError": False},
    }]}
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def test_fixture_rejects_a_call_not_present_in_the_recorded_corpus(tmp_path):
    call = _first_call()
    path = _write_fixture(tmp_path, call)
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["responses"][0]["arguments"] = {"never": "recorded"}
    path.write_text(yaml.safe_dump(document), encoding="utf-8")

    with pytest.raises(AuthoredResponseError, match="not present in the recorded corpus"):
        load_authored_responses(path, SERVER, [GOLDEN])


@pytest.mark.anyio
async def test_exact_hit_returns_authored_content_instead_of_an_empty_shape(tmp_path):
    call = _first_call()
    fixture = load_authored_responses(_write_fixture(tmp_path, call), SERVER, [GOLDEN])
    store = ReplayStore()
    store.index_session(GOLDEN)

    async with create_client_server_memory_streams() as (client_streams, server_streams):
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                run_replay_proxy, *server_streams, store, SERVER,
                tools_served_from_session(GOLDEN), None, frozenset(), None, False, None, fixture,
            )
            async with ClientSession(*client_streams) as session:
                await session.initialize()
                result = await session.call_tool(call.tool_name, call.arguments)
            tg.cancel_scope.cancel()

    assert result.content[0].text == "two rows"


def test_fixture_is_explicit_and_does_not_modify_the_recorded_session(tmp_path):
    before = GOLDEN.read_bytes()
    load_authored_responses(_write_fixture(tmp_path, _first_call()), SERVER, [GOLDEN])
    assert GOLDEN.read_bytes() == before
