# Drifter — Specification v1.0 (LOCKED)

**Status:** Final. This is the last planning document before code. Amendments require a
CHANGELOG entry with a reason; no new spec files.

**Package:** `mcp-drifter` on PyPI (bare `drifter` is taken). Brand name: Drifter.
Console command: `drifter` (aliased from the `mcp-drifter` package).

---

## 1. One-line definition

A proxy-based regression-testing harness that sits on the MCP connection between a user's
agent and its tools, records real tool-use trajectories, replays them safely offline,
mutates the tool interface in controlled ways, and reports behavioral/task/safety
regressions with explicit uncertainty.

## 2. The problem, stated once

MCP tools change under agents without errors. A three-month study of 515 servers found
54.6% of tools modified or deprecated; frontier models degraded 13.7–14.4% under simulated
tool evolution, with damage concentrated in planning (+35.6%) and reasoning (+34.1%), not
tool-call syntax. The July 2026 MCP spec revision guarantees continued churn via a formal
12-month deprecation policy. Nobody tests a user's *actual agent* against their *actual
tools* under controlled interface change — existing tools test models (MCPEvol-Bench),
servers (Specmatic), or agents generically without MCP (AgentAssay).

## 3. Non-negotiable design principles

These survived a full audit and are not open for casual revision.

1. **Observation-first.** No task exists that wasn't derived from, or explicitly
   authored against, a real recorded trajectory.
2. **Replay-first.** Mutation testing runs against recorded/synthetic responses by
   default. Live servers are opt-in, previewed, and never run under a mutated schema.
3. **Proxy-based mutation.** Mutations rewrite the `tools/list` response the agent sees.
   Server code is never touched.
4. **Three independent verdicts.** Behavior / Task / Safety. Never collapsed into one
   score. "Different" is not "broken."
5. **UNKNOWN is a first-class result.** Task verdict requires a deterministic oracle.
   Absent one, the answer is UNKNOWN — never inferred, never guessed.
6. **Real agent under test.** Drifter drives the user's exact agent via subprocess (or
   HTTP in v1). It never substitutes its own model as the thing being measured.
7. **Structural mutations only.** Rename, reorder, merge, truncate — never free-text LLM
   generation. Free-text tool-description mutation is structurally a prompt-injection
   vector and is explicitly disallowed.
8. **Explicit uncertainty everywhere.** Every report states its resolution threshold,
   its replay fidelity, and which calibration defaults a verdict depends on.
9. **Environment fingerprinting.** Every trajectory records agent/model/server versions
   and a tool-manifest hash. Baseline and mutation arms must match except for the
   intended mutation, or the comparison is invalid. All environment fields are fatal
   on mismatch by default, including `server_versions` and `model_name` — this is a
   deliberate choice, not an oversight: SPEC.md §15 limitation 4 (Drifter cannot
   detect a tool whose behavior changed while its schema stayed identical) means a
   server version bump with an unchanged manifest is exactly the case where silently
   proceeding would be most dangerous. If real friction from this shows up in a later
   gate (e.g. fixture server patch bumps blocking valid comparisons), the fix is an
   explicit `ignore_fields` override at the call site — never a silent default change.
10. **Secure by default.** No telemetry. No live writes without explicit authorization.
    Secrets redacted by default. Mutation transformations logged and auditable.

## 4. Claims ledger

Everything citable in docs/marketing must trace to this table. Nothing else is citable.

