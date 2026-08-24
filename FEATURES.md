# Drifter — Feature Breakdown

Every feature: what it does, why it exists, what it depends on, what "done" means.
Organized by module. Cross-reference SPEC.md for the invariants each feature must honor.

---

## Module: `record/`

### F-01 Proxy passthrough (stdio)

**Technical:** Drifter is invoked as the MCP server command in the client's config. It
spawns the real server as a child process, forwards all JSON-RPC frames bidirectionally
unmodified, and mirrors every frame to a raw log.

**Simple:** Drifter stands in for the real tool server. Your agent talks to Drifter,
Drifter talks to the real thing, and nothing changes for the agent — it just also gets
written down.

**Depends on:** nothing (first thing built).
**Done when:** an agent using the proxied server behaves identically to using the
server directly, with zero added latency the user would notice.

### F-02 Structured recording (JSONL)

**Technical:** Every `tools/list` and `tools/call` frame is parsed into the schema
defined in SPEC.md §6 and appended to a session JSONL file. `result_shape` is computed
(type, keys, array length) — payloads are never stored by default.

**Simple:** Turns raw network traffic into a readable, structured diary of what your
agent did — without ever writing down anything sensitive it touched.

**Depends on:** F-01.
**Done when:** a golden fixture session round-trips through write→read with zero data
loss on all schema fields.

### F-03 Raw frame mirroring

**Technical:** Alongside the parsed JSONL, literal JSON-RPC bytes are written to
`.drifter/raw/`, indexed by `raw_frame_offset` in the corresponding parsed record.

**Simple:** A backup of the original, unprocessed data. If you ever realize you needed
a field you didn't think to parse, you don't have to re-run the agent — you re-read the
receipts you already kept.

**Depends on:** F-01.
**Done when:** any parsed record can be traced back to its exact raw bytes.

### F-04 Secret redaction

**Technical:** Pattern-matching (common token/key formats: `sk-`, `Bearer `, JWT shape,
etc.) applied to argument values and headers before write, in both the parsed JSONL and
the raw mirror. `--record-full` opt-out prints a warning.

**Simple:** Drifter never writes your API keys or passwords to disk, even by accident,
even in the backup copy.

**Depends on:** F-02, F-03.
**Done when:** a fixture containing planted fake secrets produces zero leaked values
in either output file. This is a test-enforced, not documentation-enforced, guarantee.

### F-05 Environment fingerprinting

**Technical:** Every session records a hash of agent identity, model name, MCP server
names/versions, and tool-manifest hash. Comparisons across sessions require matching
fingerprints except for the intended mutation delta.

**Simple:** A label on every test run saying exactly what was running. Stops Drifter
from blaming your agent for a change that was actually the server updating underneath
everyone.

**Depends on:** F-02.
**Done when:** a mismatched fingerprint between baseline and mutation arms blocks
comparison with an explicit error, not a silent wrong answer.

### F-06 Trace-context segmentation

**Technical:** Checks `_meta` on every request for W3C trace context (`traceparent`).
When present, groups calls sharing a trace ID into one trajectory at confidence 0.99.

**Simple:** If your agent's framework already tags its work with trace IDs (many do),
Drifter uses that to know exactly where one task ends and the next begins — no guessing.

**Depends on:** F-02.
**Done when:** a session with trace-context-emitting client produces perfectly bounded
trajectories with zero manual correction needed.

### F-07 Heuristic segmentation (fallback)

**Technical:** When no trace context exists: cut on idle gap (default 30s, calibration
constant) combined with a data-flow connectivity check — consecutive calls sharing an
`references` link stay grouped regardless of timing gap.

**Simple:** If there's no trace ID to follow, Drifter guesses task boundaries by
watching for pauses and by noticing when one tool's output feeds directly into the next
tool's input.

**Depends on:** F-06 (as the primary path), F-08 (data-flow refs).
**Done when:** heuristic segmentation on a fixture without trace context produces
boundaries a human reviewer agrees with on inspection.

### F-08 Data-flow reference tracking

**Technical:** Each `tool_call` record includes `references`: an array mapping
argument values back to a specific path in a prior call's result, when detectable by
value match.

