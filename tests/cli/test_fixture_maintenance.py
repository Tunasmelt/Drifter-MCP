"""`drifter fixture capture` / `check` (docs/PHASES.md R0.5, fixture authoring
and maintenance), against the real `tests/fixtures/orders_server.py` and a
corpus recorded through `drifter observe`.
"""

from __future__ import annotations

import io
import sys
import time
from pathlib import Path

import anyio
import pytest
import yaml
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from mcp_drifter.cli.config import ConfigError
from mcp_drifter.cli.fixture import (
    check_failed,
    contains_redaction,
    content_digest,
    gate,
    plan_requests,
    run_fixture_capture,
    run_fixture_check,
)
from mcp_drifter.record.redact import redact_string
from mcp_drifter.replay.authored_responses import load_authored_responses

ORDERS_SERVER = str(Path(__file__).parent.parent / "fixtures" / "orders_server.py")
SERVER = "orders"


def _config(tmp_path: Path, command: list[str]) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    config = tmp_path / "drifter.yaml"
    parts =", ".join(f"'{c}'" for c in command)
    config.write_text(
        f"version: 1\nservers:\n  - name: {SERVER}\n    command: [{parts}]\n"
        f"record:\n  dir: '{(tmp_path / 'corpus').as_posix()}'\n",
        encoding="utf-8",
    )
    return config


@pytest.fixture
def recorded(tmp_path):
    """A real corpus: one observe session calling both tools."""
    config = _config(tmp_path, [sys.executable, ORDERS_SERVER])
    corpus = tmp_path / "corpus"

    async def _session() -> None:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "mcp_drifter.cli", "observe", "--config", str(config), "--server", SERVER],
            env={"DRIFTER_RUNS_DIR": str(corpus), "DRIFTER_RAW_DIR": str(tmp_path / "raw")},
            cwd=str(tmp_path),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                await session.list_tools()
                found = await session.call_tool("find_order", {"customer": "acme"})
                await session.call_tool("get_order", {"order_id": found.content[0].text.strip()})

    anyio.run(_session)
    return config, corpus


# --- unit ---------------------------------------------------------------------


def test_gate_calls_read_only_tools_and_refuses_writes_even_when_allowed():
    allow = frozenset({"convert_time", "delete_order"})
    assert gate("get_order", "read_only_local", frozenset()) is None
    assert gate("fetch", "read_only_external", frozenset()) is None
    assert "--allow-tool convert_time" in gate("convert_time", "unknown", frozenset())
    assert gate("convert_time", "unknown", allow) is None
    for risk in ("reversible_write", "irreversible_write", "destructive"):
        assert "never calls write or destructive tools" in gate("delete_order", risk, allow)
    assert gate("gone", None, allow) == "not in the live server's manifest"


def test_redacted_arguments_are_detected_at_any_depth():
    marker = redact_string("sk-" + "a" * 40)
    assert contains_redaction({"token": marker})
    assert contains_redaction({"filters": [{"key": marker}]})
    assert not contains_redaction({"customer": "acme", "limit": 3})


def test_content_digest_ignores_transport_meta_only():
    body = {"content": [{"type": "text", "text": "1240"}], "isError": False}
    assert content_digest(body) == content_digest({**body, "_meta": {"fastmcp": {"wrap_result": True}}})
    assert content_digest(body) != content_digest({**body, "content": [{"type": "text", "text": "1241"}]})


# --- capture --------------------------------------------------------------------


def test_capture_writes_live_bodies_that_the_replay_loader_accepts(recorded, tmp_path):
    config, corpus = recorded
    output = tmp_path / "responses.yaml"

    statuses = run_fixture_capture(fixture=[corpus], output=output, config_path=config, output_stream=io.StringIO())

    assert [(s.tool_name, s.status) for s in statuses] == [("find_order", "CAPTURED"), ("get_order", "CAPTURED")]
    text = output.read_text(encoding="utf-8")
    assert text.startswith("# Drifter response fixture")
    document = yaml.safe_load(text)
    bodies = {e["tool_name"]: e["result"]["content"][0]["text"] for e in document["responses"]}
    assert bodies == {"find_order": "ord-7f3a91", "get_order": "order ord-7f3a91 total: 1240 dollars"}
    for entry in document["responses"]:
        provenance = entry["provenance"]
        assert provenance["source"] == "live_capture"
        assert provenance["server"]["name"] == "orders-server"
        assert provenance["content_sha256"] == content_digest(entry["result"])
    loaded = load_authored_responses(output, SERVER, plan_paths := sorted(corpus.glob("*.jsonl")))
    assert len(loaded.by_key) == 2 and plan_paths


