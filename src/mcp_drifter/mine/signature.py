"""Signature grouping (F-28, docs/FEATURES.md).

Turns recorded sessions into trajectories, and trajectories into signatures: the
ordered tool-name sequence with everything volatile stripped, deduplicated with
occurrence counts.

A signature is COMPUTED HERE, at read time, never stored on disk (docs/SPEC.md
§5: a field is recorded only if it cannot be derived later from what is). That
keeps the recording schema untouched and lets the normalization change without
invalidating a single stored session.

What "normalized" means, exactly, so nobody has to guess: the signature is the
sequence of TOOL NAMES and nothing else. Request ids, timestamps, argument values,
argument keys and result shapes are all dropped. That is deliberately the
coarsest useful grouping -- two runs that called the same tools in the same order
are the same workflow, whatever they were called with. A finer signature (say, one
that also separated calls by which arguments were present) would need a stated
reason to exist; none has surfaced.

Trajectory boundaries come from `TrajectoryEnd.call_seqs`, i.e. from record/'s own
segmentation (F-06/F-07). Mining inherits that segmentation's quality rather than
re-deriving it, including its stated `segmentation_confidence` -- this module does
not filter on it. A call that no `TrajectoryEnd` names (recorded after the last
trajectory closed) is not folded into a trajectory, since that would invent a
workflow the recorder never saw; it is counted and reported instead.

Imports only `record/`, the module this one depends on.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from mcp_drifter.record.reader import read_session
from mcp_drifter.record.schema import SessionStart, ToolCall, ToolDescriptor, ToolsList, TrajectoryEnd
from mcp_drifter.replay.replay_proxy import REPLAY_SERVER_NAME_PREFIX

Signature = tuple[str, ...]


@dataclass(frozen=True)
class Trajectory:
    session_id: str
    trajectory_id: str
    tools: Signature


@dataclass
class SessionTrajectories:
    """One session's trajectories, plus what it could not place in one."""

    session_id: str
    server: str | None
    trajectories: list[Trajectory]
    unsegmented_calls: int
    # Recorded by `drifter replay-serve`, not observed: the agent was being REPLAYED.
    replayed: bool
    # Tools this session called or was served, for "which tools appear in no task".
    tools: tuple[str, ...]
    # The descriptors the real server declared (the last `tools/list`), so a caller can
    # classify them by risk. Mining never classifies: that is policy/, downstream of it.
    manifest: dict[str, ToolDescriptor] = field(default_factory=dict)


@dataclass
class MinedCorpus:
    trajectories: list[Trajectory] = field(default_factory=list)
    sessions: int = 0
    # Sessions left out because they were replays, not observations.
    replayed_sessions: int = 0
    unsegmented_calls: int = 0
    tools: tuple[str, ...] = ()
    # Union of the declared descriptors over the mined (non-replay) sessions; where a
    # tool is declared twice, the later session's declaration wins.
    manifest: dict[str, ToolDescriptor] = field(default_factory=dict)


@dataclass(frozen=True)
class SignatureGroup:
    signature: Signature
    count: int
    session_ids: tuple[str, ...]


def read_session_trajectories(path: Path) -> SessionTrajectories:
    records = list(read_session(path))
    session_id = ""
    calls: dict[int, ToolCall] = {}
    ends: list[TrajectoryEnd] = []
    served: tuple[str, ...] = ()
    manifest: dict[str, ToolDescriptor] = {}
    server: str | None = None
    replayed = False

    for record in records:
        if not session_id:
            session_id = record.session_id
        if isinstance(record, SessionStart):
            replayed = any(name.startswith(REPLAY_SERVER_NAME_PREFIX) for name in record.environment.server_versions)
        if isinstance(record, ToolCall):
            calls[record.seq] = record
            server = server or record.server
        elif isinstance(record, TrajectoryEnd):
            ends.append(record)
        elif isinstance(record, ToolsList):
            # The LAST manifest, matching policy/safety's own precedent: a later
            # re-list is the more representative one.
            served = tuple(t.name for t in record.tools_served)
            # `tools_raw`: what the real server declared, not what a mutation served.
            manifest = {t.name: t for t in record.tools_raw}
            server = record.server

    placed: set[int] = set()
    trajectories: list[Trajectory] = []
    for end in ends:
        members = [calls[s] for s in end.call_seqs if s in calls]
        if not members:
            continue
        placed.update(c.seq for c in members)
        trajectories.append(
            Trajectory(
                session_id=session_id,
                trajectory_id=end.trajectory_id,
                tools=tuple(c.tool_name for c in members),
            )
        )

    tools = tuple(sorted(set(served) | {c.tool_name for c in calls.values()}))
    return SessionTrajectories(
        session_id=session_id,
        server=server,
        trajectories=trajectories,
        unsegmented_calls=len(calls) - len(placed),
        replayed=replayed,
        tools=tools,
        manifest=manifest,
    )


def load_corpus_trajectories(paths: Sequence[Path], server: str | None = None) -> MinedCorpus:
    """Every trajectory across `paths` (session files, already expanded).

    `server` keeps only that server's sessions -- a corpus can span servers, and a
    workflow on one is not evidence about another (same reason coverage is
    per-server). A session whose server cannot be determined (no calls, no
    manifest) is kept only when no filter is given.
    """
    corpus = MinedCorpus()
    tools: set[str] = set()
    for path in paths:
        session = read_session_trajectories(Path(path))
        if server is not None and session.server != server:
            continue
        if session.replayed:
            # `drifter replay-serve` writes into the same directory `observe` does. A
            # replay records the agent being replayed, not what it does: counting it would
            # have mining feed on its own replays.
            corpus.replayed_sessions += 1
            continue
        corpus.sessions += 1
        corpus.trajectories.extend(session.trajectories)
        corpus.unsegmented_calls += session.unsegmented_calls
        tools.update(session.tools)
        corpus.manifest.update(session.manifest)
    corpus.tools = tuple(sorted(tools))
    return corpus


def group_signatures(trajectories: Sequence[Trajectory]) -> list[SignatureGroup]:
    """Collapses trajectories to distinct signatures with occurrence counts, most
    frequent first. Ties break on the signature itself so the order is fully
    deterministic -- a ranked list that reshuffles between runs is not one a user
    can review."""
    counts: dict[Signature, int] = {}
    sessions: dict[Signature, set[str]] = {}
    for trajectory in trajectories:
        counts[trajectory.tools] = counts.get(trajectory.tools, 0) + 1
        sessions.setdefault(trajectory.tools, set()).add(trajectory.session_id)
    groups = [
        SignatureGroup(signature=sig, count=count, session_ids=tuple(sorted(sessions[sig])))
        for sig, count in counts.items()
    ]
    groups.sort(key=lambda g: (-g.count, g.signature))
    return groups