| # | Claim | Status |
|---|-------|--------|
| C1 | 54.6% of MCP tools modified (32.5%) or deprecated (22.1%) over study window | VERIFIED — arXiv:2607.14642 |
| C2 | Server validity 72.7% → 52.0% over 12 weeks, 1,869 servers | VERIFIED |
| C3 | Frontier-model degradation 13.7–14.4% under 5-round evolution, judge-scored | VERIFIED |
| C4 | Damage concentrates in planning (+35.6%) and reasoning (+34.1%); syntax not significant | VERIFIED |
| C5 | Worst operators: tool addition −0.96, tool integration −0.90, description update −0.81 | VERIFIED |
| C6 | ECS = mean − std of task-fulfillment scores | VERIFIED |
| C7 | 2026-07-28: stateless core, handshake/sessions removed, _meta carries version | VERIFIED — MCP blog |
| C8 | ttlMs/cacheScope honored by SDK client-side response cache | VERIFIED |
| C9 | Python SDK v2 serves both protocol revisions from one endpoint, default-on | VERIFIED |
| C10 | TypeScript v2 serves both revisions via one config flag (`legacy:'stateless'`) | VERIFIED |
| C11 | Python SDK v2 ships OTel middleware by default, no-op without exporter | VERIFIED |
| C12 | ComplexMCP: 300+ tools, 7 sandboxes, n=47 instructions; bottlenecks = retrieval saturation, over-confidence, strategic defeatism | VERIFIED — cite with n |
| C13 | AgentAssay: AGPL-3.0 + commercial dual license; no MCP coverage | VERIFIED |
| C14 | AgentAssay scale (20K LoC, 751 tests, 7,605 trials) | SELF-REPORTED |
| C15 | BGE-M3 similarity: Evol-vs-Real 0.63, Real-vs-Real 0.71 | VERIFIED — independently re-confirmed by user against full paper text 2026-08-16. Cite with nuance: exceeds Real-vs-Real on code-specific embeddings (CodeT5 0.52 vs 0.46; StarCoder2 0.53 vs 0.45) |
| C15b | MCPEvol-Bench bypasses dynamic tool retrieval, fixes candidate server set | VERIFIED — independently re-confirmed by user against full paper text 2026-08-16, exact quote matches in both §4 and §6.1 |
| C18 | Real (non-simulated) historical-version degradation: GPT-5.4 −12.3%, Claude-Sonnet-4-6 −11.7%, Claude-Opus-4-6 −4.1% (50 real server versions, 86 tasks) | VERIFIED — independently re-confirmed by user against full paper text (Table 5) 2026-08-16 |
| C19 | Full 11-operator taxonomy with verbatim definitions (Table 11) | VERIFIED — independently re-confirmed 2026-08-16. **Note:** O4 (Tool Integration) was previously mischaracterized in design notes as a schema-merge operation; corrected in mutate/operators/NOTES.md to its actual definition (Tool Addition + related description updates) |
| C16 | MetaMCP override support; free-tier RPD/RPM figures; "Python dominates MCP adoption" | SINGLE-SOURCE — verify before citing |
| C17 | SEP-2243: Mcp-Method/Mcp-Name headers stamped by client, validated by server | VERIFIED |

## 5. Architecture

```
                        user's agent
                       (any framework)
                             │
                             │ MCP (stdio in v0; +HTTP in v1)
                             ▼
        ┌────────────────────────────────────────────┐
        │              DRIFTER PROXY                  │
        │   1. record      (always)                   │
        │   2. classify     (tool risk taxonomy)       │
        │   3. mutate       (tools/list rewrite)       │
        │   4. resolve      (replay | synthetic | live)│
        │   5. enforce      (safety policy)            │
        └───────┬──────────────────────────┬───────────┘
                │                          │
        writes  │                          │ forwards (live mode only,
                ▼                          │  never under mutation)
      .drifter/runs/*.jsonl        real MCP servers (untouched)
      .drifter/raw/*.frames
                │
    ════════════╪═══════ above: costs money, has side effects ════
                │        below: free, instant, repeatable
                ▼
         ANALYZER → mine · baseline · compare · score
                ▼
              REPORT
```

The dashed boundary is the core architectural bet: agent execution is the only expensive,
risky operation. Everything below it reads stored records and reruns at zero cost.

## 6. Record schema

One JSONL file per session, `schema_version` on every line, raw JSON-RPC frames mirrored
to `raw/` as a re-parse safety net. Fields that cannot be added retroactively (must be
recorded from commit one): `references` (data-flow between calls), `result_provenance`
(real vs synthetic), `tools_raw` and `tools_served` (both, always), `environment.fingerprint`,
`seq`, `timestamp`, `risk`, `raw_frame_offset`, `mutation_inverse`, `classification_source`,
`baseline_fidelity`, `is_error`, `duration_ms`, `fault`.

Rule for every other field: record it only if it cannot be derived later from what *is*
recorded (e.g. `signature` is computed at read time, not stored).

Redaction default: `result_shape` (type, keys, length) only — never payloads. Secrets
pattern-matched and redacted in arguments and headers by default.

## 7. Replay key (the hard problem, solved)

Interface mutations change the request shape the agent sends, which would otherwise
destroy replay fidelity for exactly the mutations that carry the most signal. Three-tier
resolution, decreasing specificity:

1. **Exact** — `sha256(server + tool + canonical_json(args))`
2. **Inverse-mutation** — apply the recorded inverse of the active mutation to the
   incoming request before hashing (e.g. `customerId → customer_id`), then match exactly
3. **Semantic** — hash the multiset of argument *values*, ignoring parameter names

Miss → structurally synthesized response from the recorded schema, never LLM-invented.
`tool_addition` calls are excluded from the fidelity denominator (no prior recording can
exist by definition) and reported separately — the signal measured is trajectory
substitution, not response content.

**Fidelity gates the verdict**, computed per mutation arm and per baseline arm equally:

