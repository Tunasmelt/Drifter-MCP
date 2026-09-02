"""Tests for the baseline runner (F-21), docs/SPEC.md §7/§8.

Focus: the explicit design change from docs/PHASES.md's original sketch — a
null `tool_manifest_hash` must be surfaced, never silently treated as a
match. This directly tests the thing the skipped Gate 1 trial would have
told us the real-world rate of (see .drifter/GATE_STATUS's gate_1_note).
"""

import statistics
from pathlib import Path

import pytest

from evaluate.baseline import _NULL_HASH_REASON, ExcludedRun, aggregate_baseline_runs, run_baseline
from record.calibration import Calibration
from record.schema import Environment, SessionStart, ToolCall


def _write_session(
    dir_path: Path,
    session_id: str,
    tool_names: list[str],
    tool_manifest_hash: str | None,
) -> Path:
    """A minimal, current-schema session: SessionStart + one ToolCall per
    entry in tool_names, in order. Built from the real Pydantic models
    (not hand-written dicts) since this fixture targets the current
    schema, unlike the pre-migration fixtures elsewhere in this project
    that deliberately simulate an old, incomplete shape.
    """
    lines = [
        SessionStart(
            session_id=session_id,
            seq=0,
            started_at="2026-08-25T00:00:00Z",
            environment=Environment(tool_manifest_hash=tool_manifest_hash),
            raw_frame_offset=0,
        ).model_dump_json()
    ]
    for i, tool_name in enumerate(tool_names, start=1):
        lines.append(
            ToolCall(
                session_id=session_id,
                seq=i,
                timestamp="2026-08-25T00:00:01Z",
                server="fake",
                tool_name=tool_name,
                arguments={},
                result_shape={"type": "object", "keys": []},
                is_error=False,
                duration_ms=1.0,
                fault=False,
                raw_frame_offset=i * 100,
            ).model_dump_json()
        )
    path = dir_path / f"{session_id}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _write_session_with_faults(
    dir_path: Path,
    session_id: str,
    calls: list[tuple[str, bool]],
    tool_manifest_hash: str | None = "h",
) -> Path:
    """Like _write_session, but each entry is (tool_name, fault) so a
    fidelity-gating test can build a session with a mix of real hits
    (fault=False) and misses/faults (fault=True), matching exactly what
    replay_proxy.py actually records for a MISS or a replayed fault
    (result_shape=None, is_error left at schema default) rather than
    faking an is_error=True hit, which is a different, already-tested
    case.
    """
    lines = [
        SessionStart(
            session_id=session_id,
            seq=0,
            started_at="2026-08-25T00:00:00Z",
            environment=Environment(tool_manifest_hash=tool_manifest_hash),
            raw_frame_offset=0,
        ).model_dump_json()
    ]
    for i, (tool_name, fault) in enumerate(calls, start=1):
        lines.append(
            ToolCall(
                session_id=session_id,
                seq=i,
                timestamp="2026-08-25T00:00:01Z",
                server="fake",
                tool_name=tool_name,
                arguments={},
                result_shape=None if fault else {"type": "object", "keys": []},
                is_error=None if fault else False,
                duration_ms=1.0,
                fault=fault,
                raw_frame_offset=i * 100,
            ).model_dump_json()
        )
    path = dir_path / f"{session_id}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _write_session_with_provenance(
    dir_path: Path,
    session_id: str,
    calls: list[tuple[str, str]],
    tool_manifest_hash: str | None = "h",
) -> Path:
    """Like _write_session_with_faults, but each entry is (tool_name,
    result_provenance) -- "real" or "synthetic" -- for testing
    F-17/F-14's fidelity-denominator exclusion. A synthetic call is
    fault=False (it genuinely resolved, no protocol error), matching
    exactly what replay_proxy.py's tool_addition support records."""
    lines = [
        SessionStart(
            session_id=session_id,
            seq=0,
            started_at="2026-08-25T00:00:00Z",
            environment=Environment(tool_manifest_hash=tool_manifest_hash),
            raw_frame_offset=0,
        ).model_dump_json()
    ]
    for i, (tool_name, provenance) in enumerate(calls, start=1):
        lines.append(
            ToolCall(
                session_id=session_id,
                seq=i,
                timestamp="2026-08-25T00:00:01Z",
                server="fake",
                tool_name=tool_name,
                arguments={},
                result_shape={"type": "object", "keys": []},
                is_error=False,
                duration_ms=1.0,
                fault=False,
                result_provenance=provenance,
                raw_frame_offset=i * 100,
            ).model_dump_json()
        )
    path = dir_path / f"{session_id}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _runner(dir_path: Path, plan: list[tuple[str, list[str], str | None]]):
    """Returns a zero-arg run_once callable that yields each (session_id,
    tool_names, tool_manifest_hash) entry in plan, in order, one per call."""
    it = iter(plan)

    def run_once() -> Path:
        session_id, tool_names, hash_ = next(it)
        return _write_session(dir_path, session_id, tool_names, hash_)

    return run_once


