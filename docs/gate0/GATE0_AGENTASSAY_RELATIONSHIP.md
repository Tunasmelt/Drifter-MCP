# Relationship to AgentAssay

**Date:** 2026-08-16. Required by PHASES.md Gate 0, item 2, before `evaluate/` (Gate 3) is
built. Read-only review — AgentAssay is AGPL-3.0-licensed; no code was copied, ported, or
closely mirrored. This document exists so that claim is auditable, not just asserted.

## What AgentAssay is

Token-efficient stochastic testing for AI agents (arXiv:2603.02601). Repo:
`github.com/qualixar/agentassay`. Dual-licensed AGPL-3.0 / commercial, part of Qualixar, a
seven-product AI-agent-reliability platform.

## What it does that overlaps with Drifter's plans

- Three-valued verdicts (PASS/FAIL/INCONCLUSIVE) via hypothesis testing — conceptually
  identical to Drifter's REGRESSION/NO_REGRESSION/UNKNOWN framing (SPEC.md principle 5, §8)
- Adaptive sampling with early stopping (SPRT-based), claimed 83% cost reduction over
  fixed-N — the same idea as Drifter's F-27 adaptive scheduling
- Behavioral fingerprinting of execution traces
- Trace-first offline analysis (test against stored traces, not live re-execution) — the
  same principle as Drifter's replay-first, record/analyze cost-boundary architecture

## What it does not do

- **No MCP coverage at all.** Doesn't sit as a proxy on any protocol connection, doesn't
  mutate tool schemas or descriptions, has no concept of `tools/list` interception.
- No trajectory data-flow tracking, no environment fingerprinting tied to a specific
  protocol's tool manifest, no replay-key resolution problem (it doesn't need one — it's
  not simulating interface mutation).

## Claims ledger correction this produces (see SPEC.md C14)

The 20K LoC / 751 tests / 7,605 trials figures are **self-reported**, from the author's own
paper and README — a solo independent researcher's claims, not independently verified.
Not dismissed as false; just not treated as established fact. SPEC.md's claims ledger
already reflects this status (C14: SELF-REPORTED).

## What this means for building `evaluate/` (Gate 3)

**Do:** implement adaptive sampling and three-valued verdicts from the published statistical
technique (sequential hypothesis testing — Wald, 1940s; not AgentAssay's invention).
Cite AgentAssay in Drifter's own docs as prior art for applying this to agents generally.

**Do not:** read or reference AgentAssay's source code when implementing `evaluate/`.
AGPL-3.0 restricts distribution and derivative works, not learning from the published paper
— but code-level similarity to their implementation, even independently arrived at, is a
risk not worth taking when the underlying statistics are public-domain techniques
implementable from first principles.

## Positioning conclusion

Drifter's methodological novelty claim should be narrow and accurate: **not** three-valued
verdicts, **not** adaptive sampling, **not** behavioral fingerprinting — all prior art,
credited as such. Drifter's actual novelty is MCP-native proxy-based interface mutation
with mutation-aware replay resolution (SPEC.md §7), which AgentAssay has no equivalent of
because it doesn't operate on MCP at all. This matches SPEC.md §15's existing limitation
statement — this document is the evidence trail behind that statement, produced before
`evaluate/` was written rather than discovered after.
