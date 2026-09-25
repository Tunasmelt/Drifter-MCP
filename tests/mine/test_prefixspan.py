"""F-29: frequent subsequence mining (PrefixSpan), docs/FEATURES.md.

Done when: a fixture with a known embedded sub-pattern
(`get_customer -> create_invoice` inside several longer flows) surfaces it as a
ranked candidate.

The strongest check here is not any single example but the brute-force cross-check:
every support PrefixSpan reports is recomputed independently by enumerating
subsequences, so a wrong count cannot hide behind a plausible-looking ranking.
"""

import random
from itertools import combinations

from mcp_drifter.mine.prefixspan import Pattern, mine_patterns
from mcp_drifter.mine.signature import SignatureGroup


def groups(*items: tuple[tuple[str, ...], int]) -> list[SignatureGroup]:
    """(signature, count) pairs -> groups, each in its own session so session
    counts are checkable too."""
    return [
        SignatureGroup(signature=sig, count=count, session_ids=(f"s{i}",))
        for i, (sig, count) in enumerate(items)
    ]


def as_dict(patterns: list[Pattern]) -> dict[tuple[str, ...], int]:
    return {p.items: p.support for p in patterns}


def is_subsequence(needle, haystack) -> bool:
    it = iter(haystack)
    return all(any(x == y for y in it) for x in needle)


def brute_force(groups_, min_support, max_length):
    """Independent recount: enumerate every subsequence of every signature."""
    candidates: set[tuple[str, ...]] = set()
    for g in groups_:
        for length in range(1, min(max_length, len(g.signature)) + 1):
            candidates.update(combinations(g.signature, length))
    out = {}
    for cand in candidates:
        support = sum(g.count for g in groups_ if is_subsequence(cand, g.signature))
        if support >= min_support:
            out[cand] = support
    return out


def test_the_embedded_subpattern_surfaces_as_the_top_ranked_candidate():
    """get_customer -> create_invoice sits inside several longer flows, with other
    steps in between in some of them. It must come out on top, with the exact number
    of trajectories that contain it."""
    data = groups(
        (("get_customer", "create_invoice"), 4),
        (("search", "get_customer", "create_invoice", "send_receipt"), 3),
        (("get_customer", "update_address", "create_invoice"), 2),
        (("list_products", "get_customer", "create_invoice", "log"), 2),
        (("list_products",), 5),
    )
    patterns = mine_patterns(data, min_support=2, min_length=2, max_length=6)
    top = patterns[0]
    assert top.items == ("get_customer", "create_invoice")
    assert top.support == 4 + 3 + 2 + 2
    # Distinct sessions it appears in: the four groups above, not the fifth.
    assert top.session_count == 4


def test_a_subpattern_with_the_same_support_as_its_superpattern_is_not_reported():
    """(a,b) always occurs inside (a,b,c) here -- reporting both is one workflow
    twice. Only the closed one (the longer, equally-supported) survives."""
    data = groups((("a", "b", "c"), 3))
    result = as_dict(mine_patterns(data, min_support=2, min_length=1, max_length=6))
    assert result == {("a", "b", "c"): 3}


def test_a_subpattern_with_strictly_higher_support_is_kept_alongside_its_superpattern():
    data = groups((("a", "b", "c"), 3), (("a", "b"), 2))
    result = as_dict(mine_patterns(data, min_support=2, min_length=2, max_length=6))
    assert result == {("a", "b", "c"): 3, ("a", "b"): 5}


def test_support_below_the_threshold_is_excluded_and_exactly_at_it_is_included():
    data = groups((("a", "b"), 2), (("c", "d"), 1))
    assert as_dict(mine_patterns(data, min_support=2, min_length=2, max_length=6)) == {("a", "b"): 2}
    assert as_dict(mine_patterns(data, min_support=1, min_length=2, max_length=6)) == {
        ("a", "b"): 2,
        ("c", "d"): 1,
    }


def test_min_length_excludes_single_tool_patterns():
    data = groups((("a", "b"), 3), (("a",), 4))
    assert as_dict(mine_patterns(data, min_support=2, min_length=2, max_length=6)) == {("a", "b"): 3}
    assert ("a",) in as_dict(mine_patterns(data, min_support=2, min_length=1, max_length=6))


def test_max_length_caps_the_search():
    data = groups((("a", "b", "c", "d"), 3))
    result = as_dict(mine_patterns(data, min_support=2, min_length=2, max_length=2))
    assert max(len(items) for items in result) == 2
    assert ("a", "b", "c") not in result


def test_order_matters_a_before_b_is_not_b_before_a():
    data = groups((("a", "b"), 3))
    assert ("b", "a") not in as_dict(mine_patterns(data, min_support=2, min_length=2, max_length=6))