# --- the explicit design change: null tool_manifest_hash is flagged ------


def test_one_null_hash_run_is_flagged_not_ignored(tmp_path):
    """3 runs share the same real path; a 4th has a null hash. The
    null-hash run must not crash the aggregation and must not be
    silently folded in as if its fingerprint matched — it must appear in
    excluded_runs and the fingerprint_warning, and must not affect
    dominant_path/variant_frequencies/natural_variation/baseline_spread.
    """
    plan = [
        ("sess1", ["list_directory", "read_file"], "sha256:aaa"),
        ("sess2", ["list_directory", "read_file"], "sha256:aaa"),
        ("sess3", ["list_directory", "read_file"], "sha256:aaa"),
        ("sess4_null", ["list_directory", "read_file"], None),  # same path, but unverifiable
    ]
    result = run_baseline("task_a", run_once=_runner(tmp_path, plan), repeats=4)

    assert result.total_runs == 4
    assert result.valid_runs == 3

    assert len(result.excluded_runs) == 1
    excluded = result.excluded_runs[0]
    assert isinstance(excluded, ExcludedRun)
    assert excluded.session_id == "sess4_null"
    assert excluded.reason == "tool_manifest_hash is null"

    assert result.fingerprint_warning is not None
    assert "1 of 4" in result.fingerprint_warning
    assert "sess4_null" in result.fingerprint_warning

    # The 3 valid runs all match; the null-hash run contributes nothing
    # to the stats below even though its own path also matched.
    assert result.dominant_path == ("list_directory", "read_file")
    assert result.variant_frequencies == {("list_directory", "read_file"): 3}
    assert result.natural_variation == 0.0
    assert result.baseline_spread == 0.0


def test_zero_null_hash_runs_has_no_warning(tmp_path):
    plan = [
        ("sess1", ["a"], "sha256:aaa"),
        ("sess2", ["a"], "sha256:aaa"),
    ]
    result = run_baseline("task_a", run_once=_runner(tmp_path, plan), repeats=2)
    assert result.excluded_runs == []
    assert result.fingerprint_warning is None
    assert result.valid_runs == 2


def test_all_runs_null_hash_does_not_crash_and_computes_nothing(tmp_path):
    """Zero valid runs is an edge case, not a crash: dominant_path etc.
    must come back None/empty, with every run accounted for in
    excluded_runs, rather than raising on an empty-sequence statistic.
    """
    plan = [
        ("sess1", ["a"], None),
        ("sess2", ["b"], None),
    ]
    result = run_baseline("task_a", run_once=_runner(tmp_path, plan), repeats=2)

    assert result.valid_runs == 0
    assert result.dominant_path is None
    assert result.variant_frequencies == {}
    assert result.natural_variation is None
    assert result.baseline_spread is None
    assert result.has_data is False
    assert len(result.excluded_runs) == 2
    assert result.fingerprint_warning is not None
    assert "2 of 2" in result.fingerprint_warning


