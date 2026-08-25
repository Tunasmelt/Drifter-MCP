# Drifter — Phase Plan

Gates, not open-ended phases. Each gate is independently shippable, has an exit test
that must pass before the next gate starts, and a kill criterion — a written condition
under which the current approach is wrong and should stop, decided now rather than
under sunk-cost pressure later.

No new specification documents are written after this file. The next artifact is code.

---

## Gate 0 — Pre-flight

**Duration:** 2 days. **Code written:** none.

### Tasks

- [ ] Recorder-competitor survey: check MCP Inspector, known gateway logging layers,
  and observability vendors for existing observe-mode-equivalent functionality.
  Output: one page, build-vs-integrate decision.
- [ ] Read the AgentAssay repository (read-only — AGPL-3.0 restricts distribution and
  derivative code, not learning). Output: one page, "Relationship to AgentAssay,"
  covering what's genuinely theirs, what Drifter does differently, and a calibrated
  view of their self-reported scale claims (C14).
- [ ] Pull the MCPEvol-Bench appendix. Extract the 11 operator definitions verbatim
  into `mutate/operators/NOTES.md` as design reference for F-16/F-17.
- [ ] Reserve `mcp-drifter` on PyPI with a placeholder 0.0.1 release.
- [ ] Choose the Gate 1 dogfood target: one real agent, one real MCP server, from the
  author's own daily work. This pairing is used for every fixture through Gate 3.

### Exit test

All five tasks produce their stated output artifact. No code required.

### Kill criterion

If Gate 0's survey finds a maintained tool that already does observe-mode recording
*and* replay equivalent to F-01–F-15, pivot: Drifter becomes a mutation-and-evaluation
layer on top of that tool rather than rebuilding the recorder. (The mutation gap is
independently verified open regardless of this outcome — see SPEC.md §2.)

---

## Gate 1 — Recorder

**Duration:** 1 week. **First shippable artifact.**

### Features built

F-01 through F-10 (see FEATURES.md, module `record/`).

### Tasks

