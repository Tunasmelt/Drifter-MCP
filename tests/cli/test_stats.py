"""Tests for `drifter stats` (F-10).

Two layers, matching this repo's established pattern (tests/cli/test_observe.py):
a precise unit layer driving record.writer.SessionRecorder directly with real
protocol objects (JSONRPCRequest/JSONRPCResponse, not mocks) for exact-value
assertions, and a real-subprocess integration layer spawning `drifter observe`
against tests/fixtures/fake_server.py end-to-end, then reading the corpus it
actually produced.
"""

import sys
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.message import SessionMessage
from mcp_types import ErrorData, JSONRPCError, JSONRPCRequest, JSONRPCResponse

from cli.stats import collect_stats, render_stats, run_stats
from record.proxy import Direction
from record.reader import read_session
from record.schema import ToolCall
from record.writer import SessionRecorder

FIXTURE_SERVER = str(Path(__file__).parent.parent / "fixtures" / "fake_server.py")


def _tools_list_result() -> dict:
    return {
        "tools": [
            {"name": "add", "description": "Add two integers.", "inputSchema": {}},
            {"name": "echo", "description": "Echo.", "inputSchema": {}},
            {"name": "fail", "description": "Always fails.", "inputSchema": {}},
        ]
    }


def _record_synthetic_session(session_dir: Path, raw_dir: Path, session_id: str) -> None:
    """Drives one SessionRecorder through a small, precisely-known sequence:
    a tools/list exposing add/echo/fail, two identical `add` calls (the
    second is a retry), and one failing `fail` call — chosen to exercise
    every stat drifter stats reports (frequency, unused tools, retry rate,
    error rate, latency) in a single tiny "corpus."
    """
    recorder = SessionRecorder(session_dir=session_dir, raw_dir=raw_dir, server_name="fake", session_id=session_id)

    req_id = 0

    def _tools_list():
        nonlocal req_id
        req_id += 1
        request = JSONRPCRequest(jsonrpc="2.0", id=req_id, method="tools/list", params={})
        response = JSONRPCResponse(jsonrpc="2.0", id=req_id, result=_tools_list_result())
        recorder.observe(Direction.AGENT_TO_SERVER, SessionMessage(request))
        recorder.observe(Direction.SERVER_TO_AGENT, SessionMessage(response))

    def _call(name: str, arguments: dict, result: dict):
        nonlocal req_id
        req_id += 1
        request = JSONRPCRequest(jsonrpc="2.0", id=req_id, method="tools/call", params={"name": name, "arguments": arguments})
        response = JSONRPCResponse(jsonrpc="2.0", id=req_id, result=result)
        recorder.observe(Direction.AGENT_TO_SERVER, SessionMessage(request))
        recorder.observe(Direction.SERVER_TO_AGENT, SessionMessage(response))

    _tools_list()
    _call("add", {"a": 1, "b": 2}, {"content": [], "structuredContent": {"result": 3}})
    _call("add", {"a": 1, "b": 2}, {"content": [], "structuredContent": {"result": 3}})  # exact repeat -> retry
    _call("fail", {"message": "boom"}, {"content": [{"type": "text", "text": "boom"}], "isError": True})

    recorder.close()


def _write_pre_prompt8_session(runs_dir: Path, session_id: str, tool_calls: list[tuple[str, dict]]) -> None:
    """Writes a session JSONL file in the exact shape record/writer.py
    produced through Prompt 7 — before is_error/duration_ms existed on
    ToolCall (docs/CHANGELOG.md v1.0.7). Raw dicts, not the current ToolCall
    model, deliberately: constructing via the current model would just
    exercise its own defaults, not reproduce what's genuinely sitting on
    disk from before this schema change. Field values below (result_shape
    shape, key ordering aside) match an authentic old-code recording of
    this exact scenario, captured by hand via `git stash` against the
    real pre-Prompt-8 commit while building this fix.
    """
    import json

    runs_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        {
            "schema_version": "0.1",
            "record_type": "session_start",
            "session_id": session_id,
            "seq": 0,
            "started_at": "2026-08-17T18:29:42Z",
            "environment": {
                "agent_identity": "mcp/0.1.0",
                "model_name": None,
                "server_versions": {"fake-server": ""},
                "tool_manifest_hash": "sha256:deadbeef",
                "fingerprint": "sha256:cafef00d",
            },
            "raw_frame_offset": 0,
        }
    ]
    for i, (tool_name, arguments) in enumerate(tool_calls, start=1):
        lines.append(
            {
                "schema_version": "0.1",
                "record_type": "tool_call",
                "session_id": session_id,
                "seq": i,
                "timestamp": "2026-08-17T18:29:43Z",
                "server": "fake",
                "tool_name": tool_name,
                "arguments": arguments,
                "result_shape": {"type": "object", "keys": ["content", "isError"], "array_lengths": {"content": 1}},
                "result_provenance": "real",
                "references": [],
                "mutation_inverse": None,
                "raw_frame_offset": 100 * i,
                # No `is_error`, no `duration_ms` — the exact gap this fix addresses.
            }
        )
    (runs_dir / f"{session_id}.jsonl").write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )


