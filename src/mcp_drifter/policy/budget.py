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

Enforcement shape, updated under docs/PHASES.md R5 (the original text below is
kept because the distinction it draws still matters, just not where the line
used to fall):

Originally, budget/wall-time was checked BEFORE each repeat starts only,
never mid-run — a hanging or looping agent inside one repeat could make an
unbounded number of tool calls (replay answers with no real latency, so
`--budget 10` was no defense at all against one repeat making thousands of
calls within its `timeout_s` window). Found while working R5's "enforce
budgets DURING execution, including hanging and invalid calls" item, and
confirmed a real gap: `_count_tool_calls` only ran AFTER a repeat's session
file existed, so nothing inside a repeat's own tool-call loop ever consulted
the budget at all.

Fixed: `BudgetTracker` is now optionally threaded all the way down to
`replay/replay_proxy.py`'s `on_call_tool` (the same trailing-optional-
parameter pattern `corpus_facts`/`authored_responses` already use), which
calls `record_call()` — a live, immediate increment, checked against the
limit BEFORE every single tool call is answered, not just before every
repeat starts. Once exhausted mid-repeat, every FURTHER call in that SAME
repeat is rejected near-instantly (`REPLAY_BUDGET_EXCEEDED_CODE`, a
distinct, recorded fault) rather than served — so a hanging/looping agent
cannot keep spending real budget once the limit is hit, even mid-repeat.

What is still NOT attempted, deliberately, same as before: the agent
subprocess itself is never preemptively killed the instant budget is
exhausted — closing the connection mid-call from inside a request handler
risks leaving an in-flight response undelivered, real separate design work.
The already-hung process keeps running until it gives up on its own or its
own `timeout_s` elapses (unchanged, proven-correct machinery from R1); it
simply cannot do any more REAL work — every further call costs nothing and
teaches it nothing — once the shared budget is spent. This is the honest,
buildable meaning of "aborts cleanly, stops mid-execution": stops doing
useful work immediately, stops spawning NEW repeats immediately, without a
forced process kill this turn either.

Because counting is now live (via `record_call()`, called once per real
tool-call attempt from inside the proxy — including a call the proxy
REJECTS, matching `_count_tool_calls`'s own established "invalid calls
count too" behavior), the previous post-hoc `record(path)` — which re-read
a whole finished session file to count its `ToolCall` records after the
fact — is redundant when `budget` was actually threaded through, and is
kept only as the correct count for a caller that has NOT wired live
tracking (a legitimate case: not every `run_once` caller passes the tracker
into the proxy).

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

    def record_call(self) -> None:
        """docs/PHASES.md R5: live, immediate accounting for ONE real
        tool-call ATTEMPT, called from inside `replay/replay_proxy.py`'s
        `on_call_tool` — not `replay/`-imported directly (see that module's
        own note on why: it must not import from `policy/`, which is
        downstream of it in this project's module order); `cli/run.py`
        passes this bound method down as a plain callable instead, the same
        shape `inverse_map`/`corpus_facts` are already threaded in.

        Counts an attempt whether it resolves as a HIT, a MISS, a schema
        rejection, or a budget rejection itself — matching `record()`'s own
        `_count_tool_calls`, which counts every recorded `ToolCall`
        (including faulted ones) with no exceptions. The two must agree:
        whichever one a given caller actually uses (see `budget_limited`'s
        `count_after`), the resulting `spent_tool_calls` means the same
        thing either way.
        """
        self.spent_tool_calls += 1


def budget_limited(run_once: Callable[[], Path], tracker: BudgetTracker, count_after: bool = True) -> Callable[[], Path]:
    """Wraps a `run_once: Callable[[], Path]` (e.g. `cli.subprocess_adapter.
    make_run_once`'s return value) so `evaluate.baseline.run_baseline`'s
    existing repeat loop stops starting new agent subprocesses once
    `tracker`'s budget is spent — no changes to `run_baseline` itself.

    `count_after=False` (docs/PHASES.md R5): when the SAME `tracker` was also
    threaded into `run_once` itself (via `tracker.exceeded`/`tracker.record_call`
    passed down to the replay proxy, enforcing the budget DURING execution,
    not just between repeats), counting is already live and correct by the
    time `run_once()` returns — re-counting here from the finished session
    file would double the total. `count_after=True` (the default) preserves
    the original, simpler behavior for any caller that has NOT wired live
    tracking through the proxy.
    """

    def wrapped() -> Path:
        tracker.check()
        path = run_once()
        if count_after:
            tracker.record(path)
        return path

    return wrapped
