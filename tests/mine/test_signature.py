"""F-28: signature grouping (docs/FEATURES.md).

Done when: a 300-trajectory fixture with known repeated patterns collapses to the
expected small number of distinct signatures. Exact values throughout -- a
grouping that returns *some* groups proves nothing.
"""

from pathlib import Path

from mcp_drifter.mine.signature import (
    group_signatures,
    load_corpus_trajectories,
    read_session_trajectories,
)
from mine_corpus import write_known_corpus, write_session

GOLDEN = Path(__file__).parent.parent / "fixtures" / "golden_v0.1.jsonl"


def test_a_real_recorded_trajectory_yields_its_exact_tool_sequence():
    """Read from a real recording, not a synthetic one: the golden fixture's one
    trajectory, in the order its `TrajectoryEnd.call_seqs` names."""
    result = read_session_trajectories(GOLDEN)
    assert result.unsegmented_calls == 0
    assert len(result.trajectories) == 1
    trajectory = result.trajectories[0]
    assert trajectory.trajectory_id == "traj_12641e5390f7"
    assert trajectory.tools == (
        "list_directory",
        "search_files",
        "read_text_file",
        "read_text_file",
        "get_file_info",
        "read_text_file",
        "list_allowed_directories",
    )


def test_calls_no_trajectory_names_are_counted_not_silently_dropped(tmp_path):
    """A call recorded after the last trajectory closed belongs to no
    trajectory. It must not be folded into one (that would invent a workflow),
    and it must not vanish without a trace."""
    path = write_session(tmp_path, "s", [["a", "b"]], unsegmented_tail=["c", "d", "e"])
    result = read_session_trajectories(path)
    assert [t.tools for t in result.trajectories] == [("a", "b")]
    assert result.unsegmented_calls == 3


def test_the_300_trajectory_fixture_collapses_to_exactly_its_four_signatures(tmp_path):
    expected = write_known_corpus(tmp_path)
    corpus = load_corpus_trajectories(sorted(tmp_path.glob("*.jsonl")))
    assert len(corpus.trajectories) == 300
    groups = group_signatures(corpus.trajectories)
    assert {g.signature: g.count for g in groups} == expected
    # Most frequent first.
    assert [g.count for g in groups] == [150, 90, 50, 10]


def test_equal_counts_order_deterministically_by_signature(tmp_path):
    write_session(tmp_path, "s", [["b"], ["a"], ["b"], ["a"]])
    corpus = load_corpus_trajectories([tmp_path / "s.jsonl"])
    groups = group_signatures(corpus.trajectories)
    assert [(g.signature, g.count) for g in groups] == [(("a",), 2), (("b",), 2)]


def test_volatile_argument_values_do_not_split_a_signature(tmp_path):
    """FEATURES.md: 'normalizes away ... volatile argument values.' Two
    trajectories that differ only in what they were called WITH are the same
    workflow."""
    write_session(
        tmp_path,
        "s",
        [
            [("search", {"q": "acme"}), ("get_customer", {"id": 1})],
            [("search", {"q": "globex"}), ("get_customer", {"id": 2})],
        ],
    )
    corpus = load_corpus_trajectories([tmp_path / "s.jsonl"])
    groups = group_signatures(corpus.trajectories)
    assert [(g.signature, g.count) for g in groups] == [(("search", "get_customer"), 2)]


def test_a_group_remembers_which_sessions_it_came_from(tmp_path):
    write_session(tmp_path, "one", [["a", "b"]])
    write_session(tmp_path, "two", [["a", "b"], ["a", "b"]])
    corpus = load_corpus_trajectories(sorted(tmp_path.glob("*.jsonl")))
    (group,) = group_signatures(corpus.trajectories)
    assert group.count == 3
    assert group.session_ids == ("one", "two")


def test_server_filter_keeps_only_that_servers_trajectories_and_tools(tmp_path):
    write_session(tmp_path, "a1", [["x", "y"]], server="alpha", extra_tools=("idle_a",))
    write_session(tmp_path, "b1", [["p", "q"]], server="beta", extra_tools=("idle_b",))
    corpus = load_corpus_trajectories(sorted(tmp_path.glob("*.jsonl")), server="alpha")
    assert [t.tools for t in corpus.trajectories] == [("x", "y")]
    assert corpus.tools == ("idle_a", "x", "y")
    assert corpus.sessions == 1


def test_a_corpus_with_no_trajectories_groups_to_nothing(tmp_path):
    write_session(tmp_path, "s", [])
    corpus = load_corpus_trajectories([tmp_path / "s.jsonl"])
    assert corpus.trajectories == []
    assert group_signatures(corpus.trajectories) == []


# --- audit finding: a session `drifter replay-serve` recorded is not an observation ----
#
# `replay-serve` writes into the same directory `observe` does by default. Mining those as
# if observed counted the agent's own replays as evidence that it does what it was
# replayed doing: mining fed on itself.


def test_a_session_recorded_by_replay_is_flagged_and_left_out_of_the_corpus(tmp_path):
    write_session(tmp_path, "real1", [["a", "b"]])
    write_session(tmp_path, "real2", [["a", "b"]])
    write_session(tmp_path, "replay1", [["c", "d"]], replayed=True)
    assert read_session_trajectories(tmp_path / "replay1.jsonl").replayed is True
    assert read_session_trajectories(tmp_path / "real1.jsonl").replayed is False

    corpus = load_corpus_trajectories(sorted(tmp_path.glob("*.jsonl")))
    assert corpus.sessions == 2
    assert corpus.replayed_sessions == 1
    assert [t.tools for t in corpus.trajectories] == [("a", "b"), ("a", "b")]
    # Its tools are not evidence of anything the agent used either.
    assert corpus.tools == ("a", "b")


def test_the_replay_marker_is_the_name_the_replay_server_gives_itself():
    """One definition, shared: if replay ever renames itself, mining must follow."""
    from mcp_drifter.replay.replay_proxy import REPLAY_SERVER_NAME_PREFIX

    assert REPLAY_SERVER_NAME_PREFIX == "drifter-replay-"


# --- tool manifest, for classification (mining pre-fill) --------------------------------


def test_the_corpus_carries_the_tools_the_server_declared_across_sessions(tmp_path):
    write_session(tmp_path, "a", [["search", "get_customer"]], extra_tools=("delete_customer",))
    write_session(tmp_path, "b", [["search"]], extra_tools=("remove_item",))
    corpus = load_corpus_trajectories(sorted(tmp_path.glob("*.jsonl")), server="srv")
    assert sorted(corpus.manifest) == ["delete_customer", "get_customer", "remove_item", "search"]
    assert all(descriptor.name == name for name, descriptor in corpus.manifest.items())


def test_a_replayed_sessions_manifest_is_not_mixed_into_the_corpus(tmp_path):
    write_session(tmp_path, "real", [["search"]])
    write_session(tmp_path, "replay", [["search"]], extra_tools=("delete_everything",), replayed=True)
    corpus = load_corpus_trajectories(sorted(tmp_path.glob("*.jsonl")), server="srv")
    assert sorted(corpus.manifest) == ["search"]
