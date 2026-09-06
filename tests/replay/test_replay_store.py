"""Tests for the replay store (F-11), docs/SPEC.md §7 tier 1 (exact-key) only.

Loads from the golden fixture (tests/fixtures/golden_v0.1.jsonl) as the
primary test corpus — per CLAUDE.md's testing discipline, that's the only
genuinely reviewed, known-correct corpus that exists right now. The real
trial corpus either doesn't exist or hasn't been validated (see
.drifter/GATE_STATUS's gate_1_note — Gate 1 was closed by override, not by
passing its exit test, precisely because that corpus was never produced).
"""

from pathlib import Path

from record.reader import read_session
from record.schema import ToolCall
from replay.replay_store import RecordedResponse, ReplayStore, replay_key

GOLDEN_FIXTURE = Path(__file__).parent.parent / "fixtures" / "golden_v0.1.jsonl"


def _golden_store() -> ReplayStore:
    store = ReplayStore()
    store.index_session(GOLDEN_FIXTURE)
    return store


def _golden_calls() -> list[ToolCall]:
    return [r for r in read_session(GOLDEN_FIXTURE) if isinstance(r, ToolCall)]


def test_every_golden_fixture_call_resolves_as_an_exact_hit():
    store = _golden_store()
    calls = _golden_calls()
    assert len(calls) == 7  # matches the golden fixture's known, reviewed content

    for call in calls:
        hit = store.lookup(call.server, call.tool_name, call.arguments)
        assert hit is not None, f"expected HIT for seq={call.seq} tool={call.tool_name}"
        assert hit.match_tier == "exact"
        assert hit.result_shape == call.result_shape
        assert hit.is_error == call.is_error
        assert hit.fault == call.fault


def test_golden_fixture_error_call_hits_with_its_recorded_is_error():
    """Exact-value spot check (CLAUDE.md's testing discipline), not just
    presence: the golden fixture's one genuine is_error=True call (a
    read_text_file against a nonexistent path) must resolve HIT with
    is_error True specifically, not merely "some truthy value"."""
    store = _golden_store()
    errored = next(c for c in _golden_calls() if c.is_error)
    hit = store.lookup(errored.server, errored.tool_name, errored.arguments)
    assert hit is not None
    assert hit.is_error is True
    assert hit.fault is False  # known-not-a-fault (v1.0.10) — reached a real CallToolResult


def test_unrecorded_arguments_return_miss_not_an_error():
    store = _golden_store()
    result = store.lookup("filesystem", "list_directory", {"path": "C:\\nowhere\\this\\was\\never\\recorded"})
    assert result is None


def test_unrecorded_tool_name_returns_miss():
    store = _golden_store()
    result = store.lookup("filesystem", "not_a_real_tool", {})
    assert result is None


def test_unrecorded_server_returns_miss():
    """Same (tool_name, arguments) as a real recorded call, different
    server — the key includes server, so this must still miss."""
    store = _golden_store()
    calls = _golden_calls()
    real_call = calls[0]
    result = store.lookup("a-different-server", real_call.tool_name, real_call.arguments)
    assert result is None


def test_replay_key_is_stable_regardless_of_argument_key_order():
    # "Canonical" per replay_store.py's own docstring: dict insertion
    # order must not affect the hash, or an exact match that should hit
    # would miss purely due to how the caller happened to build the dict.
    key_a = replay_key("srv", "tool", {"a": 1, "b": 2})
    key_b = replay_key("srv", "tool", {"b": 2, "a": 1})
    assert key_a == key_b


def test_replay_key_differs_for_different_arguments():
    key_a = replay_key("srv", "tool", {"a": 1})
    key_b = replay_key("srv", "tool", {"a": 2})
    assert key_a != key_b


