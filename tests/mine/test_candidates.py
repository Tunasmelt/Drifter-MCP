"""F-30: task candidate generation + approval, docs/FEATURES.md.

Done when: a mined candidate can be edited (prompt, assertions, safety policy) and
approved without touching raw recordings.

The file is the user's. Every operation here that changes it must leave everything the
user wrote -- comments, edited prompts, extra assertions -- exactly as they left it;
several tests compare the surrounding text byte for byte for that reason.
"""

import ast
from pathlib import Path

import pytest
import yaml

from mcp_drifter.mine.candidates import (
    CandidateFileError,
    append_entries,
    approve_in_text,
    approved_entries,
    build_entries,
    candidate_id,
    read_candidates,
    render_file,
    uncovered_tools,
)
from mcp_drifter.mine.prefixspan import Pattern


def pattern(*items: str, support: int = 5, sessions: int = 3) -> Pattern:
    return Pattern(items=tuple(items), support=support, session_count=sessions)


# --- building candidates -----------------------------------------------------


def test_a_pattern_becomes_a_candidate_with_its_exact_assertions():
    (entry,) = build_entries(
        [pattern("search", "get_customer", "create_invoice", support=12, sessions=4)],
        total_trajectories=40,
        taken_ids=set(),
    )
    assert entry == {
        "id": "search_get_customer_create_invoice",
        "status": "candidate",
        "support": 12,
        "of_trajectories": 40,
        "sessions": 4,
        "pattern": ["search", "get_customer", "create_invoice"],
        "prompt": "",
        "assert": {
            "calls": ["search", "get_customer", "create_invoice"],
            "calls_before": [["search", "get_customer"], ["get_customer", "create_invoice"]],
            "never_calls": [],
            "no_errors": False,
        },
    }


def test_a_repeated_tool_yields_distinct_calls_and_no_self_ordering():
    """`read, read, get_info`: the assertion is that read and get_info both happened
    and read came first -- not the meaningless 'read before read'."""
    (entry,) = build_entries([pattern("read", "read", "get_info")], total_trajectories=9, taken_ids=set())
    assert entry["assert"]["calls"] == ["read", "get_info"]
    assert entry["assert"]["calls_before"] == [["read", "get_info"]]


def test_ids_are_safe_and_unique():
    assert candidate_id(("get customer", "create-invoice"), set()) == "get_customer_create_invoice"
    taken = {"a_b"}
    assert candidate_id(("a", "b"), taken) == "a_b_2"
    assert candidate_id(("a", "b"), taken | {"a_b_2"}) == "a_b_3"


def test_ids_already_taken_are_not_reused_across_a_batch():
    entries = build_entries([pattern("a", "b"), pattern("a", "b", support=4)], total_trajectories=9, taken_ids=set())
    assert [e["id"] for e in entries] == ["a_b", "a_b_2"]


# --- the file ------------------------------------------------------------------


def test_a_rendered_file_reads_back_to_the_same_entries():
    entries = build_entries([pattern("a", "b", support=6)], total_trajectories=10, taken_ids=set())
    text = render_file("srv", entries)
    doc = read_candidates(text)
    assert doc.server == "srv"
    assert doc.entries == entries
    assert yaml.safe_load(text)["version"] == 1


def test_the_rendered_file_tells_the_user_what_to_do_with_it():
    text = render_file("srv", build_entries([pattern("a", "b")], total_trajectories=4, taken_ids=set()))
    assert "prompt" in text.splitlines()[0] + text.split("candidates:")[0]
    assert "drifter tasks approve" in text


def test_appending_leaves_everything_the_user_wrote_untouched():
    first = build_entries([pattern("a", "b")], total_trajectories=10, taken_ids=set())
    original = render_file("srv", first)
    # The user edits: a comment, a prompt, an extra assertion.
    edited = original.replace("prompt: ''", "prompt: Invoice acme  # my wording").replace(
        "never_calls: []", "never_calls: [delete_customer]"
    )
    edited = "# my own note, must survive\n" + edited
    new = build_entries([pattern("c", "d", support=3)], total_trajectories=10, taken_ids={"a_b"})
    result = append_entries(edited, "srv", new)
    assert result.startswith(edited.rstrip("\n"))
    doc = read_candidates(result)
    assert [e["id"] for e in doc.entries] == ["a_b", "c_d"]
    assert doc.entries[0]["prompt"] == "Invoice acme"
    assert doc.entries[0]["assert"]["never_calls"] == ["delete_customer"]


