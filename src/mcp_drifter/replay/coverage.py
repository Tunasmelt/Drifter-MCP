"""Projected replay coverage — DEC-027(c), docs/CHANGELOG.md.

The question this answers: *if I run my agent now, what fraction of the
calls it makes will actually resolve from my recordings?* docs/SPEC.md §15
limitation 16 measured the answer to be catastrophically low against a real
agent, and the user only found out AFTER spending twenty real agent runs.
This computes an estimate beforehand, from already-recorded data alone —
zero execution, zero API cost, same discipline as `cli/score.py`.

**Why the obvious version would be worthless.** Replaying the corpus's own
recorded calls against a store built from that same corpus reports ~100% by
construction: every call is in the index because it is what built the index.
That number would be pure self-congratulation. The real question is a
GENERALIZATION question — how well does this corpus answer a session it has
never seen — so this uses leave-one-out cross-validation: each session in
turn is held out, and its calls are resolved against a store built from only
the OTHER sessions.

**Computed in one pass, not N passes.** Naively that means building N stores
over N sessions each, O(N²) file reads — far too slow for a pre-flight gate
on an 80-session corpus. Instead each key is indexed once to the SET of
sessions that contain it; a held-out session's call then resolves from the
rest of the corpus exactly when some *other* session also carries that key.
That is the identical answer leave-one-out would give, at O(N) reads.

**Two honest caveats, stated because they change how the number should be
read.** First, this is biased LOW: the store it simulates has one fewer
session than the store a real run would use. The bias is negligible for a
large corpus and severe for a tiny one, which is why a single-session corpus
is reported as un-estimable rather than as 0% — with nothing to hold out
against, cross-validation has no meaning, and printing "0%" would read as a
measurement when it is an artifact. Second, it measures only the exact and
semantic tiers. Inverse (tier 2) resolution depends on a specific active
mutation's inverse map (F-12), which is not a property of the corpus at all,
so folding it in would inflate a number that is supposed to describe the
recordings.

The per-tool breakdown is the actionable half: limitation 16's root cause was
a real agent's near-universal first move (`list_allowed_directories`) being
absent from every recording. A user who can see which tools their corpus
answers worst can go record those, which is the one lever DEC-027 left open.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from mcp_drifter.record.reader import read_session
from mcp_drifter.record.schema import ToolCall
from mcp_drifter.replay.replay_store import replay_key, semantic_key


@dataclass(frozen=True)
class ToolCoverage:
    """One tool's share of the leave-one-out result."""

    tool_name: str
    calls: int
    exact: int
    semantic: int
    missed: int

    @property
    def coverage(self) -> float:
        return (self.exact + self.semantic) / self.calls if self.calls else 1.0


@dataclass(frozen=True)
class CoverageEstimate:
    """Projected replay coverage for a corpus, leave-one-out.

    `estimable` is False when the corpus is too small to cross-validate at
    all (fewer than two sessions carrying calls) — read `reason` in that
    case and ignore the rates, which are not measurements. Every rate is a
    POOLED fraction of calls, not a mean of per-session rates: sessions
    differ in length, and what a user wants to know is "what fraction of a
    run's calls resolve," which weights by call.
    """

    sessions: int
    total_calls: int
    exact_hits: int
    semantic_hits: int
    misses: int
    per_tool: tuple[ToolCoverage, ...]
    estimable: bool
    reason: str | None = None

    @property
    def coverage(self) -> float | None:
        if not self.estimable or not self.total_calls:
            return None
        return (self.exact_hits + self.semantic_hits) / self.total_calls

    @property
    def exact_coverage(self) -> float | None:
        if not self.estimable or not self.total_calls:
            return None
        return self.exact_hits / self.total_calls