def test_index_session_last_writer_wins_on_a_repeated_key(tmp_path):
    """Two ToolCall records with identical (server, tool_name,
    arguments) but different results — the second (later seq) one must
    be what lookup() returns, matching record/segment.py's existing
    last-writer-wins precedent for a repeated-value index."""
    import json

    session_id = "repeat_sess"
    lines = [
        {
            "schema_version": "0.1",
            "record_type": "session_start",
            "session_id": session_id,
            "seq": 0,
            "started_at": "2026-08-16T19:40:00Z",
            "environment": {
                "agent_identity": None, "model_name": None, "server_versions": {},
                "tool_manifest_hash": None, "fingerprint": None,
            },
            "raw_frame_offset": 0,
        },
        {
            "schema_version": "0.1", "record_type": "tool_call", "session_id": session_id,
            "seq": 1, "timestamp": "2026-08-16T19:40:01Z", "server": "srv", "tool_name": "get",
            "arguments": {"id": "1"}, "result_shape": {"type": "object", "keys": ["v"]},
            "is_error": False, "duration_ms": 1.0, "fault": False, "result_provenance": "real",
            "references": [], "mutation_inverse": None, "raw_frame_offset": 100,
        },
        {
            "schema_version": "0.1", "record_type": "tool_call", "session_id": session_id,
            "seq": 2, "timestamp": "2026-08-16T19:40:02Z", "server": "srv", "tool_name": "get",
            "arguments": {"id": "1"}, "result_shape": {"type": "object", "keys": ["v", "w"]},
            "is_error": False, "duration_ms": 1.0, "fault": False, "result_provenance": "real",
            "references": [], "mutation_inverse": None, "raw_frame_offset": 200,
        },
    ]
    path = tmp_path / "repeat.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")

    store = ReplayStore()
    store.index_session(path)
    hit = store.lookup("srv", "get", {"id": "1"})
    assert hit is not None
    assert hit.result_shape == {"type": "object", "keys": ["v", "w"]}  # the seq=2 result, not seq=1's


def _write_session(path: Path, session_id: str, tool_calls: list[dict]) -> None:
    import json

    lines = [
        {
            "schema_version": "0.1",
            "record_type": "session_start",
            "session_id": session_id,
            "seq": 0,
            "started_at": "2026-08-16T19:40:00Z",
            "environment": {
                "agent_identity": None, "model_name": None, "server_versions": {},
                "tool_manifest_hash": None, "fingerprint": None,
            },
            "raw_frame_offset": 0,
        }
    ]
    for i, call in enumerate(tool_calls, start=1):
        lines.append(
            {
                "schema_version": "0.1", "record_type": "tool_call", "session_id": session_id,
                "seq": i, "timestamp": "2026-08-16T19:40:01Z", "server": call.get("server", "srv"),
                "tool_name": call["tool_name"], "arguments": call.get("arguments", {}),
                "result_shape": call.get("result_shape"), "is_error": call.get("is_error"),
                "duration_ms": 1.0, "fault": call.get("fault", False), "result_provenance": "real",
                "references": [], "mutation_inverse": None, "raw_frame_offset": i * 100,
            }
        )
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")


def test_indexing_two_separate_session_files_into_one_store_merges_both(tmp_path):
    """The public API allows index_session() to be called multiple
    times on the same store -- not exercised by any existing test,
    which only ever indexes a single golden-fixture file. A caller
    building a store from several recorded sessions (e.g. a richer
    corpus than one file) must get hits from EITHER file, not just
    whichever was indexed most recently."""
    path_a = tmp_path / "a.jsonl"
    path_b = tmp_path / "b.jsonl"
    _write_session(path_a, "sess_a", [{"tool_name": "get", "arguments": {"id": "1"}, "result_shape": {"type": "object", "keys": ["v"]}, "is_error": False}])
    _write_session(path_b, "sess_b", [{"tool_name": "list", "arguments": {}, "result_shape": {"type": "array", "length": 0}, "is_error": False}])

    store = ReplayStore()
    store.index_session(path_a)
    store.index_session(path_b)

    hit_a = store.lookup("srv", "get", {"id": "1"})
    hit_b = store.lookup("srv", "list", {})
    assert hit_a is not None and hit_a.result_shape == {"type": "object", "keys": ["v"]}
    assert hit_b is not None and hit_b.result_shape == {"type": "array", "length": 0}