def test_no_data_is_not_confusable_with_a_real_but_empty_or_zero_answer(tmp_path):
    """dominant_path can genuinely be () and natural_variation/
    baseline_spread can genuinely be 0.0 for a real, valid baseline (see
    test_empty_path_is_a_valid_variant and
    test_all_identical_paths_have_zero_natural_variation_and_spread) --
    every one of those is Python-falsy, identically to None. A caller
    checking truthiness instead of `is None`/valid_runs/has_data would
    silently conflate "no data at all" with "real answer, perfectly
    stable." This test puts both scenarios side by side and asserts
    they're distinguishable via has_data/valid_runs specifically, not
    merely by inspecting which value happens to come back.
    """
    no_data = run_baseline(
        "task_no_data", run_once=_runner(tmp_path, [("s1", ["a"], None)]), repeats=1
    )
    real_but_empty = run_baseline(
        "task_real_empty", run_once=_runner(tmp_path, [("s2", [], "h")]), repeats=1
    )

    # Both dominant_path values are falsy (None vs. ()) -- has_data is
    # the signal that actually distinguishes them, not truthiness.
    assert not no_data.dominant_path
    assert not real_but_empty.dominant_path
    assert no_data.has_data is False
    assert real_but_empty.has_data is True
    assert no_data.valid_runs == 0
    assert real_but_empty.valid_runs == 1

    # The identity checks that ARE reliable:
    assert no_data.dominant_path is None
    assert real_but_empty.dominant_path == ()
    assert real_but_empty.dominant_path is not None


# --- run_once itself failing (not a null-hash session) -------------------


def test_run_once_failure_is_excluded_not_fatal(tmp_path):
    """A repeat whose run_once() call raises (subprocess spawn error,
    unresolved timeout, anything) must be excluded like a null-hash run
    -- degrading valid_runs and getting flagged -- not propagate out of
    run_baseline and abort the whole run, discarding the repeats that
    already succeeded.
    """
    calls = {"n": 0}

    def run_once() -> Path:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("subprocess spawn failed")
        return _write_session(tmp_path, f"s{calls['n']}", ["a"], "h")

    result = run_baseline("task_flaky", run_once=run_once, repeats=3)

    assert result.has_data is True
    assert result.total_runs == 3
    assert result.valid_runs == 2
    assert calls["n"] == 3  # the failure on repeat 2 didn't stop repeat 3 from running

    assert len(result.excluded_runs) == 1
    excluded = result.excluded_runs[0]
    assert excluded.session_id is None  # no session was ever produced -- not a placeholder
    assert excluded.path is None
    assert "run_once raised" in excluded.reason
    assert "subprocess spawn failed" in excluded.reason

    assert result.fingerprint_warning is not None
    assert "1 of 3" in result.fingerprint_warning
    assert "run_once raised" in result.fingerprint_warning

    # The two successful repeats still aggregate normally.
    assert result.dominant_path == ("a",)
    assert result.variant_frequencies == {("a",): 2}


def test_run_once_failure_and_null_hash_exclusions_coexist_with_distinct_reasons(tmp_path):
    """Both exclusion reasons can appear in the same excluded_runs list,
    each keeping its own reason string -- never collapsed into one
    generic "excluded" bucket that would make sess_null and the raised
    repeat indistinguishable from each other.
    """
    plan = iter([("sess_null", ["a"], None)])

    def run_once() -> Path:
        try:
            session_id, tool_names, hash_ = next(plan)
        except StopIteration:
            raise RuntimeError("agent never started")
        return _write_session(tmp_path, session_id, tool_names, hash_)

    result = run_baseline("task_mixed", run_once=run_once, repeats=2)

    assert result.valid_runs == 0
    assert result.has_data is False
    assert len(result.excluded_runs) == 2

    by_reason = {r.reason: r for r in result.excluded_runs}
    assert _NULL_HASH_REASON in by_reason
    assert by_reason[_NULL_HASH_REASON].session_id == "sess_null"

    raised = next(r for r in result.excluded_runs if r.session_id is None)
    assert "agent never started" in raised.reason
    assert raised.reason != _NULL_HASH_REASON