**Simple:** Notices when your agent takes the customer ID it just looked up and plugs
it into the next call — recording that connection, not just the two calls separately.

**Depends on:** F-02.
**Done when:** a two-step dependent workflow fixture has its dependency correctly
captured in `references`.

### F-09 `drifter observe`

**Technical:** CLI entrypoint wiring F-01 through F-08 into a long-running passthrough
session with live terminal feedback (trajectory count, call count, error count).

**Simple:** The single command that turns on recording. Point your agent at it and keep
working — that's the entire setup.

**Depends on:** F-01–F-08.
**Done when:** a full week of the author's own daily agent work runs under `observe`
with zero crashes and zero noticeable slowdown. **This is Gate 1's exit test.**

### F-10 `drifter stats`

**Technical:** Reads the JSONL corpus and reports tool call frequency, unused tools
(present in manifest, never called), retry rate, error rate, and latency percentiles
per tool.

**Simple:** A summary of your recordings: which tools you actually use, which you
never touch, and where things are slow or flaky. Useful on its own, with zero mutation
testing involved.

**Depends on:** F-09 having produced a corpus.
**Done when:** stats on the Gate 1 corpus visibly match what the author knows about
their own agent's real behavior.

---

## Module: `replay/`

### F-11 Replay store

**Technical:** Indexes every recorded `tool_call` under the three-tier key scheme
(SPEC.md §7). Provides lookup by (server, tool, args) returning HIT / MISS with
provenance tag.

**Simple:** The filing cabinet of everything Drifter has ever seen a tool return, so it
can answer future questions without asking the real tool again.

**Depends on:** F-02.
**Done when:** every exact-match request in a fixture resolves to HIT with correct
provenance.

### F-12 Inverse-mutation key resolution

**Technical:** Given an active mutation's recorded transformation, applies the inverse
to an incoming request before hashing, recovering the pre-mutation key for invertible
operators (rename, type-change, optional-add).

**Simple:** If Drifter renamed an argument, and the agent now uses the new name, this
translates it back to the old name so the original recording still matches.

**Depends on:** F-11, `mutate/` operator definitions (F-20+) providing an inverse.
**Done when:** a `parameter_rename` mutation test produces HIT (inverse) rather than
MISS on all previously-recorded call shapes.

### F-13 Semantic key resolution

**Technical:** Fallback matching on the sorted multiset of argument *values*, ignoring
parameter names, for cases the inverse mapping can't cleanly recover (e.g.
`tool_integration`).

**Simple:** A looser last resort: even if Drifter can't figure out the exact renamed
field, if the actual data being passed looks the same as something it's seen before, it
can still make a reasonable guess.

**Depends on:** F-11.
**Done when:** a merged-tool fixture resolves via semantic match at a measurably better
rate than falling straight to synthetic.

### F-14 Synthetic response generation

**Technical:** On a full miss, constructs a structurally valid response from the
recorded schema for that tool (correct types, plausible-shaped keys) — never
LLM-generated content.

**Simple:** If Drifter genuinely has no recording to answer with, it builds an
empty-but-correctly-shaped fake answer rather than making something up with an AI.

**Depends on:** F-11.
**Done when:** synthetic responses pass the tool's own declared schema validation.

### F-15 Fidelity computation and gating

**Technical:** Per mutation arm (and per baseline arm — SPEC.md §7/§8), computes
`fidelity = (exact + inverse + w×semantic) / total`, excluding `tool_addition` calls
from the denominator. Forces verdict to UNKNOWN below `fidelity_floor`.

**Simple:** Drifter grades its own homework before trusting its answer. If it had to
guess too much during a test, it says "I don't know" instead of reporting a fake
finding.

**Depends on:** F-11–F-14.
**Done when:** an artificially low-fidelity fixture (forced high miss rate) produces
UNKNOWN rather than a REGRESSION verdict.

---

## Module: `mutate/`

### F-16 Mutation operator: description_update

**Technical:** Rewrites a tool's `description` field via bounded structural paraphrase
(synonym substitution, sentence reordering within the existing content) — rejected if
output matches imperative-instruction regex patterns.