# --- backward compatibility: pre-Prompt-8 data (no is_error/duration_ms) --


def test_pre_prompt8_data_does_not_crash_and_reports_unknown_not_zero(tmp_path):
    """The exact scenario the user flagged: running the current
    `drifter stats` against data recorded before is_error/duration_ms
    existed must neither crash (a raw ValidationError used to happen here
    before this fix — is_error/duration_ms were required fields) nor
    silently read as "0% errors" (which would be the sixth instance of
    this project's recurring "field populated/unpopulated with a
    plausible-but-wrong value" bug pattern, just surfaced through old
    data instead of new code).
    """
    runs_dir = tmp_path / "runs"
    _write_pre_prompt8_session(runs_dir, "old_sess", [("add", {"a": 1, "b": 2}), ("add", {"a": 1, "b": 2})])

    stats = collect_stats(runs_dir)  # must not raise
    add_stats = stats.per_tool[("fake", "add")]

    assert add_stats.calls == 2
    assert add_stats.errors == 0
    assert add_stats.error_unknown == 2
    assert add_stats.error_rate is None  # not 0.0 — genuinely unknown, not "no errors"
    assert add_stats.percentiles() is None  # no known durations at all

    # Retries are unaffected: `arguments` always existed, even pre-Prompt-8.
    assert add_stats.retries == 1
    assert add_stats.retry_rate == 0.5

    text = render_stats(stats)
    add_row = next(line for line in text.splitlines() if line.startswith("fake.add"))
    label, calls, err_col, fault_col, retry_col = add_row.split()[:5]
    assert err_col == "N/A*"  # the ERR% column specifically — not "0.0%", not conflated with RETRY%'s 50.0%
    # This corpus predates `fault` too (it's older than is_error/duration_ms even) — FAULT% is
    # equally unknown here, not a claim this test is making about fault specifically.
    assert fault_col == "N/A^"
    assert retry_col == "50.0%"


def test_mixed_old_and_new_data_excludes_only_unknown_calls_from_error_rate(tmp_path):
    """A corpus spanning the schema migration (trial started before
    Prompt 8, kept running after) must compute error_rate over the known
    subset only — not silently treat the unknown-status calls as
    non-errors by including them in the denominator."""
    runs_dir, raw_dir = tmp_path / "runs", tmp_path / "raw"
    _write_pre_prompt8_session(runs_dir, "old_sess", [("add", {"a": 1, "b": 2})])
    _record_synthetic_session(runs_dir, raw_dir, "new_sess")  # has 2 "add" calls, 0 errors, is_error known

    stats = collect_stats(runs_dir)
    add_stats = stats.per_tool[("fake", "add")]

    assert add_stats.calls == 3  # 1 old (unknown) + 2 new (known)
    assert add_stats.error_unknown == 1
    assert add_stats.known_error_calls == 2
    assert add_stats.errors == 0
    assert add_stats.error_rate == 0.0  # computed over the 2 known calls, not diluted by the 1 unknown one


def _write_pre_fault_field_session(runs_dir: Path, session_id: str, tool_calls: list[tuple[str, dict, bool]]) -> None:
    """Writes a session JSONL file in the shape record/writer.py produced
    between v1.0.8 and the fault-field addition: is_error/duration_ms
    present (the earlier backward-compat fix), but no `fault` key at all
    — a genuinely different "age" than _write_pre_prompt8_session's
    records above, since a real corpus could span either boundary
    independently depending on when the trial started relative to each
    schema change. `tool_calls` items are (tool_name, arguments, is_error).
    """
    import json

    runs_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        {
            "schema_version": "0.1",
            "record_type": "session_start",
            "session_id": session_id,
            "seq": 0,
            "started_at": "2026-08-17T18:29:42Z",
            "environment": {
                "agent_identity": "mcp/0.1.0",
                "model_name": None,
                "server_versions": {"fake-server": ""},
                "tool_manifest_hash": "sha256:deadbeef",
                "fingerprint": "sha256:cafef00d",
            },
            "raw_frame_offset": 0,
        }
    ]
    for i, (tool_name, arguments, is_error) in enumerate(tool_calls, start=1):
        lines.append(
            {
                "schema_version": "0.1",
                "record_type": "tool_call",
                "session_id": session_id,
                "seq": i,
                "timestamp": "2026-08-17T18:29:43Z",
                "server": "fake",
                "tool_name": tool_name,
                "arguments": arguments,
                "result_shape": {"type": "object", "keys": ["content", "isError"], "array_lengths": {"content": 1}},
                "is_error": is_error,
                "duration_ms": 12.5,
                "result_provenance": "real",
                "references": [],
                "mutation_inverse": None,
                "raw_frame_offset": 100 * i,
                # No `fault` key at all — the exact gap this test targets.
            }
        )
    (runs_dir / f"{session_id}.jsonl").write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )


