"""The coverage curve -- the analysis half of the limitation-16 experiment.

`estimate_coverage` answers "how good is this corpus?" for ONE corpus. The
question limitation 16 actually turns on is different, and is about the
DERIVATIVE: as more sessions of the same task are recorded, does projected
coverage climb toward the 0.70 fidelity floor, or does it plateau below it?

Those two outcomes mean opposite things for the project. Climbing means
corpus-based replay works and the only remaining cost is recording effort. A
plateau means exact/semantic-tier replay cannot be made adequate for an
exploratory agent by ANY amount of recording, and DEC-027's premise -- that
replay adequacy is a property of the corpus -- is true but unreachable in
practice.

So the curve's job is not to produce a number, it is to tell those two
shapes apart honestly. That drives three design choices asserted below:

  - Subsets are SAMPLED, not taken as prefixes. Using the first n sessions
    would make the curve an artifact of recording order.
  - Sampling is seeded, so a reported curve is reproducible and two runs of
    the experiment are comparable.
  - Every point carries a spread, not just a mean. At small n the variance
    between subsets is large, and a bare mean would imply a precision the
    measurement does not have.
"""

from __future__ import annotations

from pathlib import Path

from mcp_drifter.record.schema import Environment, SessionStart, ToolCall, ToolDescriptor, ToolsList
from mcp_drifter.replay.coverage_curve import coverage_curve, render_curve


