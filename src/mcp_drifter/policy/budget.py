"""Budget and hard limits (F-32), docs/SPEC.md §11/§13.

Two of the three named limits are real, honestly scoped ceilings on
`drifter run`'s own actual cost (real agent subprocess invocations — see
`policy/blast_radius.py`'s own module docstring for why "model calls" isn't
directly observable here at all: docs/SPEC.md §15 limitation 2, "the proxy
sees only MCP traffic — no prompts, no system prompt, no model reasoning").
`--budget N` is therefore reframed, explicitly, as a TOOL-CALL budget — the
one thing this project can actually count from recorded sessions — not a
literal model-call counter, which would require visibility Drifter
structurally does not have.

Enforcement shape, stated precisely because it's a real, checked limitation,
not silently glossed over: budget/wall-time is checked BEFORE each repeat
starts, never mid-run. A real agent subprocess, once spawned, is never
preemptively killed partway through for exceeding a budget — that would
need this module to reach into `cli/subprocess_adapter.py`'s live process
management, real, separate design work not attempted here. The repeat that
crosses the threshold still completes and its calls count toward the total;
every repeat AFTER that is skipped before it's ever spawned. This is the
honest, buildable approximation of "aborts cleanly, stops mid-execution" —
stops starting NEW work, not stops IN-FLIGHT work.

`BudgetExceededError` is raised from a wrapped `run_once` callable
(`budget_limited`) — `evaluate.baseline.run_baseline`'s existing loop
already catches any `run_once()` exception and records it as an
`ExcludedRun` with a reason, continuing to aggregate whatever repeats DID
complete. This gives "partial results reportable" for free: no change to
`evaluate/baseline.py` was needed at all, matching this project's own
"caller owns configuration, aggregation logic is a pure downstream
consumer" shape used throughout `evaluate/`/`policy/`.

`--dry-run` needed no new computation at all: it's F-31's blast-radius
preview, shown, with execution simply never started — see `cli/run.py`'s
`run_run`.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from mcp_drifter.record.reader import read_session
from mcp_drifter.record.schema import ToolCall


class BudgetExceededError(RuntimeError):
    """Raised by a `budget_limited`-wrapped `run_once` when either limit is
    already exhausted before this repeat would start."""


def _count_tool_calls(session_path: Path) -> int:
    return sum(1 for r in read_session(session_path) if isinstance(r, ToolCall))


@dataclass
class BudgetTracker:
    """Shared across BOTH arms of a `drifter run` comparison (baseline +
    mutated) — a real, deliberate design choice: the budget is for the
    whole invocation's real cost, not per-arm, matching docs/FEATURES.md's own
    "Done when" framing ("a run exceeding budget stops... mid-execution"),
    singular run, not per-arm accounting. `None` on either limit means
    unlimited — matching this project's own nullable-field-means-absence
    convention elsewhere, not a sentinel like `0` or `-1`.
    """

    max_tool_calls: int | None = None
    max_wall_time_s: float | None = None
    spent_tool_calls: int = 0
    started_at: float = field(default_factory=time.monotonic)

    @property
    def elapsed_s(self) -> float:
        return time.monotonic() - self.started_at

    def check(self) -> None:
        """Raises if either limit is already exhausted. Called BEFORE a
        repeat starts, never during — see module docstring."""
        if self.max_tool_calls is not None and self.spent_tool_calls >= self.max_tool_calls:
            raise BudgetExceededError(
                f"tool-call budget exhausted: {self.spent_tool_calls}/{self.max_tool_calls} spent"
            )
        if self.max_wall_time_s is not None and self.elapsed_s >= self.max_wall_time_s:
            raise BudgetExceededError(
                f"wall-time budget exhausted: {self.elapsed_s:.0f}s/{self.max_wall_time_s:.0f}s elapsed"
            )

    def exceeded(self) -> bool:
        """Same condition as `check()`, without raising — used after a
        comparison finishes to report exit code 5 (docs/SPEC.md §12)
        without threading exceptions back out through `run_baseline`'s
        own exclusion-recording loop."""
        try:
            self.check()
        except BudgetExceededError:
            return True
        return False

    def record(self, session_path: Path) -> None:
        self.spent_tool_calls += _count_tool_calls(session_path)


def budget_limited(run_once: Callable[[], Path], tracker: BudgetTracker) -> Callable[[], Path]:
    """Wraps a `run_once: Callable[[], Path]` (e.g. `cli.subprocess_adapter.
    make_run_once`'s return value) so `evaluate.baseline.run_baseline`'s
    existing repeat loop stops starting new agent subprocesses once
    `tracker`'s budget is spent — no changes to `run_baseline` itself.
    """

    def wrapped() -> Path:
        tracker.check()
        path = run_once()
        tracker.record(path)
        return path

    return wrapped
