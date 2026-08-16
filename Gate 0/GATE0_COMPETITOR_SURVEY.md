# Gate 0 — Recorder Competitor Survey

**Date:** 2026-08-16. **Decision required by PHASES.md Gate 0, item 1.**

## Question

Does a maintained tool already do observe-mode MCP recording *and* replay equivalent to
FEATURES.md F-01–F-15? If yes, PHASES.md's kill criterion pivots Drifter to a
mutation-and-evaluation layer built on top of that tool rather than rebuilding the recorder.

## What exists (found via web search, 2026-08-16)

| Tool | Recording | Replay | Trace segmentation | Data-flow refs | Redaction | Env fingerprint | Mutation-aware replay |
|---|---|---|---|---|---|---|---|
| mcp-tape | ✓ (JSONL, byte-for-byte) | not evident | no | no | not evident | no | no |
| mcpsnoop | ✓ | ✓ | no | no | not evident | no | no |
| mcpscope | ✓ (DB, not JSONL) | claimed, unclear extent | no | no | not evident | no | no |
| MockServer AI Traffic Inspection | ✓ | ✓ (Record→Snapshot→Replay) | no (general LLM/HTTP, not MCP-trajectory-aware) | no | ✓ (auto) | no | no |
| Official MCP Inspector | **no** — confirmed not a transparent proxy; it's its own MCP client | n/a | n/a | n/a | n/a | n/a | n/a |

## Decision: BUILD, with a narrowed public claim

**None of these meet the kill criterion's bar.** The kill criterion requires equivalence to
F-01–F-15 as a set — trace-context segmentation, data-flow references, environment
fingerprinting, redaction-by-default, and the three-tier mutation-aware replay key
(SPEC.md §7). No surveyed tool has more than two of these five, and **zero** have the
mutation-aware replay key, because none of them do mutation testing at all — that
differentiator is untouched.

Proceed with Gate 1 as planned in FEATURES.md and PHASES.md.

## What must change before this reaches public docs

The recorder is not a blue ocean, and claiming otherwise would be a false claim discovered
publicly and embarrassingly. Specifically:

- HANDOFF.md and any future README must not imply "nobody records MCP traffic." Several
  people built exactly that in 2026, independently, including one in a single evening.
- The differentiated claim is narrower and more defensible: *no existing recorder captures
  MCP-tool-call semantics (data-flow dependencies, trajectory segmentation, environment
  fingerprinting) in a form that supports mutation-aware replay.* That's still true, still
  citable, and doesn't overreach.
- **mcpsnoop and MockServer's redaction/replay maturity are worth reading closely during
  Gate 1** as implementation references — not to import code (license terms unconfirmed
  for either, verify before any code-level borrowing), but because a MIT-licensed transparent
  proxy with working replay already exists and solved the "byte-for-byte transparent stdio
  shim" problem, which is exactly F-01/F-02's scope. Re-deriving that from zero when a
  reference implementation's *approach* (not code) is public is wasted effort.

## Action item logged to CHANGELOG.md

This finding corrects HANDOFF.md's implicit "nothing records MCP traffic" framing. Tracked
as a dated CHANGELOG entry, not a silent edit, per the project's own discipline (CLAUDE.md,
"when something in the spec turns out to be wrong").