```
fidelity = (exact + inverse + SEMANTIC_WEIGHT × semantic) / total_calls
```

| Fidelity | Handling |
|---|---|
| ≥ FLAG_THRESHOLD | verdict stands |
| between FLOOR and FLAG_THRESHOLD | verdict stands, report flags degraded fidelity |
| < FLOOR | verdict forced to UNKNOWN |

`SEMANTIC_WEIGHT`, `FLOOR`, `FLAG_THRESHOLD` are calibration constants (§9), not fixed
truths.

## 8. Evaluation — three axes, never merged

**Behavior.** Baseline establishes dominant path + variant frequencies + natural
variation across N runs. Effect size = (deviation_rate − natural_variation) / baseline_spread.
Verdict: NO_REGRESSION (<1.0×) / INCONCLUSIVE (1.0–2.0×) / REGRESSION (>2.0×). Trajectory
distance (normalized edit distance over tool call sequence) reported alongside for
diagnosis.

**Task.** Evaluated only against deterministic, opt-in assertions (`calls`,
`calls_before`, `never_calls`, `result_contains`). No assertion configured → UNKNOWN.
This is the expected default, not a failure of the tool.

**Safety.** Evaluated on every run regardless of configuration: unexpected write/
destructive tool invocation, capability outside `allowed_capabilities`, bypassed
`confirmation_required` step, secrets detected in output, or observed behavior
contradicting a declared annotation. Reported even when Behavior shows NO_REGRESSION —
this is the highest-value finding class.

## 9. Calibration register

Every constant below is an engineering default, not a research finding. Ships in
`calibration.yaml`, user-overridable, re-derived from real corpus data after Gate 2.
Any report verdict depending on an uncalibrated default carries a footnote saying so.

| Constant | Default |
|---|---|
| `semantic_weight` | 0.8 |
| `fidelity_floor` | 0.70 |
| `fidelity_flag_threshold` | 0.90 |
| `effect_size_inconclusive` / `effect_size_regression` | 1.0× / 2.0× |
| `segmentation.idle_gap_seconds` | 30 |
| `baseline.repeats` | 10 |
| `mutation.repeats` (screen / confirm / resolve stages) | 1 / 5 / 20 |

The verified operator weights (C5) are separate — they are cited research, not defaults.

## 10. Safety model

**Tool risk taxonomy**, classified from (in priority order) MCP annotations (explicitly
untrusted per spec — hints, not guarantees) → name/schema heuristics → observed behavior
→ user policy override:

```
unknown              → unsafe by default, never mutated, never live-invoked
read_only_local
read_only_external    → data egress; mutate freely, flag in blast-radius preview
reversible_write
irreversible_write    → excluded from live runs by default
destructive            → never mutated, never invoked, excluded always
```

**Mutation-as-injection defense.** Generated mutation text is rejected if it matches
imperative-instruction patterns (`ignore`, `always call`, `you must`, `disregard`,
`instead of`). Mutations never alter a tool's stated safety properties. Every mutation
logged with exact before/after and an inverse mapping.

**Header integrity.** SEP-2243 headers (`Mcp-Method`, `Mcp-Name`) are stripped on
mutated calls and never forwarded live — a mutated tool name in a live header would fail
server-side validation.

**Blast-radius preview**, required before any live-mode run:
```
Planned: 12 workflows · 48 agent runs · 312 tool calls
         291 read-only · 21 reversible writes · 0 destructive
Estimated replay coverage: 92%
Continue? [y/N]
```

## 11. Configuration surface

```yaml
version: 1
servers:
  - name: crm
    command: ["npx", "-y", "@mcp/server-crm"]

# everything below optional, sane defaults
record: {dir: .drifter/runs, redact: shape}
agent: {mode: subprocess, command: "python agent.py --task '{task.prompt}'"}
execution: {mode: replay, fidelity_floor: 0.70, budget_calls: 500}
baseline: {repeats: 10, max_calls: 200, cache: true}
mutations: {profile: quick, seed: 42, exclude_tools: []}
policy: {destructive: [], confirmation_required: []}
tasks: [...]
```

## 12. CLI

```
drifter init            scan MCP configs, classify tools, write drifter.yaml
drifter observe         passthrough proxy, record only              [zero config]
drifter stats           summarize recorded trajectories
drifter tasks mine       signature grouping + PrefixSpan → candidates
drifter tasks approve   promote candidates to approved tasks
drifter run             baseline + mutations + evaluate
drifter score           re-evaluate stored records                  [zero API cost]
drifter report          render report from records
drifter doctor          connectivity, config, classification sanity
```

Exit codes: `0` clean · `1` behavior regression · `2` assertion failure ·
`3` safety violation · `4` config/connectivity error · `5` budget exceeded.