# --- aggregate_baseline_runs: the pure, execution-free analysis core -----
# (F-36's prerequisite extraction -- what cli/score.py calls directly)


def test_aggregate_baseline_runs_computes_the_same_result_from_paths_alone(tmp_path):
    """No run_once anywhere -- session paths already exist on disk (as
    they would from a prior drifter observe / baseline run), and the
    exact same aggregation drifter score needs comes back."""
    paths = [
        _write_session(tmp_path, "s1", ["a", "b"], "h"),
        _write_session(tmp_path, "s2", ["a", "b"], "h"),
        _write_session(tmp_path, "s3", ["a", "b", "c"], "h"),
    ]
    result = aggregate_baseline_runs("task_from_disk", paths)

    assert result.has_data is True
    assert result.total_runs == 3
    assert result.valid_runs == 3
    assert result.dominant_path == ("a", "b")
    assert result.baseline_fidelity == 1.0


def test_unreadable_session_path_is_excluded_with_its_own_reason(tmp_path):
    """A path that exists but isn't a parseable session -- plausible for
    drifter score, reading whatever *.jsonl happens to sit in a
    directory, in a way run_baseline's own run_once-produced paths never
    needed to guard against. Must exclude, not crash the whole score."""
    good_path = _write_session(tmp_path, "s_good", ["a"], "h")
    bad_path = tmp_path / "not_a_session.jsonl"
    bad_path.write_text("{this is not valid json\n", encoding="utf-8")

    result = aggregate_baseline_runs("task_mixed", [good_path, bad_path])

    assert result.has_data is True
    assert result.valid_runs == 1
    assert result.total_runs == 2
    assert len(result.excluded_runs) == 1
    excluded = result.excluded_runs[0]
    assert excluded.session_id is None
    assert excluded.path == bad_path
    assert "session unreadable" in excluded.reason


def test_empty_session_file_gets_an_informative_reason_not_a_blank_one(tmp_path):
    """A zero-byte session file (a real, observed case -- claude mcp
    get/health-check connections through drifter observe leave these in
    a real corpus) makes `next(...)` raise a bare StopIteration with no
    message. str(exc) alone would be "" -- confirmed live while
    producing drifter score's exit-test evidence against .drifter/runs/.
    The reason string must never be blank."""
    empty_path = tmp_path / "empty_sess.jsonl"
    empty_path.write_text("", encoding="utf-8")

    result = aggregate_baseline_runs("task", [empty_path])

    assert len(result.excluded_runs) == 1
    reason = result.excluded_runs[0].reason
    assert reason != "session unreadable: "
    assert "session unreadable:" in reason
    assert len(reason) > len("session unreadable: ")


def test_run_baseline_and_aggregate_baseline_runs_agree_given_the_same_sessions(tmp_path):
    """run_baseline (execution) is now a thin wrapper around
    aggregate_baseline_runs (analysis) -- feeding aggregate_baseline_runs
    the exact paths run_baseline's run_once would have produced must
    give back an equivalent result, confirming the extraction didn't
    change behavior."""
    plan = [("s1", ["a", "b"], "h"), ("s2", ["a", "b"], "h"), ("s3", ["a"], "h")]
    via_run_baseline = run_baseline("task_equiv", run_once=_runner(tmp_path, plan), repeats=3)

    paths = [_write_session(tmp_path, f"{sid}_direct", tools, h) for sid, tools, h in plan]
    via_aggregate = aggregate_baseline_runs("task_equiv", paths)

    assert via_run_baseline.dominant_path == via_aggregate.dominant_path
    assert via_run_baseline.valid_runs == via_aggregate.valid_runs
    assert via_run_baseline.natural_variation == via_aggregate.natural_variation
    assert via_run_baseline.baseline_spread == via_aggregate.baseline_spread
    assert via_run_baseline.baseline_fidelity == via_aggregate.baseline_fidelity


