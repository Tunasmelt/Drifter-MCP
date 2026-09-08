"""Tests for corpus resolution (DEC-027(b), docs/CHANGELOG.md).

The lever limitation 16's decision named: replay adequacy is a property of
the CORPUS, so `drifter run` indexes every recorded session it's pointed at
rather than a single `--fixture`. These cover what gets resolved, what
counts as contributing, and what gets reported about it.
"""

from pathlib import Path

import pytest

from mcp_drifter.record.schema import Environment, SessionStart, ToolCall, ToolDescriptor, ToolsList
from mcp_drifter.replay.corpus import CorpusError, load_corpus, render_corpus_summary, resolve_session_paths
from mcp_drifter.replay.replay_store import ReplayStore

GOLDEN_FIXTURE = Path(__file__).parent.parent / "fixtures" / "golden_v0.1.jsonl"
GOLDEN_SERVER = "filesystem"


def _write_session(
    dir_path: Path,
    session_id: str,
    calls: list[tuple[str, dict]],
    server: str = "srv",
    started_at: str = "2026-08-25T00:00:00Z",
    tool_names: list[str] | None = None,
) -> Path:
    """One session recording `calls` against `server`, with a tools/list
    manifest covering `tool_names` (defaulting to the tools actually
    called)."""
    dir_path.mkdir(parents=True, exist_ok=True)
    names = tool_names if tool_names is not None else sorted({name for name, _ in calls})
    served = [ToolDescriptor(name=n, description="d", input_schema={}) for n in names]
    lines = [
        SessionStart(
            session_id=session_id, seq=0, started_at=started_at,
            environment=Environment(tool_manifest_hash="h"), raw_frame_offset=0,
        ).model_dump_json(),
        ToolsList(
            session_id=session_id, seq=1, timestamp=started_at, server=server,
            tools_raw=served, tools_served=served, raw_frame_offset=1,
        ).model_dump_json(),
    ]
    for i, (tool_name, arguments) in enumerate(calls, start=2):
        lines.append(
            ToolCall(
                session_id=session_id, seq=i, timestamp=started_at, server=server,
                tool_name=tool_name, arguments=arguments,
                result_shape={"type": "object", "keys": []},
                is_error=False, duration_ms=1.0, fault=False, raw_frame_offset=i * 100,
            ).model_dump_json()
        )
    path = dir_path / f"{session_id}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# --- path resolution --------------------------------------------------------


def test_a_directory_resolves_to_every_session_inside_it(tmp_path):
    _write_session(tmp_path, "a", [("t", {})])
    _write_session(tmp_path, "b", [("t", {})])

    resolved = resolve_session_paths([tmp_path])
    assert [p.name for p in resolved] == ["a.jsonl", "b.jsonl"]


def test_directory_expansion_is_not_recursive_so_drifter_runs_own_arms_are_excluded(tmp_path):
    """The load-bearing reason this is non-recursive (see replay/corpus.py):
    `drifter run` writes its arms under <runs>/run/<task>/{baseline,mutated}/.
    Indexing a previous run's baseline arm would be circular, and indexing
    its MUTATED arm would silently make a deliberately-altered manifest look
    like the real server's."""
    _write_session(tmp_path, "observed", [("t", {})])
    _write_session(tmp_path / "run" / "task" / "mutated", "m0", [("t", {})])
    _write_session(tmp_path / "run" / "task" / "baseline", "b0", [("t", {})])

    resolved = resolve_session_paths([tmp_path])
    assert [p.name for p in resolved] == ["observed.jsonl"]


def test_files_and_directories_can_be_mixed(tmp_path):
    corpus_dir = tmp_path / "corpus"
    _write_session(corpus_dir, "a", [("t", {})])
    loose = _write_session(tmp_path / "elsewhere", "b", [("t", {})])

    resolved = resolve_session_paths([corpus_dir, loose])
    assert [p.name for p in resolved] == ["a.jsonl", "b.jsonl"]


def test_a_file_listed_twice_is_indexed_once(tmp_path):
    """Passing a directory AND a file inside it is a natural mistake; it
    must not double-index."""
    one = _write_session(tmp_path, "a", [("t", {})])
    resolved = resolve_session_paths([tmp_path, one])
    assert len(resolved) == 1


def test_a_nonexistent_path_is_an_actionable_error_not_an_empty_corpus(tmp_path):
    with pytest.raises(CorpusError, match="does not exist"):
        resolve_session_paths([tmp_path / "nope.jsonl"])


def test_an_empty_directory_is_an_actionable_error(tmp_path):
    """An empty store would present as "every call MISSed" and send the user
    hunting a fidelity problem that is really a wrong path."""
    (tmp_path / "empty").mkdir()
    with pytest.raises(CorpusError, match="corpus is empty"):
        resolve_session_paths([tmp_path / "empty"])


# --- what the corpus contains -----------------------------------------------


def test_load_corpus_counts_only_sessions_recorded_against_the_target_server(tmp_path):
    _write_session(tmp_path, "ours1", [("t", {"a": 1})], server="srv")
    _write_session(tmp_path, "ours2", [("t", {"a": 2})], server="srv")
    _write_session(tmp_path, "theirs", [("t", {"a": 3})], server="other")

    corpus = load_corpus([tmp_path], "srv")

    assert corpus.session_count == 3  # all resolved -- harmless to index
    assert corpus.contributing_count == 2  # but only two are ours
    assert corpus.indexed_calls == 2


