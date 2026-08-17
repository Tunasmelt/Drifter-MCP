# Drifter — Specification Changelog

Tracks how SPEC.md, FEATURES.md, and PHASES.md arrived at their current state.
Written so future amendments follow the same discipline: every change gets a reason,
not just a diff.

---

## v1.0.11 — 2026-08-17 — CI dependency audit fixed: `--strict` failed on every run, not just vulnerable ones

**Change:** Found during Gate 1 exit-test verification, not previously exercised — this
repo has no git remote and had never been pushed, so `.github/workflows/ci.yml`'s
dependency-audit step had never actually run. Verified locally, exactly as CI would
run it: `uv run --with pip-audit pip-audit --strict` fails unconditionally —
`ERROR: mcp-drifter: Dependency not found on PyPI and could not be audited:
mcp-drifter (0.1.0)`, exit 1 — because `pip-audit` audits every installed package
including the project's own editable-installed local package, `--strict` treats "can't
be looked up on PyPI" as a hard failure, and this project's own unpublished-at-0.1.0
package can never be looked up on PyPI. This isn't a transient or environment-specific
failure: it reproduces identically on every run, so as written this step would never
once turn green, on any commit, regardless of whether a real dependency vulnerability
exists — the opposite of PHASES.md's stated goal ("failing the build on high/critical
CVEs"), since a permanently-red check gets ignored rather than trusted.

