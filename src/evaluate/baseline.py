"""Baseline calibration (F-21), SPEC.md §7/§8.

Runs a task N times (default `calibration.yaml`'s `baseline.repeats`) and
establishes what's *normal* for it before any mutation is active: the
dominant tool-call path, how often each variant occurs, and how much the
agent naturally wobbles run to run (`natural_variation`/`baseline_spread`
— F-23's later `effect_size = (deviation_rate − natural_variation) /
baseline_spread` reads these two values directly, so they're named and
computed here to match that formula's units, not renamed later).

Scope, deliberately: this module owns the aggregation/analysis logic only.
`run_once` is injected — the real subprocess agent adapter (F-34) and a
replay-serving proxy mode don't exist yet, and building them is its own
task with real architectural decisions (how a stdio-based agent gets
pointed at a replay-serving server) that aren't settled anywhere in
SPEC.md yet. A future real `run_once` implementation plugs into this same
slot without this module's contract changing. Fidelity gating (F-22) and
effect-size scoring against a mutation arm (F-23) are separate, later
checklist items — not attempted here.

Explicit design change from PHASES.md's original sketch (per
.drifter/GATE_STATUS's `gate_1_note`): the real-world null rate for
`SessionStart.environment.tool_manifest_hash` was never measured — the
Gate 1 trial that would have told us was skipped. So this module never
assumes the hash is populated. A run with a null hash is excluded from
the path/variant/natural_variation/baseline_spread computation and
reported separately, with a reason — never silently treated as if its
fingerprint matched everything (which would be exactly the "field
populated with a plausible-but-wrong value" bug pattern CLAUDE.md's
testing-discipline note warns about, just with "unpopulated" standing in
for "populated").

Second exclusion reason, added once `run_once` had a real implementation
to fail (cli/subprocess_adapter.py's `make_run_once`, wired for the
first time in the integration test this exclusion path was built
for): a repeat whose `run_once()` call itself raises — subprocess spawn
failure, an unresolved timeout, anything — is excluded the same way,
with its own distinct reason, rather than letting the exception
propagate out of `run_baseline` and abort the whole run, discarding
every already-successful repeat. Same principle as the null-hash case:
a single flaky repeat degrades `valid_runs`, it doesn't nuke the run.
`ExcludedRun.session_id`/`.path` are `None` for this reason specifically
— there is no session to point at when `run_once` never produced one.

Third exclusion reason — fidelity gating (SPEC.md §7/§8, DEC-020, F-22),
now implemented since a real replay-served pipeline exists to gate. Per-
run fidelity is `exact_hit_calls / total_attempted_calls` over that run's
recorded `ToolCall`s. Only the exact tier exists right now
(`replay_store.py`'s `MatchTier` is `"exact"` only) — inverse/semantic
tiers and SPEC.md §9's `semantic_weight` apply once Gate 3's mutations
exist; no placeholder weighting is built for tiers that don't exist yet.

STOP-AND-CHECK done before writing this, not assumed: does a replay MISS
or FAULT actually reach the recorded session as a `ToolCall` at all, or
does the recorder's write path silently drop it (no `CallToolResult` to
build one from)? Read `record/writer.py`'s `observe()`/`_write_tool_call*`
branch structure directly — a `JSONRPCError` response for a `tools/call`
already routes to `_write_tool_call_fault`, writing a real `ToolCall`
with `fault=True`, `result_shape=None` (added v1.0.10, per CHANGELOG.md,
and already exercised by `tests/replay/test_replay_proxy.py`'s
`test_on_message_lets_sessionrecorder_produce_a_valid_new_session`,
which asserts the MISS call IS recorded with `fault=True`). So the
denominator here is NOT silently missing every miss — no prerequisite
writer.py fix was needed.

`calibration.fidelity_floor` (0.70) already existed in `calibration.yaml`
as an unconsumed constant — same situation `idle_gap_seconds` was in
before Gate 1 Prompt 6 actually read it. This module is now the first
reader. `calibration.fidelity_flag_threshold` (0.90) also already exists;
SPEC.md §8's table describes a fuller three-tier scheme (below floor:
excluded; between floor and flag_threshold: included but flagged
degraded; at/above flag_threshold: clean) — only the floor-based
exclusion is built here. The "flagged but included" middle tier is a
real, documented gap left for a later prompt, not silently built or
silently dropped.

`baseline_fidelity` (mean fidelity across valid, non-excluded runs) is
subject to the exact same truthiness trap `has_data` was added to catch
for `natural_variation`/`baseline_spread`: a genuinely low, real
fidelity (say `0.1`, a run that mostly missed) is Python-truthy but so
is a *high* one, and a fidelity of exactly `0.0` is Python-falsy but
fully real (every call missed, still a valid measurement, not "unknown
fidelity"). `has_data`/`valid_runs == 0` is what actually distinguishes
"never computed" (`None`) from "computed, and it's bad" (`0.0`) — same
rule as the other three aggregate fields, checked explicitly here rather
than assumed safe because it "returns a float."
"""

