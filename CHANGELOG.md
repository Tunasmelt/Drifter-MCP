# Drifter — Specification Changelog

Tracks how SPEC.md, FEATURES.md, and PHASES.md arrived at their current state.
Written so future amendments follow the same discipline: every change gets a reason,
not just a diff.

---

## v1.0.3 — Independent re-verification catches an interpretation error

**Change:** User independently fetched and pasted the full MCPEvol-Bench paper text
(not search snippets, not a single-pass fetch) specifically to verify C15b, C18, and
C19 rather than trusting the prior session's single verification pass. Every direct
quote checked out exactly. One interpretation built on top of a correct quote did
not.

**What was actually wrong:** `mutate/operators/NOTES.md` described Operator 4 (Tool
Integration) as merging two tools' schemas together, and used that description as the
stated reason to defer it from Gate 3 to v1 (claimed no clean replay-key inverse
existed for a schema merge). The paper's actual Table 11 definition, extracted for
the first time during this re-verification pass, says something different: Tool
Integration is "adds a new tool and refines related tool descriptions" — no merge at
all. It's compositionally Tool Addition (F-17) plus description-only updates to
adjacent tools, both of which are already scoped for Gate 3. The deferral reasoning
was built on an unverified assumption about what "Integration" meant, not on the
paper's actual definition.

**Why this matters more than a typo fix:** this is exactly the failure mode the
claims ledger (§4) and Gate 0's verification discipline exist to catch — not
fabricated quotes, but *correct quotes with an incorrect inference layered on top*,
which is harder to catch because the citation itself checks out under casual review.
It was caught here specifically because the user re-verified independently rather
than accepting the prior session's "VERIFIED" label at face value — which is the
correct response to any claims-ledger entry, including this project's own.

**Also corrected:**
- Full verbatim definitions for all 11 operators (previously only 6 had definitions,
  via their scored-subset numbers; the other 5 had names only) — added to
  `mutate/operators/NOTES.md` from Table 11.
- Corroborating evidence added: the paper's own mutation prompts (Appendix H.1)
  enforce a "Schema Immunity" constraint nearly identical to SPEC.md principle 7
  (structural mutations only) — cited as prior-art support for that design decision.
- Noted, for the record, a cross-reference error in the source paper itself (§6.3
  cites "Table 6" for data that is actually in Table 5) — not a Drifter error, logged
  so it isn't later mistaken for one.

**Process note:** the user's PyPI-adjacent question earlier in this session — "is
this really you, or was something tampered with" — and this independent
re-verification are the same discipline applied twice. Both were correct instincts.
Nothing in this project should be trusted at "VERIFIED" status, including entries
this project's own process produced, without an independent check when the stakes of
being wrong are high enough to warrant one.

---

## v1.0.2 — Gate 0 findings applied

**Change:** Gate 0's competitor survey and paper re-verification (PHASES.md items 1 and 3)
produced two corrections and one new finding, all applied before Gate 1 scaffolding, per
CLAUDE.md's directive that gate discipline is checked, not assumed.

1. **The recorder landscape is not empty — HANDOFF.md's framing was too strong.** Search
   found mcp-tape, mcpsnoop, mcpscope, and MockServer's AI Traffic Inspection, all doing
   some form of MCP traffic recording as of 2026. None meet Gate 0's kill-criterion bar
   (none combine trace segmentation, data-flow references, environment fingerprinting,
   redaction-by-default, AND mutation-aware replay — see GATE0_COMPETITOR_SURVEY.md), so
   Gate 1 proceeds as planned. But the public-facing claim narrows: not "nobody records MCP
   traffic" but "no existing recorder supports mutation-aware replay." HANDOFF.md's
   competitive framing should be read as superseded by GATE0_COMPETITOR_SURVEY.md.
2. **Two claims ledger entries (C15) were incorrectly marked UNVERIFIED.** Direct
   verification against the MCPEvol-Bench paper text (not just search snippets) confirms
   both the BGE-M3 similarity figures and the dynamic-tool-retrieval-bypass claim are
   actually stated in the paper. Corrected to VERIFIED, with the BGE-M3 citation now
   including the nuance that Drifter's earlier flat "0.63 vs 0.71" framing would have
   omitted (code-specific embeddings show the opposite pattern).
3. **New evidence found, not previously in the claims ledger (C18, C19):** real (not
   simulated) historical-version degradation numbers, and the full 11-operator taxonomy
   with all three hierarchy levels named, extracted to `mutate/operators/NOTES.md` per
   Gate 0's required deliverable.