# --- fidelity gating (docs/SPEC.md §7/§8, DEC-020) -----------------------------


def test_low_fidelity_run_is_excluded_with_its_own_reason(tmp_path):
    """A run whose fidelity falls below calibration.fidelity_floor is
    excluded the same way as a null-hash run, but with a distinct
    reason string -- 2 misses out of 4 calls (fidelity 0.5) must be
    caught by a floor of 0.7, and a clean run in the same batch must
    still contribute."""
    low_fidelity_calls = [("a", False), ("b", True), ("c", True), ("d", False)]
    low_fidelity_path = _write_session_with_faults(tmp_path, "sess_low", low_fidelity_calls)
    clean_path = _write_session_with_faults(tmp_path, "sess_clean", [("a", False), ("b", False)])
    paths = iter([low_fidelity_path, clean_path])

    calibration = Calibration()
    calibration.fidelity_floor = 0.7
    result = run_baseline("task_fidelity", run_once=lambda: next(paths), repeats=2, calibration=calibration)

    assert result.has_data is True
    assert result.valid_runs == 1
    assert result.dominant_path == ("a", "b")

    assert len(result.excluded_runs) == 1
    excluded = result.excluded_runs[0]
    assert excluded.session_id == "sess_low"
    assert excluded.path == low_fidelity_path
    assert "fidelity 0.50 below floor 0.70" == excluded.reason

    assert result.baseline_fidelity == 1.0  # only the clean run's fidelity is averaged in


def test_baseline_fidelity_of_zero_is_not_confused_with_no_data(tmp_path):
    """A run that clears the floor but still has a real, low fidelity
    (0.0 is allowed to pass a floor of 0.0) must report that real value,
    distinguishable from "never computed" via has_data -- same
    truthiness-trap check already applied to natural_variation/
    baseline_spread."""
    all_miss_path = _write_session_with_faults(tmp_path, "sess_all_miss", [("a", True), ("b", True)])
    paths = iter([all_miss_path])

    calibration = Calibration()
    calibration.fidelity_floor = 0.0  # everything clears a floor of 0.0
    result = run_baseline("task_zero_fidelity", run_once=lambda: next(paths), repeats=1, calibration=calibration)

    assert result.has_data is True
    assert result.valid_runs == 1
    assert result.excluded_runs == []
    assert result.baseline_fidelity == 0.0  # real, computed, not None
    assert result.baseline_fidelity is not None


def test_no_baseline_fidelity_computed_when_every_run_is_excluded(tmp_path):
    low_fidelity_path = _write_session_with_faults(tmp_path, "sess_low", [("a", True)])
    paths = iter([low_fidelity_path])

    result = run_baseline("task_all_excluded", run_once=lambda: next(paths), repeats=1)

    assert result.has_data is False
    assert result.baseline_fidelity is None
    assert len(result.excluded_runs) == 1
    assert "fidelity 0.00 below floor" in result.excluded_runs[0].reason


def test_zero_call_run_has_fidelity_one_and_is_never_fidelity_excluded(tmp_path):
    """An agent that calls no tools is a legitimate baseline path (see
    test_empty_path_is_a_valid_variant) -- it must never be excluded on
    fidelity-floor grounds specifically, since there's nothing unfaithful
    about calling nothing."""
    empty_path = _write_session_with_faults(tmp_path, "sess_empty", [])
    paths = iter([empty_path])

    calibration = Calibration()
    calibration.fidelity_floor = 0.99  # a near-maximal floor
    result = run_baseline("task_empty", run_once=lambda: next(paths), repeats=1, calibration=calibration)

    assert result.has_data is True
    assert result.excluded_runs == []
    assert result.baseline_fidelity == 1.0
    assert result.dominant_path == ()