from __future__ import annotations

import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from record.calibration import Calibration, load_calibration
from record.reader import read_session
from record.schema import SessionStart, ToolCall

_NULL_HASH_REASON = "tool_manifest_hash is null"


def _run_once_failed_reason(exc: Exception) -> str:
    return f"run_once raised: {exc}"


def _fidelity_excluded_reason(fidelity: float, floor: float) -> str:
    return f"fidelity {fidelity:.2f} below floor {floor:.2f}"


@dataclass(frozen=True)
class ExcludedRun:
    """One baseline repeat excluded from the computation, and why.

    Three distinct reasons currently reach here, and `session_id`/`path`
    are only meaningful for the first two: a session WAS recorded, so
    there is something to point at. For `run_once raised`, `session_id`/
    `path` are `None`, not a placeholder value — there is no session to
    identify when `run_once()` never produced one.
    """

    session_id: str | None
    path: Path | None
    reason: str


@dataclass(frozen=True)
class BaselineResult:
    task_id: str
    total_runs: int

    # Computed over `valid_runs` (runs that passed every exclusion check:
    # non-null tool_manifest_hash, run_once didn't raise, fidelity at or
    # above calibration.fidelity_floor) only. All five are None (or {}
    # for variant_frequencies) exactly when valid_runs == 0 — confirmed
    # by construction (see run_baseline's two return statements), not
    # just by convention.
    #
    # DO NOT check these fields' truthiness to ask "is there data" —
    # `dominant_path` can genuinely be `()` (every valid run called no
    # tools), `natural_variation`/`baseline_spread` can genuinely be
    # `0.0` (a perfectly stable baseline — the *best* real outcome, not
    # an edge case), and `baseline_fidelity` can genuinely be `0.0` too
    # (every surviving call missed — still a real, computed measurement,
    # not "fidelity unknown"). All four are Python-falsy, identically to
    # `None`. `if not result.dominant_path:` silently conflates "no
    # data" with "real answer, perfectly stable/fully missed." Check
    # `valid_runs == 0` (or the `has_data` property below), or use
    # `is None` explicitly — never bare truthiness.
    valid_runs: int
    dominant_path: tuple[str, ...] | None
    variant_frequencies: dict[tuple[str, ...], int]
    natural_variation: float | None
    baseline_spread: float | None
    baseline_fidelity: float | None

    # Runs excluded from the computation above, and why. Never silently
    # dropped — see this module's docstring.
    excluded_runs: list[ExcludedRun] = field(default_factory=list)

    @property
    def has_data(self) -> bool:
        """The one unambiguous "was anything computed" signal — prefer
        this (or `valid_runs == 0`) over checking dominant_path/
        natural_variation/baseline_spread/baseline_fidelity's
        truthiness, which conflates "no data" with a real-but-empty/zero
        answer. See the field comments above.
        """
        return self.valid_runs > 0

    @property
    def fingerprint_warning(self) -> str | None:
        """A ready-to-surface warning string, or None if every repeat
        contributed. Whoever reads a BaselineResult should not have to
        re-derive this from excluded_runs by hand to notice some repeats
        weren't counted. Name kept from when null-hash exclusion was the
        only reason; now covers any exclusion reason (see ExcludedRun),
        each repeat identified by session_id where one exists, or its
        reason alone when it doesn't (run_once itself failed)."""
        if not self.excluded_runs:
            return None
        details = ", ".join(
            f"{r.session_id} ({r.reason})" if r.session_id is not None else r.reason for r in self.excluded_runs
        )
        return f"{len(self.excluded_runs)} of {self.total_runs} run(s) excluded: {details}"


def _tool_path(records: list) -> tuple[str, ...]:
    """The ordered sequence of tool names called in a session — SPEC.md
    §8's "path" (e.g. the report format's `search → get_customer →
    create_invoice`). Whole-session, not per-trajectory: a baseline run
    is one task, one session, so there's no need to segment further here.
    """
    return tuple(r.tool_name for r in records if isinstance(r, ToolCall))