**Why this matters beyond the specific corrections:** this is the audit discipline from
CHANGELOG.md's v1.0 entry catching something *during* Gate 0 rather than being applied
retroactively after an error shipped. That's the intended function of Gate 0 existing at
all — it worked on the first real check.

---

## v1.0.1 — Security gaps closed pre-code

**Change:** A design-level security review (applying `/security-check`'s threat
categories to SPEC.md, since no code exists yet to run the skill against directly)
found two gaps in the build plan. Both fixed in PHASES.md; full reasoning in the new
SECURITY.md.

1. **`.drifter/` had no `.gitignore` specification.** Recorded trajectories reveal
   internal tool names, server topology, and usage patterns even after F-04's
   secret-value redaction — that's a repo-hygiene exposure, not a redaction bug.
   Gate 1's task list now requires the `.gitignore` entry in the same commit that
   creates the directory, and Gate 1's exit test now checks `git status` is clean
   under `.drifter/` after a full observe session.
2. **No dependency-vulnerability scanning was specified anywhere in the plan.**
   Gate 1 is where `pyproject.toml` first exists, so it's where this needed to be
   specified. `pip-audit` (or `uv`'s equivalent) now runs in CI from the first commit
   with a dependency tree, gating the build on high/critical CVEs, added to Gate 1's
   task list and exit test.

**Why this is a `.0.1` bump, not a new major revision:** neither change touches
SPEC.md's architecture, invariants, or claims ledger — both are additions to
PHASES.md's Gate 1 checklist plus a new standalone SECURITY.md. DEC-026 (no new
planning documents before Gate 1 code exists) is not violated by SECURITY.md, since
it documents a security *posture*, not a new architectural plan — the same
distinction that lets this changelog itself exist without violating that rule.

---

## v1.0 — Locked specification

**Change:** Consolidated three prior planning passes (initial design conversation,
research memo, independent Perplexity direction doc) plus a full audit pass into one
locked spec, one feature breakdown, and one gated phase plan. Declared this the final
planning document — DEC-026 (below) forbids further spec documents before Gate 1 code
exists.

**Why now, not sooner:** an audit of the full project conversation found errors
accumulating at a rate of roughly one material mistake per major synthesis round
(see "Corrections applied" below). Each individual round felt like progress; the
audit's job was to check whether that feeling was earned. It mostly was, on the core
architecture — but not on several specific numbers and claims, which needed fixing
before they hardened into assumed fact.

### Corrections applied from the audit

| # | What was wrong | What it's fixed to |
|---|---|---|
| 1 | AgentAudit described as doing "continuous schema tracking" | It's a security vulnerability scanner; schema-drift detection is Specmatic's territory, not theirs |
| 2 | ComplexMCP's finding renamed "Clean-Slate bias" in earlier notes, then "corrected" to "over-confidence" as if the original term was wrong | Both terms actually appear in the paper — "Clean-Slate bias" in the conclusion, "over-confidence" in the abstract. Neither correction was needed; flagging this because the *correction* was itself unverified |
| 3 | BGE-M3 similarity figures (0.63 vs 0.71) cited as fact | Never found in the paper extraction. Marked UNVERIFIED, moved out of anything citable |
| 4 | "MCPEvol-Bench bypasses dynamic tool retrieval" stated as verified | It's a reasonable inference from methodology, not a confirmed claim. Downgraded to UNVERIFIED |
| 5 | TypeScript's dual-protocol-revision support framed as "you'd have to build it yourself, ~2 weeks of work" | Verified: TypeScript v2 serves both revisions via one config flag (`legacy:'stateless'`), not a from-scratch build. Python's real advantage is *default-on* vs *one flag*, not *free* vs *two weeks*. The Python decision was re-justified on its remaining, still-valid grounds (default OTel middleware, `uvx` distribution, ecosystem gravity) rather than quietly patched |
| 6 | Free-tier API numbers (e.g. "~1,500 requests/day") corrected once mid-conversation, then reused uncorrected in later cost arithmetic | Order-of-magnitude conclusions held; specific figures shouldn't have been reused after being flagged as stale. Marked SINGLE-SOURCE / verify-before-citing in the claims ledger |
| 7 | ComplexMCP's "300+ tools" cited without its task-count caveat | The benchmark's headline success-rate finding (<60% vs 90% human) rests on a curated set of 47 instructions — a deliberate determinism trade-off per the paper. Now cited with `n` attached |
| 8 | AgentAssay's scale claims (20K LoC, 751 tests, 7,605 trials) treated as established fact | These are self-reported by a solo researcher's own README/paper. Downgraded to SELF-REPORTED in the claims ledger — not dismissed, just not independently verified |
| 9 | Package name assumed available without checking | Checked directly: `drifter` is taken on PyPI (v0.0.3 exists). Package renamed to `mcp-drifter`; brand name unchanged |