def _write_session(dir_path: Path, session_id: str, calls: list[tuple[str, dict]], server: str = "srv") -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    names = sorted({name for name, _ in calls}) or ["t"]
    served = [ToolDescriptor(name=n, description="d", input_schema={}) for n in names]
    lines = [
        SessionStart(
            session_id=session_id,
            seq=0,
            started_at="2026-08-25T00:00:00Z",
            environment=Environment(tool_manifest_hash="h"),
            raw_frame_offset=0,
        ).model_dump_json(),
        ToolsList(
            session_id=session_id,
            seq=1,
            timestamp="2026-08-25T00:00:00Z",
            server=server,
            tools_raw=served,
            tools_served=served,
            raw_frame_offset=1,
        ).model_dump_json(),
    ]
    for i, (tool_name, arguments) in enumerate(calls, start=2):
        lines.append(
            ToolCall(
                session_id=session_id,
                seq=i,
                timestamp="2026-08-25T00:00:01Z",
                server=server,
                tool_name=tool_name,
                arguments=arguments,
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


def _overlapping_corpus(tmp_path: Path, n: int) -> list[Path]:
    """Session i visits `{i, i+1}` -- adjacent sessions share exactly one
    call. Partial overlap is the whole point: a corpus where every session
    is identical is trivially 100% at every size, and one where none
    overlap is 0% at every size. Neither has a curve at all. This exact
    shape is the one an earlier version of the coverage tests got wrong,
    producing a dead-flat line that briefly implicated the estimator
    rather than the test's own model of reality.
    """
    return [_write_session(tmp_path, f"s{i}", [("t", {"i": i}), ("t", {"i": i + 1})]) for i in range(n)]


class TestCurveShape:
    def test_coverage_rises_with_corpus_size_on_a_partially_overlapping_corpus(self, tmp_path):
        curve = coverage_curve(_overlapping_corpus(tmp_path, 6), "srv", samples_per_size=8, seed=1)

        rates = [p.mean_coverage for p in curve.points]
        assert len(rates) >= 3
        # Monotone non-decreasing, and genuinely increasing overall.
        assert all(b >= a - 1e-9 for a, b in zip(rates, rates[1:]))
        assert rates[-1] > rates[0]

    def test_a_corpus_of_identical_sessions_is_flat_at_full_coverage(self, tmp_path):
        """The degenerate best case. A flat line here is CORRECT, and the
        curve must not manufacture a slope out of it."""
        paths = [_write_session(tmp_path, f"s{i}", [("t", {"a": 1})]) for i in range(5)]

        curve = coverage_curve(paths, "srv", samples_per_size=4, seed=1)

        assert all(p.mean_coverage == 1.0 for p in curve.points)

    def test_a_corpus_with_no_overlap_at_all_is_flat_at_zero(self, tmp_path):
        """The degenerate worst case, and the shape a genuinely
        unenumerable agent would produce. Must read as a real, flat 0.0,
        never as un-estimable."""
        paths = [_write_session(tmp_path, f"s{i}", [("t", {"i": i})]) for i in range(5)]

        curve = coverage_curve(paths, "srv", samples_per_size=4, seed=1)

        assert all(p.mean_coverage == 0.0 for p in curve.points)


class TestReproducibilityAndHonesty:
    def test_the_same_seed_produces_an_identical_curve(self, tmp_path):
        paths = _overlapping_corpus(tmp_path, 6)

        first = coverage_curve(paths, "srv", samples_per_size=5, seed=7)
        second = coverage_curve(paths, "srv", samples_per_size=5, seed=7)

        assert [p.mean_coverage for p in first.points] == [p.mean_coverage for p in second.points]

    def test_subsets_are_sampled_not_taken_in_recording_order(self, tmp_path):
        """If the curve used the first n sessions, reversing the input
        order would change the answer. It must not.
        """
        paths = _overlapping_corpus(tmp_path, 6)

        forward = coverage_curve(paths, "srv", samples_per_size=40, seed=3)
        backward = coverage_curve(list(reversed(paths)), "srv", samples_per_size=40, seed=3)

        for a, b in zip(forward.points, backward.points):
            assert a.mean_coverage == b.mean_coverage

    def test_each_point_reports_its_spread_and_sample_count(self, tmp_path):
        curve = coverage_curve(_overlapping_corpus(tmp_path, 6), "srv", samples_per_size=6, seed=1)

        for point in curve.points:
            assert point.min_coverage <= point.mean_coverage <= point.max_coverage
            assert point.subsets_sampled >= 1

    def test_the_curve_starts_at_two_sessions_since_one_cannot_be_cross_validated(self, tmp_path):
        curve = coverage_curve(_overlapping_corpus(tmp_path, 5), "srv", samples_per_size=3, seed=1)

        assert curve.points[0].corpus_size == 2
        assert curve.points[-1].corpus_size == 5


class TestPlateauDetection:
    """The question the experiment exists to answer, asked directly."""

    def test_a_flat_curve_below_the_floor_is_reported_as_a_plateau(self, tmp_path):
        paths = [_write_session(tmp_path, f"s{i}", [("t", {"i": i})]) for i in range(6)]

        curve = coverage_curve(paths, "srv", samples_per_size=4, seed=1)

        assert curve.plateaued(floor=0.70) is True
        assert curve.reaches(floor=0.70) is False

    def test_a_curve_that_reaches_the_floor_is_not_a_plateau(self, tmp_path):
        paths = [_write_session(tmp_path, f"s{i}", [("t", {"a": 1})]) for i in range(5)]

        curve = coverage_curve(paths, "srv", samples_per_size=4, seed=1)

        assert curve.reaches(floor=0.70) is True
        assert curve.plateaued(floor=0.70) is False

    def test_marginal_gain_is_reported_per_added_session(self, tmp_path):
        curve = coverage_curve(_overlapping_corpus(tmp_path, 6), "srv", samples_per_size=8, seed=1)

        assert curve.points[0].marginal_gain is None  # nothing to compare the first point to
        assert all(p.marginal_gain is not None for p in curve.points[1:])


def test_render_states_the_verdict_and_never_prints_a_bare_number(tmp_path):
    """A cold reader must be able to act on this. limitation 16's whole
    lesson is that a confident-looking number with no stated foundation is
    worse than no number at all.
    """
    curve = coverage_curve(_overlapping_corpus(tmp_path, 6), "srv", samples_per_size=6, seed=1)

    out = render_curve(curve, floor=0.70)

    assert "corpus size" in out.lower()
    assert "0.70" in out or "70%" in out
    # The spread and the sample count are both visible, not just the mean.
    assert "seed" in out.lower()
    assert any(word in out.lower() for word in ("plateau", "reaches", "projected"))


# --- defects found by running the curve against the project's own corpus ---
#
# The first real run was against `.drifter/runs/` (108 sessions) for server
# `filesystem`. It printed ~96 rows of apparently-precise percentages, and
# declared a PLATEAU. All three of those were wrong in a way the output did
# not show, which is exactly the failure shape limitation 16 documents.


def test_sessions_carrying_no_calls_for_the_server_are_excluded_from_corpus_size(tmp_path):
    """The corpus that matters is the one holding calls for THIS server.

    Against the real corpus, 104 of 108 sessions were other servers or
    scripted runs with no filesystem calls. Counting them as corpus size
    meant most sampled subsets contained no relevant calls at all, were
    dropped as un-estimable, and the surviving points were computed from
    one or two subsets while printed to a tenth of a percent. Worse, it
    answered the wrong question: "how many recordings of THIS task do I
    need" cannot be answered by counting recordings of a different one.
    """
    _write_session(tmp_path, "a", [("t", {"i": 1})], server="srv")
    _write_session(tmp_path, "b", [("t", {"i": 2})], server="srv")
    _write_session(tmp_path, "c", [("t", {"i": 3})], server="srv")
    _write_session(tmp_path, "other1", [("t", {"i": 1})], server="different")
    _write_session(tmp_path, "other2", [("t", {"i": 2})], server="different")

    curve = coverage_curve(sorted(tmp_path.glob("*.jsonl")), "srv", samples_per_size=4, seed=1)

    assert curve.sessions_available == 5
    assert curve.contributing_sessions == 3
    assert curve.points[-1].corpus_size == 3


def test_the_render_states_how_many_sessions_were_dropped_as_irrelevant(tmp_path):
    _write_session(tmp_path, "a", [("t", {"i": 1})], server="srv")
    _write_session(tmp_path, "b", [("t", {"i": 2})], server="srv")
    _write_session(tmp_path, "other", [("t", {"i": 1})], server="different")

    out = render_curve(coverage_curve(sorted(tmp_path.glob("*.jsonl")), "srv", samples_per_size=4, seed=1), floor=0.70)

    assert "1" in out and "no calls" in out.lower()


def test_the_curve_reports_the_call_count_underpinning_it(tmp_path):
    """8 calls is not a corpus. A reader must see the sample size, not
    just the percentage computed from it.
    """
    curve = coverage_curve(_overlapping_corpus(tmp_path, 4), "srv", samples_per_size=4, seed=1)

    assert curve.total_calls == 8
    assert "8" in render_curve(curve, floor=0.70)


def test_the_exhaustive_final_point_cannot_by_itself_trigger_a_plateau(tmp_path):
    """At corpus_size == N there is exactly one possible subset, so its
    "gain" compares a sampled mean against a single deterministic value
    and is near-zero by construction. Letting that count as evidence of
    flatness made the real run declare PLATEAU partly from an artifact.
    """
    # A corpus still climbing, whose only flat-looking points are the
    # exhaustive tail.
    paths = _overlapping_corpus(tmp_path, 5)
    curve = coverage_curve(paths, "srv", samples_per_size=12, seed=1)

    # The final point is exhaustive: one subset, zero spread.
    assert curve.points[-1].subsets_sampled == 1
    assert curve.points[-1].exhaustive is True
    # Earlier points are not.
    assert curve.points[0].exhaustive is False