**Simple:** Reworks a tool's explanation the way a human editor might reword a
sentence — never adds new instructions, only changes phrasing.

**Depends on:** none (pure function over the manifest).
**Done when:** applied to a fixture, output is a valid tool description, structurally
different, and passes the injection-pattern rejection test.

### F-17 Mutation operator: tool_addition

**Technical:** Injects a new tool definition (name, description, schema) into the
served manifest, styled plausibly consistent with sibling tools, with no backing
implementation — all calls to it resolve via F-14 synthetic.

**Simple:** Adds a fake extra tool to the menu that looks like it belongs there, to see
if your agent gets confused and picks it over the tool it should use.

**Depends on:** F-14 (its calls are always synthetic by definition).
**Done when:** the added tool is indistinguishable in style from real siblings on
manual review, and calls to it are correctly excluded from fidelity accounting.

### F-18 Mutation audit log

**Technical:** Every applied mutation is recorded with `mutation_id`, operator,
target, exact before/after values, and (where applicable) the inverse mapping consumed
by F-12.

**Simple:** A paper trail of exactly what Drifter changed and how, so any result can be
traced back to the precise edit that caused it.

**Depends on:** F-16, F-17.
**Done when:** every mutation applied in a test run is reconstructable from the audit
log alone, without needing the mutation code itself.

### F-19 Cache-busting on mutated responses

**Technical:** Every mutated `tools/list` response sets `ttlMs: 0` and a private
`cacheScope`, per SPEC.md §10 and verified requirement C8.

**Simple:** Tells the agent's client "don't remember this menu" — otherwise it might
reuse a mutated menu across different tests and corrupt every result after the first.

**Depends on:** F-01 (proxy response path).
**Done when:** a caching-capable test client never reuses a mutated tool list across
two different mutation arms in a fixture.

### F-20 Header integrity on live forwards

**Technical:** Strips inbound `Mcp-Method`/`Mcp-Name` headers on any call touched by an
active mutation; never forwards a mutated call live, by design (SPEC.md §10).

**Simple:** Prevents a scenario where Drifter's fake tool name would get sent to a real
server and rejected — mutated calls simply never go live at all.

**Depends on:** F-16, F-17.
**Done when:** no fixture produces a live forward for any call whose tool was touched
by an active mutation.

---

## Module: `evaluate/`

### F-21 Baseline calibration

**Technical:** Runs an approved task N times (default 10, `calibration.yaml`
overridable) via the agent adapter with no mutation active, replay-served. Computes
dominant path, variant frequencies, mean, spread (natural_variation).

**Simple:** Runs your agent normally, several times, to learn how much it naturally
wobbles before Drifter changes anything — the "what's normal for you" measurement.

**Depends on:** F-11 (replay), agent adapter (F-34).
**Done when:** repeated baseline runs on the same fixture produce a stable, reproducible
spread estimate.

### F-22 Baseline fidelity gating

**Technical:** Applies F-15's fidelity gate to baseline arms specifically — closes the
gap identified in the audit where a contaminated reference distribution silently widens
the detection threshold.

**Simple:** Makes sure the "normal" measurement itself isn't built on guessed data —
if it is, Drifter says so rather than quietly trusting a shaky baseline.

**Depends on:** F-15, F-21.
**Done when:** a baseline run with forced high miss rate is flagged in its own report,
not silently absorbed into a wider noise floor.

### F-23 Behavior effect-size scoring

**Technical:** `effect_size = (deviation_rate − natural_variation) / baseline_spread`.
Verdict thresholds from `calibration.yaml`. Trajectory distance (normalized edit
distance) computed alongside for diagnostic detail.

**Simple:** Compares "what happened after the mutation" against "what's normal" and
decides whether the difference is big enough to matter, or just ordinary noise.

**Depends on:** F-21, mutation run data.
**Done when:** a fixture with a known-real regression scores REGRESSION; a fixture
with only natural variance scores NO_REGRESSION.

### F-24 Task assertion engine

**Technical:** Evaluates opt-in deterministic assertions (`calls`, `calls_before`,
`never_calls`, `result_contains`) against a trajectory. No assertions configured →
verdict UNKNOWN.

