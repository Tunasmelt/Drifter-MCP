"""F-14: a synthesized miss counts as a MISS, never as a hit and never
as an exclusion.

This is the single most important test guarding F-14, because the bug
it prevents is silent and produces a confident wrong answer rather than
a crash.

`_run_fidelity` excludes calls whose `result_provenance` is
`"synthetic"` from the denominator entirely. That is correct for F-17's
`tool_addition`: a mutation-injected tool has no prior recording by
definition, so counting it either way would misrepresent what fraction
of genuinely-checkable calls matched (docs/SPEC.md §7).

F-14's general synthesis is the opposite case -- a recording could have
existed for this call and did not. If it reused `"synthetic"`, then a
run that missed EVERY call would have every call excluded, hit
`_run_fidelity`'s vacuous-1.0 empty path, clear the 0.70 floor, and
feed a confident verdict built on zero matched evidence. That is
precisely the defect DEC-027's minimum-evidence gate closed, re-entered
through a different door: not by inflating the numerator this time, but
by emptying the denominator.

There is a second, subtler trap. A synthesized miss returns a real
result on the wire rather than a JSON-RPC error, so it is recorded with
`fault=False` -- and `fault is False` is `_run_fidelity`'s "confirmed
hit" signal. Without an explicit provenance guard, a synthesized miss
would therefore be counted as a full-weight HIT, which is worse than
the exclusion bug: it would drive fidelity UP in exact proportion to
how badly replay was failing.
"""

from __future__ import annotations

from mcp_drifter.evaluate.baseline import _provenance_counts, _run_fidelity
from mcp_drifter.record.schema import ToolCall


def _call(tool_name: str, *, provenance: str = "real", fault: bool | None = False, match_tier: str | None = "exact") -> ToolCall:
    return ToolCall(
        session_id="s",
        seq=1,
        timestamp="2026-01-01T00:00:00Z",
        server="fake",
        tool_name=tool_name,
        arguments={},
        result_shape={"type": "object", "keys": []},
        is_error=False,
        duration_ms=1.0,
        fault=fault,
        match_tier=match_tier,
        result_provenance=provenance,
        raw_frame_offset=0,
    )


def test_a_synthesized_miss_is_not_counted_as_a_hit_despite_fault_being_false():
    """The trap: it answered on the wire, so fault=False. It still did
    not match a recording.
    """
    records = [
        _call("a"),
        _call("b", provenance="synthetic_miss", match_tier=None),
    ]

    # One real hit out of two checkable calls.
    assert _run_fidelity(records) == 0.5


def test_a_run_that_synthesized_every_miss_has_fidelity_zero_not_one():
    """The headline case. All misses, so fidelity must be 0.0 and the
    run must fall below the floor -- not 1.0 via the vacuous empty path.
    """
    records = [_call(name, provenance="synthetic_miss", match_tier=None) for name in ("a", "b", "c")]

    assert _run_fidelity(records) == 0.0


def test_tool_addition_synthesis_is_still_excluded_unlike_a_synthesized_miss():
    """The distinction this whole design rests on, asserted directly:
    the two provenances must NOT behave the same way.
    """
    with_addition = [_call("a"), _call("injected", provenance="synthetic", match_tier=None)]
    with_miss = [_call("a"), _call("missed", provenance="synthetic_miss", match_tier=None)]

    # tool_addition: excluded from the denominator -> the one real hit is
    # the whole population.
    assert _run_fidelity(with_addition) == 1.0
    # synthesized miss: counted -> half.
    assert _run_fidelity(with_miss) == 0.5


def test_synthesized_misses_are_reported_in_their_own_provenance_bucket():
    """Reported separately rather than folded into `unresolved`: an
    operator needs to see that replay answered with a fabricated shape,
    which is a different operational fact from a call that hard-failed.
    """
    records = [
        _call("a"),
        _call("b", provenance="synthetic_miss", match_tier=None),
        _call("c", provenance="synthetic_miss", match_tier=None),
    ]

    counts = _provenance_counts(records)

    assert counts["exact"] == 1
    assert counts["synthetic_miss"] == 2
    assert counts["synthetic"] == 0
    assert counts["unresolved"] == 0
