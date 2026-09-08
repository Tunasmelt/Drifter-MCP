"""Tests for projected replay coverage (DEC-027(c), docs/CHANGELOG.md).

The estimate that answers "what fraction of my agent's calls will actually
resolve" BEFORE real agent runs are spent — and, since it is leave-one-out
rather than in-sample, the first thing in this project able to measure
whether growing a corpus actually helps.
"""

from pathlib import Path

from mcp_drifter.record.schema import Environment, SessionStart, ToolCall, ToolDescriptor, ToolsList
from mcp_drifter.replay.coverage import estimate_coverage, render_coverage

GOLDEN_FIXTURE = Path(__file__).parent.parent / "fixtures" / "golden_v0.1.jsonl"
GOLDEN_SERVER = "filesystem"


def _write_session(dir_path: Path, session_id: str, calls: list[tuple[str, dict]], server: str = "srv") -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    names = sorted({name for name, _ in calls}) or ["t"]
    served = [ToolDescriptor(name=n, description="d", input_schema={}) for n in names]
    lines = [
        SessionStart(
            session_id=session_id, seq=0, started_at="2026-08-25T00:00:00Z",
            environment=Environment(tool_manifest_hash="h"), raw_frame_offset=0,
        ).model_dump_json(),
        ToolsList(
            session_id=session_id, seq=1, timestamp="2026-08-25T00:00:00Z", server=server,
            tools_raw=served, tools_served=served, raw_frame_offset=1,
        ).model_dump_json(),
    ]
    for i, (tool_name, arguments) in enumerate(calls, start=2):
        lines.append(
            ToolCall(
                session_id=session_id, seq=i, timestamp="2026-08-25T00:00:01Z", server=server,
                tool_name=tool_name, arguments=arguments, result_shape={"type": "object", "keys": []},
                is_error=False, duration_ms=1.0, fault=False, raw_frame_offset=i * 100,
            ).model_dump_json()
        )
    path = dir_path / f"{session_id}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# --- the estimate is a GENERALIZATION estimate, not self-congratulation ------


def test_identical_sessions_project_full_coverage(tmp_path):
    """Two sessions making the same calls: holding either out still leaves
    the other able to answer every call, so coverage is 100% -- the
    best case, and the one a deterministic agent would produce."""
    _write_session(tmp_path, "a", [("read_file", {"path": "/x"})])
    _write_session(tmp_path, "b", [("read_file", {"path": "/x"})])

    est = estimate_coverage(sorted(tmp_path.glob("*.jsonl")), "srv")

    assert est.estimable is True
    assert est.coverage == 1.0
    assert est.exact_hits == 2 and est.misses == 0


def test_wholly_disjoint_sessions_project_zero_coverage(tmp_path):
    """The opposite extreme, and the one limitation 16 describes: every
    session explores somewhere new, so holding any of them out leaves
    nothing able to answer it."""
    _write_session(tmp_path, "a", [("read_file", {"path": "/x"})])
    _write_session(tmp_path, "b", [("read_file", {"path": "/y"})])
    _write_session(tmp_path, "c", [("read_file", {"path": "/z"})])

    est = estimate_coverage(sorted(tmp_path.glob("*.jsonl")), "srv")

    assert est.coverage == 0.0
    assert est.misses == 3


def test_coverage_is_not_measured_in_sample(tmp_path):
    """The trap this whole module exists to avoid: resolving the corpus's
    own calls against a store built from that same corpus reports ~100% by
    construction. A single session's calls are all in the index, so an
    in-sample check would say 100% -- leave-one-out correctly says it can't
    generalize at all."""
    _write_session(tmp_path, "a", [("read_file", {"path": "/x"})])
    _write_session(tmp_path, "b", [("write_file", {"path": "/y"})])

    est = estimate_coverage(sorted(tmp_path.glob("*.jsonl")), "srv")

    assert est.coverage == 0.0  # NOT 1.0, which in-sample scoring would give


def test_a_semantic_only_match_is_counted_separately_from_exact(tmp_path):
    """Same tool, same VALUE, different parameter name -- resolves via the
    semantic tier, and must be reported as such rather than folded into
    exact, since the two carry different confidence (F-15)."""
    _write_session(tmp_path, "a", [("get_customer", {"customer_id": 42})])
    _write_session(tmp_path, "b", [("get_customer", {"customerId": 42})])

    est = estimate_coverage(sorted(tmp_path.glob("*.jsonl")), "srv")

    assert est.exact_hits == 0
    assert est.semantic_hits == 2
    assert est.coverage == 1.0
    assert est.exact_coverage == 0.0


# --- too small to estimate is stated, not faked ------------------------------


def test_a_single_session_corpus_is_not_estimable_rather_than_zero(tmp_path):
    """Leave-one-out on one session leaves a store of nothing, so the
    arithmetic answer is 0% -- but that is an artifact of the method, not a
    measurement of the corpus, and printing it as a rate would read as the
    latter."""
    _write_session(tmp_path, "a", [("read_file", {"path": "/x"})])

    est = estimate_coverage(sorted(tmp_path.glob("*.jsonl")), "srv")

    assert est.estimable is False
    assert est.coverage is None
    assert "at least 2" in est.reason
    assert "not estimable" in render_coverage(est)


