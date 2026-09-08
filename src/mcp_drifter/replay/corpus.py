"""Corpus resolution for replay — DEC-027(b), docs/CHANGELOG.md.

`drifter run` originally replayed from exactly one `--fixture` session.
docs/SPEC.md §15 limitation 16 established, with real measurements, why that
is not enough: a real agent explores, so a single recorded trajectory covers
very little of what the same agent does on its next run, and the resulting
MISS rate excluded 85% of runs in the real Gate 4 test. DEC-027 rejected
closing that gap by matching more loosely (a guess recorded as a HIT
inverts the meaning of the fidelity number the exclusion floor gates on)
and amended docs/SPEC.md §3 principle 2 instead: replay adequacy is a
property of the CORPUS, and coverage is the only honest lever.

This module is that lever's mechanism. It resolves a set of user-supplied
paths — session files, directories of them, or a mix — into the concrete
list of recordings a `ReplayStore` should index, and reports what that
corpus actually contains so the caller can tell the user how much coverage
they have BEFORE reading a verdict computed on top of it.

Two decisions worth stating, since neither is arbitrary:

**Directory expansion is deliberately NOT recursive.** `drifter observe`
writes sessions directly into the runs directory; `drifter run` writes its
own arms into `<runs>/run/<task_id>/{baseline,mutated}/`. A non-recursive
glob therefore picks up real observed recordings and structurally excludes
`drifter run`'s own output — which matters for more than tidiness: indexing
a previous run's BASELINE arm would be circular (replaying Drifter's own
replay), and indexing its MUTATED arm would poison the corpus with a
manifest that was deliberately altered, silently making the mutated
interface look like the real server's. `cli/score.py` already globs
non-recursively over the same directory for its own reasons; this matches
that precedent rather than inventing a second convention.

**Task grouping is not required, and deliberately not attempted.** No
recorded field ties a session to the task that produced it (`cli/score.py`'s
own docstring documents this gap at length; `task_id` is a caller-supplied
label that never reaches the schema). That gap does not block this feature,
because replay keys on `(server, tool, arguments)` — not on task. A session
recorded while doing some *other* task against the same server is perfectly
valid replay material for this one, and in fact task diversity across a
corpus is exactly what widens coverage of the server's call space. Sessions
recorded against a DIFFERENT server are harmless to index (their keys carry
that server's name and can never match a lookup for this one) but are
reported separately, so "12 sessions indexed" never overstates how many
actually contribute.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from mcp_drifter.record.reader import read_session
from mcp_drifter.record.schema import SessionStart, ToolCall, ToolDescriptor, ToolsList


class CorpusError(Exception):
    """A corpus input that cannot be resolved into any session at all —
    a nonexistent path, or a set of paths containing no `*.jsonl`. Raised
    rather than silently proceeding with an empty store, which would
    present as "every call MISSed" and send the user looking for a
    fidelity problem that is really a typo. Defined here rather than
    reusing `cli.config.ConfigError`: `replay/` sits upstream of `cli/`
    in this project's module dependency order (CLAUDE.md) and must not
    import from it.
    """


@dataclass(frozen=True)
class Corpus:
    """What a set of resolved recordings actually contains.

    `session_paths` is everything resolved and worth indexing;
    `contributing_paths` is the subset that recorded at least one call
    against the target server — the honest denominator for any statement
    about coverage. `tools_served` comes from `manifest_source`, the most
    recently started contributing session that carries a `tools/list`
    record (most recent = most representative of the server's current
    interface). `distinct_manifests` counts how many genuinely different
    manifests the contributing sessions disagree on: more than one means
    the tool interface changed part-way through the user's own recordings,
    which is real drift the caller should surface rather than silently
    resolve by picking the newest.
    """

    session_paths: tuple[Path, ...]
    contributing_paths: tuple[Path, ...]
    tools_served: tuple[ToolDescriptor, ...]
    manifest_source: Path | None
    distinct_manifests: int
    indexed_calls: int
    # The contributing session carrying the MOST calls for this server, and
    # that count. Used as the single-session sample for `policy/blast_radius.
    # py`'s per-run cost estimate — deliberately the heaviest session rather
    # than the newest or the mean: blast radius is a cost-and-risk ceiling
    # shown before real agent runs are authorized, so of the available
    # estimates the one that must not UNDERSTATE is the right one. Found by
    # running it: an earlier version sampled `manifest_source`, which in a
    # real corpus was a session with a manifest and zero calls, and the
    # preview cheerfully reported "~0 tool calls" for a run that would make
    # plenty.
    heaviest_path: Path | None
    heaviest_call_count: int

    @property
    def session_count(self) -> int:
        return len(self.session_paths)

    @property
    def contributing_count(self) -> int:
        return len(self.contributing_paths)

    @property
    def manifests_disagree(self) -> bool:
        return self.distinct_manifests > 1


def resolve_session_paths(inputs: Sequence[Path]) -> list[Path]:
    """Expands `inputs` (session files, directories, or a mix) into the
    concrete session files to index — directories non-recursively, see
    this module's docstring for why that matters.

    Order is stable and deterministic (each input in the order given;
    directory contents sorted by name), and duplicates are dropped while
    preserving first-seen order, so passing both a directory and one file
    inside it indexes that file once rather than twice.
    """
    resolved: list[Path] = []
    seen: set[Path] = set()

    for raw in inputs:
        path = Path(raw)
        if path.is_dir():
            candidates = sorted(path.glob("*.jsonl"))
        elif path.exists():
            candidates = [path]
        else:
            raise CorpusError(
                f"replay corpus path does not exist: {path} — pass a recorded session "
                f"JSONL, or a directory of them (e.g. the `record.dir` "
                f"`drifter observe` writes to)."
            )
        for candidate in candidates:
            key = candidate.resolve()
            if key not in seen:
                seen.add(key)
                resolved.append(candidate)

    if not resolved:
        given = ", ".join(str(p) for p in inputs)
        raise CorpusError(
            f"replay corpus is empty: no *.jsonl sessions found under {given}. "
            f"Record some first with `drifter observe`."
        )
    return resolved


def _manifest_key(tools: list[ToolDescriptor]) -> str:
    """A canonical, comparable form for one manifest — used only to count
    how many DISTINCT manifests a corpus holds, never to reconstruct one."""
    return "\n".join(t.model_dump_json() for t in tools)


def load_corpus(inputs: Sequence[Path], server: str) -> Corpus:
    """Resolves `inputs` and reads each session once to summarize what the
    corpus holds for `server` — the manifest to serve, how many sessions
    actually contribute, how many calls they carry, and whether the
    contributing sessions' manifests agree with each other.

    Does not build the `ReplayStore` itself: indexing is
    `ReplayStore.index_sessions(corpus.session_paths)`, kept separate so
    this stays a pure read over the corpus (`cli/score.py`'s own
    zero-execution discipline) and so a caller that only wants to REPORT
    coverage — DEC-027(c)'s pre-flight check — never has to build an index
    it isn't going to query.
    """
    session_paths = resolve_session_paths(inputs)

    contributing: list[Path] = []
    indexed_calls = 0
    heaviest_path: Path | None = None
    heaviest_call_count = 0
    # (started_at, path, manifest) for contributing sessions that carry one.
    manifests: list[tuple[str, Path, list[ToolDescriptor]]] = []

    for path in session_paths:
        records = list(read_session(path))
        calls = [r for r in records if isinstance(r, ToolCall) and r.server == server]
        tools_lists = [r for r in records if isinstance(r, ToolsList) and r.server == server]
        if not calls and not tools_lists:
            continue  # recorded against some other server -- harmless, just not ours

        contributing.append(path)
        indexed_calls += len(calls)
        if len(calls) > heaviest_call_count:
            heaviest_call_count = len(calls)
            heaviest_path = path
        if tools_lists:
            starts = [r for r in records if isinstance(r, SessionStart)]
            started_at = starts[0].started_at if starts else ""
            manifests.append((started_at, path, tools_lists[-1].tools_served))

    manifest_source: Path | None = None
    tools_served: list[ToolDescriptor] = []
    if manifests:
        # Most recently STARTED session wins, ties broken by resolution
        # order (`sorted` is stable) rather than by filesystem mtime, which
        # a copy or a checkout would change without the recording itself
        # having changed.
        started_at, manifest_source, tools_served = sorted(manifests, key=lambda m: m[0])[-1]

    distinct = len({_manifest_key(m) for _s, _p, m in manifests})

    return Corpus(
        session_paths=tuple(session_paths),
        contributing_paths=tuple(contributing),
        tools_served=tuple(tools_served),
        manifest_source=manifest_source,
        distinct_manifests=distinct,
        indexed_calls=indexed_calls,
        heaviest_path=heaviest_path,
        heaviest_call_count=heaviest_call_count,
    )


def render_corpus_summary(corpus: Corpus, server: str) -> str:
    """One short block for `drifter run`'s own output, shown BEFORE any
    agent is spawned — DEC-027's "measure and report adequacy" obligation
    at its cheapest useful point. Deliberately states the contributing
    count separately from the resolved count: a user who points `--fixture`
    at a directory full of recordings for a different server should see
    that immediately, not infer it later from an unexplained MISS rate.
    """
    lines = [
        f"REPLAY CORPUS  {corpus.contributing_count} of {corpus.session_count} "
        f"session(s) recorded against {server!r}, {corpus.indexed_calls} call(s) indexed"
    ]
    if corpus.contributing_count == 0:
        lines.append(
            f"               WARNING: no session in this corpus recorded against "
            f"{server!r} — every call will MISS."
        )
    if corpus.manifests_disagree:
        lines.append(
            f"               WARNING: {corpus.distinct_manifests} different tool manifests "
            f"across these sessions — the interface changed mid-corpus; serving the "
            f"most recent ({corpus.manifest_source})."
        )
    return "\n".join(lines)
