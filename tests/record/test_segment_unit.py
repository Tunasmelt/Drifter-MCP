"""Direct, fast unit tests for record/segment.py's `TrajectoryTracker` /
`Trajectory` (F-06/F-07/F-08) — driven directly against the classes, not
through a real subprocess round-trip (test_segment.py's own job). The
existing 3 integration tests cover the three documented top-level
scenarios (trace context, heuristic + data-flow link, the known
adversarial limitation) but never touch several real pieces of this
module's own logic directly: `extract_trace_id`'s malformed-input
fallback, `_iter_leaf_paths`'s JSON-string-unwrapping, "last writer
wins" on a repeated value, and whether trace-tagged and heuristic calls
interleaved in one session cross-contaminate each other's state.
"""

from __future__ import annotations

from record.segment import TrajectoryTracker, extract_trace_id

IDLE_GAP = 30.0
HEURISTIC_CONFIDENCE = 0.6


def _tracker() -> TrajectoryTracker:
    return TrajectoryTracker(idle_gap_seconds=IDLE_GAP, heuristic_confidence=HEURISTIC_CONFIDENCE)


# --- extract_trace_id: malformed-input fallback (F-06) ----------------------


def test_extract_trace_id_returns_none_for_absent_meta():
    assert extract_trace_id(None) == extract_trace_id({}) is None


def test_extract_trace_id_returns_none_for_a_malformed_traceparent():
    """A garbage or truncated traceparent must fall back to heuristic
    segmentation (return None), not crash or half-match -- confirmed
    against several real malformed shapes, not just one."""
    malformed_examples = [
        "not-a-traceparent-at-all",
        "00-tooshort-00f067aa0ba902b7-01",  # trace id wrong length
        "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7",  # missing flags segment
        "",
        "zz-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",  # non-hex version
    ]
    for traceparent in malformed_examples:
        assert extract_trace_id({"traceparent": traceparent}) is None, traceparent


def test_extract_trace_id_ignores_a_non_string_traceparent_value():
    assert extract_trace_id({"traceparent": 12345}) is None
    assert extract_trace_id({"traceparent": None}) is None


def test_extract_trace_id_is_case_insensitive_on_hex_digits():
    """The regex is compiled with re.IGNORECASE -- confirmed a real
    uppercase-hex traceparent (valid per the W3C spec, which doesn't
    mandate lowercase) still extracts correctly, not just lowercase
    examples."""
    traceparent = "00-4BF92F3577B34DA6A3CE929D0E0E4736-00F067AA0BA902B7-01"
    assert extract_trace_id({"traceparent": traceparent}) == "4BF92F3577B34DA6A3CE929D0E0E4736"


# --- TrajectoryTracker: trace-context / heuristic interaction ---------------


def test_two_distinct_trace_ids_produce_two_separate_trajectories():
    """The existing real-subprocess test only ever exercises ONE trace
    ID. Two genuinely different trace IDs in the same session must never
    be merged just because both are "trace context" trajectories."""
    tracker = _tracker()
    r1 = tracker.record_call(0, "trace-a", {}, {})
    r2 = tracker.record_call(1, "trace-b", {}, {})
    r3 = tracker.record_call(2, "trace-a", {}, {})
    assert r1.trajectory_id == r3.trajectory_id
    assert r1.trajectory_id != r2.trajectory_id
    assert tracker.trajectories_started == 2


def test_a_traced_call_interleaved_does_not_reset_the_heuristic_idle_timer():
    """record_call's trace-context branch never touches
    `self._current_heuristic` at all -- confirmed directly: a heuristic
    trajectory's continuation decision must depend only on ITS OWN last
    heuristic touch, never on an intervening trace-tagged call's
    timing."""
    tracker = _tracker()
    r1 = tracker.record_call(0, None, {"a": 1}, {})  # heuristic trajectory #1
    tracker.record_call(1, "some-trace", {}, {})  # a traced call, unrelated
    r3 = tracker.record_call(2, None, {"b": 2}, {})  # heuristic again, immediately after
    assert r1.trajectory_id == r3.trajectory_id  # continues the same heuristic trajectory
    assert tracker.trajectories_started == 2  # one heuristic + one trace, not three