def test_last_writer_wins_across_two_separate_files_not_just_within_one(tmp_path):
    """The existing last-writer-wins test only covers two records in
    ONE file. Confirmed here across two SEPARATE files indexed in
    order -- the second file's recording must win, matching the same
    "most recent recording is most representative" rationale."""
    path_a = tmp_path / "a.jsonl"
    path_b = tmp_path / "b.jsonl"
    _write_session(path_a, "sess_a", [{"tool_name": "get", "arguments": {"id": "1"}, "result_shape": {"type": "object", "keys": ["old"]}, "is_error": False}])
    _write_session(path_b, "sess_b", [{"tool_name": "get", "arguments": {"id": "1"}, "result_shape": {"type": "object", "keys": ["new"]}, "is_error": False}])

    store = ReplayStore()
    store.index_session(path_a)
    store.index_session(path_b)
    hit = store.lookup("srv", "get", {"id": "1"})
    assert hit.result_shape == {"type": "object", "keys": ["new"]}


def test_a_protocol_level_fault_call_hits_with_a_null_result_shape(tmp_path):
    """The golden fixture has zero fault=True calls (confirmed directly,
    not assumed) -- a protocol-level fault (a JSON-RPC error response,
    never reaching a CallToolResult) has no result_shape at all, per
    record/schema.py's own nullable field. Hand-built here since no
    existing fixture covers this real, distinct outcome."""
    path = tmp_path / "fault.jsonl"
    _write_session(path, "sess_fault", [{"tool_name": "broken_tool", "arguments": {}, "result_shape": None, "is_error": None, "fault": True}])

    store = ReplayStore()
    store.index_session(path)
    hit = store.lookup("srv", "broken_tool", {})
    assert hit is not None
    assert hit.fault is True
    assert hit.result_shape is None


def test_index_session_against_a_session_with_zero_tool_calls_does_not_crash(tmp_path):
    """A session that's just a SessionStart (e.g. a connectivity check,
    SPEC.md limitation 12) contributes nothing to the index but must
    not raise."""
    path = tmp_path / "empty.jsonl"
    _write_session(path, "sess_empty", [])
    store = ReplayStore()
    store.index_session(path)  # must not raise
    assert store.lookup("srv", "anything", {}) is None


def test_replay_key_canonicalizes_nested_dict_key_order_too():
    """The existing order-independence test only covers TOP-LEVEL key
    order. json.dumps(sort_keys=True) recursively sorts nested dicts
    too -- confirmed explicitly with a nested example, not assumed from
    reading the stdlib's own behavior."""
    key_a = replay_key("srv", "tool", {"outer": {"z": 1, "a": 2}})
    key_b = replay_key("srv", "tool", {"outer": {"a": 2, "z": 1}})
    assert key_a == key_b


def test_secret_shaped_arguments_hit_when_the_live_lookup_uses_the_real_unredacted_value():
    """The recorded ToolCall.arguments on disk is already redacted
    (F-04) — a live lookup with the real, unredacted secret value must
    still hit, or exact-key replay would silently and permanently miss
    every call whose arguments ever contained a secret-shaped value.
    replay_key() redacts on both sides specifically to prevent this.
    """
    # A real-looking secret, shaped to trigger redact_string's pattern match.
    live_arguments = {"token": "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"}
    key_from_live_value = replay_key("srv", "auth_tool", live_arguments)

    store = ReplayStore()
    store._index[key_from_live_value] = RecordedResponse(
        result_shape={"type": "object", "keys": ["ok"]}, is_error=False, fault=False, match_tier="exact",
    )

    hit = store.lookup("srv", "auth_tool", live_arguments)
    assert hit is not None
    assert hit.result_shape == {"type": "object", "keys": ["ok"]}
