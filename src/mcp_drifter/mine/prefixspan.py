"""Frequent subsequence mining, PrefixSpan (F-29, docs/FEATURES.md).

Finds recurring workflows: ordered tool sequences that appear, gaps allowed, inside
many trajectories -- including inside longer, varied ones. `get_customer ->
create_invoice` is found even when some runs did `get_customer -> update_address ->
create_invoice`, because the point is the core steps, not contiguity.

Implemented here rather than imported: PrefixSpan is a short algorithm, the input is
small, and a dependency would be a supply-chain surface for something a screen of
code does. It is the published technique (Pei et al., 2001), written from the
description.

SUPPORT is counted in TRAJECTORIES, weighted by each signature's occurrence count --
a workflow that ran forty times counts forty, not once. That is what "how often does
the agent do this" means.

CLOSED patterns only, by default: a pattern is dropped when a longer pattern that
contains it has exactly the same support, since it then never occurs except as part
of the longer one -- reporting both is one workflow twice. The comparison is against
patterns up to `max_length`, so "closed" means closed WITHIN that cap; a pattern at
the cap is never demoted by a longer one it cannot see.

`max_length` is not just a presentation choice, it is what keeps this tractable:
gapped subsequences of a long trajectory grow exponentially, so an unbounded search
over an exploring agent's fifty-call session would not terminate.

Imports only `mine.signature` (its own sibling).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from mcp_drifter.mine.signature import SignatureGroup


@dataclass(frozen=True)
class Pattern:
    items: tuple[str, ...]
    # Trajectories containing this pattern (each signature weighted by its count).
    support: int
    # Distinct recorded sessions those trajectories came from.
    session_count: int


def _is_subsequence(needle: tuple[str, ...], haystack: tuple[str, ...]) -> bool:
    it = iter(haystack)
    return all(any(x == y for y in it) for x in needle)


def mine_patterns(
    groups: Sequence[SignatureGroup],
    *,
    min_support: int,
    min_length: int,
    max_length: int,
    closed: bool = True,
) -> list[Pattern]:
    """Ranked frequent patterns: support, then length, then the items themselves
    (so the order is fully deterministic). `min_length` filters AFTER closedness is
    decided, so a single-tool pattern that is really part of a longer workflow is
    absorbed by it instead of resurfacing when short patterns are allowed."""
    db = [(g.signature, g.count, g.session_ids) for g in groups]
    found: list[tuple[tuple[str, ...], int, int]] = []

    def grow(prefix: tuple[str, ...], projected: list[tuple[int, int]]) -> None:
        if len(prefix) >= max_length:
            return
        # For each candidate next item: which sequences it appears in (after each
        # sequence's current position), and where its FIRST such occurrence is --
        # PrefixSpan projects onto the first occurrence only.
        weight: dict[str, int] = {}
        firsts: dict[str, list[tuple[int, int]]] = {}
        for index, start in projected:
            signature, count, _ = db[index]
            seen: set[str] = set()
            for offset in range(start, len(signature)):
                item = signature[offset]
                if item in seen:
                    continue
                seen.add(item)
                weight[item] = weight.get(item, 0) + count
                firsts.setdefault(item, []).append((index, offset + 1))
        for item in sorted(weight):
            if weight[item] < min_support:
                continue
            extended = prefix + (item,)
            members = firsts[item]
            sessions = {s for index, _ in members for s in db[index][2]}
            found.append((extended, weight[item], len(sessions)))
            grow(extended, members)

    grow((), [(index, 0) for index in range(len(db))])

    if closed:
        by_support: dict[int, list[tuple[str, ...]]] = {}
        for items, support, _ in found:
            by_support.setdefault(support, []).append(items)
        found = [
            (items, support, sessions)
            for items, support, sessions in found
            if not any(
                len(other) > len(items) and _is_subsequence(items, other)
                for other in by_support[support]
            )
        ]

    patterns = [
        Pattern(items=items, support=support, session_count=sessions)
        for items, support, sessions in found
        if len(items) >= min_length
    ]
    patterns.sort(key=lambda p: (-p.support, -len(p.items), p.items))
    return patterns