# --- backward compatibility: pre-fault-field data (is_error known, fault unknown) --


def test_pre_fault_field_data_does_not_crash_and_does_not_misreport_as_no_fault(tmp_path):
    """Written before any fault-tracking code exists, per the user's
    explicit request: construct a corpus of records that predate the
    `fault` field (is_error/duration_ms present — this is v1.0.8-era data,
    not v1.0.7-era) and confirm `drifter stats` neither crashes nor
    reports fault_rate as 0.0 for them. The last two schema touches
    (v1.0.7 required-field crash, then its v1.0.8 fix) both got the
    "how do old records behave" question wrong on the first pass — this
    test exists to catch the same mistake here before it ships, not
    after. It is expected to fail right now (no `fault` field exists at
    all yet); it must also be shown to fail against a naive
    non-Optional `fault: bool = False` implementation before the real,
    Optional-with-explicit-unknown-tracking implementation lands — see
    docs/CHANGELOG.md's entry for this change for that verification step.
    """
    runs_dir = tmp_path / "runs"
    _write_pre_fault_field_session(
        runs_dir,
        "pre_fault_sess",
        [("add", {"a": 1, "b": 2}, False), ("add", {"a": 1, "b": 2}, False)],
    )

    stats = collect_stats(runs_dir)  # must not raise
    add_stats = stats.per_tool[("fake", "add")]

    assert add_stats.calls == 2
    assert add_stats.faults == 0
    assert add_stats.fault_unknown == 2
    assert add_stats.fault_rate is None  # not 0.0 — genuinely unknown, not "no faults"

    # is_error IS known for this era of data (unlike the pre-Prompt-8 case)
    # — this test would also catch a fault-field regression that
    # accidentally broke the already-working is_error path.
    assert add_stats.error_unknown == 0
    assert add_stats.error_rate == 0.0

    text = render_stats(stats)
    add_row = next(line for line in text.splitlines() if line.startswith("fake.add"))
    fault_col = add_row.split()[3]  # TOOL CALLS ERR% FAULT% RETRY% ...
    assert fault_col.startswith("N/A")  # not "0.0%"


# --- fault path: genuine protocol-level tools/call failures --------------


def test_fault_response_writes_a_tool_call_record_distinct_from_is_error(tmp_path):
    """A `tools/call` that fails at the protocol level (a real JSONRPCError
    response, not a CallToolResult) must now produce a ToolCall record —
    previously dropped with no per-tool attribution at all. `fault=True`,
    `is_error=None` (not applicable — no CallToolResult ever existed to
    read isError from), `result_shape=None` (no result), `duration_ms`
    still measured (the attempt still took real wall-clock time).
    """
    runs_dir, raw_dir = tmp_path / "runs", tmp_path / "raw"
    recorder = SessionRecorder(session_dir=runs_dir, raw_dir=raw_dir, server_name="fake", session_id="fault_sess")

    request = JSONRPCRequest(jsonrpc="2.0", id=1, method="tools/call", params={"name": "add", "arguments": {"a": 1, "b": 2}})
    error_response = JSONRPCError(jsonrpc="2.0", id=1, error=ErrorData(code=-32601, message="Method not found"))
    recorder.observe(Direction.AGENT_TO_SERVER, SessionMessage(request))
    recorder.observe(Direction.SERVER_TO_AGENT, SessionMessage(error_response))
    recorder.close()

    jsonl_files = list(runs_dir.glob("*.jsonl"))
    assert len(jsonl_files) == 1
    calls = [r for r in read_session(jsonl_files[0]) if isinstance(r, ToolCall)]
    assert len(calls) == 1
    call = calls[0]

    assert call.tool_name == "add"
    assert call.fault is True
    assert call.is_error is None  # not applicable, not "no error" — no CallToolResult existed
    assert call.result_shape is None
    assert call.duration_ms is not None and call.duration_ms >= 0
    assert call.arguments == {"a": 1, "b": 2}