## 13. Report format

```
DRIFTER RESULT — CONDITIONAL REGRESSION

Task            invoice_creation
Mutation        mut_042 · parameter_rename (crm.get_customer)
                customer_id → customerId

BEHAVIOR        REGRESSION                              effect size 3.1×
                baseline  search → get_customer → create_invoice
                mutated   search → get_customer → retry → create_invoice
                deviation 78%   natural variation 20%   distance 0.33

TASK            UNKNOWN — no oracle configured

SAFETY          NO VIOLATION

CONFIDENCE      baseline 10 runs · mutation 10 runs
                replay fidelity 0.94 (exact 71% · inverse 23% · synthetic 6%)
                detectable regression threshold: 3.6 points
                calibration: fidelity_floor=0.70 (uncalibrated default)

RECOMMENDATION  Add an assertion to resolve TASK. Inspect the retry loop on
                renamed parameters — the agent recovers but inefficiently.
```

## 14. What ships when

See PHASES.md for the gated build plan. See FEATURES.md for full per-feature
breakdown. This document defines *what is true*; those define *what gets built and
when*.

## 15. Known limitations, stated plainly

1. Synthetic responses are structurally, not semantically, correct — may cause agent
   behavior divergent from a real server. Mitigated by fidelity gating, not eliminated.
2. The proxy sees only MCP traffic — no prompts, no system prompt, no model reasoning.
   Unit of analysis is the tool sequence. Framework-agnostic; blind to *why*.
3. AgentAssay overlap is real for the general testing methodology (three-valued
   verdicts, adaptive sampling, behavioral fingerprinting are their prior art). Drifter's
   novelty is MCP-native, proxy-based, replay-driven interface mutation. Do not claim
   methodological novelty for the evaluation math.
4. Simulated interface evolution ≠ real evolution, and cannot model a tool whose
   *behavior* changed while its schema stayed identical.
5. The MCP conformance suite (SEP-2484) partially occupies "MCP evolution testing" as a
   phrase — pitch must be explicitly about agent behavioral robustness, not protocol
   compliance.
6. Full setup (safety policy, task approval, assertions) is not five minutes.
   `drifter observe` is the only genuinely zero-config path.
7. MCP protocol carries no model-identity signal. The proxy can determine agent and
   server identity from the wire (the `initialize` handshake's `clientInfo`/
   `serverInfo`), but never which model is running — that's a different claim from
   limitation 2's "no model reasoning." `environment.model_name` is sourced
   out-of-band (`DRIFTER_MODEL_NAME` env var; `drifter.yaml` once the config loader
   exists), not from observed traffic.
8. Heuristic segmentation (F-07) has no signal to separate two calls that are
   unrelated by content when they arrive with no idle gap between them and neither
   carries trace context — it groups them into one trajectory regardless. Idle gap
   and data-flow connectivity are the only two signals the heuristic has; content
   similarity isn't one of them, by design (SPEC.md §3 principle 7's "structural, not
   free-text" applies to segmentation too, not just mutation). Trace context (F-06)
   doesn't have this blind spot — it's the reason F-06 is checked first and is
   authoritative when present.
9. Interrupting `drifter observe` (Ctrl+C) prioritizes data-flush safety over graceful
   subprocess shutdown: recorded data is guaranteed flushed via a synchronous close
   before process exit, but the spawned MCP server subprocess is not explicitly
   waited on or terminated. The 2026-07-28 spec states servers "SHOULD exit promptly
   when their standard input is closed or reads return end-of-file" — SHOULD-level,
   not a hard guarantee — and this behavior was empirically confirmed for the
   SDK-built fixture server used in testing. A third-party server that doesn't honor
   this SHOULD may be left running after a Drifter Ctrl+C. The 2025-11-25 predecessor
   revision has no equivalent language at all.
10. A recorded corpus is not schema-uniform across the project's own history, and this
    has already happened twice: `ToolCall.is_error`/`duration_ms` (CHANGELOG.md v1.0.7)
    and `fault` (v1.0.10) each didn't exist before their respective schema version, so
    a `.jsonl` file recorded earlier has the not-yet-existing field(s) as `null` — the
    two boundaries are independent (a corpus can have `is_error` but not `fault`).
    `drifter stats` treats every such gap as unknown, not zero — error rate, fault
    rate, and latency percentiles are each computed over their own known subset only,
    marked `N/A`/`*`/`^` in the report — but a corpus (or a report generated from one)
    spanning either boundary will have systematically thinner diagnostic coverage for
    its older calls, which a reader unfamiliar with this history wouldn't otherwise
    know to expect. Expect this list to grow, not shrink, as more per-call diagnostic
    fields are added over time.