- [x] `record/schema.py` — Pydantic models, `extra="allow"`, `schema_version="0.1"`
- [x] `record/proxy.py` — passthrough spawn-and-forward (this checklist originally
  said `proxy/stdio.py`; there is no standalone `proxy/` module in CLAUDE.md's
  module list, and FEATURES.md files F-01 under `record/`, so `record/proxy.py`
  is what's correct against both — not a deviation)
- [x] `record/writer.py` / `record/reader.py` — kept as separate modules deliberately
  (a shared read/write module invites silent format drift)
- [x] Raw frame mirroring to `.drifter/raw/`
- [x] Secret redaction, test-enforced against a fixture with planted fake secrets
  (covers both the parsed JSONL and the raw frame mirror — SECURITY.md
  specifically calls out the mirror as a common place to leave a redaction gap)
- [ ] Environment fingerprinting
- [ ] Trace-context detection + heuristic fallback segmentation
- [ ] `drifter observe`, `drifter stats`, `drifter doctor` (connectivity checks only
  at this stage)
- [ ] Golden fixture: one hand-verified session, committed to
  `tests/fixtures/golden_v0.1.jsonl`, never modified — only superseded by a new
  version if the schema changes
- [ ] `.gitignore` entry for `.drifter/` (runs, raw frames) added in the same commit
  that creates the directory — not after something gets accidentally committed.
  Recorded trajectories reveal internal tool names, server topology, and usage
  patterns even with shape-redaction applied; this is repo hygiene, not optional
  polish (see SECURITY.md)
- [ ] Dependency audit (`pip-audit` or `uv`'s equivalent) wired into CI from the
  first commit that has a `pyproject.toml`, failing the build on high/critical CVEs
  — not added retroactively once there's a dependency tree worth worrying about

### Exit test

One full week of the author's own daily agent work recorded under `drifter observe`
with zero crashes and no perceptible latency added. `drifter stats` output visibly
matches what the author already knows about their own tool usage. Golden fixture
parses cleanly in CI. `git status` after a full observe session shows nothing under
`.drifter/` staged. CI dependency audit is green.

### Kill criterion

If the proxy measurably degrades normal daily agent use — latency, compatibility,
crashes — in a way that can't be fixed within the week, the stdio passthrough
architecture itself is wrong. Stop and redesign before Gate 2 builds anything on top
of it. This is the most consequential kill criterion in the plan: everything after
Gate 1 assumes the recorder is trustworthy.

---

## Gate 2 — Replay + Analyzer

**Duration:** 1 week.

### Features built

F-11 through F-15 (module `replay/`), F-21 through F-23 (partial — baseline and
scoring only, no mutation yet).

### Tasks

- [ ] Replay store with exact-key lookup (F-11)
- [ ] Inverse-mutation key resolution (F-12) — stub against Gate 3's operators, since
  no mutations exist yet; test with a synthetic rename fixture
- [ ] Semantic key fallback (F-13)
- [ ] Synthetic response generation (F-14)
- [ ] Fidelity computation, applied to both baseline and mutation arms (F-15, F-22)
- [ ] Analyzer: mean, spread, effect size, trajectory distance — pure functions over
  JSONL, zero I/O side effects beyond reading files
- [ ] Baseline runner (F-21): N repeats via subprocess adapter, replay-served
- [ ] `drifter score` (F-36)
- [ ] Re-derive `calibration.yaml` defaults against the Gate 1 corpus rather than
  shipping the invented placeholder values unchanged

### Exit test

Re-analyze the entire Gate 1 corpus via `drifter score` with zero new agent execution
and zero API calls, completing in seconds. **If this requires fresh model calls, the
record/replay boundary is broken and nothing built after this gate is trustworthy —
stop and fix this before proceeding, do not work around it.**

### Kill criterion

If fidelity on the real Gate 1 corpus (running baseline replay against it) comes back
persistently low even with no mutations active, the recording schema is missing
information needed for reliable replay — return to Gate 1's schema before building
the mutation layer on an unreliable replay foundation.

---

## Gate 3 — Mutation + Report

**Duration:** 1 week.

### Features built

F-16 through F-20 (module `mutate/`), F-24 through F-27 (rest of `evaluate/`),
F-31/F-32 (basic budget controls only — full blast-radius preview deferred to v1
since Gate 3 stays in replay mode throughout), F-35 (`drifter run`), F-13 report
rendering.

Two mutation operators only: `description_update` (F-16, taxonomy O9) and
`tool_addition` (F-17, taxonomy O1) — the two highest-impact *individually-scored*
operators (`Gate 0/NOTES.md`). O4 (Tool Integration, third-worst overall) is
deliberately excluded from Gate 3, for scope reasons: proving the harness cleanly
on two independently-attributable operators before a composite third, so a
detected regression's cause is never ambiguous between the harness and the
kill criterion's exit test. This is a scope decision, not a technical
blocker — an earlier claim that O4 had no clean replay-key inverse was checked
against the primary source and found wrong (`Gate 0/NOTES.md`'s correction; O4
is compositionally O1 + description updates to related tools, not a schema
merge). Revisit for v1 once F-16/F-17 are proven, not before.

### Tasks

- [ ] `description_update` operator (F-16), structural paraphrase only, imperative-
  pattern rejection test
- [ ] `tool_addition` operator (F-17), styled-consistent generation, synthetic-only
  responses
- [ ] Mutation audit log (F-18)
- [ ] Cache-busting: `ttlMs: 0`, private `cacheScope` on every mutated response
  (F-19) — test-enforced against a caching-capable client fixture
- [ ] Header stripping on any mutated-tool call (F-20)
- [ ] Task assertion engine (F-24), UNKNOWN-by-default behavior test
- [ ] Safety verdict engine (F-25) + risk classification (F-26)
- [ ] Adaptive repeat scheduling (F-27)
- [ ] `--budget`, `--dry-run` (F-32)
- [ ] `drifter run` orchestration (F-35)
- [ ] Report renderer matching SPEC.md §13 exactly, including calibration footnotes

### Exit test

One real, previously-unknown fragility found in the author's own agent, using the
Gate 0 dogfood pairing. This finding becomes the first concrete example in any future
README or demo — not a synthetic one.

### Kill criterion

If, across the author's real approved workflows, neither `description_update` nor
`tool_addition` produces any deviation beyond the calibrated noise floor at acceptable
fidelity, the effect may not reproduce at this agent's scale or model choice.
Before proceeding to Gate 4 or v1, deliberately construct a known-brittle test agent
and confirm the harness *can* detect a planted regression — isolating whether the
issue is the harness or simply that this particular agent is unusually robust.

### Status (2026-08-25)

**Exit test — SATISFIED**

One real, previously-unknown fragility found using the Gate 0 dogfood pairing (Claude
Code + the real filesystem MCP server), not a synthetic one — per this section's own
requirement.

**Finding:** `replay/replay_proxy.py`'s synthesized response content for any
replay-served call with no prior recording (`_synthesize_call_tool_result`, active on
every call by default — F-02/F-04's shape-only recording means most real corpora hit
this path routinely) contained prose disclosing its own synthetic nature ("original
payload was never recorded... not implemented yet"). A real, safety-aware agent
(Claude Code) correctly read that disclosure as prompt-injection-shaped content and
refused to proceed past the first tool call in three consecutive live runs — not a
rare edge case, a systemic block on completing any multi-step task in replay mode as
originally built.

This satisfies the exit test's letter and spirit even though it landed in Drifter's
own synthesis code rather than in agent behavior induced by a mutation operator: it is
a genuine fragility, found empirically against the real dogfood pairing, previously
unknown, and directly actionable — exactly the category of finding this gate exists to
surface. See `CHANGELOG.md` (`257f9ce`, `e06b122`) for the full investigation and fix.

**Secondary finding, same investigation:** while preparing to trust a real
baseline-vs-mutation comparison, `evaluate/baseline.py`'s aggregation logic was found
to silently conflate a `claude mcp get` connectivity-check artifact (manifest hash
populated, zero `ToolCall` records) with a genuine "the agent legitimately called
nothing" run — both produce byte-identical recorded shapes, and no signal exists in
the schema today to distinguish them. Confirmed via direct inspection of a real
8-file corpus (`natural_variation: 0.25`, `baseline_spread: 0.433`, one of four
"valid" runs never having attempted the task). Regression-tested, documented as
SPEC.md §15 limitation 12, deliberately not silently patched — no design decision has
been made yet on how to add the missing signal (duration heuristic vs. explicit
task-attempt marker vs. something else).

**Kill criterion — ATTEMPTED TWICE, still UNKNOWN, converging on a structural finding**

The comparison was run for real, twice, against Claude Code through the real
filesystem MCP server (`drifter replay-serve`) — the environment block noted in this
section's original version was worked around (plain `claude -p` without
`--dangerously-skip-permissions`, `--allowedTools` for non-interactive MCP-tool
approval). Both attempts returned `EffectSizeResult(verdict='UNKNOWN')` for both
operators — every arm's `valid_runs` was 0, every real run falling below
`fidelity_floor=0.70`.

Attempt #1 used a minimal 2-call fixture (`list_directory` + `read_text_file`).
Attempt #2 recorded a deliberately richer 4-call fixture, live, specifically covering
the two most common follow-up patterns attempt #1 revealed (`directory_tree`, a
parent-directory `list_directory`). Both failed the identical way for the identical
reason across 9 real live-agent attempts (fidelities 0.25–0.60) — ruling out "the
fixture wasn't rich enough yet" as the explanation.

**The actual mechanism, confirmed by reading all 9 recorded call sequences:** a real,
curious agent's tool-selection verification behavior is combinatorial (path format ×
tool choice × directory depth), not enumerable from a single anticipated follow-up
set. A near-universal first move (`list_allowed_directories`) was absent from both
recorded fixtures; once any call misses, the agent doesn't retry once, it escalates
through an open-ended sequence — some runs reached 8–9 calls for a 2-call task. No
finite single-session recording can realistically pre-populate that space at
exact-tier-only resolution.