def test_a_connectivity_check_artifact_is_indistinguishable_from_a_real_zero_call_run_and_silently_contaminates_the_aggregate(tmp_path):
    """A confirmed, real gap (not a hypothetical) -- found while checking
    a real dogfood baseline corpus before trusting its aggregate numbers.
    `claude mcp get`'s own connectivity check reaches far enough into
    replay_proxy.py's eager bootstrap to populate tool_manifest_hash (a
    real tools/list happens) but never calls a tool -- the identical
    on-disk shape as test_zero_call_run_has_fidelity_one_and_is_never_
    fidelity_excluded's GENUINE "agent legitimately called nothing"
    case. aggregate_baseline_runs has no signal in the recorded data to
    tell these apart, so a plumbing artifact gets counted as a real,
    valid empty-path variant -- no crash, no exclusion, a silent
    contamination of natural_variation/baseline_spread as if a real run
    had genuinely deviated. This test locks in and documents that this
    is real and does not crash; it does NOT assert this is correct
    behavior -- distinguishing the two cases is a real, undecided design
    question (what signal would even tell them apart?), not something
    to fix reflexively as a side effect of this test.
    """
    real_path = ["list_directory"]
    plan = [
        ("sess_real_1", real_path, "h"),
        ("sess_real_2", real_path, "h"),
        ("sess_real_3", real_path, "h"),
        ("sess_connectivity_check_artifact", [], "h"),  # zero calls, hash populated -- the exact shape found
    ]
    result = run_baseline("task_mixed_corpus", run_once=_runner(tmp_path, plan), repeats=4)

    assert result.has_data is True
    assert result.excluded_runs == []  # confirmed: NOT excluded -- no exclusion reason fires for this shape
    assert result.valid_runs == 4  # confirmed: silently counted as a 4th valid run
    assert result.variant_frequencies == {("list_directory",): 3, (): 1}
    assert result.dominant_path == ("list_directory",)  # the 3 real runs still win, but only because 3 > 1
    # The contaminating artifact inflates natural_variation as if a real
    # run had deviated from the dominant path -- it did not; it never
    # attempted the task at all.
    assert result.natural_variation == pytest.approx(0.25)


def test_synthetic_calls_excluded_from_fidelity_denominator_not_counted_as_hits_or_misses(tmp_path):
    """F-17's own done-when (docs/FEATURES.md): tool_addition calls are
    correctly excluded from fidelity accounting -- docs/SPEC.md §7's text is
    explicit that they're neither a hit nor a miss for this purpose. A
    session with 1 real hit + 1 synthetic call must report fidelity
    1.0 (computed over the 1 real call alone), not 0.5 (which is what
    counting the synthetic call as a miss would produce) and not
    excluded from the run entirely (the run itself is still valid --
    only the synthetic CALL is excluded from the ratio)."""
    path = _write_session_with_provenance(tmp_path, "sess_mixed", [("real_tool", "real"), ("added_tool", "synthetic")])
    paths = iter([path])

    result = run_baseline("task_synthetic", run_once=lambda: next(paths), repeats=1)

    assert result.has_data is True
    assert result.valid_runs == 1
    assert result.excluded_runs == []  # the RUN isn't excluded -- only the call is excluded from fidelity
    assert result.baseline_fidelity == 1.0  # 1/1 real calls hit -- the synthetic call isn't in the denominator
    # dominant_path still reflects the FULL tool-call sequence, including
    # the synthetic call -- fidelity exclusion is not path exclusion.
    assert result.dominant_path == ("real_tool", "added_tool")


def test_a_session_of_only_synthetic_calls_has_vacuous_fidelity_one(tmp_path):
    path = _write_session_with_provenance(tmp_path, "sess_all_synthetic", [("added_tool", "synthetic")])
    paths = iter([path])

    result = run_baseline("task_all_synthetic", run_once=lambda: next(paths), repeats=1)

    assert result.has_data is True
    assert result.baseline_fidelity == 1.0
    assert result.excluded_runs == []


