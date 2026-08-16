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

- [ ] `record/schema.py` — Pydantic models, `extra="allow"`, `schema_version="0.1"`
- [ ] `proxy/stdio.py` — passthrough spawn-and-forward
- [ ] `record/writer.py` / `record/reader.py` — kept as separate modules deliberately
  (a shared read/write module invites silent format drift)
- [ ] Raw frame mirroring to `.drifter/raw/`
- [ ] Secret redaction, test-enforced against a fixture with planted fake secrets
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