**Simple:** If you've told Drifter exactly what "success" looks like for a workflow, it
checks for that. If you haven't, it honestly says it doesn't know rather than guessing.

**Depends on:** task definitions (F-30).
**Done when:** a fixture with a planted assertion failure correctly reports FAIL; an
unassessed fixture correctly reports UNKNOWN, never PASS by default.

### F-25 Safety verdict engine

**Technical:** Checks every trajectory against tool risk classification (F-26):
unexpected write/destructive invocation, capability outside policy,
`confirmation_required` bypass, secret leakage, annotation-behavior mismatch.

**Simple:** Checks whether the agent did anything genuinely risky during the test —
independently of whether the task technically succeeded or the behavior merely
changed.

**Depends on:** F-26.
**Done when:** a fixture with a planted unexpected write to a destructive tool is
caught as a SAFETY VIOLATION even when Behavior shows NO_REGRESSION.

### F-26 Tool risk classification

**Technical:** Six-level taxonomy (SPEC.md §10), derived in priority order from MCP
annotations (untrusted hints) → name/schema heuristics → observed behavior → user
policy override. `classification_source` recorded per tool.

**Simple:** Sorts every tool into a danger level, and is honest about *why* it made
that call — a guess from the tool's name is treated with less trust than something
Drifter actually watched happen.

**Depends on:** F-02 (manifest data), F-09 (observed behavior).
**Done when:** `drifter doctor` surfaces every ambiguous classification for one-time
user confirmation before any live-mode run is possible.

### F-27 Adaptive repeat scheduling

**Technical:** Three-stage: 1 run × all mutations (screen) → 5 runs on flagged
mutations (confirm) → up to 20 runs on still-inconclusive ones (resolve). Stops early
once a confidence interval clears the verdict threshold.

**Simple:** Doesn't waste time and money running every test the maximum number of
times — it runs a quick pass on everything, then only digs deeper on the ones that
looked suspicious.

**Depends on:** F-23.
**Done when:** total run count on a mixed fixture (some clearly-broken, some
clearly-fine mutations) is measurably lower than a fixed-N-for-everything approach,
with the same final verdicts.

---

## Module: `mine/`

### F-28 Signature grouping

**Technical:** Collapses trajectories to their normalized tool-call sequence
(`signature`), deduplicating with occurrence counts. Normalizes away request IDs,
timestamps, and volatile argument values.

**Simple:** Groups together all the times your agent did basically the same thing,
even if small details differed, so you're not staring at 300 near-duplicate examples.

**Depends on:** F-06/F-07 (segmented trajectories).
**Done when:** a 300-trajectory fixture with known repeated patterns collapses to the
expected small number of distinct signatures.

### F-29 Frequent subsequence mining (PrefixSpan)

**Technical:** Runs sequential pattern mining over grouped signatures to surface
recurring sub-workflows even inside longer, varied trajectories.

**Simple:** Finds the small repeated "core" steps that show up across many different
longer workflows — often the most important thing to test.

**Depends on:** F-28.
**Done when:** a fixture with a known embedded sub-pattern (e.g.
`get_customer → create_invoice` appearing inside several longer flows) surfaces it as
a ranked candidate.

### F-30 Task candidate generation + approval

**Technical:** Converts mined workflows into editable YAML task candidates
(`status: candidate`). User edits and promotes via `drifter tasks approve`
(`status: approved`). Coverage report lists tools appearing in no selected task.

**Simple:** Turns "here's a pattern we noticed" into a real, named test — but only
after you've looked at it and said yes. Nothing becomes an official test without your
approval.

**Depends on:** F-29.
**Done when:** a mined candidate can be edited (prompt, assertions, safety policy) and
approved without touching raw recordings.

---

## Module: `policy/`

### F-31 Blast-radius preview

**Technical:** Before any live-mode run, computes and displays planned workflow count,
agent run count, tool call count broken down by risk level, and estimated replay
coverage. Requires explicit confirmation.

**Simple:** Shows you exactly what's about to happen — how many real actions, how
risky — before anything actually runs live, so nothing surprising happens silently.