def test_collect_stats_counts_fault_separately_from_is_error(tmp_path):
    """A tool with one genuine tool-level error (is_error=True) and one
    genuine protocol fault must show up as errors=1/faults=0 for the
    first and errors=0/faults=1 for the second — never summed into one
    number, per the user's explicit ask that these stay separate columns.
    """
    runs_dir, raw_dir = tmp_path / "runs", tmp_path / "raw"
    recorder = SessionRecorder(session_dir=runs_dir, raw_dir=raw_dir, server_name="fake", session_id="mixed_sess")

    # A genuine tool-level error (isError: true) on "fail".
    req1 = JSONRPCRequest(jsonrpc="2.0", id=1, method="tools/call", params={"name": "fail", "arguments": {}})
    resp1 = JSONRPCResponse(jsonrpc="2.0", id=1, result={"content": [{"type": "text", "text": "boom"}], "isError": True})
    recorder.observe(Direction.AGENT_TO_SERVER, SessionMessage(req1))
    recorder.observe(Direction.SERVER_TO_AGENT, SessionMessage(resp1))

    # A genuine protocol fault on "fail" (different call, same tool).
    req2 = JSONRPCRequest(jsonrpc="2.0", id=2, method="tools/call", params={"name": "fail", "arguments": {}})
    resp2 = JSONRPCError(jsonrpc="2.0", id=2, error=ErrorData(code=-32602, message="Invalid params"))
    recorder.observe(Direction.AGENT_TO_SERVER, SessionMessage(req2))
    recorder.observe(Direction.SERVER_TO_AGENT, SessionMessage(resp2))

    # A clean success on "fail" (third call), so known_error/fault denominators aren't trivially 1.
    req3 = JSONRPCRequest(jsonrpc="2.0", id=3, method="tools/call", params={"name": "fail", "arguments": {"x": 1}})
    resp3 = JSONRPCResponse(jsonrpc="2.0", id=3, result={"content": [], "isError": False})
    recorder.observe(Direction.AGENT_TO_SERVER, SessionMessage(req3))
    recorder.observe(Direction.SERVER_TO_AGENT, SessionMessage(resp3))

    recorder.close()

    stats = collect_stats(runs_dir)
    fail_stats = stats.per_tool[("fake", "fail")]

    assert fail_stats.calls == 3
    assert fail_stats.errors == 1
    assert fail_stats.faults == 1
    # Denominators: is_error is unknown for the fault call (2 known: call 1 & 3), fault is
    # known for all 3 (calls 1 & 3 are known-non-fault via the writer's explicit fault=False).
    assert fail_stats.known_error_calls == 2
    assert fail_stats.known_fault_calls == 3
    assert fail_stats.error_rate == 0.5  # 1 error / 2 known-error calls
    assert fail_stats.fault_rate == pytest.approx(1 / 3)  # 1 fault / 3 known-fault calls


# --- collect_stats / render_stats: precise, synthetic-driven -------------


def test_collect_stats_empty_corpus_reports_zero_sessions(tmp_path):
    stats = collect_stats(tmp_path)
    assert stats.sessions == 0
    assert stats.total_calls == 0
    assert stats.per_tool == {}
    assert "no sessions found" in render_stats(stats).lower()


def test_collect_stats_exact_values_on_a_small_known_session(tmp_path):
    runs_dir, raw_dir = tmp_path / "runs", tmp_path / "raw"
    _record_synthetic_session(runs_dir, raw_dir, "sess1")

    stats = collect_stats(runs_dir)
    assert stats.sessions == 1
    assert stats.total_calls == 3

    add_stats = stats.per_tool[("fake", "add")]
    assert add_stats.calls == 2
    assert add_stats.errors == 0
    assert add_stats.retries == 1  # the second, identical call
    assert add_stats.retry_rate == 0.5
    assert add_stats.error_rate == 0.0
    assert len(add_stats.durations_ms) == 2
    assert all(d >= 0 for d in add_stats.durations_ms)

    fail_stats = stats.per_tool[("fake", "fail")]
    assert fail_stats.calls == 1
    assert fail_stats.errors == 1
    assert fail_stats.error_rate == 1.0
    assert fail_stats.retries == 0

    # "echo" is in the manifest (tools_served) but was never called.
    assert stats.unused_tools() == {("fake", "echo")}

    assert stats.total_errors == 1
    assert stats.total_retries == 1