def test_capture_refuses_to_overwrite_without_force(recorded, tmp_path):
    config, corpus = recorded
    output = tmp_path / "responses.yaml"
    output.write_text("hand-written, keep me\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="--force"):
        run_fixture_capture(fixture=[corpus], output=output, config_path=config, output_stream=io.StringIO())
    assert output.read_text(encoding="utf-8") == "hand-written, keep me\n"


def test_capture_only_plans_real_answered_requests(recorded):
    _, corpus = recorded
    assert [(r.tool_name, r.arguments) for r in plan_requests([corpus], SERVER)] == [
        ("find_order", {"customer": "acme"}),
        ("get_order", {"order_id": "ord-7f3a91"}),
    ]
    assert plan_requests([corpus], "some-other-server") == []


# --- check ----------------------------------------------------------------------


def _captured(recorded, tmp_path) -> Path:
    config, corpus = recorded
    output = tmp_path / "responses.yaml"
    run_fixture_capture(fixture=[corpus], output=output, config_path=config, output_stream=io.StringIO())
    return output


def test_check_reports_fresh_for_an_unchanged_server(recorded, tmp_path):
    config, corpus = recorded
    responses = _captured(recorded, tmp_path)

    statuses = run_fixture_check(fixture=[corpus], responses=responses, config_path=config, output_stream=io.StringIO())

    assert [s.status for s in statuses] == ["FRESH", "FRESH"]
    assert not check_failed(statuses)


def test_check_reports_stale_when_the_stored_body_no_longer_matches(recorded, tmp_path):
    config, corpus = recorded
    responses = _captured(recorded, tmp_path)
    document = yaml.safe_load(responses.read_text(encoding="utf-8"))
    document["responses"][1]["result"]["content"][0]["text"] = "order ord-7f3a91 total: 999 dollars"
    responses.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    statuses = run_fixture_check(fixture=[corpus], responses=responses, config_path=config, output_stream=io.StringIO())

    assert [s.status for s in statuses] == ["FRESH", "STALE"]
    assert "live response differs" in statuses[1].detail
    assert "edited after capture" in statuses[1].detail
    assert check_failed(statuses)


def test_check_reports_unbound_entries_without_calling_them(recorded, tmp_path):
    config, corpus = recorded
    responses = _captured(recorded, tmp_path)
    document = yaml.safe_load(responses.read_text(encoding="utf-8"))
    document["responses"].append({
        "tool_name": "get_order", "arguments": {"order_id": "ord-never-recorded"},
        "result": {"content": [{"type": "text", "text": "x"}], "isError": False},
    })
    responses.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    statuses = run_fixture_check(fixture=[corpus], responses=responses, config_path=config, output_stream=io.StringIO())

    assert [s.status for s in statuses] == ["FRESH", "FRESH", "UNBOUND"]
    assert check_failed(statuses)


def test_check_marks_hand_authored_entries_without_provenance(recorded, tmp_path):
    config, corpus = recorded
    responses = _captured(recorded, tmp_path)
    document = yaml.safe_load(responses.read_text(encoding="utf-8"))
    for entry in document["responses"]:
        entry.pop("provenance")
    responses.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    statuses = run_fixture_check(fixture=[corpus], responses=responses, config_path=config, output_stream=io.StringIO())

    assert [s.status for s in statuses] == ["FRESH", "FRESH"]
    assert all("hand-authored" in s.detail for s in statuses)


# --- shutdown timing (CLAUDE.md: spawn/wait code must be timed, not just pass) ----


def test_a_hanging_server_is_reported_as_error_promptly(recorded, tmp_path):
    _, corpus = recorded
    hang = _config(tmp_path / "hang", [sys.executable, "-c", "import time; time.sleep(120)"])

    started = time.monotonic()
    statuses = run_fixture_check(
        fixture=[corpus], responses=_captured(recorded, tmp_path), config_path=hang, timeout_s=5.0,
        output_stream=io.StringIO(),
    )
    elapsed = time.monotonic() - started

    assert [s.status for s in statuses] == ["ERROR", "ERROR"]
    assert elapsed < 20.0, f"hanging server held check for {elapsed:.1f}s with a 5s timeout"