def test_a_pattern_already_in_the_file_is_not_added_again():
    entries = build_entries([pattern("a", "b")], total_trajectories=10, taken_ids=set())
    text = render_file("srv", entries)
    again = build_entries([pattern("a", "b", support=99)], total_trajectories=50, taken_ids={"a_b"})
    assert append_entries(text, "srv", again) == text


def test_an_empty_candidates_list_is_filled_in_place():
    text = "version: 1\nserver: srv\ncandidates: []\n"
    new = build_entries([pattern("a", "b")], total_trajectories=4, taken_ids=set())
    result = append_entries(text, "srv", new)
    assert [e["id"] for e in read_candidates(result).entries] == ["a_b"]


def test_appending_to_another_servers_file_is_refused():
    text = render_file("alpha", build_entries([pattern("a", "b")], total_trajectories=4, taken_ids=set()))
    with pytest.raises(CandidateFileError, match="alpha"):
        append_entries(text, "beta", build_entries([pattern("c", "d")], total_trajectories=4, taken_ids=set()))


# --- approval ------------------------------------------------------------------


def _two_candidates() -> str:
    entries = build_entries(
        [pattern("a", "b", support=6), pattern("c", "d", support=3)], total_trajectories=10, taken_ids=set()
    )
    return "# keep me\n" + render_file("srv", entries)


def test_approving_flips_only_that_entrys_status_and_nothing_else():
    text = _two_candidates()
    result = approve_in_text(text, "c_d")
    doc = read_candidates(result)
    assert [(e["id"], e["status"]) for e in doc.entries] == [("a_b", "candidate"), ("c_d", "approved")]
    # Byte-identical apart from that one word.
    assert result.replace("status: approved", "status: candidate") == text


def test_approving_an_already_approved_task_says_so():
    once = approve_in_text(_two_candidates(), "a_b")
    with pytest.raises(CandidateFileError, match="already approved"):
        approve_in_text(once, "a_b")


def test_approving_an_unknown_id_names_the_ones_that_exist():
    with pytest.raises(CandidateFileError, match="a_b.*c_d|c_d.*a_b"):
        approve_in_text(_two_candidates(), "nope")


def test_a_hand_reformatted_entry_is_never_rewritten_around_the_users_back():
    """If the status line can't be located safely, refuse and say what to do --
    never fall back to re-serializing the file and losing its comments."""
    text = "version: 1\nserver: srv\ncandidates:\n- {id: x, status: candidate, prompt: p}\n"
    with pytest.raises(CandidateFileError, match="status: approved"):
        approve_in_text(text, "x")


def test_only_approved_entries_become_tasks():
    text = approve_in_text(_two_candidates(), "a_b")
    assert [e["id"] for e in approved_entries(read_candidates(text))] == ["a_b"]


# --- validation ----------------------------------------------------------------


@pytest.mark.parametrize(
    "text, fragment",
    [
        ("- not a mapping", "mapping"),
        ("version: 2\nserver: s\ncandidates: []\n", "version"),
        ("version: 1\ncandidates: []\n", "server"),
        ("version: 1\nserver: s\ncandidates: [{status: candidate}]\n", "id"),
        ("version: 1\nserver: s\ncandidates: [{id: x, status: maybe}]\n", "status"),
        ("version: 1\nserver: s\ncandidates: [{id: x, status: candidate}, {id: x, status: candidate}]\n", "duplicate"),
    ],
)
def test_a_malformed_file_is_refused_with_a_reason_the_user_can_act_on(text, fragment):
    with pytest.raises(CandidateFileError, match=fragment):
        read_candidates(text)


# --- coverage ------------------------------------------------------------------


def test_tools_in_no_approved_task_are_listed():
    text = approve_in_text(
        render_file("srv", build_entries([pattern("a", "b")], total_trajectories=4, taken_ids=set())), "a_b"
    )
    assert uncovered_tools(("a", "b", "c", "d"), approved_entries(read_candidates(text))) == ["c", "d"]


def test_with_nothing_approved_every_tool_is_uncovered():
    text = render_file("srv", build_entries([pattern("a", "b")], total_trajectories=4, taken_ids=set()))
    assert uncovered_tools(("a", "b"), approved_entries(read_candidates(text))) == ["a", "b"]


# --- module order ----------------------------------------------------------------


def test_mine_imports_nothing_from_a_module_downstream_of_it():
    """CLAUDE.md's dependency order: record -> replay -> mutate -> evaluate -> mine ->
    policy -> cli. mine/ may use what is upstream of it, never policy/ or cli/."""
    root = Path(__file__).parent.parent.parent / "src" / "mcp_drifter" / "mine"
    for path in root.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            for name in names:
                assert not name.startswith(("mcp_drifter.policy", "mcp_drifter.cli")), f"{path.name} imports {name}"
