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


class PatternLimitError(RuntimeError):
    """The search found more patterns than it was allowed to. Raised, not truncated: a
    silently cut-off list would rank whatever the search happened to reach first."""


@dataclass(frozen=True)
class Pattern:
    items: tuple[str, ...]
    # Trajectories containing this pattern (each signature weighted by its count).
    support: int
    # Distinct recorded sessions those trajectories came from.
    session_count: int


def _is_subsequence(pattern: Sequence[str], signature: Sequence[str]) -> bool:
    remaining = iter(signature)
    return all(item in remaining for item in pattern)


def supporting_tools(items: Sequence[str], groups: Sequence[SignatureGroup]) -> set[str]:
    """Every tool called in any trajectory that CONTAINS `items` (gaps allowed): the calls
    this workflow is actually seen alongside. A candidate assertion must not forbid one of
    these, or the workflow would fail against the very recordings it was mined from."""
    tools: set[str] = set()
    for group in groups:
        if _is_subsequence(items, group.signature):
            tools.update(group.signature)
    return tools


def mine_patterns(
    groups: Sequence[SignatureGroup],
    *,
    min_support: int,
    min_length: int,
    max_length: int,
    closed: bool = True,
    max_patterns: int | None = None,
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
            if max_patterns is not None and len(found) > max_patterns:
                raise PatternLimitError(
                    f"found more than {max_patterns} recurring patterns and stopped. That many means "
                    f"min_support ({min_support}) is too low for a corpus this size or this varied, "
                    "so nearly every short sequence 'recurs': raise `mine.min_support` (or lower "
                    "`mine.max_length`) in calibration.yaml"
                )
            grow(extended, members)

    grow((), [(index, 0) for index in range(len(db))])

    if closed:
        # A pattern is absorbed when some longer pattern that contains it has the SAME
        # support. It is enough to look one item up: support can only fall as a pattern
        # grows, so if a longer equal-support super-pattern exists, so does every pattern
        # between the two (its support is sandwiched between equal values), including one
        # exactly one item longer -- and that one is in `found`, because it is no longer
        # than the super-pattern and no less supported. So delete each item in turn from
        # each pattern and look the result up. (An earlier version compared every pattern
        # against every other, which was quadratic and took ~14s on 100 trajectories.)
        support_of = {items: support for items, support, _ in found}
        absorbed: set[tuple[str, ...]] = set()
        for items, support, _ in found:
            for i in range(len(items)):
                shorter = items[:i] + items[i + 1 :]
                if shorter and support_of.get(shorter) == support:
                    absorbed.add(shorter)
        found = [entry for entry in found if entry[0] not in absorbed]

    patterns = [
        Pattern(items=items, support=support, session_count=sessions)
        for items, support, sessions in found
        if len(items) >= min_length
    ]
    patterns.sort(key=lambda p: (-p.support, -len(p.items), p.items))
    return patterns