**Fix:** `--skip-editable` (excludes the local project package from the audit,
verified it still exits 0 with "No known vulnerabilities found" for the real third-
party dependency tree) in place of `--strict` (which specifically fails on audit
*collection* failures, not just found vulnerabilities — verified the two flags
together still fail on the same editable-package error, so this isn't just "add
skip-editable," `--strict` had to go). `pip-audit`'s core behavior — non-zero exit on
an actual found vulnerability — needs no special flag and is unaffected by this
change.

---

## v1.0.10 — 2026-08-17 — `fault` added to ToolCall (SPEC.md §6 commit-one list)

**Change:** Closes the exact gap the is_error precision-pass investigation (v1.0.7–
v1.0.9) surfaced, not a new unrelated feature. That investigation established: a
`tools/call` that fails at the protocol level (a JSON-RPC error response, not a
`CallToolResult`) was, and until this change still is, dropped by `record/writer.py`'s
`observe()` with no ToolCall record written at all — no per-tool attribution possible,
invisible to `drifter stats` entirely, distinct from (and until now, structurally
unfixable in the same way as) the `is_error` gap those prior entries closed.

Added `ToolCall.fault: bool | None`. Deliberately a separate field from `is_error`, not
a shared boolean, per explicit design intent: `is_error` is `CallToolResult.isError` —
semantic, tool-reported, and often legitimate business behavior (a filesystem `search`
reporting no matches). `fault` is transport/protocol-level — the call never reached a
`CallToolResult` at all. Conflating them would make a routine "not found" read the same
as a dropped connection in a diagnostic ("where are you slow or flaky") tool whose
whole purpose is telling those apart. `record/writer.py`'s `JSONRPCError` branch now
writes a `ToolCall` for a `tools/call` fault specifically (`fault=True`,
`is_error=None` — not applicable, no result ever existed to check — `result_shape=None`,
`duration_ms` still measured); the normal success path now sets `fault=False`
explicitly, not left at the field's own default, so a corpus with zero faults can read
"definitely zero" rather than "unknown." `cli/stats.py` reports `FAULT%` as a column
separate from `ERR%`.

**Process, per explicit instruction:** the last two schema touches (v1.0.7's
required-field crash, v1.0.8's fix) both got the "how do old records without this
field behave" question wrong on the first attempt. This time: a test
(`tests/cli/test_stats.py::test_pre_fault_field_data_does_not_crash_and_does_not_misreport_as_no_fault`)
constructing a corpus that predates `fault` (but has `is_error`/`duration_ms` — a
distinct, later "age" than the v1.0.7 boundary) was written and run *before* any
`fault` implementation existed (failed: `AttributeError`, the field didn't exist),
then run again against a deliberately naive `fault: bool = False` (non-Optional,
defaulted) implementation. That naive version parsed without crashing but silently
reported `fault_rate == 0.0` for data with no fault information at all — confirmed
directly, not assumed (`collect_stats` against a hand-built pre-fault-field session:
`fault_rate: 0.0`, printed and inspected). Only then was the real implementation
(`bool | None`, `fault_unknown` tracked separately, `fault_rate` excluding unknowns
from its denominator, mirroring `error_rate` exactly) written, and the same test
re-run to confirm it now passes for the right reason.

---

## v1.0.9 — 2026-08-17 — SPEC.md §15: thinner diagnostics on pre-v1.0.7 corpora documented

**Change:** v1.0.8 made `drifter stats` handle pre-v1.0.7 data (missing `is_error`/
`duration_ms`) without crashing or misreporting it as zero. That fix is visible in a
single report's output (the `N/A`/`*` marker), but the underlying fact — that any
corpus spanning the v1.0.7 boundary has genuinely thinner diagnostic coverage for its
older calls — is a standing property of the schema's history, not something a reader
unfamiliar with that history would know to expect from the report alone. Added as
SPEC.md §15 item 10, following the same "document, don't just patch" precedent as
item 9 (v1.0.6). No behavior change; no corpus exists yet for this to apply to (Gate
1's real trial hasn't started), so this is documentation ahead of the fact, not a
correction of an observed problem.

---

## v1.0.8 — 2026-08-17 — `is_error`/`duration_ms` made backward-compatible (`| None`), not required

**Change:** Correction to v1.0.7, same day. v1.0.7 added `is_error: bool` and
`duration_ms: float` to `ToolCall` as required fields, matching the precedent set by
`timestamp` (v1.0.5). That precedent doesn't actually transfer: when `timestamp` was
added, no real recorded data existed anywhere, so nothing broke. `is_error`/
`duration_ms` are landing with Gate 1's real weekly trial imminent (PHASES.md's actual
exit test), so a required field risks exactly the failure a user request surfaced by
testing it directly: reconstructed an authentic pre-v1.0.7 corpus (via `git stash`
against the real prior commit, not a hand-guessed fixture) and ran the current
`drifter stats` against it. Result: an unhandled `pydantic.ValidationError` on the
first old-format `ToolCall`, crashing the entire read — not the silent "reads as 0%
errors" failure mode that was the original worry, but a different real failure
(`record/reader.py` has no old/new schema negotiation; a required field with no
default simply can't parse data recorded before it existed).

**Fix:** `is_error`/`duration_ms` changed to `bool | None` / `float | None`, default
`None`. `record/writer.py` is unaffected — it always knows both values at write time.
`cli/stats.py`'s `ToolStats` now tracks `error_unknown` (calls whose `is_error` is
`None`) separately from `errors`, and `error_rate`/`percentiles()` compute over the
*known* subset only, returning `None` (rendered "N/A", never coerced to `0.0%`) when
nothing is known. Verified against three cases: an all-old-data corpus (previously
crashed; now parses, reports "N/A"), an all-new-data corpus (unaffected), and a mixed
corpus spanning the migration point (error_rate computed over the known calls only,
not diluted by unknown ones landing in the denominator as if they were confirmed
non-errors).

**Why this is a correction, not a new feature:** required-with-no-default was a
plausible-looking choice that happened to be wrong for these two fields specifically,
caught by testing against real reconstructed data rather than by reasoning about the
schema in the abstract — exactly the gap CLAUDE.md's testing-discipline note names
("prefer tests that assert exact expected values... this project's recurring bug
pattern is fields populated with plausible-but-wrong values"). SPEC.md §6's
"cannot be added retroactively" list is unchanged by this correction — both fields
still can't be backfilled onto an existing record; what changed is only that a
record predating them must still be *readable*, with their absence represented
honestly as unknown.

---

## v1.0.7 — 2026-08-17 — `is_error` and `duration_ms` added to SPEC.md §6's commit-one field list

**Change:** Gate 1 Prompt 8 (`drifter stats`, F-10) requires error rate and latency
percentiles per tool — but neither was recoverable from what Prompt 1–7's schema
actually recorded. `result_shape` (SPEC.md §6's redaction default: type/keys/length
only) never carried the `isError` boolean itself, only whether the key `isError` was
present — indistinguishable between a successful and a failed call once written. And
`timestamp` (added in v1.0.5) is an ISO 8601 string with one-second resolution, far too
coarse for a typical tool-call round trip, and only one is recorded per call rather than
a request/response pair — latency simply wasn't derivable from it. Both are genuine
schema gaps, not new requirements being introduced: F-10 was already scoped in
FEATURES.md before Prompt 8 started.

Added `is_error: bool` (from MCP's `CallToolResult.isError` — per the SDK's own
docstring, this SHOULD be how tool-execution failures are reported, versus a
protocol-level JSON-RPC error for the rarer "couldn't find the tool" case, which Gate 1
still doesn't attribute per-tool; see `record/writer.py`'s `observe()`) and
`duration_ms: float` (measured with `time.monotonic()` between the request and response
being observed in `record/writer.py`, immune to both wall-clock adjustments and
`timestamp`'s coarse resolution) to `ToolCall`, and to the "cannot be added
retroactively" list in SPEC.md §6. Schema stays at version `0.1`: Gate 1's golden
fixture (Prompt 9) hasn't been committed yet, so nothing external depends on the
pre-this-change shape.

As a direct consequence, `cli/observe.py`'s live `errors:` counter (F-09) — previously
only counting parse errors and protocol-level JSON-RPC errors — now also counts
`isError: true` tool results, which is the common case for an actual tool failure. This
was always what F-09's "see failures happening during a week-long trial" was meant to
show; it just wasn't wired up before `is_error` existed to check.

---

## v1.0.6 — 2026-08-17 — SPEC.md §15: Ctrl+C subprocess-teardown limitation documented

**Change:** Gate 1 Prompt 7 (`drifter observe`, F-09) found that `run_passthrough_proxy`'s
cooperative-cancellation shutdown path (relied on by the default Ctrl+C handling
`anyio.run()`/`asyncio.Runner` install) hangs indefinitely: `stdio_server()`'s internal
stdin read is delegated to a worker thread, and a blocking OS-level read already in
flight in a thread cannot be cancelled. `cli/observe.py`'s `handle_sigint` fixes this by
bypassing cooperative cancellation — flushing recorded data via a synchronous
`recorder.close()` and exiting directly — but as a consequence, the spawned MCP server
subprocess is never explicitly waited on or terminated by Drifter. Whether that leaves it
orphaned depends on the subprocess itself: empirically verified (real subprocess, PID
tracked via `psutil` before and after) that the SDK-built fixture server self-terminates
promptly, because the MCP spec's 2026-07-28 revision states servers "SHOULD exit promptly
when their standard input is closed or reads return end-of-file." This is a new,
genuinely-discovered limitation being documented, not a correction of a prior wrong
claim — see SPEC.md §15 item 9 for the full statement, including the SHOULD-level (not
guaranteed) caveat for third-party servers and the note that the 2025-11-25 predecessor
revision has no equivalent language.

---

## v1.0.5 — `timestamp` added to SPEC.md §6's commit-one field list

**Change:** Gate 1 Prompt 6 (F-06/F-07 trajectory segmentation) requires idle-gap
heuristic segmentation, which needs a per-call wall-clock timestamp to measure elapsed
time between calls — but no `timestamp` field existed anywhere in SPEC.md §6's schema
description or in `record/schema.py` as committed through Prompt 5. FEATURES.md's F-28
(signature grouping) already presupposed one implicitly ("normalizes away request IDs,
timestamps, and volatile argument values"), so this was a real gap in what SPEC.md §6
specified, not a new requirement being introduced. Added `timestamp` to the "cannot be
added retroactively" list (transient real-time data — an already-recorded call can't
be retroactively timestamped) and to `ToolCall`, `ToolsList`, and `TrajectoryEnd` in
`record/schema.py`. Schema stays at version `0.1`: Gate 1's golden fixture (Prompt 9)
hasn't been committed yet, so nothing external depends on the pre-timestamp shape.

---

## v1.0.4 — SPEC.md §3 principle 9 clarified

**Change:** Gate 1 Prompt 5 (F-05, environment fingerprinting) implemented every
environment field — `agent_identity`, `model_name`, `tool_manifest_hash`,
`server_versions` — as equally fatal on mismatch, with none treated as advisory.
Principle 9's original wording ("must match... or the comparison is invalid") was
ambiguous enough that resolving whether this was intended took a full options menu to
work through. Field-level fatality is now stated explicitly in SPEC.md §3 principle 9
as a deliberate choice, not just default behavior that happened to land this way —
tied to SPEC.md §15 limitation 4 (a tool's behavior can change with an unchanged
schema), which is exactly the case a server-version mismatch might be catching. No
behavior changed; this closes a real ambiguity in what was already implemented.

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