### Structural changes made in response to the audit (not corrections, additions)

- **Claims ledger** (SPEC.md §4) — every citable fact now carries a verification
  status. Nothing outside the table is citable in docs or marketing. This exists
  because the audit's error rate was found via ad hoc re-checking; the ledger makes
  that checking systematic and permanent rather than a one-time cleanup.
- **Calibration register** (SPEC.md §9) — every invented constant (fidelity floor,
  segmentation idle-gap, baseline repeat count, etc.) is now explicitly labeled as an
  engineering default, not a research finding, and lives in an overridable config
  file separate from the verified operator weights.
- **Baseline fidelity gating** (F-22) — closed a gap where the audit found the
  *mutation* arm's replay quality was checked but the *baseline* arm's wasn't,
  meaning a contaminated reference distribution could silently widen the detection
  threshold and suppress real findings.
- **stdio-first onboarding story** — the "change one URL line" pitch (from an earlier
  round of the conversation) described HTTP transport, while the actual build plan
  started with stdio. Reconciled: v0 pitch is "wrap the command," v1 pitch is
  "swap the URL," and the docs will say whichever matches what's actually shipped.
- **Header integrity on mutated calls** (F-20) — the audit's fresh verification pass
  surfaced that MCP's SEP-2243 header scheme (`Mcp-Method`/`Mcp-Name`, stamped by the
  client, validated by the server) would reject a live-forwarded call whose tool name
  had been mutated. Closed by never forwarding mutated calls live at all, and
  stripping the headers as a defense in depth.
- **Gated build plan replacing open-ended phases** (PHASES.md) — each gate now has an
  explicit kill criterion, not just a success criterion. Written because the audit's
  single biggest structural finding was that the project's real risk had stopped
  being "wrong technical decision" and become "infinite correct decisions, zero code"
  — three planning documents in a row grew scope rather than shrinking it.
- **DEC-026: no further spec documents before Gate 1 code exists.** This changelog
  entry is itself compliant with that rule — it documents changes to the locked spec,
  it does not introduce a new one.

### What did NOT change

The three-axis verdict model (Behavior/Task/Safety, never collapsed), replay-first
execution, proxy-based interface-only mutation, UNKNOWN as a first-class result, the
irreversible-fields list in the record schema, and the three-tier replay key design
all survived the audit with their logic unchallenged — only their supporting
constants and evidence citations were checked and, where needed, corrected. Stability
under scrutiny is itself informative: these are treated as settled, not because they
weren't checked, but because they were checked and held.

---

## v0.2 — Post-audit revision (superseded by v1.0)

Introduced the claims ledger, calibration register, and gated phase structure for the
first time, in response to the audit described above. Folded into v1.0 without
further changes to its substance — v1.0 primarily reorganized v0.2's content across
SPEC.md / FEATURES.md / PHASES.md / HANDOFF.md instead of one combined document, and
added the feature-level breakdown (F-01–F-37) that v0.2 didn't yet have.

---

## v0.1 — Initial locked spec (superseded)

First attempt at freezing scope after three prior unstructured planning rounds
(the original architecture conversation, a research memo cross-checking claims
against arXiv sources, and an independently produced Perplexity direction document).
Introduced: the proxy architecture, the record/replay separation as the central cost-
control mechanism, the three-tier replay key concept (informally), and the
two-week-proof-of-concept framing that later became Gates 1–3.

Superseded because a subsequent audit pass found the corrections listed above, and
because v0.1's "v1 scope" had already grown across its own three source documents
in a way that needed structural (not just factual) correction.

---

## Pre-spec history (not versioned)

Prior to v0.1, the project existed only as conversational exploration: the original
problem framing (agents degrading under tool schema drift), evaluation of adjacent
research (MCPEvol-Bench, ComplexMCP), architecture and tech-stack discussion (Python
vs Rust vs TypeScript vs Go), and free-tier infrastructure planning. Useful context,
not load-bearing — anything from that period that mattered is now either in SPEC.md
or explicitly retracted above.
