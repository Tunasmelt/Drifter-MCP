# Drifter — Feature Breakdown

Every feature: what it does, why it exists, what it depends on, what "done" means.
Organized by module. Cross-reference SPEC.md for the invariants each feature must honor.

---

## Build status (as of 2026-09-06, `.drifter/GATE_STATUS`: `gate: v1`)

This table is the living answer to "what's actually built, what needs building, and
what's left in something already built" — updated in place as gates close and features
land, per this project's own discipline of fixing the locked planning surface rather
than letting it drift from reality. See docs/CHANGELOG.md for the entry that added this
table and docs/PHASES.md for gate-level narrative.

| # | Feature | Status | Note |
|---|---|---|---|
| F-01 | Proxy passthrough (stdio) | ✅ Built | Gate 1 |
| F-02 | Structured recording (JSONL) | ✅ Built | Gate 1 |
| F-03 | Raw frame mirroring | ✅ Built | Gate 1 |
| F-04 | Secret redaction | ✅ Built | Gate 1, red-test-first |
| F-05 | Environment fingerprinting | ⚠️ Built, real gap left | §15 limitation 14 — permanently null `tool_manifest_hash` if `list_tools()` isn't called first; now covered by a direct `tests/record/test_writer.py` unit test (test-only, not fixed — see docs/CHANGELOG.md) |
| F-06 | Trace-context segmentation | ✅ Built | Gate 1 |
| F-07 | Heuristic segmentation (fallback) | ⚠️ Built, known gap | §15 limitation 8 — no signal for two unrelated calls with no idle gap |
| F-08 | Data-flow reference tracking | ✅ Built | Gate 1. Literal-equality matching can produce a spurious reference on a common falsy value (True/0/"") — an accepted, documented tradeoff (`segment.py`'s own docstring), now also locked in by a direct unit test |
| F-09 | `drifter observe` | ⚠️ Built, known gap | §15 limitation 9 — Ctrl+C doesn't wait for/terminate the real spawned server. A second gap (a bad server command crashed with a raw traceback instead of an actionable error, unlike `drifter doctor`) was found and FIXED during the same edge-case pass — see docs/CHANGELOG.md |
| F-10 | `drifter stats` | ✅ Built | Retry detection compares stored (already-redacted) arguments — previously two different real secrets redacted identically and were misdetected as a retry; fixed by making the redaction marker a deterministic hash of the matched value (record/redact.py) instead of a fixed placeholder, so different secrets now redact differently while the same secret still redacts identically (required for both retry detection and F-11's replay matching) |
| F-11 | Replay store | ⚠️ Built, exact-key only | Tier-3 finding (PHASES.md Gate 3) — may not be viable against any real agent alone. Multi-session merging, cross-file last-writer-wins, fault/null-shape hits, and nested-key canonicalization now directly tested |
| F-12 | Inverse-mutation key resolution | ✅ Built | Built alongside F-40 (`parameter_rename`), which gave it the first real mutation with a real inverse to resolve against — `ReplayStore.lookup`'s new `inverse_param_map` parameter |
| F-13 | Semantic key resolution | ✅ Built | Falls back to a sorted-value-multiset match when exact misses, only when exact misses. Fidelity gating (F-15) now weights a semantic hit at `semantic_weight` (0.8) instead of full 1.0 — see F-15's own row |
| F-14 | Synthetic response generation | ⚠️ Scoped to `tool_addition` only | General schema-inference synthesis explicitly out of scope; deliberate |
| F-15 | Fidelity computation and gating | ✅ Built | Gate 2; tier-weighting (exact=1.0, inverse=1.0, semantic=`semantic_weight`) wired up once F-13/F-12 gave `ToolCall` a `match_tier` field with all three tiers reachable |
| F-16 | `description_update` | ⚠️ Built, one known gap (one fixed) | §15 limitation 13 — 5-pattern injection check is closed-set, a real published description slips past it, still open. The other known gap (ALL-CAPS source words losing case class, e.g. "GET" → "Obtain" instead of "OBTAIN") is fixed — case class (all-caps/title-case/lowercase) is now preserved, not just the first letter |
| F-17 | `tool_addition` | ✅ Built | Gate 3, safety-reviewed |
| F-18 | Mutation audit log | ⚠️ Minimal shared shape, deliberate | Not F-18's own eventual general log format — see `description_update.py`'s docstring |
| F-19 | Cache-busting on mutated responses | ❌ Not built — real-protocol gap | §15 limitation 15 — `ttlMs`/`cacheScope` exist only in a draft, not-yet-real MCP protocol version; no real client speaks it |
| F-20 | Header integrity on live forwards | ✅ Built | Gate 3 — stronger than planned: the originally-designed `Mcp-Method`/`Mcp-Name` header-stripping was never implemented in `src/`, but `replay/replay_proxy.py` structurally has no live-forwarding code path at all, now locked in by an import-inspection regression test |
| F-21 | Baseline calibration | ✅ Built | Gate 2 |
| F-22 | Baseline fidelity gating | ✅ Built | Gate 2 |
| F-23 | Behavior effect-size scoring | ✅ Built | Gate 2, zero-spread edge case is a stated design decision |
| F-24 | Task assertion engine | ⚠️ UNKNOWN-default only | No real assertion authoring exists — **needs building** for v1 (depends on F-30) |
| F-25 | Safety verdict engine | ✅ Built | `policy/safety.py`, 2 of docs/SPEC.md §8's 5 check categories built (destructive invocation, confirmation_required bypass), 3 real documented gaps — wired into `drifter run`'s real report, evaluated with no fidelity gate |
| F-26 | Tool risk classification | ✅ Built | `policy/classify.py`'s 4-tier resolution (user override → MCP annotations → name heuristics → observed behavior), wired into `drifter doctor`. Tier 4 (observed behavior) is a documented, deliberate stub — no signal currently recorded can honestly distinguish write from read-only |
| F-27 | Adaptive repeat scheduling | ❌ Not built | **Needs building** — v1 scope |
| F-28 | Signature grouping | ❌ Not built | **Needs building** — `mine/` is empty; deferred past Gate 3 deliberately (no real multi-week corpus yet) |
| F-29 | Frequent subsequence mining (PrefixSpan) | ❌ Not built | **Needs building** — depends on F-28 |
| F-30 | Task candidate generation + approval | ❌ Not built | **Needs building** — depends on F-29; blocks F-24's real authoring UX |
| F-31 | Blast-radius preview | ✅ Built, reframed | `policy/blast_radius.py`, gates real agent-process spawning (drifter run's actual real-cost path today), not a live-server mode that doesn't exist. "Estimated replay coverage" is a real, documented gap — see docs/SPEC.md §10's implementation-status note |
| F-32 | Budget and hard limits | ✅ Built, reframed | `policy/budget.py`; `--budget` is a TOOL-CALL ceiling (not literally "model calls" — unobservable, docs/SPEC.md §15 limitation 2), checked before each repeat starts, never mid-run. `--dry-run` reuses F-31's preview |
| F-33 | `drifter init` | ⚠️ Built narrower than spec, deliberate | F-26 now exists but `init` still doesn't call it — done-when bar doesn't require it, wiring classification into `init` itself is separate, unrequested scope |
| F-34 | Subprocess agent adapter (stdio) | ✅ Built | Gate 2 scope, deliberately narrower than original spec text (now widened by F-38, not replaced) |
| F-35 | `drifter run` | ✅ Built | Gate 3 minimal scope, deliberate (no `--budget`, no adaptive scheduling, no full report format) |
| F-36 | `drifter score` / `drifter report` | ✅ Both built | `cli/report_format.py` split out of `cli/run.py` so `cli/report.py` stays genuinely execution-free — `mutation_log` is a real, stated gap (not reconstructable from disk, see F-36's own entry) |
| F-37 | `drifter doctor` | ⚠️ Gate 1 scope + F-38's http check + F-26 classification | Surfaces unresolved classifications as `[WARN]`, not yet a hard live-mode gate (F-31/F-32 don't exist to gate against) |
| F-38 | HTTP agent adapter | ✅ Built, twice-audited | v1, four real bugs found and fixed; final-answer capture is scope beyond the literal ask (see CHANGELOG) |
| F-39 | HTTP real-server connection | ✅ Built | `record/proxy.py`'s `connect_to_server` picks `streamable_http_client`/`stdio_client` by the target's own type; `cli/config.py`'s `ServerConfig.url` mutually exclusive with `command`; `drifter observe`/`drifter doctor` both give actionable errors (not a raw `ExceptionGroup`) on an unreachable url |
| F-40 | `parameter_rename` mutation operator | ✅ Built | `mutate/parameter_rename.py` — the third Level 0/1 operator, added specifically to give F-12 a real inverse to resolve against (neither F-16 nor F-17 can: schema-immune / no prior recording respectively). Closed-set, deterministic snake_case→camelCase, exactly one property per tool |

**Priority order for what to build next**, per docs/PHASES.md's own v1 ordering and the
dependency chain above (not a re-ranking, just made explicit in one place):
1. ~~**F-13** (semantic key resolution)~~ — **built.** Was the tier-3 gap docs/SPEC.md
   §15 limitation 16's real evidence (Gate 4's real second-user test) confirmed as
   blocking, not just "nice to have." **Re-examined after building it (docs/SPEC.md
   §15 limitation 16's own update): F-13 only matches identical VALUES under a
   different parameter NAME — it cannot resolve the real, curious agent's actual
   failure mode (different tools, different argument VALUES). Limitation 16 remains
   open**; F-13 is a real, separate, narrower improvement, not the fix.
2. ~~**F-15's remaining scope**~~ — **built.** `ToolCall` gained a `match_tier` field
   (F-13's own follow-on, same private-marker-key pattern `result_provenance` already
   used), and `_run_fidelity` now weights a semantic hit at `semantic_weight` (0.8)
   instead of full 1.0, per docs/SPEC.md §7's formula.
3. ~~**F-39**~~ — **built.** `record/proxy.py`'s `connect_to_server` picks the
   transport by the target's own type (`StdioServerParameters` vs. a bare `str`
   URL); `cli/config.py`'s `ServerConfig.url` is mutually exclusive with `command`.
   Confirmed against a real Streamable HTTP server, not just unit-level.
4. ~~**F-26**~~ — **built.** `policy/classify.py`'s 4-tier resolution (user override
   → MCP annotations → name heuristics → observed behavior — the last a documented
   stub, see F-26's own entry), wired into `drifter doctor`. Now unblocks F-25 →
   F-31/F-32.
5. ~~**F-25**~~ — **built.** `policy/safety.py`, wired into `drifter run`'s real
   report. 2 of 5 check categories built (destructive invocation,
   confirmation_required bypass), 3 real documented gaps — see F-25's own entry.
   Now unblocks F-31/F-32.
6. ~~**F-31**~~ — **built, reframed.** `policy/blast_radius.py`, gates spawning real
   agent processes — `drifter run`'s actual real-cost path today — with `drifter
   run` now requiring `--yes` or interactive confirmation. See F-31's own entry for
   why "live mode" and "estimated replay coverage" from docs/SPEC.md §10's mockup don't
   map onto this codebase's actual architecture.
7. ~~**F-32**~~ — **built, reframed.** `policy/budget.py`, `policy/` module now
   complete. `--budget` is a tool-call ceiling (not a literal model-call count —
   see F-32's own entry), checked before each repeat starts, never mid-run.
   `--dry-run` reuses F-31's blast-radius preview with zero new computation.
8. ~~**F-36's `drifter report`**~~ — **built.** `cli/report.py` re-renders a prior
   `drifter run`'s full BEHAVIOR/TASK/SAFETY report from stored sessions alone,
   zero new execution — `render_run_result`/`RunResult` split into a new,
   deliberately execution-free `cli/report_format.py` shared with `cli/run.py`,
   rather than `report.py` importing `cli.run` directly (which would have
   transitively pulled in real subprocess-spawning code just by being imported).
9. **F-28 → F-29 → F-30 → F-24's real authoring UX** — the `mine/` module, once a real
   multi-week corpus exists to mine (the reason this was deferred past Gate 3 in the
   first place, still true).
10. **F-27** (adaptive scheduling) — lower urgency, no blocking dependents. All other
    v1 priority-list items are now built.

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
MISS on all previously-recorded call shapes. **Built** (docs/CHANGELOG.md), together with
F-40 (`mutate/parameter_rename.py`) — the depends-on above sat unmet from Gate 2 through
this point for exactly the stated reason: neither F-16 (`description_update`, schema-
immune) nor F-17 (`tool_addition`, no prior recording to invert against) can ever produce
a real inverse, so F-12 had nothing to resolve against until a third operator that could.
`ReplayStore.lookup` gained an `inverse_param_map: dict[str, str] | None` parameter — a
live call's arguments are translated `{new_name: old_name}` and looked up again under the
exact index on an initial exact miss, before falling to semantic (docs/SPEC.md §7's
ordering: exact, inverse, semantic). Tagged `match_tier="inverse"` and given the same
full fidelity weight as `"exact"` (`evaluate/baseline.py`'s `_run_fidelity`) — not
discounted like semantic — since it recovers the exact original call under a known,
deterministic transformation, not an approximation. Confirmed end to end through the
real proxy (`tests/replay/test_replay_proxy.py`'s
`test_an_inverse_map_hit_is_recorded_with_match_tier_inverse`), not just at the
`ReplayStore` unit level.

### F-13 Semantic key resolution

**Technical:** Fallback matching on the sorted multiset of argument *values*, ignoring
parameter names, for cases the inverse mapping can't cleanly recover (e.g.
`tool_integration`). **Built** (`replay/replay_store.py`'s `semantic_key`/
`ReplayStore._semantic_index`): `ReplayStore.lookup` tries the exact key first,
falling back to a semantic-key lookup only on an exact miss, never the reverse —
matching docs/SPEC.md §7's decreasing-specificity tier ordering. Redacted the same
way and for the same reason `replay_key` already is, so a live lookup with real
secret values still matches an index built from already-redacted recorded ones.
Deliberately narrow: a genuine multiset match on VALUES, never fuzzy/partial-value
matching, and argument COUNT still has to line up (a 3-argument call can't
semantically match a 2-argument recording) — matching this project's "structural,
not free-text" stance elsewhere. Fidelity gating (F-15) discounts a semantic hit
relative to an exact one via the `match_tier` field this feature's own follow-on
added to `ToolCall` — see F-15's entry.

**Simple:** A looser last resort: even if Drifter can't figure out the exact renamed
field, if the actual data being passed looks the same as something it's seen before, it
can still make a reasonable guess.

**Depends on:** F-11.
**Done when:** a merged-tool fixture resolves via semantic match at a measurably better
rate than falling straight to synthetic — confirmed:
`tests/replay/test_replay_store.py`'s `test_a_renamed_parameter_resolves_via_
semantic_match_when_exact_misses` and `test_semantic_match_on_the_golden_fixture_
resolves_a_renamed_argument` both show a renamed-parameter lookup resolving via
semantic HIT where exact-only resolution would MISS.

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
from the denominator. Forces verdict to UNKNOWN below `fidelity_floor`. **Tier-
weighting built** (`evaluate/baseline.py`'s `_run_fidelity`, `calibration.yaml`'s
`semantic_weight` at 0.8): `record/schema.py`'s `ToolCall` gained a `match_tier`
field, set by `replay/replay_proxy.py`'s `on_call_tool` on every real HIT via the
same private-marker-key pattern (`MATCH_TIER_MARKER_KEY`) `result_provenance`
already used, so the served session's own records now carry which tier resolved
each call. A confirmed hit (`fault is False`) with `match_tier is None` — a record
from before this field existed — is treated as `"exact"`, not unknown: every tier
besides `"exact"` postdates this field's own introduction, so that's the verified-
correct reading of old data, not a risky assumption (see `ToolCall.match_tier`'s
own docstring). Inverse (tier 2, F-12) is now reachable too, as of F-40 — weighted at
the same full 1.0 as exact, not discounted like semantic (see F-12's own entry above).

**Simple:** Drifter grades its own homework before trusting its answer. If it had to
guess too much during a test, it says "I don't know" instead of reporting a fake
finding — and now, if some of what it used to answer was a looser guess (semantic
match) rather than an exact recording, that counts for a little less, not the same
as a sure thing.

**Depends on:** F-11–F-14.
**Done when:** an artificially low-fidelity fixture (forced high miss rate) produces
UNKNOWN rather than a REGRESSION verdict — plus (this round):
`tests/evaluate/test_baseline.py`'s `test_a_semantic_hit_is_discounted_relative_to_
an_exact_hit` confirms a mixed exact/semantic session's fidelity is the weighted
0.9, not the tier-blind 1.0 a plain hit ratio would report.

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

**Technical:** Originally: every mutated `tools/list` response sets `ttlMs: 0` and a
private `cacheScope`, per SPEC.md §10 and verified requirement C8. **Corrected,
docs/SPEC.md §15 limitation 15:** those fields exist only on the MCP SDK's draft
`2026-07-28` protocol type — every currently-negotiable real protocol version
(2024-11-05 through 2025-11-25) strips them from the wire via its own
version-specific surface model (`extra="ignore"`), confirmed by a real wire
capture. Not implementable against any real client today. The real protocol's
only tool-list invalidation mechanism, `notifications/tools/list_changed`, is
built for a manifest changing mid-connection, not for defending a cache reused
across connections — and doesn't map cleanly onto Drifter's baseline/mutated
arms, which are always separate, fresh subprocess connections in the first
place (which independently narrows how much of the original threat model even
applies). `replay/replay_proxy.py`'s `on_list_tools` deliberately does not set
these fields; see its own comment for the full account.

**Simple:** The original idea was to tell the agent's client "don't remember this
menu" — the real MCP protocol has no field to say that on a per-response basis, so
this doesn't do anything today. Drifter's fresh-process-per-arm design already
avoids most of the actual risk this was meant to guard against.

**Depends on:** F-01 (proxy response path).
**Done when:** ~~a caching-capable test client never reuses a mutated tool list
across two different mutation arms in a fixture~~ — superseded; see docs/SPEC.md
§15 limitation 15. Current done-criterion: a wire-level test confirms `ttlMs`/
`cacheScope` are honestly absent, not silently defaulted-in by a client parse.

### F-20 Header integrity on live forwards

**Technical:** Originally planned: strip inbound `Mcp-Method`/`Mcp-Name` headers on any
call touched by an active mutation; never forward a mutated call live, by design
(SPEC.md §10). **As actually built (F-20 audit, confirmed by grep — zero header-
stripping code exists in `src/`):** the primary defense turned out stronger than the
header-stripping plan — `replay/replay_proxy.py`, the module that serves the mutated
arm, has no code path capable of forwarding to a live server at all (no
`mcp.client`/`subprocess` import anywhere in the file), so header stripping as
"defense in depth" was never needed and was never built. The invariant holds by
construction, not by a runtime check.

**Simple:** Prevents a scenario where Drifter's fake tool name would get sent to a real
server and rejected — mutated calls simply never go live at all, because the code that
serves them doesn't know how to reach a live server in the first place.

**Depends on:** F-16, F-17.
**Done when:** no fixture produces a live forward for any call whose tool was touched
by an active mutation — locked in by
`test_replay_proxy_module_imports_nothing_capable_of_a_live_forward`
(`tests/replay/test_replay_proxy.py`), which inspects the module's own imports rather
than relying on a fixture never happening to exercise a future live path.

### F-40 Mutation operator: parameter_rename

**Technical:** Renames exactly one JSON-Schema top-level property per tool from
snake_case to camelCase (`customer_id` → `customerId`, docs/SPEC.md §13's own
illustrative report example), updating `required` in lockstep. Deterministic given
only the schema (alphabetically-first eligible property, seed selects nothing — there's
only one correct choice once eligibility is decided) and closed-set — a pure, mechanical
string rule, not chosen content, so unlike F-16/F-17 there is no table/pool to
safety-review. Tool name, description, and every other property are untouched.

**Simple:** Renames one input field the way a schema refactor might (`customer_id`
becomes `customerId`), so Drifter can test whether an agent still calls the tool
correctly when a parameter's name changes but its meaning doesn't.

**Depends on:** none (pure function over the manifest, same shape as F-16). Built
specifically to give F-12 a real inverse to resolve against — neither F-16
(schema-immune) nor F-17 (no prior recording to invert against) can.
**Done when:** a renamed parameter's `MutationLogEntry.inverse` (`{new_name: old_name}`)
correctly feeds `ReplayStore.lookup`'s `inverse_param_map`, resolving a live call using
the new name as a `match_tier="inverse"` HIT rather than MISS — confirmed by
`tests/replay/test_replay_proxy.py`'s
`test_an_inverse_map_hit_is_recorded_with_match_tier_inverse` through the real proxy, not
just at the operator's own unit-test level (`tests/mutate/test_parameter_rename.py`).

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
**Built** (`policy/safety.py`): `evaluate_safety`/`evaluate_safety_for_session`
resolve 2 of the 5 categories above from data this project actually records —
a call to a tool F-26 classifies `"destructive"`/`"irreversible_write"`, and a
call to a `policy.confirmation_required`-listed tool (treated as an automatic
finding, since no live-mode confirmation UX exists anywhere in this codebase to
have genuinely bypassed — see docs/SPEC.md §8's own implementation-status note for
the full account). The other 3 (capability outside `allowed_capabilities`, secret
leakage, annotation-behavior mismatch) are real, documented gaps, not silently
dropped — each blocked by a real, separate reason (an unspecified config field, a
structural recording invariant, F-26's own tier-3 stub respectively), not
reinterpreted loosely to look built. Wired into `drifter run`'s real report
(`cli/run.py`'s `_evaluate_safety_across_arms`) — evaluated across EVERY recorded
session from both arms, deliberately with no fidelity gate, matching docs/SPEC.md §8's
"evaluated on every run regardless of configuration."

**Simple:** Checks whether the agent did anything genuinely risky during the test —
independently of whether the task technically succeeded or the behavior merely
changed.

**Depends on:** F-26.
**Done when:** a fixture with a planted unexpected write to a destructive tool is
caught as a SAFETY VIOLATION even when Behavior shows NO_REGRESSION — confirmed at
the unit level (`tests/policy/test_safety.py`) and, more importantly, through the
REAL end-to-end pipeline: `tests/cli/test_run.py`'s `test_run_mutation_comparison_
reports_a_real_safety_violation_via_policy_override` runs a real replay-served
agent, forces a real golden-fixture tool into `policy.destructive`, and confirms
the rendered report shows `SAFETY VIOLATION` alongside a clean `BEHAVIOR
NO_REGRESSION` — the exact "reported even when Behavior shows NO_REGRESSION"
case docs/SPEC.md §8 describes.

### F-26 Tool risk classification

**Technical:** Six-level taxonomy (SPEC.md §10). **Built** (`policy/classify.py`):
`classify_tool`/`classify_manifest` resolve a `ToolDescriptor` through 4 tiers —
user policy override (`drifter.yaml`'s `policy.destructive`, `cli/config.py`'s new
`PolicyConfig`) wins UNCONDITIONALLY when set, then MCP annotations (only explicit
`True`/`False` hint values used as signal — an absent hint is never assumed to carry
the MCP SDK's own client-facing default), then a small, fixed, reviewable name-prefix
table (closed-set, same spirit as `mutate/description_update.py`'s synonym table —
a calibration-register-style heuristic, not a validated boundary), then observed
behavior. `classification_source` recorded per tool via a new `ClassificationSource`
value, `"unresolved"` — distinct from `"heuristic"`, since labeling an unresolved
result as if a tier had actually answered would be exactly the "plausible but wrong
value" pattern CLAUDE.md's testing discipline warns against.

Real, documented scope boundary, not a silent gap: the observed-behavior tier is a
deliberate stub that always declines — no signal Drifter currently records
(`result_shape`/`is_error`/`fault`) can honestly distinguish a write from a
read-only call, and inventing an unfounded heuristic here would violate this
project's own "verified, not assumed" discipline. `record/schema.py`'s
`ToolDescriptor` gains an `annotations: dict | None` field (the real wire
`tools/list` annotations block, captured by `record/writer.py`) to feed tier 1 —
cannot be added retroactively, same class of field as `is_error`/`timestamp`.

**Simple:** Sorts every tool into a danger level, and is honest about *why* it made
that call — a guess from the tool's name is treated with less trust than something
Drifter actually watched happen, and an explicit user decision beats every automated
guess.

**Depends on:** F-02 (manifest data), F-09 (observed behavior).
**Done when:** `drifter doctor` surfaces every ambiguous classification for one-time
user confirmation — confirmed against a real server (`tests/fixtures/fake_server.py`'s
add/echo/fail, none matching any known tier, all correctly reported `[WARN]
...unresolved`) and a real clean pass (`classifiable_server.py`'s get_status/
delete_record). Literal "before any live-mode run is possible" blocking is not
wired — no live-mode invocation path exists in this codebase at all (F-31/F-32 are
now built, but against `drifter run`'s real agent-spawning cost, not a live-server
mode that was never real to begin with — see their own entries) — a real,
narrower-than-spec scope decision, not silently dropped;
see `cli/doctor.py`'s own module docstring.

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
coverage. Requires explicit confirmation. **Built, honestly reframed**
(`policy/blast_radius.py`): "live mode" doesn't exist anywhere in this codebase (no
code path connects to a real MCP server during evaluation — every prior module that
touched this, F-25/F-26/F-37, already confirmed the same thing independently), and
"estimated replay coverage" as SPEC.md's own mockup describes it presupposes a live
server FALLBACK for a replay MISS, which also doesn't exist (a MISS synthesizes or
reports MISS, never falls through to a real call). Both are real, stated gaps, not
built. What IS real and gated: `drifter run`'s actual un-deferred cost TODAY —
spawning real agent subprocesses, `repeats` times per arm, twice — is now
unreachable without the preview (workflow count fixed at 1, matching `drifter run`'s
current one-task-one-operator scope; planned agent runs = `repeats * 2`; estimated
tool-call volume and risk breakdown computed from the fixture's OWN recorded call
sequence, classified via F-26, honestly labeled as an estimate) being shown and
either `--yes`/`assume_yes=True` or an interactive `y`/`yes` confirmation given.
Declining aborts before the real agent is ever spawned — confirmed with a
deliberately nonexistent agent command, so a would-be spawn failure is
distinguishable from a clean, pre-spawn abort.

**Simple:** Shows you exactly what's about to happen — how many real actions, how
risky — before anything actually runs live, so nothing surprising happens silently.

**Depends on:** F-26 (risk classification), F-15 (fidelity estimate — not actually
used; see "estimated replay coverage" above).
**Done when:** live mode is architecturally unreachable without this preview having
been shown and confirmed — reframed as: spawning a real agent process (`drifter
run`'s actual live-cost path) is architecturally unreachable without it. Confirmed:
`tests/cli/test_run.py`'s `test_run_run_declining_confirmation_aborts_without_
running_the_agent`.

### F-32 Budget and hard limits

**Technical:** `--budget N` (model calls), `--dry-run` (plan without executing),
`baseline.max_calls`, wall-time cap. Aborts cleanly with partial results reportable.
**Built, `--budget` honestly reframed** (`policy/budget.py`): "model calls" isn't
observable at all from what this proxy sees (docs/SPEC.md §15 limitation 2 — MCP
traffic is tool calls, never prompts or model reasoning), so `--budget N` counts
TOOL calls instead, the one real, countable proxy for cost Drifter actually has.
`BudgetTracker` is checked BEFORE each repeat starts, never mid-run — a real,
stated limitation, not silently glossed: a real agent subprocess, once spawned, is
never preemptively killed partway through for exceeding budget (that would need
this feature to reach into `cli/subprocess_adapter.py`'s live process management,
separate work not attempted here). The repeat that crosses the threshold still
completes and counts; every repeat after that is skipped before it's ever spawned.
One `BudgetTracker` is shared across BOTH arms (baseline + mutated) deliberately —
the budget is for the whole `drifter run` invocation's real cost, not per-arm.
`--dry-run` needed zero new computation: it's F-31's blast-radius preview, shown,
with execution simply never started. `baseline.max_calls` (SPEC.md §11's example)
and a true wall-time-based ABORT mid-agent-run remain unbuilt in their literal
form — `max_wall_time_s` here is the same before-each-repeat check as the
tool-call budget, not a mid-run timeout (a per-run `timeout_s` already existed,
separately, since F-34).

**Simple:** Hard ceilings so a test run can never quietly burn through your entire
daily API quota (or bill) without you knowing in advance roughly what it'll cost.

**Depends on:** all execution paths (F-21, mutation runner).
**Done when:** a run exceeding budget stops cleanly mid-execution and still produces a
report on the partial data collected — confirmed through the real end-to-end
pipeline: `tests/cli/test_run.py`'s `test_run_mutation_comparison_budget_limits_
the_number_of_real_agent_runs` runs a real replay-served agent with a budget of
exactly one successful run's worth of tool calls, and confirms the baseline arm
gets 1 valid run + 4 budget-exhausted exclusions while the shared tracker leaves
zero budget for the mutated arm at all — a real partial report, not a crash.

---

## Module: `cli/` and adapters

### F-33 `drifter init`

**Technical:** Scans common MCP client config locations (`.mcp.json`,
`.cursor/mcp.json`, `claude_desktop_config.json`), extracts server definitions, runs
initial tool classification (F-26), writes a starter `drifter.yaml`. **Built narrower
than this text** (see CHANGELOG.md's `drifter init` entry, added while sanity-checking
Gate 4's own handoff checklist): F-26 (`policy/`) doesn't exist yet, and
`cli.config.DrifterConfig` has no risk-classification field to populate even if it
did, so the shipped `cli/init.py` scans and writes `drifter.yaml` without the
classification step. The "Done when" bar below doesn't require classification output,
so this narrower version still satisfies it — same pattern as F-34's own documented
narrower-than-spec scope immediately below.

**Simple:** Finds your existing tool setup automatically and writes most of the config
file for you — you shouldn't have to type your own server list by hand.

**Depends on:** F-26 for the full spec above; the shipped version depends on none of
the above (see the narrowing note).
**Done when:** run against a real project, produces a working `drifter.yaml` with zero
manual edits required to run `drifter observe`. **Met** — verified end-to-end against
a real scanned `.mcp.json`.

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
tool calls correctly captured and correlated to a trajectory. **Met, for this
narrower scope, in Gate 2.** The eventual full scope this entry originally
described is now F-38, below — a separate, later feature, not a rewrite of what F-34
already shipped and passed.

### F-38 HTTP agent adapter (`agent.mode: http`)

**Technical:** The widened F-34 this entry's own text always pointed at, built now
that a real HTTP transport exists to build it against (SPEC.md §5.1). Drifter serves
`run_replay_proxy` over real Streamable HTTP (`mcp.server.streamable_http_manager.
StreamableHTTPSessionManager`, bound to `127.0.0.1` only, `Origin` validated on every
request per the MCP spec's own security requirements — never `0.0.0.0`, never
unauthenticated-by-oversight vs. unauthenticated-by-documented-decision) instead of
piping the agent's own stdin/stdout. The listening URL is injected into the spawned
process's environment (a configurable variable name, default `DRIFTER_PROXY_URL`) —
Drifter still launches the agent process (this is a widened MODE of the existing
adapter, not a new "point Drifter at an already-running service" capability; that's
explicitly out of scope here, see this entry's own Kill criterion in PHASES.md). Once
stdout is no longer occupied by the wire protocol, it becomes available to capture as
a separate final-answer string — restoring the capability F-34's own docstring noted
as dropped, though scoring that string against a task is still F-24/F-30 territory,
not this feature's job.

**Simple:** Lets Drifter test agents that can't have their stdin/stdout hijacked
directly — anything that expects to reach its tools over a URL, which is how most
real agent frameworks outside a bare CLI script actually work.

**Depends on:** F-34 (this widens it, doesn't replace it).
**Done when:** a real agent that talks to its tools via an HTTP-configured MCP client
(not a piped-stdio script) runs correctly under `agent.mode: http`, with its tool
calls correctly captured — the same bar F-34 met for stdio, met again for HTTP. This
is v1's first priority specifically because it's the direct retirement of Gate 4's
own unresolved kill criterion (docs/PHASES.md, docs/CHANGELOG.md). **Met.**

**Built, docs/PHASES.md's v1 task list has the full account.** Three real bugs found and
fixed during implementation, not just the happy path getting a passing test on the
first try:
1. `run_agent_subprocess_http` initially REPLACED the spawned agent's environment
   instead of inheriting it, silently dropping `PATH`/`SYSTEMROOT` and causing every
   real agent to fail to connect (zero recorded calls, no exception).
2. Forcibly cancelling uvicorn's serve task on shutdown raised a raw `WinError 995`
   mid-`accept()` on Windows — fixed with `should_exit` + a graceful task-group exit.
3. The severe one: `sse_starlette.sse.AppStatus.should_exit` is a bare class
   attribute shared across every server this process ever starts — the FIRST
   `agent.mode: http` run in a process always worked, and every subsequent one
   silently failed its first real request. This would have made the feature
   completely non-functional for its actual real use case
   (`evaluate.baseline.run_baseline`'s `repeats` loop runs the same process's server
   multiple times, 10 by default) had it shipped — a single passing test was not
   sufficient evidence, which is exactly why
   `test_several_sequential_http_mode_runs_in_the_same_process_all_succeed` exists as
   a permanent regression test, not just the original single-run test.

### F-39 HTTP real-server connection (`servers[].url`)

**Technical:** The separate half of "+HTTP in v1" (SPEC.md §5.1). **Built.**
`record/proxy.py` gains `ServerTarget = StdioServerParameters | str` and
`connect_to_server(server)`, which swaps `mcp.client.stdio.stdio_client(params)` for
`mcp.client.streamable_http.streamable_http_client(url)` purely by the target's own
type (a bare `str` is always a URL) — both are async context managers yielding the
identical `(read_stream, write_stream)` shape, confirmed against the installed SDK
before scoping this as low-risk, so `_pump`'s forwarding logic needed zero changes,
only the connection setup. `cli/config.py`'s `ServerConfig` gains `url: str | None`,
mutually exclusive with `command` (a `model_validator` enforces exactly one), and
`server_target()` is the one place that distinction turns into what `connect_to_server`
consumes — used by both `cli/observe.py` and `cli/doctor.py` (`connect_to_server` is
public, not `_`-prefixed, specifically so `doctor` doesn't need a second copy of the
same branch). This is what makes SPEC.md §2's "change one config line" onboarding
pitch literally true for the first time: a user with an existing remote MCP server
swaps `command: [...]` for `url: "..."` and `drifter observe` works unchanged.

Real, confirmed-empirically failure-mode difference from stdio, handled explicitly:
an unreachable URL doesn't fail synchronously at connect time the way a bad stdio
command does (`streamable_http_client` only actually attempts a connection once a
real request is sent), and the underlying `httpx2.ConnectError` arrives wrapped in
an `ExceptionGroup` (PEP 654), not bare. `cli/observe.py`/`cli/doctor.py` both use
`except*` (not a plain `except`) so this still surfaces as the same actionable
`ConfigError`/`ServerCheck` a bad stdio command already did, not a raw traceback —
confirmed by a dedicated test in each, not assumed from the stdio case's own fix
generalizing for free.

**Simple:** Lets Drifter record and replay against a real server that lives on the
network, not just one it spawns locally — most production MCP servers, as opposed to
local dev tools, are exactly this shape.

**Depends on:** none (mirrors F-01's own stdio connection, doesn't depend on F-38).
**Done when:** `drifter observe` against a real, network-reachable HTTP MCP server
records an identical-shaped session to an equivalent stdio server, and `drifter run`
replays it with no code path caring which transport originally recorded it — confirmed
end to end: `tests/record/test_proxy_http.py`'s
`test_run_passthrough_proxy_over_a_real_http_server_end_to_end` spawns
`run_passthrough_proxy` in a real separate process (agent-facing stdio, real-server-
facing Streamable HTTP against a real, replay-served server) and drives it exactly the
way `drifter observe` is really invoked, not just at the unit level.

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
**Both built.** `drifter score` met Gate 2's own exit test long ago (aggregate a
whole runs directory as one undifferentiated group, zero execution). `drifter
report` (`cli/report.py`) is the other half — `build_report_result` reconstructs a
`RunResult` from a specific prior `drifter run`'s `session_dir/{baseline,mutated}`
layout (`aggregate_baseline_runs` on each arm, `compute_behavior_effect_size`
between them, `policy.safety.evaluate_safety_across_arms` for Safety), and
`render_run_result` prints the exact same BEHAVIOR/TASK/SAFETY format `drifter run`
itself would. `RunResult`/`render_run_result` were split out of `cli/run.py` into a
new `cli/report_format.py` specifically so `report.py` never has to import
`cli.run` (which transitively pulls in `cli.subprocess_adapter`'s real
subprocess-spawning code merely by being imported) — checked by the same
AST-based no-live-connection test `cli/score.py` already established, applied to
both new modules, not just `report.py` itself. `policy.safety.
evaluate_safety_across_arms` moved from `cli/run.py` into `policy/safety.py` for
the same sharing reason, taking plain `destructive_override`/
`confirmation_required` sequences rather than a `cli.config.PolicyConfig` object,
since `policy/` sits below `cli/` in this project's module dependency order and
can't import from it.

Real, stated scope gap, not silently glossed: `RunResult.mutation_log` (which tool
was mutated, old→after description) is genuinely not reconstructable from disk —
nothing in the recorded session schema, or anywhere `run_mutation_comparison`
writes, persists which operator or seed produced a given `session_dir`. A
reconstructed report always has an empty `mutation_log` (so `render_run_result`'s
"MUTATION LOG:" section is simply omitted, no special-casing needed) and an
`operator` field that says explicitly it wasn't verified, rather than guessing.

**Simple:** Lets you re-analyze old test results instantly and for free — the whole
point of recording everything in the first place. `drifter report` specifically
gets you back the SAME full report a `drifter run` printed once, any time later,
without spending anything to re-generate it.

**Depends on:** any prior `drifter run` output.
**Done when:** re-scoring a week-old corpus produces output in seconds with zero
network calls. **This is Gate 2's exit test.** For `drifter report` specifically:
reconstructing a report from an existing `drifter run`'s stored sessions produces
the same BEHAVIOR/TASK/SAFETY verdicts as the original run did — confirmed by
`tests/cli/test_report.py`'s `test_report_reconstructs_the_same_verdict_a_real_
drifter_run_produced`, which runs a real `drifter run` end to end, then confirms
`drifter report` reconstructs an identical verdict from those same sessions alone.

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

Response mutation, state mutation, ~~HTTP adapter~~ (now in progress as F-38/F-39,
v1 — struck through here rather than removed, so this list stays an honest record of
what was excluded from F-01–F-37 and when each exclusion ended, not silently edited),
plan-only mode, delta debugging, workflow graph mining, LLM judge oracle, hosted mode,
dashboard, accounts. Each is a real, previously-discussed idea. None was required to
prove the core loop (F-01 through F-37) works, and each added cost, safety risk, or
setup burden that would have delayed Gate 1. See PHASES.md for where each resurfaces.