def test_percentiles_well_defined_for_a_single_call(tmp_path):
    """The user explicitly wants this to work against a very small corpus —
    a tool called exactly once must still produce p50/p90/p99 (all equal
    to that one duration), not crash or return None."""
    runs_dir, raw_dir = tmp_path / "runs", tmp_path / "raw"
    _record_synthetic_session(runs_dir, raw_dir, "sess1")

    stats = collect_stats(runs_dir)
    fail_stats = stats.per_tool[("fake", "fail")]
    percentiles = fail_stats.percentiles()
    assert percentiles is not None
    only_duration = fail_stats.durations_ms[0]
    assert percentiles[0.5] == only_duration
    assert percentiles[0.9] == only_duration
    assert percentiles[0.99] == only_duration


def test_collect_stats_aggregates_across_multiple_session_files(tmp_path):
    runs_dir, raw_dir = tmp_path / "runs", tmp_path / "raw"
    _record_synthetic_session(runs_dir, raw_dir, "sess1")
    _record_synthetic_session(runs_dir, raw_dir, "sess2")

    stats = collect_stats(runs_dir)
    assert stats.sessions == 2
    assert stats.per_tool[("fake", "add")].calls == 4
    # Retries reset per session: sess2's first "add" call is NOT a retry of
    # sess1's last "add" call, even though the arguments are identical.
    assert stats.per_tool[("fake", "add")].retries == 2


def test_render_stats_output_contains_expected_summary(tmp_path):
    runs_dir, raw_dir = tmp_path / "runs", tmp_path / "raw"
    _record_synthetic_session(runs_dir, raw_dir, "sess1")

    text = render_stats(collect_stats(runs_dir))
    assert "fake.add" in text
    assert "fake.fail" in text
    assert "UNUSED TOOLS" in text
    assert "fake.echo" in text
    assert "calls=3" in text


def test_run_stats_server_filter_excludes_other_servers(tmp_path):
    runs_dir, raw_dir = tmp_path / "runs", tmp_path / "raw"
    _record_synthetic_session(runs_dir, raw_dir, "sess1")

    stats = collect_stats(runs_dir, server_filter="not-fake")
    assert stats.per_tool == {}
    assert stats.known_tools == set()


# --- integration: real subprocess, real `drifter observe` corpus ---------


@pytest.mark.anyio
async def test_stats_against_a_real_observe_session(tmp_path):
    runs_dir = tmp_path / "runs"
    config_path = tmp_path / "drifter.yaml"
    config_path.write_text(
        "\n".join(
            [
                "version: 1",
                "servers:",
                "  - name: fake",
                f"    command: ['{sys.executable}', '{FIXTURE_SERVER}']",
                "record:",
                f"  dir: {runs_dir.as_posix()}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "cli", "observe", "--config", str(config_path), "--server", "fake"],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            await session.call_tool("add", {"a": 1, "b": 2})
            await session.call_tool("add", {"a": 1, "b": 2})
            await session.call_tool("fail", {"message": "boom"})

    stats = collect_stats(runs_dir)
    assert stats.sessions == 1
    assert stats.per_tool[("fake", "add")].calls == 2
    assert stats.per_tool[("fake", "add")].retries == 1
    assert stats.per_tool[("fake", "fail")].errors == 1
    assert stats.unused_tools() == {("fake", "echo")}


def test_stats_subprocess_stdout_is_valid_utf8_not_mangled_by_console_codepage(tmp_path):
    """Regression test for the same class of bug cli/app.py's
    _ensure_utf8_console_streams fixed for stderr in Prompt 7: Windows'
    default console codepage (observed: cp1252) silently mangles the em
    dash render_stats() emits unless stdout is explicitly reconfigured to
    UTF-8. Found by hand running `drifter stats` in a real terminal while
    building this feature — codifying it here rather than leaving it as a
    one-off manual check.
    """
    import subprocess

    runs_dir, raw_dir = tmp_path / "runs", tmp_path / "raw"
    _record_synthetic_session(runs_dir, raw_dir, "sess1")

    result = subprocess.run(
        [sys.executable, "-m", "cli", "stats", "--runs-dir", str(runs_dir)],
        capture_output=True,
        check=True,
    )
    decoded = result.stdout.decode("utf-8")  # raises UnicodeDecodeError if mangled
    assert "—" in decoded  # the em dash itself, not a replacement/mojibake byte


def test_run_stats_writes_to_given_stream(tmp_path):
    import io

    runs_dir, raw_dir = tmp_path / "runs", tmp_path / "raw"
    _record_synthetic_session(runs_dir, raw_dir, "sess1")

    out = io.StringIO()
    run_stats(runs_dir=runs_dir, output_stream=out)
    assert "fake.add" in out.getvalue()


@pytest.fixture
def anyio_backend():
    return "asyncio"
