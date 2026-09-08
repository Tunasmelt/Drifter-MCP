"""Task assertion engine (F-24), docs/SPEC.md §8's Task axis.

The Task axis has reported `UNKNOWN` unconditionally since Gate 3 — not as
a stub, but because no oracle existed to ask. This is that oracle: opt-in,
deterministic assertions authored against a task, evaluated over the
trajectories a run actually recorded.

**The non-negotiable this module exists to honor** (CLAUDE.md): a task
verdict defaults to UNKNOWN, never PASS, when no assertion is configured.
Getting that backwards is the single easiest way to make Drifter quietly
untrustworthy — it doesn't crash, it just calmly reports success. So
`UNKNOWN` here is a first-class result with a stated reason, and PASS is
reachable only when real assertions were configured AND real runs were
evaluated against them.

**One of docs/SPEC.md's four named assertion types cannot be built, and
saying so is the point.** §8 lists `calls`, `calls_before`, `never_calls`,
and `result_contains`. The first three are evaluable from what a session
records. `result_contains` is not, structurally and by design: F-02/F-04
record only a result's SHAPE (`{type, keys, array_lengths}` — see
`record/writer.py`'s `_result_shape`), never its payload, and that
shape-only rule is one of docs/SPEC.md §3's non-negotiable invariants
rather than an implementation gap to close later. An assertion about what a
result CONTAINS therefore has nothing to read. Rather than silently drop it
or quietly weaken recording to support it, two recordable checks in the
same spirit are offered in its place — `result_has_keys` (the recorded
shape's keys, which is genuinely what "did the tool return the right kind
of thing" reduces to under shape-only recording) and `no_errors` (the
recorded `is_error` flag). See docs/CHANGELOG.md for the amendment.

Assertions are evaluated per-run, then aggregated. They are deliberately
NOT subject to the minimum-evidence gate the Behavior axis needs
(`evaluate/effect_size.py`, docs/SPEC.md §15 limitation 16): that gate
exists because `natural_variation` and `baseline_spread` are statistical
estimates that mean nothing at n=1. An assertion is a deterministic check
on one real trajectory — a single run genuinely failing `never_calls:
[delete_everything]` is a real finding, not a sampling artifact. The run
counts are always reported so a reader can weigh the evidence themselves.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from mcp_drifter.record.reader import read_session
from mcp_drifter.record.schema import ToolCall

TaskVerdict = Literal["PASS", "FAIL", "UNKNOWN"]


@dataclass(frozen=True)
class TaskAssertions:
    """One task's oracle. Every field is optional; an instance with none of
    them set is `empty` and yields UNKNOWN rather than a vacuous PASS.

    `calls_before` pairs are `(earlier, later)` — asserting that the first
    tool is called at some point before the first call to the second, which
    is the ordering question a workflow actually cares about ("searched
    before creating"), not that they are adjacent.

    `result_has_keys` maps a tool name to keys its recorded result shape
    must contain. `no_errors` asserts no recorded call reported
    `is_error: true`.
    """

    calls: tuple[str, ...] = ()
    calls_before: tuple[tuple[str, str], ...] = ()
    never_calls: tuple[str, ...] = ()
    result_has_keys: dict[str, tuple[str, ...]] = field(default_factory=dict)
    no_errors: bool = False

    @property
    def empty(self) -> bool:
        return not (
            self.calls or self.calls_before or self.never_calls or self.result_has_keys or self.no_errors
        )


@dataclass(frozen=True)
class AssertionFailure:
    """One assertion that did not hold, in a form a user can act on."""

    kind: str
    detail: str


@dataclass(frozen=True)
class RunAssertionResult:
    passed: bool
    failures: tuple[AssertionFailure, ...]


@dataclass(frozen=True)
class TaskResult:
    """The Task axis's own verdict, aggregated across an arm's valid runs.

    `verdict` is UNKNOWN exactly when there was nothing to ask (no
    assertions) or nothing to ask it of (no evaluated runs) — `reason` says
    which. PASS requires every evaluated run to satisfy every assertion;
    any run failing any assertion is FAIL, since an assertion is a
    deterministic oracle rather than a statistical estimate (see this
    module's docstring).
    """

    verdict: TaskVerdict
    runs_evaluated: int
    runs_passed: int
    failures: tuple[AssertionFailure, ...]
    reason: str | None = None


_NO_ASSERTIONS_REASON = "no assertions configured for this task"


def evaluate_run(records: Sequence[object], assertions: TaskAssertions) -> RunAssertionResult:
    """Evaluates `assertions` against one session's already-read records.

    Takes records rather than a path so a caller that has already read a
    session (as the aggregation below does) doesn't pay to read it twice,
    matching `evaluate/baseline.py`'s own `_run_fidelity`/`_tool_path`
    shape.
    """
    calls = [r for r in records if isinstance(r, ToolCall)]
    called_names = [c.tool_name for c in calls]
    failures: list[AssertionFailure] = []

    for required in assertions.calls:
        if required not in called_names:
            failures.append(
                AssertionFailure("calls", f"expected a call to {required!r}, but it was never called")
            )

    for earlier, later in assertions.calls_before:
        # Both must be present for an ordering claim to hold at all; a
        # missing tool is reported as an ordering failure with a distinct
        # message rather than silently passing on a vacuous truth.
        if earlier not in called_names or later not in called_names:
            missing = [n for n in (earlier, later) if n not in called_names]
            failures.append(
                AssertionFailure(
                    "calls_before",
                    f"expected {earlier!r} before {later!r}, but {', '.join(repr(m) for m in missing)} "
                    f"was never called",
                )
            )
            continue
        if called_names.index(earlier) > called_names.index(later):
            failures.append(
                AssertionFailure(
                    "calls_before",
                    f"expected {earlier!r} before {later!r}, but {later!r} was called first",
                )
            )

    for forbidden in assertions.never_calls:
        if forbidden in called_names:
            count = called_names.count(forbidden)
            failures.append(
                AssertionFailure("never_calls", f"{forbidden!r} must never be called, but was called {count}x")
            )

    for tool_name, required_keys in assertions.result_has_keys.items():
        matching = [c for c in calls if c.tool_name == tool_name]
        if not matching:
            failures.append(
                AssertionFailure(
                    "result_has_keys",
                    f"expected {tool_name!r} to return keys {list(required_keys)}, but it was never called",
                )
            )
            continue
        for call in matching:
            shape_keys = set((call.result_shape or {}).get("keys") or [])
            missing = [k for k in required_keys if k not in shape_keys]
            if missing:
                failures.append(
                    AssertionFailure(
                        "result_has_keys",
                        f"{tool_name!r} result is missing key(s) {missing} (recorded shape had "
                        f"{sorted(shape_keys) or 'no keys'})",
                    )
                )
                break  # one report per tool is enough to act on

    if assertions.no_errors:
        # `is_error is True` specifically, not truthiness: None means
        # "unknown -- recorded before the field existed, or the call
        # faulted before producing a result at all" (record/schema.py's own
        # nullable-field discipline), which is not evidence of an error.
        errored = [c.tool_name for c in calls if c.is_error is True]
        if errored:
            failures.append(
                AssertionFailure("no_errors", f"call(s) returned is_error: {', '.join(sorted(set(errored)))}")
            )

    return RunAssertionResult(passed=not failures, failures=tuple(failures))


def evaluate_task(session_paths: Sequence[Path], assertions: TaskAssertions) -> TaskResult:
    """Aggregates `evaluate_run` across an arm's runs.

    `session_paths` should be the arm's VALID (fidelity-passing) sessions,
    not every attempted run — a run excluded because replay failed is one
    where the agent could not do the task for harness reasons, and
    asserting on it would report a task failure that is really a fidelity
    failure. `cli/run.py` passes `BaselineResult.valid_session_paths` for
    exactly this reason.
    """
    if assertions.empty:
        return TaskResult(
            verdict="UNKNOWN",
            runs_evaluated=0,
            runs_passed=0,
            failures=(),
            reason=_NO_ASSERTIONS_REASON,
        )

    if not session_paths:
        return TaskResult(
            verdict="UNKNOWN",
            runs_evaluated=0,
            runs_passed=0,
            failures=(),
            reason="assertions are configured, but no valid run survived to evaluate them against",
        )

    passed = 0
    failures: list[AssertionFailure] = []
    seen: set[tuple[str, str]] = set()
    for path in session_paths:
        result = evaluate_run(list(read_session(path)), assertions)
        if result.passed:
            passed += 1
        for failure in result.failures:
            key = (failure.kind, failure.detail)
            if key not in seen:
                seen.add(key)
                failures.append(failure)

    evaluated = len(session_paths)
    return TaskResult(
        verdict="PASS" if passed == evaluated else "FAIL",
        runs_evaluated=evaluated,
        runs_passed=passed,
        failures=tuple(failures),
    )