**Depends on:** F-26 (risk classification), F-15 (fidelity estimate).
**Done when:** live mode is architecturally unreachable without this preview having
been shown and confirmed.

### F-32 Budget and hard limits

**Technical:** `--budget N` (model calls), `--dry-run` (plan without executing),
`baseline.max_calls`, wall-time cap. Aborts cleanly with partial results reportable.

**Simple:** Hard ceilings so a test run can never quietly burn through your entire
daily API quota (or bill) without you knowing in advance roughly what it'll cost.

**Depends on:** all execution paths (F-21, mutation runner).
**Done when:** a run exceeding budget stops cleanly mid-execution and still produces a
report on the partial data collected.

---

## Module: `cli/` and adapters

### F-33 `drifter init`

**Technical:** Scans common MCP client config locations (`.mcp.json`,
`.cursor/mcp.json`, `claude_desktop_config.json`), extracts server definitions, runs
initial tool classification (F-26), writes a starter `drifter.yaml`.

**Simple:** Finds your existing tool setup automatically and writes most of the config
file for you — you shouldn't have to type your own server list by hand.

**Depends on:** F-26.
**Done when:** run against a real project, produces a working `drifter.yaml` with zero
manual edits required to run `drifter observe`.

### F-34 Subprocess agent adapter

**Technical:** Executes `agent.command` as a subprocess per task, task prompt
templated in. **Gate 2 scope, narrower than the eventual full scope** (see
CHANGELOG.md's 2026-08-25 F-34 entry): the spawned agent's own stdin/stdout is wired
directly to an in-process replay-serving proxy (`replay/replay_proxy.py`) — matching
SPEC.md's v0 stdio-only architecture — not a URL-addressed proxy over an injected
environment variable. No separate final-answer string is captured; `run_baseline`
consumes the recorded tool-call sequence (via `SessionRecorder`), not a task
conclusion. A URL-based proxy address and separate stdout final-answer capture belong
to a later, HTTP-transport-era version of this adapter (SPEC.md's "+HTTP in v1"),
once that transport actually exists — not Gate 2's.

**Simple:** The way Drifter actually runs *your* agent — not a stand-in, not a demo
model, the real thing, however you'd normally run it from a terminal.

**Depends on:** none (only touches process boundaries).
**Done when:** a real CLI agent script runs correctly under the adapter with its
tool calls correctly captured and correlated to a trajectory.

### F-35 `drifter run`

**Technical:** Orchestrates F-21 (baseline) → F-16/17 (mutate) → F-27 (adaptive
scheduling) → F-23/24/25 (evaluate) → report generation, for one or more approved
tasks.

**Simple:** The single command that runs the whole test: establish normal, break
things on purpose, compare, and tell you what it found.

**Depends on:** essentially everything above.
**Done when:** Gate 3's exit test passes — a real fragility found in the author's own
agent that wasn't previously known.

### F-36 `drifter score` / `drifter report`

**Technical:** Re-runs F-21–F-25 evaluation logic against already-stored JSONL with
zero new agent execution or API calls; renders the report format from SPEC.md §13.

**Simple:** Lets you re-analyze old test results instantly and for free — the whole
point of recording everything in the first place.

**Depends on:** any prior `drifter run` output.
**Done when:** re-scoring a week-old corpus produces output in seconds with zero
network calls. **This is Gate 2's exit test.**

### F-37 `drifter doctor`

**Technical:** Validates config syntax, server connectivity, ambiguous tool
classifications requiring confirmation, and calibration-file presence.

**Simple:** A pre-flight check that catches setup mistakes before you waste a test run
on a broken config.

**Depends on:** F-33, F-26.
**Done when:** every category of common misconfiguration (bad server command, missing
task assertions, unclassified destructive tool) produces a specific, actionable error.

---

## Deliberately excluded from this feature set

Response mutation, state mutation, HTTP adapter, plan-only mode, delta debugging,
workflow graph mining, LLM judge oracle, hosted mode, dashboard, accounts. Each is a
real, previously-discussed idea. None is required to prove the core loop (F-01
through F-37) works, and each adds cost, safety risk, or setup burden that would delay
Gate 1. See PHASES.md for where they resurface.