def test_load_corpus_serves_the_manifest_from_the_most_recently_started_session(tmp_path):
    _write_session(tmp_path, "older", [("t", {})], started_at="2026-01-01T00:00:00Z", tool_names=["old_tool"])
    newer = _write_session(tmp_path, "newer", [("t", {})], started_at="2026-06-01T00:00:00Z", tool_names=["new_tool"])

    corpus = load_corpus([tmp_path], "srv")

    assert corpus.manifest_source == newer
    assert [t.name for t in corpus.tools_served] == ["new_tool"]


def test_disagreeing_manifests_are_surfaced_not_silently_resolved(tmp_path):
    """More than one manifest across a corpus means the interface changed
    part-way through the user's own recordings -- real drift, worth saying
    out loud rather than quietly picking the newest."""
    _write_session(tmp_path, "a", [("t", {})], tool_names=["one"])
    _write_session(tmp_path, "b", [("t", {})], tool_names=["one", "two"])

    corpus = load_corpus([tmp_path], "srv")

    assert corpus.distinct_manifests == 2
    assert corpus.manifests_disagree is True
    assert "different tool manifests" in render_corpus_summary(corpus, "srv")


def test_identical_manifests_across_sessions_do_not_report_disagreement(tmp_path):
    _write_session(tmp_path, "a", [("t", {})], tool_names=["one"])
    _write_session(tmp_path, "b", [("t", {})], tool_names=["one"])

    corpus = load_corpus([tmp_path], "srv")

    assert corpus.distinct_manifests == 1
    assert corpus.manifests_disagree is False
    assert "different tool manifests" not in render_corpus_summary(corpus, "srv")


def test_a_corpus_with_no_sessions_for_this_server_warns_loudly(tmp_path):
    _write_session(tmp_path, "theirs", [("t", {})], server="other")

    corpus = load_corpus([tmp_path], "srv")

    assert corpus.contributing_count == 0
    assert corpus.tools_served == ()
    summary = render_corpus_summary(corpus, "srv")
    assert "every call will MISS" in summary


def test_the_heaviest_session_is_the_blast_radius_sample_not_the_newest(tmp_path):
    """A real bug, found by running `drifter run --dry-run` against the
    actual accumulated corpus rather than by reasoning: an earlier version
    sampled the manifest source for the per-run cost estimate, and the
    newest session in a real corpus turned out to carry a manifest and ZERO
    calls -- so the preview a user authorizes spending through reported
    "~0 tool calls" for a run that would make plenty. Blast radius is a
    cost-and-risk ceiling; of the available single-session samples it must
    take the one that cannot understate.
    """
    _write_session(tmp_path, "newest_but_empty", [], started_at="2026-09-01T00:00:00Z", tool_names=["t"])
    heavy = _write_session(
        tmp_path, "older_but_busy",
        [("t", {"i": i}) for i in range(5)],
        started_at="2026-01-01T00:00:00Z",
    )

    corpus = load_corpus([tmp_path], "srv")

    assert corpus.manifest_source == tmp_path / "newest_but_empty.jsonl"  # newest still serves the manifest
    assert corpus.heaviest_path == heavy  # but the COST estimate samples this one
    assert corpus.heaviest_call_count == 5


# --- the actual point: coverage --------------------------------------------


def test_a_corpus_resolves_calls_that_any_single_session_alone_would_miss(tmp_path):
    """DEC-027(b)'s whole justification, demonstrated rather than asserted:
    three sessions each explore a different path, and only the union of all
    three answers all three calls. Replaying from any ONE of them MISSes the
    other two -- which is exactly the shape docs/SPEC.md §15 limitation 16
    measured against a real agent."""
    a = _write_session(tmp_path, "a", [("read_file", {"path": "/x"})])
    _write_session(tmp_path, "b", [("read_file", {"path": "/y"})])
    _write_session(tmp_path, "c", [("list_directory", {"path": "/z"})])

    single = ReplayStore()
    single.index_sessions(load_corpus([a], "srv").session_paths)
    whole = ReplayStore()
    whole.index_sessions(load_corpus([tmp_path], "srv").session_paths)

    probes = [("read_file", {"path": "/x"}), ("read_file", {"path": "/y"}), ("list_directory", {"path": "/z"})]
    single_hits = sum(1 for tool, args in probes if single.lookup("srv", tool, args) is not None)
    whole_hits = sum(1 for tool, args in probes if whole.lookup("srv", tool, args) is not None)

    assert single_hits == 1
    assert whole_hits == 3


def test_the_golden_fixture_still_works_as_a_corpus_of_one():
    """Backward compatibility, stated as a real requirement: one session is
    still a legitimate corpus, just usually an inadequate one."""
    corpus = load_corpus([GOLDEN_FIXTURE], GOLDEN_SERVER)

    assert corpus.session_count == 1
    assert corpus.contributing_count == 1
    assert corpus.indexed_calls > 0
    assert len(corpus.tools_served) > 0
    assert corpus.manifests_disagree is False