def test_fidelity_and_null_hash_exclusions_coexist_with_distinct_reasons(tmp_path):
    null_hash_path = _write_session_with_faults(tmp_path, "sess_null", [("a", False)], tool_manifest_hash=None)
    low_fidelity_path = _write_session_with_faults(tmp_path, "sess_low", [("a", True)])
    paths = iter([null_hash_path, low_fidelity_path])

    result = run_baseline("task_mixed_reasons", run_once=lambda: next(paths), repeats=2)

    assert result.has_data is False
    assert len(result.excluded_runs) == 2
    reasons = {r.reason for r in result.excluded_runs}
    assert _NULL_HASH_REASON in reasons
    assert any(r.startswith("fidelity") for r in reasons)
    assert len(reasons) == 2  # genuinely distinct strings, not collapsed


# --- dominant path / variant frequency / natural_variation / spread ------


def test_dominant_path_and_variant_frequencies_with_a_real_split(tmp_path):
    plan = [
        ("s1", ["search", "get_customer"], "h"),
        ("s2", ["search", "get_customer"], "h"),
        ("s3", ["search", "get_customer"], "h"),
        ("s4", ["search", "get_customer", "retry"], "h"),  # the one variant
    ]
    result = run_baseline("task_b", run_once=_runner(tmp_path, plan), repeats=4)

    assert result.valid_runs == 4
    assert result.dominant_path == ("search", "get_customer")
    assert result.variant_frequencies == {
        ("search", "get_customer"): 3,
        ("search", "get_customer", "retry"): 1,
    }
    # 1 of 4 valid runs deviates from the dominant path.
    assert result.natural_variation == pytest.approx(0.25)
    assert result.baseline_spread == pytest.approx(statistics.pstdev([0, 0, 0, 1]))


def test_all_identical_paths_have_zero_natural_variation_and_spread(tmp_path):
    plan = [("s1", ["x"], "h"), ("s2", ["x"], "h"), ("s3", ["x"], "h")]
    result = run_baseline("task_c", run_once=_runner(tmp_path, plan), repeats=3)
    assert result.natural_variation == 0.0
    assert result.baseline_spread == 0.0
    # 0.0 is falsy, same as None -- has_data is what actually confirms
    # this is a real, valid answer and not the no-data case.
    assert result.has_data is True
    assert result.valid_runs == 3


def test_empty_path_is_a_valid_variant(tmp_path):
    """A run that called no tools at all is still a legitimate path (the
    empty tuple), not a special case that needs separate handling."""
    plan = [("s1", [], "h"), ("s2", [], "h")]
    result = run_baseline("task_d", run_once=_runner(tmp_path, plan), repeats=2)
    assert result.dominant_path == ()
    assert result.variant_frequencies == {(): 2}
    # () is falsy, same as None -- has_data is what actually confirms
    # this is a real, valid answer and not the no-data case.
    assert result.has_data is True
    assert result.valid_runs == 2


# --- repeats / calibration wiring -----------------------------------------


def test_repeats_defaults_from_calibration(tmp_path):
    calls = {"n": 0}

    def run_once() -> Path:
        calls["n"] += 1
        return _write_session(tmp_path, f"s{calls['n']}", ["a"], "h")

    calibration = Calibration()
    calibration.baseline.repeats = 3
    result = run_baseline("task_e", run_once=run_once, calibration=calibration)
    assert calls["n"] == 3
    assert result.total_runs == 3


def test_explicit_repeats_overrides_calibration(tmp_path):
    calls = {"n": 0}

    def run_once() -> Path:
        calls["n"] += 1
        return _write_session(tmp_path, f"s{calls['n']}", ["a"], "h")

    calibration = Calibration()
    calibration.baseline.repeats = 10
    result = run_baseline("task_f", run_once=run_once, repeats=2, calibration=calibration)
    assert calls["n"] == 2
    assert result.total_runs == 2
