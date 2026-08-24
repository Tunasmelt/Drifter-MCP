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


@dataclass(frozen=True)
class ExcludedRun:
    """One baseline run that could not be validated against a fingerprint."""

    session_id: str
    path: Path
    reason: str


@dataclass(frozen=True)
class BaselineResult:
    task_id: str
    total_runs: int

    # Computed over `valid_runs` (runs whose tool_manifest_hash was
    # non-null) only. All four are None (or {} for variant_frequencies)
    # exactly when valid_runs == 0 — confirmed by construction (see
    # run_baseline's two return statements), not just by convention.
    #
    # DO NOT check these fields' truthiness to ask "is there data" —
    # `dominant_path` can genuinely be `()` (every valid run called no
    # tools) and `natural_variation`/`baseline_spread` can genuinely be
    # `0.0` (a perfectly stable baseline — the *best* real outcome, not
    # an edge case). All three are Python-falsy, identically to `None`.
    # `if not result.dominant_path:` silently conflates "no data" with
    # "real answer, perfectly stable." Check `valid_runs == 0` (or the
    # `has_data` property below), or use `is None` explicitly — never
    # bare truthiness.
    valid_runs: int
    dominant_path: tuple[str, ...] | None
    variant_frequencies: dict[tuple[str, ...], int]
    natural_variation: float | None
    baseline_spread: float | None

    # Runs excluded from the computation above, and why. Never silently
    # dropped — see this module's docstring.
    excluded_runs: list[ExcludedRun] = field(default_factory=list)

    @property
    def has_data(self) -> bool:
        """The one unambiguous "was anything computed" signal — prefer
        this (or `valid_runs == 0`) over checking dominant_path/
        natural_variation/baseline_spread's truthiness, which conflates
        "no data" with a real-but-empty/zero answer. See the field
        comments above.
        """
        return self.valid_runs > 0

    @property
    def fingerprint_warning(self) -> str | None:
        """A ready-to-surface warning string, or None if every run had a
        usable fingerprint. Whoever reads a BaselineResult should not
        have to re-derive this from excluded_runs by hand to notice
        fingerprint-based comparison wasn't fully available."""
        if not self.excluded_runs:
            return None
        return (
            f"{len(self.excluded_runs)} of {self.total_runs} run(s) excluded from "
            f"fingerprint-based validation ({_NULL_HASH_REASON}): "
            f"{', '.join(r.session_id for r in self.excluded_runs)}"
        )


def _tool_path(records: list) -> tuple[str, ...]:
    """The ordered sequence of tool names called in a session — SPEC.md
    §8's "path" (e.g. the report format's `search → get_customer →
    create_invoice`). Whole-session, not per-trajectory: a baseline run
    is one task, one session, so there's no need to segment further here.
    """
    return tuple(r.tool_name for r in records if isinstance(r, ToolCall))


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
    excluded_runs: list[ExcludedRun] = []

    for _ in range(repeats):
        session_path = run_once()
        records = list(read_session(session_path))
        session_start = next(r for r in records if isinstance(r, SessionStart))

        if session_start.environment.tool_manifest_hash is None:
            excluded_runs.append(
                ExcludedRun(session_id=session_start.session_id, path=session_path, reason=_NULL_HASH_REASON)
            )
            continue

        valid_paths.append(_tool_path(records))

    if not valid_paths:
        return BaselineResult(
            task_id=task_id,
            total_runs=repeats,
            valid_runs=0,
            dominant_path=None,
            variant_frequencies={},
            natural_variation=None,
            baseline_spread=None,
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

    return BaselineResult(
        task_id=task_id,
        total_runs=repeats,
        valid_runs=len(valid_paths),
        dominant_path=dominant_path,
        variant_frequencies=variant_frequencies,
        natural_variation=natural_variation,
        baseline_spread=baseline_spread,
        excluded_runs=excluded_runs,
    )
