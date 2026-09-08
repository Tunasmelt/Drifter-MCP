"""Blast-radius preview (F-31), docs/SPEC.md §10.

docs/SPEC.md's own mockup ("Planned: 12 workflows · 48 agent runs · 312 tool calls
... Estimated replay coverage: 92% ... Continue? [y/N]") is written for a v1
command surface `drifter run` doesn't have yet (multi-task orchestration, F-35's
own docstring: "essentially everything above" full orchestration is unbuilt) and
a mechanism ("estimated replay coverage") that presupposes a live server fallback
for a replay MISS — which does not exist anywhere in this codebase (every MISS
either synthesizes a placeholder for a `tool_addition`-injected tool or reports
MISS outright; there is no live-fallback code path at all, matching this
project's repeated confirmation that Drifter has no live-mode invocation path).

Built here, real and computable without either of those: given the ALREADY-
RECORDED fixture `drifter run` is about to replay from, and the planned repeat
count, estimate the volume and risk profile of what's about to happen — the
real, un-deferred cost this command incurs today, which is spawning REAL agent
subprocesses (repeats × 2 arms), not a live MCP server connection. "Estimated
tool calls by risk level" uses the fixture's OWN recorded call sequence
(classified via F-26) as the predictor, honestly labeled as an estimate, not a
guarantee — a fresh agent invocation can and does diverge from the fixture
(docs/SPEC.md §15 limitation 16's own real-test finding), so this is "if the
agent behaves similarly to the fixture," the same honest framing F-15's
fidelity gating already uses for the same underlying uncertainty.

"Estimated replay coverage" itself is a real, documented gap, not built — see
this module's own docstring above for why the concept as SPEC.md's mockup
describes it doesn't map onto Drifter's actual, live-fallback-free architecture.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import get_args

from mcp_drifter.policy.classify import classify_manifest
from mcp_drifter.record.reader import read_session
from mcp_drifter.record.schema import RiskLevel, ToolCall, ToolDescriptor

_RISK_LEVELS: tuple[RiskLevel, ...] = get_args(RiskLevel)


@dataclass(frozen=True)
class BlastRadiusPreview:
    """`workflow_count` is hardcoded to 1 — `drifter run`'s current scope is
    exactly one task, one operator (Gate 3 minimal scope, `cli/run.py`'s own
    docstring) — not computed, since there is nothing yet to count beyond
    that single planned comparison. Real, not a placeholder: this becomes a
    genuine count once multi-task orchestration (the rest of F-35) exists.
    """

    workflow_count: int
    planned_agent_runs: int
    estimated_tool_calls: int
    estimated_calls_by_risk: dict[RiskLevel, int]


def compute_blast_radius(
    fixture_path: Path,
    tools_served: list[ToolDescriptor],
    repeats: int,
    destructive_override: Sequence[str] = (),
) -> BlastRadiusPreview:
    """`repeats` is the EFFECTIVE per-arm repeat count already resolved by
    the caller (`calibration.baseline.repeats` if not explicitly overridden
    — same resolution `evaluate.baseline.run_baseline` performs; this
    function does not re-derive it, matching this project's "caller owns
    configuration" shape elsewhere in `policy/`). Both arms (baseline,
    mutated) run `repeats` times each, so total planned agent runs is
    `repeats * 2`.
    """
    fixture_calls = [r for r in read_session(fixture_path) if isinstance(r, ToolCall)]
    classifications = classify_manifest(tools_served, destructive_override)

    per_run_counts: dict[RiskLevel, int] = {level: 0 for level in _RISK_LEVELS}
    for call in fixture_calls:
        classification = classifications.get(call.tool_name)
        risk = classification.risk if classification is not None else "unknown"
        per_run_counts[risk] += 1

    planned_agent_runs = repeats * 2
    calls_by_risk = {level: count * planned_agent_runs for level, count in per_run_counts.items()}

    return BlastRadiusPreview(
        workflow_count=1,
        planned_agent_runs=planned_agent_runs,
        estimated_tool_calls=len(fixture_calls) * planned_agent_runs,
        estimated_calls_by_risk=calls_by_risk,
    )


def render_blast_radius(preview: BlastRadiusPreview) -> str:
    """docs/SPEC.md §10's own mockup groups risk levels into three terse
    buckets ("read-only", "reversible writes", "destructive") for a
    hypothetical richer command surface. Deviated from deliberately, not by
    oversight: this shows every non-zero risk level individually, including
    `irreversible_write` and `unknown` (SPEC's own mockup example happened
    to have zero of both, so it never had to decide how to render them) —
    an unknown-classified tool about to be called for real is exactly the
    kind of thing a safety preview should never quietly fold into a bucket
    that reads as "safe," matching this project's broader "unknown is never
    silently upgraded to something more permissive" stance (docs/SPEC.md §10's
    own taxonomy text).
    """
    lines = [
        f"Planned: {preview.workflow_count} workflow(s) · {preview.planned_agent_runs} agent run(s) "
        f"· ~{preview.estimated_tool_calls} tool call(s) (estimated from the fixture)"
    ]
    breakdown = ", ".join(
        f"{count} {level}" for level, count in preview.estimated_calls_by_risk.items() if count > 0
    )
    lines.append(f"         {breakdown}" if breakdown else "         (no tool calls in the fixture)")
    return "\n".join(lines)