def test_heuristic_and_trace_context_calls_never_share_a_trajectory_id():
    tracker = _tracker()
    heuristic = tracker.record_call(0, None, {}, {})
    traced = tracker.record_call(1, "some-trace", {}, {})
    assert heuristic.trajectory_id != traced.trajectory_id


def test_close_all_clears_state_and_a_second_call_returns_nothing_new():
    tracker = _tracker()
    tracker.record_call(0, "trace-a", {}, {})
    tracker.record_call(1, None, {}, {})
    first_close = tracker.close_all()
    assert len(first_close) == 2  # one trace trajectory + one heuristic trajectory
    second_close = tracker.close_all()
    assert second_close == []


# --- F-08 data-flow references: JSON-string unwrapping, "last writer wins" -


def test_a_json_object_wrapped_as_a_text_string_result_is_still_matched():
    """MCPServer's own behavior for an undeclared-schema dict/list return:
    content: [{"type": "text", "text": "<json>"}] -- without unwrapping,
    F-08 would only ever see one opaque string leaf and could never match
    a value nested inside it. Confirmed directly, not just via the
    fixture server's own already-structured echo tool."""
    tracker = _tracker()
    wrapped_result = {"content": [{"type": "text", "text": '{"customer_id": "cust_42"}'}]}
    tracker.record_call(0, None, {}, wrapped_result)
    outcome = tracker.record_call(1, None, {"id": "cust_42"}, {})
    assert len(outcome.references) == 1
    assert outcome.references[0].source_seq == 0
    assert outcome.references[0].target_path == "$.id"


def test_a_plain_non_json_looking_string_result_is_treated_as_an_opaque_leaf():
    """The inverse case: a string that does NOT look like JSON (doesn't
    start with { or [) must be treated as a scalar leaf value itself,
    not silently dropped by a failed parse attempt."""
    tracker = _tracker()
    tracker.record_call(0, None, {}, {"content": [{"type": "text", "text": "just a plain sentence"}]})
    outcome = tracker.record_call(1, None, {"note": "just a plain sentence"}, {})
    assert len(outcome.references) == 1
    assert outcome.references[0].target_path == "$.note"


def test_malformed_json_looking_string_does_not_crash_and_is_treated_as_opaque():
    """A string that STARTS like JSON but fails to parse (truncated,
    invalid) must not raise -- _try_parse_json_object_or_array's own
    documented fallback."""
    tracker = _tracker()
    tracker.record_call(0, None, {}, {"content": [{"type": "text", "text": '{"broken": '}]})
    outcome = tracker.record_call(1, None, {"note": '{"broken": '}, {})
    assert len(outcome.references) == 1  # matched as the literal, unparsed string itself


def test_last_writer_wins_on_a_repeated_value_across_two_prior_calls():
    """Two DIFFERENT prior calls producing the identical value -- a later
    reference must point at the MOST RECENT one, not the first, per
    Trajectory.index_result's own documented tie-breaking rule."""
    tracker = _tracker()
    tracker.record_call(0, None, {}, {"id": "cust_42"})
    tracker.record_call(1, None, {}, {"id": "cust_42"})  # same value, later call
    outcome = tracker.record_call(2, None, {"customer": "cust_42"}, {})
    assert len(outcome.references) == 1
    assert outcome.references[0].source_seq == 1  # the more recent producer, not seq 0


def test_a_falsy_small_value_can_produce_a_spurious_reference_as_documented():
    """The module's own documented, accepted tradeoff (literal equality
    matching, not similarity-based): common small/falsy values like 0 or
    "" can produce a spurious data-flow reference. Locked in as real,
    confirmed behavior, not silently relied upon without a test."""
    tracker = _tracker()
    tracker.record_call(0, None, {}, {"count": 0})
    outcome = tracker.record_call(1, None, {"unrelated_but_also_zero": 0}, {})
    assert len(outcome.references) == 1  # spurious, but exactly the documented tradeoff


def test_no_reference_when_no_value_actually_matches():
    tracker = _tracker()
    tracker.record_call(0, None, {}, {"id": "cust_1"})
    outcome = tracker.record_call(1, None, {"id": "cust_2"}, {})
    assert outcome.references == []