def estimate_coverage(session_paths: Sequence[Path], server: str) -> CoverageEstimate:
    """Leave-one-out projected coverage over `session_paths` for `server`.

    Reads each session once. Sessions with no calls for `server` contribute
    nothing and are not counted as held-out samples — an empty session is
    not evidence that the corpus generalizes.
    """
    # session index -> its calls; and key -> set of session indices holding it
    per_session_calls: list[list[ToolCall]] = []
    exact_owners: dict[str, set[int]] = {}
    semantic_owners: dict[str, set[int]] = {}

    for path in session_paths:
        calls = [r for r in read_session(path) if isinstance(r, ToolCall) and r.server == server]
        if not calls:
            continue
        index = len(per_session_calls)
        per_session_calls.append(calls)
        for call in calls:
            exact_owners.setdefault(replay_key(server, call.tool_name, call.arguments), set()).add(index)
            semantic_owners.setdefault(semantic_key(server, call.tool_name, call.arguments), set()).add(index)

    sessions = len(per_session_calls)
    if sessions < 2:
        return CoverageEstimate(
            sessions=sessions,
            total_calls=sum(len(c) for c in per_session_calls),
            exact_hits=0,
            semantic_hits=0,
            misses=0,
            per_tool=(),
            estimable=False,
            reason=(
                f"{sessions} session(s) with calls for {server!r} — at least 2 are needed to "
                f"estimate coverage, since the estimate works by holding one session out and "
                f"resolving it against the others. Record more sessions with `drifter observe`."
            ),
        )

    exact_hits = semantic_hits = misses = 0
    per_tool_counts: dict[str, list[int]] = {}  # tool -> [calls, exact, semantic, missed]

    for index, calls in enumerate(per_session_calls):
        for call in calls:
            counts = per_tool_counts.setdefault(call.tool_name, [0, 0, 0, 0])
            counts[0] += 1
            # Resolvable from the REST of the corpus: some other session
            # carries this key. Exact is checked first and wins outright,
            # matching ReplayStore.lookup's own tier ordering.
            if exact_owners[replay_key(server, call.tool_name, call.arguments)] - {index}:
                exact_hits += 1
                counts[1] += 1
            elif semantic_owners[semantic_key(server, call.tool_name, call.arguments)] - {index}:
                semantic_hits += 1
                counts[2] += 1
            else:
                misses += 1
                counts[3] += 1

    per_tool = tuple(
        sorted(
            (
                ToolCoverage(tool_name=name, calls=c[0], exact=c[1], semantic=c[2], missed=c[3])
                for name, c in per_tool_counts.items()
            ),
            # Worst coverage first, then most-called -- the tools a user
            # should go record are the ones that miss most often and matter
            # most, in that order.
            key=lambda t: (t.coverage, -t.calls),
        )
    )

    return CoverageEstimate(
        sessions=sessions,
        total_calls=sum(len(c) for c in per_session_calls),
        exact_hits=exact_hits,
        semantic_hits=semantic_hits,
        misses=misses,
        per_tool=per_tool,
        estimable=True,
    )


def render_coverage(estimate: CoverageEstimate, fidelity_floor: float | None = None, max_tools: int = 3) -> str:
    """A short block for `drifter run`'s pre-flight and `drifter doctor`.

    `fidelity_floor` (from `calibration.yaml`), when given, turns the number
    into a prediction the user can act on: a projected coverage below the
    floor means most runs are heading for exclusion, which is the single
    most useful thing to know BEFORE spending them.

    It is a ONE-SIDED prediction, and the rendered block now says so. A LOW
    number reliably predicts exclusions. A HIGH number does not promise the
    opposite -- docs/SPEC.md §15 limitation 17 recorded a corpus this scored at
    100% whose real replay runs came back at 0.20-0.67 and were all
    excluded. The cause is structural, not a bug here: this replays
    RECORDED calls, whose arguments already encode content the agent will
    not receive at replay time (shape-only recording), so it measures
    corpus self-consistency rather than reproducibility under content-free
    replay. Saying that in the output matters more than the number does --
    limitation 16's whole lesson is that a confident figure with an
    invisible foundation is worse than none.
    """
    if not estimable_ok(estimate):
        return f"REPLAY COVERAGE  not estimable — {estimate.reason}"

    coverage = estimate.coverage or 0.0
    lines = [
        f"REPLAY COVERAGE  ~{coverage * 100:.0f}% projected "
        f"(exact {estimate.exact_hits}, semantic {estimate.semantic_hits}, "
        f"missed {estimate.misses} of {estimate.total_calls} calls "
        f"across {estimate.sessions} sessions, leave-one-out)"
    ]

    if fidelity_floor is not None and coverage < fidelity_floor:
        lines.append(
            f"                 WARNING: below the {fidelity_floor:.2f} fidelity floor — most runs "
            f"are likely to be EXCLUDED and the verdict to come back UNKNOWN. Record more "
            f"sessions before spending agent runs."
        )
    elif fidelity_floor is not None:
        # Deliberately printed on the GOOD path, where it is easy to omit and
        # most likely to mislead. A high projection is not a promise: see
        # docs/SPEC.md §15 limitation 17 for a 100%-projected corpus whose real
        # runs came back 0.20-0.67 and were all excluded.
        lines.append(
            "                 NOTE: a high projection is not a guarantee — this replays RECORDED "
            "calls, whose arguments already encode response content your agent will NOT receive "
            "at replay time (recording is shape-only). An agent that builds arguments from prior "
            "results (paths from a listing, ids from a search) can still miss. See docs/SPEC.md "
            "§15 limitation 17."
        )

    worst = [t for t in estimate.per_tool if t.missed][:max_tools]
    if worst:
        lines.append("                 worst-covered tools:")
        for tool in worst:
            lines.append(
                f"                   {tool.tool_name}: {tool.coverage * 100:.0f}% "
                f"({tool.missed}/{tool.calls} calls unresolved)"
            )
    return "\n".join(lines)


def estimable_ok(estimate: CoverageEstimate) -> bool:
    """Tiny helper so callers read as intent rather than attribute-poking."""
    return estimate.estimable