def _run_fidelity(records: list) -> float:
    """`exact_hit_calls / total_attempted_calls` for one session's
    ToolCall records. `fault is False` is the only "confirmed hit"
    signal — a replay MISS or a replayed protocol fault are both
    recorded as `fault=True` (replay_proxy.py's own deliberate,
    documented choice: both look identical on the wire, so both are
    recorded identically), and `fault is None` (a legacy pre-v1.0.10
    corpus, not something a freshly replay-served run ever produces)
    is treated as "not a confirmed hit" too — conservative, matching
    this project's nullable-field discipline rather than assuming an
    unknown fault status was fine.

    A run with zero ToolCall records gets fidelity 1.0, vacuously —
    there were no unfaithful calls, matching the existing "empty path
    is a valid variant" precedent (an agent that calls nothing isn't
    unreliable, it just didn't call anything; nothing here would ever
    trigger fidelity-floor exclusion for a reason unrelated to replay
    fidelity itself).
    """
    calls = [r for r in records if isinstance(r, ToolCall)]
    if not calls:
        return 1.0
    hits = sum(1 for c in calls if c.fault is False)
    return hits / len(calls)


def run_baseline(
    task_id: str,
    run_once: Callable[[], Path],
    repeats: int | None = None,
    calibration: Calibration | None = None,
) -> BaselineResult:
    """Runs `task_id` `repeats` times via `run_once` (each call must
    return the path to that run's session JSONL) and aggregates the
    result. `repeats` defaults to `calibration.yaml`'s `baseline.repeats`
    when not given explicitly, matching `record/writer.py`'s existing
    `calibration or load_calibration()` pattern.
    """
    calibration = calibration or load_calibration()
    if repeats is None:
        repeats = calibration.baseline.repeats

    valid_paths: list[tuple[str, ...]] = []
    valid_fidelities: list[float] = []
    excluded_runs: list[ExcludedRun] = []

    for _ in range(repeats):
        try:
            session_path = run_once()
        except Exception as exc:
            excluded_runs.append(ExcludedRun(session_id=None, path=None, reason=_run_once_failed_reason(exc)))
            continue

        records = list(read_session(session_path))
        session_start = next(r for r in records if isinstance(r, SessionStart))

        if session_start.environment.tool_manifest_hash is None:
            excluded_runs.append(
                ExcludedRun(session_id=session_start.session_id, path=session_path, reason=_NULL_HASH_REASON)
            )
            continue

        fidelity = _run_fidelity(records)
        if fidelity < calibration.fidelity_floor:
            excluded_runs.append(
                ExcludedRun(
                    session_id=session_start.session_id,
                    path=session_path,
                    reason=_fidelity_excluded_reason(fidelity, calibration.fidelity_floor),
                )
            )
            continue

        valid_paths.append(_tool_path(records))
        valid_fidelities.append(fidelity)

    if not valid_paths:
        return BaselineResult(
            task_id=task_id,
            total_runs=repeats,
            valid_runs=0,
            dominant_path=None,
            variant_frequencies={},
            natural_variation=None,
            baseline_spread=None,
            baseline_fidelity=None,
            excluded_runs=excluded_runs,
        )

    variant_frequencies: dict[tuple[str, ...], int] = {}
    for path in valid_paths:
        variant_frequencies[path] = variant_frequencies.get(path, 0) + 1
    # Most frequent wins; ties broken by first-seen order (dict insertion
    # order over valid_paths, which is run order) — deterministic and
    # traceable back to which run set it, not an arbitrary hash order.
    dominant_path = max(variant_frequencies, key=lambda p: variant_frequencies[p])

    # natural_variation: fraction of valid runs that deviated from the
    # dominant path (a rate, 0..1 — same unit F-23's deviation_rate will
    # later use, so the two are directly comparable). baseline_spread:
    # population stdev of that same per-run binary indicator, i.e. how
    # much the deviation rate itself varies — the denominator that turns
    # a future (deviation_rate - natural_variation) difference into an
    # effect size rather than a bare rate difference.
    deviated = [0 if path == dominant_path else 1 for path in valid_paths]
    natural_variation = statistics.mean(deviated)
    baseline_spread = statistics.pstdev(deviated)

    baseline_fidelity = statistics.mean(valid_fidelities)

    return BaselineResult(
        task_id=task_id,
        total_runs=repeats,
        valid_runs=len(valid_paths),
        dominant_path=dominant_path,
        variant_frequencies=variant_frequencies,
        natural_variation=natural_variation,
        baseline_spread=baseline_spread,
        baseline_fidelity=baseline_fidelity,
        excluded_runs=excluded_runs,
    )