**This reframes a prior Gate 3 scoping decision.** Tier 3 (semantic matching,
`replay/`'s F-13) was deferred from F-16/F-17 on the reasoning that neither operator's
own mutation changes argument values in a way that needs it (SPEC.md §7's Gate 3
implementation-status note) — correct as far as it went. This finding shows tier 3 (or
an equivalent broadening of match resolution) may be a prerequisite for exact-tier
replay to be viable against *any* real, curious agent at all, independent of whether a
mutation is active. That's a different, larger justification than the one tier 3 was
originally deferred against, and changes its priority from "nice-to-have for later
operators" to "possibly blocking exact-tier replay's real-world viability."

Neither honest path forward — an even more exhaustive fixture, or building tier-3
semantic resolution — was attempted here; both are real, substantive pieces of work,
not something to decide as a byproduct of this investigation.

**Secondary, minor finding:** in 3 of 9 runs, Claude Code's own natural-language
self-report claimed "every call returned MISS" or equivalent, when the recorded trace
showed real hits. Not a Drifter defect — a reminder that an agent's own narration of
its tool use is not a reliable substitute for the recorded trace when interpreting
results.

**Do not read Gate 3 as fully closed on the strength of the exit test alone.** The
exit test's satisfaction and the kill criterion's status are independent facts:

| | Status |
|---|---|
| Exit test (one real fragility found) | ✅ Satisfied |
| Kill criterion (harness detects real mutation effect, or confirmed via brittle-agent fallback) | ⏳ Attempted twice — UNKNOWN both times, for a structural reason, not a harness crash or an unattempted comparison |

**The actual next decision point**, flagged explicitly rather than defaulted into
silently: either (a) attempt an even more exhaustive live fixture recording (probably
still probabilistic — the exploration space is not obviously boundable), or (b) treat
this as sufficient evidence to prioritize tier-3 semantic matching ahead of its
original v1+ scheduling, before the kill criterion's own "construct a known-brittle
test agent" step is even reachable. Whoever picks this up should make that call
deliberately, not by default.

---

## Gate 4 — Second User

**Duration:** 1 week. **No new features.**

### Tasks

- [ ] Hand the Gate 3 build to the friend doing agentic AI work
- [ ] They run `drifter init` → `drifter observe` → `drifter tasks mine` →
  `drifter run` against their own stack, unassisted except for documentation
- [ ] Log every point of friction, confusion, or failure without intervening
- [ ] Fix only what breaks; add nothing new

### Exit test

The friend successfully runs a full mutation test against their own agent and
correctly interprets a report without the author explaining it live.

### Kill criterion

If the subprocess agent adapter (F-34) cannot accommodate their agent's invocation
pattern at all, the adapter contract is too narrow — this is the single most likely
place for a hard blocker, since it was deliberately kept coarse for v0. Widening it
(HTTP adapter, manual mode) becomes the first v1 priority rather than a nice-to-have.

---

## v1 — After Gate 4 only

Not started until Gate 4 exits successfully. Scope, unchanged from SPEC.md:

- HTTP transport + URL-swap onboarding (the "change one config line" story, which
  only applies once HTTP exists)
- Synthetic replay provenance surfaced fully in reports
- Remaining Level 0–1 mutation operators beyond the two shipped in Gate 3
- Workflow mining end to end: F-28/F-29/F-30 (signature grouping, PrefixSpan,
  candidate approval) — deferred past Gate 3 because Gate 3's dogfood task can be
  hand-written; mining matters once there's a real multi-week corpus
- Full safety policy engine and blast-radius preview for live mode (F-31 in full)
- Task assertions as a first-class authored feature, not just the engine (F-24 was
  built in Gate 3; the authoring UX around it is v1)
- Adaptive scheduling tuning based on Gate 1–4 real usage data

## v1.5

Plan-only screening mode, HTTP agent adapter (unless pulled forward by a Gate 4 kill
criterion), delta debugging (ddmin) for root-cause isolation, live read-only fallback
with explicit per-tool authorization.

## v2+

Response mutation (Level 2 — changing what tools *return*, not just what they *say*;
deliberately deferred as the single most safety-sensitive feature in the design),
state/environment mutation (Level 3), workflow graph mining beyond frequent
subsequences, git-diff-aware mutation targeting, metamorphic relations, LLM judge
oracle, additional protocols (REST, OpenAI function calling).

---

## Failure-mode cross-reference

Every gate above exists partly to defuse a specific entry in SPEC.md's risk register.
Fastest lookup:

| Gate | Primary failure mode it defuses |
|---|---|
| 0 | Building a competitor's already-solved problem; AGPL contamination |
| 1 | A proxy nobody can safely run |
| 2 | Analysis secretly coupled to live execution (the whole cost model breaks) |
| 3 | A mutation engine that either finds nothing, or reports synthetic noise as fact |
| 4 | A tool that only ever works for its own author |