def test_a_repeated_tool_within_a_trajectory_is_mined_as_the_repetition():
    """Explorer agents call the same tool repeatedly (read, read, get_info, read).
    The repetition is part of the workflow, not an artifact to collapse."""
    data = groups((("a", "a", "b"), 3))
    assert as_dict(mine_patterns(data, min_support=2, min_length=2, max_length=6)) == {("a", "a", "b"): 3}


def test_a_signature_seen_many_times_counts_every_trajectory_not_one():
    data = groups((("a", "b"), 40), (("c",), 1))
    (top,) = mine_patterns(data, min_support=2, min_length=2, max_length=6)
    assert top.support == 40


def test_ranking_is_support_then_length_then_items_and_is_deterministic():
    data = groups((("a", "b"), 3), (("c", "d", "e"), 3), (("f", "g"), 5), (("h", "i"), 3))
    ranked = [p.items for p in mine_patterns(data, min_support=2, min_length=2, max_length=6)]
    assert ranked == [("f", "g"), ("c", "d", "e"), ("a", "b"), ("h", "i")]
    assert ranked == [p.items for p in mine_patterns(list(reversed(data)), min_support=2, min_length=2, max_length=6)]


def test_no_groups_mine_nothing():
    assert mine_patterns([], min_support=2, min_length=2, max_length=6) == []


def test_every_reported_support_matches_an_independent_brute_force_count():
    """The check that would catch a wrong PrefixSpan: random signatures over a
    small alphabet (so patterns genuinely collide), non-closed mode compared
    pattern-for-pattern with an enumeration that shares no code with the miner."""
    rng = random.Random(20260925)
    alphabet = ["a", "b", "c", "d"]
    data = groups(
        *[
            (tuple(rng.choice(alphabet) for _ in range(rng.randint(1, 6))), rng.randint(1, 4))
            for _ in range(14)
        ]
    )
    # Duplicate signatures from the random draw are fine: they are separate groups.
    for max_length in (2, 3, 4):
        mined = as_dict(mine_patterns(data, min_support=2, min_length=1, max_length=max_length, closed=False))
        assert mined == brute_force(data, min_support=2, max_length=max_length)


def test_closed_mode_is_exactly_the_closed_subset_of_the_full_result():
    rng = random.Random(7)
    alphabet = ["a", "b", "c"]
    data = groups(
        *[
            (tuple(rng.choice(alphabet) for _ in range(rng.randint(2, 5))), rng.randint(1, 3))
            for _ in range(10)
        ]
    )
    full = brute_force(data, min_support=2, max_length=4)
    expected_closed = {
        p: s
        for p, s in full.items()
        if not any(len(q) > len(p) and sq == s and is_subsequence(p, q) for q, sq in full.items())
    }
    got = as_dict(mine_patterns(data, min_support=2, min_length=1, max_length=4, closed=True))
    assert got == expected_closed


def test_closed_filtering_scales_to_a_realistically_sized_corpus():
    """Found by audit, not by any earlier test: the search took 0.1s on 100
    trajectories x 12 calls but the closed-pattern filter compared every pattern with
    every other and took ~14s, so `drifter tasks mine` looked hung on exactly the
    corpora mining exists for. A timing bound is the honest test for a scaling defect;
    5s is ~25x what the fixed version needs and ~3x under the broken one, so it fails
    for the bug and not for a slow machine."""
    import time

    rng = random.Random(1)
    tools = [f"t{i}" for i in range(14)]
    data = groups(*[(tuple(rng.choice(tools) for _ in range(12)), 1) for _ in range(100)])
    started = time.perf_counter()
    result = mine_patterns(data, min_support=2, min_length=2, max_length=6)
    elapsed = time.perf_counter() - started
    assert len(result) == 8216
    assert elapsed < 5.0, f"closed filtering took {elapsed:.1f}s"


# --- supporting_tools (mining pre-fill) ---------------------------------------------------


def test_supporting_tools_are_exactly_those_called_in_trajectories_containing_the_pattern():
    from mcp_drifter.mine.prefixspan import supporting_tools
    from mcp_drifter.mine.signature import SignatureGroup

    groups = [
        SignatureGroup(signature=("a", "b", "c"), count=3, session_ids=("s1",)),
        SignatureGroup(signature=("a", "x", "b"), count=2, session_ids=("s2",)),
        SignatureGroup(signature=("b", "a", "z"), count=1, session_ids=("s3",)),  # b BEFORE a: no support
        SignatureGroup(signature=("q",), count=9, session_ids=("s4",)),
    ]
    # (a, b) is a gapped subsequence of the first two only; z and q never co-occur with it.
    assert supporting_tools(("a", "b"), groups) == {"a", "b", "c", "x"}
    # (a, c) is only in the first.
    assert supporting_tools(("a", "c"), groups) == {"a", "b", "c"}
    # A pattern nothing contains has no supporting trajectories.
    assert supporting_tools(("c", "a"), groups) == set()