def test_sessions_with_no_calls_are_not_counted_as_evidence(tmp_path):
    """An empty session is not evidence that a corpus generalizes, so it
    must not count toward the held-out sample size."""
    _write_session(tmp_path, "empty", [])
    _write_session(tmp_path, "real", [("read_file", {"path": "/x"})])

    est = estimate_coverage(sorted(tmp_path.glob("*.jsonl")), "srv")

    assert est.sessions == 1  # only the one carrying calls
    assert est.estimable is False


def test_calls_recorded_against_another_server_are_ignored(tmp_path):
    _write_session(tmp_path, "ours_a", [("read_file", {"path": "/x"})], server="srv")
    _write_session(tmp_path, "ours_b", [("read_file", {"path": "/x"})], server="srv")
    _write_session(tmp_path, "theirs", [("read_file", {"path": "/q"})], server="other")

    est = estimate_coverage(sorted(tmp_path.glob("*.jsonl")), "srv")

    assert est.sessions == 2
    assert est.total_calls == 2
    assert est.coverage == 1.0


# --- the actionable half -----------------------------------------------------


def test_per_tool_breakdown_names_the_worst_covered_tool_first(tmp_path):
    """Limitation 16's root cause was a specific tool the corpus had never
    recorded. Naming which tools miss is the one directly actionable output
    here -- a user can go record those."""
    _write_session(tmp_path, "a", [("common", {"p": 1}), ("rare", {"p": "x"})])
    _write_session(tmp_path, "b", [("common", {"p": 1}), ("rare", {"p": "y"})])

    est = estimate_coverage(sorted(tmp_path.glob("*.jsonl")), "srv")

    assert est.per_tool[0].tool_name == "rare"
    assert est.per_tool[0].coverage == 0.0
    assert est.per_tool[-1].tool_name == "common"
    assert est.per_tool[-1].coverage == 1.0
    assert "rare" in render_coverage(est)


def test_render_warns_when_projected_coverage_is_below_the_fidelity_floor(tmp_path):
    """The single most useful thing to know before spending: these runs are
    heading for exclusion."""
    _write_session(tmp_path, "a", [("read_file", {"path": "/x"})])
    _write_session(tmp_path, "b", [("read_file", {"path": "/y"})])

    est = estimate_coverage(sorted(tmp_path.glob("*.jsonl")), "srv")
    rendered = render_coverage(est, fidelity_floor=0.70)

    assert "WARNING" in rendered
    assert "EXCLUDED" in rendered


def test_no_warning_when_coverage_clears_the_floor(tmp_path):
    _write_session(tmp_path, "a", [("read_file", {"path": "/x"})])
    _write_session(tmp_path, "b", [("read_file", {"path": "/x"})])

    est = estimate_coverage(sorted(tmp_path.glob("*.jsonl")), "srv")
    assert "WARNING" not in render_coverage(est, fidelity_floor=0.70)


# --- the empirical question DEC-027 left open --------------------------------


def test_coverage_increases_as_the_corpus_grows(tmp_path):
    """DEC-027 stated plainly that whether corpus size actually improves the
    MISS rate was UNMEASURED, and that this feature exists to answer it
    rather than assume it. This is that answer in miniature, on data whose
    shape is known: a pool of repeated calls, sampled across a growing
    number of sessions, must project monotonically better coverage.
    """
    # Sessions PARTIALLY overlap while exploring a shared space of six
    # paths -- session i visits paths {i, i+1}. That shape matters: a first
    # draft used a fixed shared set plus a fixed per-session unique call,
    # which produces a dead-FLAT curve at any corpus size (the shared calls
    # always resolve, the unique ones never do) and taught nothing. Partial
    # overlap is what real exploration looks like, and is why the measured
    # curve over this repo's own corpus climbed 10% -> 27% rather than
    # sitting flat.
    pool = 6
    paths = [
        _write_session(tmp_path, f"s{i}", [("read_file", {"path": f"/p{i % pool}"}),
                                           ("read_file", {"path": f"/p{(i + 1) % pool}"})])
        for i in range(pool)
    ]

    coverages = [estimate_coverage(paths[:k], "srv").coverage for k in (2, 4, 6)]

    assert all(c is not None for c in coverages)
    assert coverages[0] < coverages[1] < coverages[2], coverages
    assert coverages[-1] == 1.0  # full overlap once every path is in two sessions


def test_the_golden_fixture_alone_cannot_be_estimated():
    """The real single-fixture case docs/SPEC.md §15 limitation 16 was
    measured against: one session, so nothing to hold out -- exactly the
    situation the estimate refuses to put a number on."""
    est = estimate_coverage([GOLDEN_FIXTURE], GOLDEN_SERVER)
    assert est.estimable is False
