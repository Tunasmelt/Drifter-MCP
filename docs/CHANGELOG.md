# Drifter — Specification Changelog

Tracks how SPEC.md, FEATURES.md, and PHASES.md arrived at their current state.
Written so future amendments follow the same discipline: every change gets a reason,
not just a diff.

---

## F-19 audit: build-status table was wrong, the "fix" was real-protocol-ineffective, documented instead

Continuing the edge-case pass onto F-17 through F-24, F-19 (cache-busting on mutated
`tools/list` responses) was the first stop and turned into a full investigation rather
than a quick audit. docs/FEATURES.md's build-status table claimed "✅ Built | Gate 3" for
it; a whole-tree grep for `ttlMs`/`cacheScope` returned zero matches in `src/` or
`tests/` — confirmed unimplemented before touching anything.

The obvious fix — `types.ListToolsResult(tools=tools, ttl_ms=0, cache_scope="private")`
in `replay/replay_proxy.py`'s `on_list_tools`, using the MCP Python SDK's own real
field names — was implemented and looked correct. A new wire-level regression test
(tapping the literal JSON bytes on the wire, not a client-parsed result object, since
`ListToolsResult`'s own `ttl_ms`/`cache_scope` fields carry non-None client-side
defaults regardless of what the server actually sent) still failed identically to
before the fix. Ruled out `exclude_unset` client-side stripping (confirmed empirically
that passing a field's own default still marks it "set" in `model_fields_set`) and a
stale-file issue before tracing the SDK's real dispatch chain:
`mcp/server/runner.py`'s `_dump_result` → `mcp_types.methods.serialize_server_result`,
which re-validates a handler's result dict against a **version-specific surface
model** keyed by the session's negotiated protocol version, with `extra="ignore"`
silently stripping anything that version's model doesn't define. `ttl_ms`/
`cache_scope` exist only on `mcp_types._v2026_07_28.ListToolsResult` — a draft,
not-yet-real protocol version — never on 2024-11-05 through 2025-11-25, i.e. never on
any protocol version any real MCP client can currently negotiate.

A direct fetch of the real, current MCP spec (2025-11-25) confirmed no per-response
cache-control mechanism exists in the real protocol at all; the only real
invalidation path is `capabilities.tools.listChanged` + `notifications/
tools/list_changed`, built for "the list changed mid-session," not for defending a
cache reused across connections — and it doesn't map cleanly onto Drifter anyway,
since `run_mutation_comparison`'s baseline and mutated arms are always separate,
fresh subprocess connections, which independently narrows how much of the original
threat model (C8) even applies here.

Given a real, currently-standardized alternative existed (varying `serverInfo.version`
between arms) but wasn't yet reviewed or built, the choice was: document the gap and
revert the ineffective fields, rather than ship code that implies a guarantee the real
protocol doesn't carry, or build a new mechanism as a byproduct of an edge-case audit.
Reverted `on_list_tools` to its original `types.ListToolsResult(tools=tools)`, with a
comment recording the full chain. docs/SPEC.md gets a new §15 limitation 15 with the
complete investigation. docs/FEATURES.md's F-19 row and feature entry are corrected to
"❌ Not built — real-protocol gap" with a pointer to limitation 15.
`tests/replay/test_replay_proxy.py`'s regression test now asserts the confirmed
ABSENCE of `ttlMs`/`cacheScope` on the real wire — an honest lock on current behavior,
not a forced-green assertion of something that doesn't work.

---

## F-13 (semantic key resolution) built: the priority-1 item from docs/SPEC.md §15 limitation 16

Following the user's explicit choice to pursue "adapting" over the report-redesign
alternative for limitation 16 (exact-match replay collapsing against a real,
unscripted agent — the Gate 4 real-test finding), F-13 is built: `replay/
replay_store.py` gains a second index keyed on `semantic_key(server, tool_name,
arguments)` — `sha256` over the sorted, canonicalized multiset of argument VALUES,
ignoring parameter names entirely, per docs/SPEC.md §7 tier 3.

`ReplayStore.lookup` tries the exact key first and only falls back to the semantic
key on an exact miss, matching §7's decreasing-specificity ordering (exact, then
inverse [F-12, still unbuilt], then semantic) — a tighter match available is never
discarded for a looser one. `semantic_key` reuses `replay_key`'s own redaction and
canonicalization conventions (redact before hashing, `sort_keys` + no incidental
whitespace) and treats the values as a genuine multiset, not a set — `{"a": 5, "b":
5}` and `{"a": 5}` must hash differently, or a real duplicate-value argument set
would silently collapse. `replay/replay_proxy.py` needed zero changes: it already
calls `replay_store.lookup(...)` generically and doesn't branch on tier, so a
semantic hit resolves at the wire level exactly like an exact one, automatically.
13 new tests in `tests/replay/test_replay_store.py`, including a renamed-parameter
lookup against both a hand-built session and the real golden fixture, confirming
F-13's own "Done when" bar directly (resolves via semantic HIT where exact-only
resolution would MISS).

**Deliberately not done here, flagged rather than silently assumed:** `evaluate/
baseline.py`'s `_run_fidelity` is still completely tier-blind — a semantic hit
counts as a full 1.0-weight "confirmed hit," identically to an exact one, because
the served session's recorded `ToolCall` has no field carrying which tier resolved
it. docs/SPEC.md §7's target formula (`exact + inverse + SEMANTIC_WEIGHT × semantic`,
weight already 0.8 in `calibration.yaml`) is not wired up. This means real fidelity,
as SPEC.md itself defines it, is somewhat OVER-stated wherever semantic matches are
now involved — the opposite direction of a false negative, and worth being explicit
about specifically because this project just spent real effort (the Gate 4 finding)
on the danger of a report looking more confident than its underlying data supports.
Wiring in the weight needs a schema change (a new nullable field on `ToolCall`,
recorded at replay-serve time) — this project's own required pre-change-test
procedure applies, and F-13 itself didn't need that change, so it's left as F-15's
own next increment rather than folded in here as scope creep. docs/SPEC.md §7's
implementation-status note, docs/FEATURES.md's F-13/F-15 entries, and
`evaluate/baseline.py`'s own docstring are all updated to say this plainly.

---

## F-10 retry-misdetection: redesigned and fixed, after a false start caught before it shipped

Following the user's explicit go-ahead to redesign the F-10 fix despite it touching
CLAUDE.md's non-negotiable "never write payload data" invariant: `record/redact.py`'s
fixed `"[REDACTED]"` placeholder collapsed every secret matching a given pattern to
the same value, which `drifter stats`' retry heuristic (identical-consecutive-
arguments) read as a false retry whenever two DIFFERENT real secrets happened to
match the same pattern.

**First design attempt was wrong and was caught before landing, not after:** a
per-`SessionRecorder`-instance random salt looked like the obviously more private
fix (same secret → same marker within a session; different secret → different
marker; same secret across two sessions → different marker, defeating any
cross-corpus dictionary check). It was never committed — re-reading `replay/
replay_store.py`'s own `replay_key` docstring before implementing surfaced that
tier-1 exact-key replay redacts a LIVE call's raw arguments at lookup time and
requires landing on the EXACT SAME marker the original recording session already
wrote, from a completely different process with no access to that session's salt.
A random salt would make every secret-shaped argument miss on replay, permanently
and silently — regressing F-11 to fix F-10.

**Actual fix:** the marker is now `sha256(matched_value)`, truncated to 12 hex
chars (`[REDACTED:xxxxxxxxxxxx]`) — deterministic (no salt), so F-11's replay
matching keeps working exactly as before, while still distinguishing different
secrets from each other (fixing F-10) and remaining infeasible to invert (every
input routed through it is high-entropy by construction — a structured credential
format or already past the entropy/character-class gate). Documented trade-off,
not silently accepted: an attacker holding both a candidate secret guess and a
recorded corpus can confirm whether that guess was used, across sessions — the
same property the old fixed placeholder already had zero of either way, so this is
a net addition of same-value-equality information only, not a regression from a
stronger baseline.

`record.redact.REDACTED` (the old fixed-string constant) is removed — nothing
should compare against a specific marker value anymore, since it's now
value-derived. Replaced by `record.redact.is_redaction_marker(value)`, a proper
shape check, used everywhere the old constant-equality checks were
(`tests/record/test_redact_unit.py`, `tests/cli/test_stats.py`). The exact test
that had locked in the old bug as documented behavior
(`test_two_different_secrets_that_both_redact_identically_are_misdetected_as_a_
retry`) is flipped to confirm the fix
(`test_two_different_secrets_are_no_longer_misdetected_as_a_retry`) rather than
deleted, preserving the regression coverage. Two new unit tests
(`test_two_different_secrets_redact_to_different_markers`,
`test_the_same_secret_always_redacts_to_the_same_marker`) pin down the two
properties the whole design rests on. `docs/FEATURES.md`'s F-10 row moves from
"Built, real interaction confirmed" (documented tradeoff) to "✅ Built" (fixed).

---

## Two "needs fixing" items closed out: one real fix, one confirmed non-bug

Following up directly on the previous entry's triage: two of the three narrow,
non-architectural items were run down to a real conclusion rather than left open.

**F-16's ALL-CAPS case-preservation gap — fixed, red-test-first.** `_substitute_
synonyms`'s case logic only ever checked the matched word's first letter
(`if matched_word[0].isupper(): synonym[0].upper() + synonym[1:]`), so an ALL-CAPS
source word ("GET") came out title-cased ("Obtain") rather than ALL-CAPS ("OBTAIN").
A new test asserting the correct behavior was written and confirmed to fail against
the pre-fix code first (`test_all_caps_substitution_preserves_all_caps_not_just_
title_case`, replacing the old test that asserted the bug's own behavior as a
"documented limitation"), then fixed: case CLASS (all-caps vs. title-case vs.
lowercase) is now preserved, not just the first letter, with
`test_title_case_substitution_still_preserves_title_case_not_all_caps` added
alongside it to confirm the existing, correct title-case path didn't regress. All 22
tests in `tests/mutate/test_description_update.py` pass. FEATURES.md's F-16 row
updated: one known gap fixed, the closed-set injection-pattern gap (limitation 13)
remains, deliberately, per that gap's own already-reviewed decision not to widen the
pattern list reflexively.

**The Gate-4-report's "unconfirmed proxy fidelity" signal — investigated, does not
reproduce.** The prior entry's third finding (a `list_directory` call against an
out-of-bounds parent directory reportedly recording `is_error: false` through the
proxy) was run down directly rather than left as an open flag: a real
`@modelcontextprotocol/server-filesystem` instance and a real `drifter observe`
proxy, driven by a hand-built MCP client, both agree — the identical out-of-bounds
call returns `is_error: true` in both the direct and proxied case, and the raw
recorded JSONL confirms `"is_error": true` was actually written, not just reported
correctly by an aggregate. `record/proxy.py`'s error forwarding is correct for this
case. docs/SPEC.md §15 limitation 16 updated to record this as investigated and not
reproduced, rather than leaving an ambiguous "needs follow-up" hanging indefinitely —
the original subagent's report almost certainly compared against a differently-scoped
or differently-resolved path than it believed, not an actual fidelity gap.

**F-10's retry-misdetection item was NOT fixed, and shouldn't be without a separate
decision.** On closer inspection this doesn't have a narrow fix available: retry
detection (`cli/stats.py`) compares already-redacted arguments because that's the
only form of the arguments this project's own non-negotiable invariant (CLAUDE.md,
docs/SPEC.md §3 — recording never writes payload data by default, only shapes) allows
onto disk in the first place. Any fix that distinguishes two different real secrets
from each other necessarily requires writing *something* derived from the actual
secret value that isn't a fixed placeholder — even a hash, despite revealing nothing
about the value itself beyond equality, is a real, deliberate loosening of that
invariant, not a bug fix. This is the "architectural invariant seems wrong" case
CLAUDE.md reserves for a deliberate decision, moved from the "needs fixing" bucket to
"needs adapting," not silently patched.

---

## Gate 4's real second-user test, attempted for real: exact-match replay does not hold up against a genuine agent

Gate 4 was previously closed by explicit, unverified override (previous entry below):
no real second user had ever run `init → observe → run` unassisted, and the honest
record said so. Rather than leave that debt sitting under an ever-growing v1 feature
stack indefinitely, and since no real second human is available, the closest available
substitute was actually attempted: an independent, blind subagent — zero memory of
this codebase or conversation, briefed only from `README.md`, explicitly instructed
not to read `CLAUDE.md`/`docs/*` unless genuinely stuck the way a real user only reads
internal docs after hitting a wall — was tasked with setting Drifter up from scratch
against a real MCP server (`@modelcontextprotocol/server-filesystem`) and driving a
REAL, non-scripted agent through it: a headless `claude -p` subprocess, not
`tests/fixtures/scripted_agent.py`, which every prior gate's own testing (including
the Gate 4 dry run) has used instead of a genuinely independent agent.

**What worked, genuinely:** `drifter init` (found nothing before a `.mcp.json`
existed, worked correctly once one did — an honest, actionable failure, not a crash),
`drifter observe`, `drifter doctor`, `drifter stats`, `drifter score` — all usable
unassisted, all honestly interpretable, matching this project's own stated design
principles.

**What didn't:**

1. **A real documentation gap** — `drifter run` cannot be configured from README
   alone. The required `agent:` block in `drifter.yaml` (`agent.mode`/`agent.command`,
   docs/SPEC.md §11) was never mentioned in the Quickstart, and `mode: subprocess`
   doesn't fit how Claude Code CLI actually invokes MCP servers (it spawns its own
   children via `--mcp-config`, it doesn't speak MCP on its own stdio) — the test
   agent had to source-dive `cli/run.py`/`cli/subprocess_adapter.py`/`cli/config.py`
   and hand-build an `agent.mode: http` wrapper before `drifter run` would run at all.
   **Fixed in this entry** — README's Quickstart now documents both `agent.mode`
   values and the `mode: http` case's actual fit for a CLI-driven agent like Claude
   Code, a straightforward factual correction, not an architectural decision.
2. **The real, architectural-level finding** — once configured and run
   (`calibration.yaml`'s default `repeats: 10`/arm), 9/10 baseline and 8/10 mutated
   real runs were excluded for fidelity below the 0.70 floor (F-15/F-22's gating,
   working exactly as designed), leaving 1 valid baseline and 2 valid mutated runs to
   compute a verdict from. The report displayed a confident, unqualified `BEHAVIOR
   REGRESSION`. Reading the actual transcripts of every surviving "valid" run showed
   each was itself a near-total-failure session — the agent reporting the proxied
   tools "aren't returning results" or hitting "replay MISS," several mutated-arm
   agents falling back to native tools entirely. The verdict was measuring which of
   two small, mostly-degenerate failure pools happened to fail in a more similar
   shape, not behavioral drift on a completed task — and nothing in the report
   surfaces that thinness to a reader who doesn't go read the raw transcripts. This
   confirms and quantifies, with real numbers for the first time, the tier-3 finding
   Gate 3 carried forward as "possibly blocking exact-tier replay's real-world
   viability" — see docs/SPEC.md §15, new **limitation 16**, for the full account,
   including two secondary findings (the doc gap above; a real agent preferring
   native tools over MCP-proxied ones when both are available for a vague prompt) and
   one unconfirmed signal flagged for follow-up (a possible proxy fidelity/
   error-forwarding discrepancy on an out-of-bounds path — `list_directory` against a
   parent directory recorded `is_error: false` through the proxy while a direct,
   non-proxied call to the same real server correctly refused; payload redaction
   blocked full root-causing, so this is reported, not asserted, as a bug).

**Deliberately not patched here.** README's `agent:` block was a genuine, simple,
factual documentation gap — fixed directly in this entry per this project's own
standard for that class of issue. The exact-match-replay finding is not: it strikes
at README's own "replayed... at zero marginal cost" / "record once, replay for free"
framing, which is squarely the "architectural invariant seems wrong" case CLAUDE.md
reserves for a deliberate decision, not a reflexive patch. `.drifter/GATE_STATUS`'s
`gate_4_status` is updated to `override_followed_by_real_test_blocking_issue_found` —
neither the original unverified override nor a clean pass, its own honest category —
with the original override text preserved verbatim beneath the update rather than
overwritten. Whoever picks up v1 next needs to decide explicitly how to respond to
this before trusting `drifter run`'s verdicts or building further v1 features
(F-39/F-13/`policy/`/`mine/`, the previously-recorded priority order) on top of the
mutation-comparison pipeline as it currently stands.

---

## F-20 audit: the planned mechanism was never built, but the real invariant holds — now locked in

Continuing straight from the F-19 finding, F-20 (header integrity on live forwards)
showed the same symptom on inspection: a whole-tree grep for the originally-planned
`Mcp-Method`/`Mcp-Name` header-stripping (docs/SPEC.md §10, SEP-2243) returned zero
matches in `src/`. Unlike F-19, this did not turn into a real-protocol dead end —
`replay/replay_proxy.py`'s own module docstring already documents the actual, stronger
mechanism: the module has no code path capable of forwarding to a live server at all
(no `mcp.client`/`subprocess` import anywhere in the file), so a mutated call
structurally cannot reach a live server, which is what F-20's invariant actually
requires — header stripping was only ever "defense in depth" for a path that turns out
not to exist.

The gap was that nothing enforced this structurally: the docstring's claim
("confirmed by inspection, not by a flag defaulting the 'right' way") had no test
behind it, so a future edit that added a live-connection import to this file would
regress the invariant silently. Added
`test_replay_proxy_module_imports_nothing_capable_of_a_live_forward`
(`tests/replay/test_replay_proxy.py`), which parses the module's own AST and asserts
no `mcp.client.*` or `subprocess` import is present — checking the guarantee at the
same layer it actually lives (imports), the way CLAUDE.md's own instruction for this
class of invariant asks for ("verify this is still structurally true, not just true by
default configuration"). docs/FEATURES.md's F-20 row and feature entry are corrected to
describe the real, as-built mechanism rather than the original header-stripping plan.

---

## F-19 audit: build-status table was wrong, the "fix" was real-protocol-ineffective, documented instead

Continuing the edge-case pass onto F-11 through F-16. F-12 (inverse-mutation key
resolution) and F-13 (semantic key resolution) have no code at all — confirmed directly
from `replay_store.py`'s own docstring, not assumed — so no tests apply to them; they
remain **needs building** per the build-status table.

**`tests/replay/test_replay_store.py`** (F-11) — 6 new tests. The existing 9 were
thorough but every one only ever indexed a single file (the golden fixture) or a single
hand-built one: confirmed `index_session()` called twice on the same store correctly
merges both files, and that last-writer-wins holds ACROSS files, not just within one.
Also: a real protocol-level fault hit (fault=True, result_shape=None — never exercised
by the golden fixture, which has zero fault calls, confirmed directly) round-trips
correctly; an empty session (zero ToolCalls) doesn't crash; and `replay_key`'s
canonicalization was only ever confirmed at the top level before this — now confirmed
for NESTED dict key order too.

**`tests/replay/test_replay_proxy.py`** (F-14) — 2 new tests for
`_synthesize_call_tool_result`'s `content_length` reconstruction, which the existing
11 tests only ever exercised implicitly via the golden fixture's own real content
shapes. Hand-built `RecordedResponse`s confirm the two ends of the real range the golden
fixture never contains: a recorded response with a genuinely EMPTY content array
(`array_lengths["content"] == 0`) synthesizes as empty, not the length-1 default; and a
recorded MULTI-block response (3 content items) synthesizes with the same count, not
silently collapsed to 1.

**`tests/mutate/test_description_update.py`** (F-16) — 2 new tests. Empty and
whitespace-only descriptions don't crash (changed=False, honestly reported). A second,
narrower known limitation found and confirmed alongside the already-documented
injection-pattern gap: case preservation in `_substitute_synonyms` only checks a
matched word's first letter, so an ALL-CAPS source ("GET") comes out title-cased
("Obtain"), not ALL-CAPS ("OBTAIN") — the same class of narrow, low-probability,
confirmed-not-fixed limitation as the existing letter-based (not phonetic) article-
agreement fix.

F-15 (fidelity computation/gating, `evaluate/baseline.py`) was reviewed but already had
22 exceptionally thorough existing tests covering every documented edge case — no gap
found worth adding to.

No new bugs found in this batch (F-01–F-10's pass found and fixed a real `drifter
observe` bug; this one didn't surface an equivalent). Full suite re-run after all of the
above.

---

## F-06–F-10 edge-case pass: a real `drifter observe` bug found and fixed

Continuing the F-01–F-05 edge-case pass onto the next five features.

**`tests/record/test_segment_unit.py` (new)** — `record/segment.py`'s `TrajectoryTracker`/
`Trajectory` (F-06/F-07/F-08) was previously only exercised through 3 real-subprocess
integration tests (`test_segment.py`, still in place, unchanged), each covering exactly
one documented top-level scenario. 14 new fast, direct tests cover pieces of the actual
logic never touched directly: `extract_trace_id`'s malformed-input fallback (5 shapes:
truncated, wrong-length trace id, missing segment, empty, non-hex version — all fall
back to `None`/heuristic, none crash) and case-insensitive hex; two distinct trace IDs
in one session correctly kept separate (the existing integration test only ever uses
one); a trace-tagged call interleaved between two heuristic calls never resets the
heuristic idle timer (confirms the two mechanisms genuinely don't cross-contaminate);
`close_all()` clears state; the JSON-string-unwrapping branch of `_iter_leaf_paths`
(text-wrapped structured results, a non-JSON-looking string treated as an opaque leaf,
a malformed-JSON-looking string that doesn't crash); "last writer wins" when two prior
calls produce an identical value; and the module's own documented falsy-value
spurious-reference tradeoff, now actually confirmed rather than only claimed in a
comment.

**A real bug found and FIXED in `cli/observe.py` (F-09)**, not just documented: `drifter
observe` against a bad server command crashed with a raw ~40-line Python traceback
(`FileNotFoundError` from deep inside `anyio`/`asyncio`/`subprocess`), while `drifter
doctor`'s `_check_server` already handles the IDENTICAL failure actionably. Confirmed
empirically before fixing (a real `drifter observe` invocation against a nonexistent
executable, not assumed from reading the source). Unlike F-05's gap, this wasn't a
genuine architecture dilemma — asked explicitly, and confirmed a straightforward fix
with clear precedent already in the codebase: `run_observe` now wraps the same `OSError`
in the same `ConfigError` every other subcommand's config/connectivity failure already
surfaces as, so `cli/app.py`'s existing `except ConfigError` handling (exit code 4,
docs/SPEC.md §12) picks it up for free — no dispatch change needed. Verified against the
real CLI after the fix: clean one-line message, exit code 4, no traceback.
Red-test-first: `test_run_observe_raises_actionable_config_error_for_a_bad_server_command`
failed against the original code before the fix, confirmed passing after.

**`tests/cli/test_stats.py`** — 1 new test confirming a real, previously untested
interaction between F-04 (redaction) and F-10 (retry detection): `record/writer.py`'s
`_write_tool_call` redacts `arguments` BEFORE write, so `drifter stats`'s retry
comparison only ever sees the post-redaction values. Two calls carrying two DIFFERENT
real secrets that both redact to the same `[REDACTED]` marker are therefore genuinely
indistinguishable by the time stats runs, and get counted as a retry even though they
weren't one — confirmed with two real, distinct planted secret values, not fixed (same
class of accepted literal-matching tradeoff as F-08's spurious-reference case).

Full suite re-run after all of the above (F-01–F-10 combined).

---

## F-01–F-05 edge-case pass: `record/writer.py` gets its first dedicated unit-test file

Asked directly to test F-01 through F-05 for edge cases and give F-05's real, known gap
(SPEC.md §15 limitation 14) proper coverage. Confirmed explicitly with the user first
that "cover it" meant test-only, not a fix — a real fix means either deferring
`SessionStart`'s write until end-of-session (contradicting the recorder's own
"always-first-record" invariant) or giving the hash a separate, later-arriving home in
the schema, a genuine design decision this pass deliberately does not make.

**`tests/record/test_writer.py` (new)** — `record/writer.py` (437 lines, the largest
module in `record/`) had no dedicated unit-test file before this; it was only exercised
indirectly through real-subprocess integration tests (`test_proxy.py`) and fixture-based
read tests, neither of which can deterministically control exact message ORDERING —
exactly what limitation 14 is about. 11 new tests, driven directly via `observe()` with
hand-built JSON-RPC messages (no subprocess, sub-second total runtime):
- `SessionStart` always `seq=0`; `seq` monotonic and never reused; `close()` idempotent.
- A parse-error message increments `error_count` and writes nothing; a response with an
  untracked request id is silently ignored, not crashed — both previously only implied
  by the source, never directly tested.
- Raw frame offsets stay correct and distinct across multiple sequential calls (the
  existing integration test only ever checked one call).
- Two `_meta.traceparent`-tagged trajectories are correctly kept separate, never merged.
- **The real F-05 gap**, given its proper home: `tools/call` before any `tools/list`
  permanently nulls `tool_manifest_hash`, confirmed directly at the layer the bug lives
  in, plus a sharper version — `tools/list` arriving LATER in the same session is still
  too late, confirming this is genuinely about order, not about whether `tools/list`
  ever happens.
- A purely empty session (bare `initialize`, no calls at all) still gets a `SessionStart`
  flushed at `close()`, with a null hash — the documented "safety net," confirmed.

**`tests/record/test_redact_unit.py`** — 7 new tests. Non-string/non-container values
(int, bool, None, float) pass through `redact_secrets` completely unchanged; a secret
embedded in surrounding text is substring-replaced, not whole-string-nuked (exact
expected output, not just "the secret is gone"); empty strings/dicts/lists don't crash.
`redact_rpc_payload` — the function specifically scoping redaction to `params`/`result`/
`error.data` on the RAW FRAME MIRROR path, the exact place SECURITY.md calls out as easy
to leave a gap in — had zero direct test coverage before this: now confirmed the
envelope fields (`jsonrpc`/`id`/`method`, structural `error.code`/`message`) survive
untouched, the payload fields get redacted, the input isn't mutated, and a bare
notification with none of the optional fields doesn't raise `KeyError`.

**`tests/record/test_proxy.py`** — 1 new test, `tests/fixtures/crashing_server.py`
(new): a real, immediate crash (`os._exit(1)`, not a cooperative exit) in the spawned
server, right after `initialize` — confirms the proxy notices the dead child and tears
down promptly (timed, per this project's own standard for shutdown-path code) rather
than hanging waiting for a process that will never respond again. Every existing
shutdown test in this file covered the AGENT disconnecting; this is the other real
direction. No bug found here — this test locks in correct existing behavior.

Full suite: 61/61 in `tests/record/` (36 pre-existing + 25 new).

---

## FEATURES.md gains a build-status table — 24/39 built, audited for creep vs. deliberate scope

FEATURES.md was written pre-code (Gate 0) and, unlike PHASES.md (which gets dated
Status sections per gate) or SPEC.md (which gets numbered limitations), never gained a
per-feature record of what's actually built versus still aspirational. Asked directly
to audit the 24 features already marked done and classify what's left, what was
deliberately scoped down with a documented reason, and what (if anything) crept beyond
what was actually asked — a real accounting, not assumed from memory:

- **Left out** (a real gap found empirically, after the fact, in a feature already
  built): F-05/F-09 (limitation 14, null `tool_manifest_hash` on call-order), F-07
  (limitation 8), F-09 (limitation 9), F-16 (limitation 13), F-11 (the still-open
  tier-3 finding). None of these were planned narrowings — each was found by actually
  running the thing against something real and locked in with a regression test.
- **Deliberately scoped narrower, documented at build time**: F-16/F-17's closed-set
  mechanism, F-18's minimal shared audit-log shape, F-23's zero-spread edge case, F-33's
  missing F-26 classification, F-34's original stdio-only scope, F-35's Gate-3-minimal
  orchestration, F-38's Kill-criterion-bounded widening. Every one has a doc trail
  (a module docstring, a FEATURES.md note, or a CHANGELOG entry) written before or
  during the narrower version shipping, not after being questioned.
- **Scope creep**: one real, mild case — F-38's final-answer stdout capture (a
  `.stdout.txt` sidecar file) wasn't strictly required by "widen the subprocess
  adapter," added because F-34's own docstring had noted the capability as dropped and
  restoring it became free once stdout stopped being the wire protocol. Narrowly scoped
  (no consumer wired) and documented, but a judgment call beyond the literal ask, not
  an instruction followed.

The new table (top of FEATURES.md, before the per-module breakdown) records all of
this plus every not-yet-built feature's status, and ends with a priority order for
what to build next that's a restatement of docs/PHASES.md's own v1/v1.5/v2 dependency
chain in one place, not a new ranking: F-39 first (scoped, lower-risk than F-38 was),
then F-13 (the tier-3 gap, now "possibly blocking" rather than "nice to have"), then
`policy/` (F-26 → F-25 → F-31/F-32), then `mine/` (F-28 → F-29 → F-30 → F-24's real
authoring UX), then F-27/F-36's `drifter report` last.

---

## F-38 re-audit: a fourth real bug found, 8 new edge-case tests

A deliberate second pass over F-38's just-committed implementation, not new feature
work — re-reading `cli/http_proxy.py`/`cli/subprocess_adapter.py` fresh, the way a
reviewer would, rather than trusting the previous round's passing tests as the last
word.

**A fourth real bug, found on re-read, not by a failing test:** the shutdown-safety
fix from the previous round (`should_exit` + graceful task-group exit, avoiding the
`WinError 995` bug) only ran when the `yield url` block exited normally. If
`_wait_until_actually_answering` itself timed out and raised — or if the caller's own
code inside the `async with serve_replay_over_http(...)` block raised — that exception
would unwind the task group directly, triggering anyio's own cancel-everything-on-
exception behavior BEFORE `should_exit` had a chance to be noticed: the exact unsafe
path the previous fix was meant to close. Fixed by tracking the serve task's own
completion explicitly (`serve_done: anyio.Event`) and awaiting it (bounded) inside a
`_graceful_shutdown()` helper that runs regardless of how the block exits, so no exit
path — normal or abnormal — can reach the task group's own forced-cancellation
behavior. Locked in by `test_an_exception_inside_the_context_manager_still_shuts_down_
gracefully`, which also documents a real, non-obvious consequence of this design worth
knowing about: an exception raised inside the context manager now comes back wrapped
in an `ExceptionGroup` (PEP 654), not bare — a caller catching a specific exception
type around it needs `except*`.

8 new edge-case tests added across the F-38 surface, covering real gaps the original
round's tests didn't reach:
- `tool_addition` and a real REGRESSION verdict, both previously only exercised over
  stdio, now confirmed over HTTP too (`test_run.py`) — using the same real planted
  substring and recorded arguments as Gate 3's own kill-criterion test.
- A MISS/fault recorded correctly over HTTP (parity with the existing stdio test).
- The Origin-validation boundary made explicit: a loopback-*looking* Origin is still
  rejected (not just a foreign one), since `allowed_origins` is deliberately empty.
- `env=` overrides are preserved alongside the inherited environment, not just one or
  the other (a direct regression test for the third round's env-replacement bug).
- `drifter doctor`'s one previously-untested failure branch: an unbindable loopback
  port reported actionably, not a doctor crash. Needed a properly SCOPED monkeypatch
  (replacing the `socket` name inside `cli.doctor`'s own namespace, not mutating the
  real, process-wide `socket.socket` class) — the first attempt patched the real
  class and broke `asyncio`'s own Windows event-loop setup, which uses
  `socket.socketpair()` internally.

Full suite re-run after all of the above: previously 275 passed, now +8 (this round's
new tests) + the http_proxy exception-handling fix.

---

## F-38 (HTTP agent adapter) built and tested — three real bugs found and fixed

Implements the plan from the previous entry. `cli/config.py` gains `AgentConfig.mode`/
`env_var`; `replay/replay_proxy.py`'s `build_replay_server` is extracted (pure
refactor, zero behavior change — the existing 34-test suite passed unchanged) so the
new `cli/http_proxy.py` can host it over real Streamable HTTP via
`Server.streamable_http_app()`, loopback-bound and `Origin`-validated against the
SDK's actual validation source (confirmed by reading it, not the settings model's
field names alone); `cli/subprocess_adapter.py` gains `run_agent_subprocess_http`;
`cli/doctor.py` gains an `agent.mode: http` diagnosability check;
`tests/fixtures/scripted_agent.py` gains a real HTTP reference-agent mode sharing its
existing per-spec loop; `cli/run.py` wires `agent.mode`/`agent.env_var` end to end
through the real config-driven `drifter run` path. 21 new tests, plus the extraction
verified against the existing suite.

Three real bugs found and fixed during implementation, not assumed away by a passing
happy-path test:

1. **Environment replacement.** The first version of `run_agent_subprocess_http` built
   the spawned agent's environment as `{**(env or {}), env_var: url}` — this REPLACES
   the child's entire environment rather than inheriting it (unlike passing `env=None`
   straight through, which inherits — standard `subprocess.Popen` semantics). Silently
   dropped `PATH`/`SYSTEMROOT`/everything else a real interpreter and its networking
   stack need, causing the agent to fail to connect with zero recorded calls and no
   exception. Fixed to `{**os.environ, **(env or {}), env_var: url}`.
2. **Windows shutdown.** Forcibly cancelling uvicorn's serve task
   (`tg.cancel_scope.cancel()`) while it may be mid-`accept()` on our own raw socket
   raised a raw `WinError 995` ("I/O operation aborted") on Windows' ProactorEventLoop
   — not cooperative shutdown at all. Fixed by using `uv_server.should_exit = True`
   and a graceful task-group exit instead, matching this project's own three-times-
   confirmed async-shutdown-hang discipline (CLAUDE.md) cutting both ways: cancelling
   too aggressively can be exactly as broken as not cancelling at all.
3. **The severe one.** Running `agent.mode: http` twice in the same process — even
   within a single event loop, ruling out any Windows-event-loop-lifecycle theory —
   made the SECOND (and every subsequent) run fail its first real request with
   uvicorn's "ASGI callable returned without completing response." Root-caused by
   direct reproduction outside pytest entirely, then reading the actual library
   source: `sse_starlette.sse.AppStatus.should_exit` is a bare CLASS attribute, not
   per-instance and not per-server, shared across every Streamable HTTP server this
   process ever starts. This would have made the feature completely non-functional
   for its real use case — `evaluate.baseline.run_baseline`'s `repeats` loop runs the
   same process's server multiple times (10, by `calibration.yaml`'s default) — while
   every single-run test kept passing, since the FIRST run in any process always
   worked. Fixed using the library's own documented API
   (`AppStatus.disable_automatic_graceful_drain()`, with this module's own shutdown
   path explicitly setting `AppStatus.should_exit = True` per that API's documented
   contract), verified with a 20-iteration stress test before trusting it, and locked
   in permanently by
   `test_several_sequential_http_mode_runs_in_the_same_process_all_succeed`.

`uvicorn` and `sse-starlette` added as explicit direct dependencies in `pyproject.toml`
(both already present transitively via `mcp`, but this project's own code now imports
both directly).

---

## v1 scoped: HTTP agent adapter (F-38) pulled forward, F-39 split out, no code yet

Gate 4 closed by explicit override (previous entry) with its kill criterion —
whether F-34's subprocess adapter fits a real second agent's invocation pattern —
unconfirmed rather than resolved. Rather than build v1 features on top of that
unknown, F-38 (the HTTP agent adapter) is deliberately pulled forward to start v1,
exactly matching the condition `docs/PHASES.md`'s own pre-existing v1.5 text named
("HTTP agent adapter, unless pulled forward by a Gate 4 kill criterion") — struck
through there, not silently deleted.

This entry is planning only — grounded in real research before any code was written,
not assumed from memory of an older transport revision:

- Fetched the current MCP spec (2025-06-18) directly: the HTTP+SSE transport from
  2024-11-05 is deprecated, replaced by **Streamable HTTP** — a single endpoint
  handling both POST and GET, session tracking via `Mcp-Session-Id`, and explicit
  security requirements (`Origin` validation, loopback-only binding for local
  servers) that are now load-bearing design constraints, not options.
- Inspected the installed SDK (`mcp==2.0.0`) directly rather than assuming API shape:
  `mcp.client.streamable_http.streamable_http_client` is a drop-in-shaped replacement
  for `mcp.client.stdio.stdio_client` (identical `(read_stream, write_stream)` yield),
  and `starlette`/`uvicorn`/`sse-starlette` are already present as transitive
  dependencies of `mcp` — no new top-level dependency needed to build either
  direction.
- Split "+HTTP in v1" into two genuinely separate features sharing no code path: F-38
  (agent-facing — widens F-34, the thing Gate 4's kill criterion is actually about)
  and F-39 (server-facing — the "change one config line" onboarding story for a
  user's real remote server). Kept apart deliberately, matching this project's own
  precedent (F-16/F-17's Schema Immunity boundary) for not letting two features
  sharing infrastructure blur into one.
- `docs/SPEC.md` §5.1 (new) carries the technical grounding; `docs/FEATURES.md` gains
  F-38/F-39 and marks F-34's original "Done when" as met for its own, narrower,
  already-shipped scope (not rewritten); `docs/PHASES.md`'s v1 section gains F-38 a
  full gate-shaped Tasks/Exit-test/Kill-criterion structure, matching every prior
  gate's rigor even though "v1" itself isn't gate-numbered; `SECURITY.md` gains a new
  dated, pre-code entry (gap 3) for the new local network listener F-38 introduces —
  Drifter's first ever, even though loopback-bound and single-invocation — with the
  mitigations (loopback-only binding, `Origin` validation, ephemeral port, no
  auth-by-reviewed-decision) decided now, before the code exists, matching this
  file's own stated Gate-0 precedent for security design.

No implementation exists yet. This is the plan; building it is separate, later work.

---

## Gate 4 closed by explicit override — exit test and kill criterion NOT verified

Every prior gate in this project closed on real, empirical evidence — a passing exit
test, a confirmed kill-criterion resolution, or (Gate 1) an explicit, separately
recorded override. Gate 4 breaks that pattern differently: asked directly whether the
real second user (the friend doing agentic AI work) hit any friction running `drifter
init` → `observe` → `run` unassisted — Gate 4's own required task — the answer was
"not verified, just close it."

Recorded honestly rather than silently: **Gate 4's exit test (a real person,
unassisted, correctly interpreting a real mutation-test report) has not been
confirmed**, and neither has its kill criterion (whether F-34's subprocess adapter
accommodates a real second agent's invocation pattern — the gate's own named most-
likely hard blocker). `.drifter/GATE_STATUS` records `gate_4_status:
closed_by_user_override_unverified`, distinct from every other gate's
`closed_by_passing_exit_test`. `docs/PHASES.md`'s Gate 4 section carries the same
caveat, visibly, not buried.

What IS real and stays on the record: the pre-handoff dry run
(`tests/cli/gate4_dry_run/`, previous entry) exercised the real CLI end to end and
surfaced two genuine findings before this override — that work isn't retracted or
diminished by the override, it's just explicitly not equivalent to it.

Gate advanced to `v1` in `.drifter/GATE_STATUS`. Whoever works on v1's onboarding path
or the subprocess adapter should treat both as less battle-tested than a genuinely
closed Gate 4 would imply.

---

## Gate 4 pre-handoff dry run: 7 synthetic personas + a shutdown-timing check

**Scope, stated explicitly:** this is pre-handoff stress-testing, NOT Gate 4 closure.
Gate 4's exit test requires a real, unassisted human — nothing here substitutes for
that, and `.drifter/GATE_STATUS` / PHASES.md's Gate 4 status are unchanged by this
work. The point was finding and fixing real bugs cheaply before the actual friend
touches the build, the same way Gate 3's brittle-agent fallback used a synthetic
stand-in for a narrower, explicitly-scoped purpose without retiring the real finding
it couldn't produce.

`tests/cli/gate4_dry_run/` holds 7 numbered personas (test_user_1 through
test_user_7, no persona names — plain numbered identifiers), each driving the REAL
CLI entry points (`drifter init`, a real `drifter observe` subprocess connection,
`drifter run`, `drifter replay-serve`, `drifter doctor`, `drifter stats`/`drifter
score`) end to end against real spawned fake MCP servers, not internal function calls
standing in for them:

- **test_user_1** — happy path, multi-server config (one real server, one non-stdio
  entry `init` must skip), a `description_update`-robust agent correctly getting
  NO_REGRESSION.
- **test_user_2** — adversarial config: two config files declaring a colliding server
  name (tests `init`'s documented earlier-location-wins precedence for real), a
  malformed entry, a pre-existing `drifter.yaml` (overwrite refusal + `--force`), and
  a real `description_update` REGRESSION via the brittle `SELECT:<substring>` agent
  mode, discovered through `init` -> `observe` -> `run` as the actual entry points —
  not `run_mutation_comparison` called directly the way Gate 3's own kill-criterion
  test did.
- **test_user_3** — zero configs found anywhere (`init`'s actionable failure path,
  never a crash or an empty file) and a bad hand-written config caught by `drifter
  doctor` before `observe` ever starts.
- **test_user_4** — the first end-to-end (not unit-level) `tool_addition` regression:
  a new `LAST_TOOL` scripted-agent mode (tests/fixtures/scripted_agent.py) exploits
  the fact that `add_tool` always appends to the end of the manifest.
- **test_user_5** — `drifter replay-serve`'s real-external-client code path (distinct
  from `run`'s in-process wiring), closed end to end for the first time using a
  session the persona itself just recorded, not the pre-existing golden fixture every
  other `replay-serve` test replays.
- **test_user_6** — SPEC.md §15 limitation 12 (connectivity-check-artifact
  contamination) reproduced via a real recorded session instead of a hand-built
  fixture, plus `drifter stats`/`drifter score` against the resulting mixed corpus.
  **Also surfaced a second, new, previously undocumented finding** (SPEC.md §15
  limitation 14): a real, successful `drifter observe` session gets a permanently
  null `tool_manifest_hash` — and is therefore silently excluded from
  `aggregate_baseline_runs` — if its first tool call happens before its first
  `list_tools()` call, confirmed by direct repro (`tools/call` then `list_tools()`
  still produces a null hash) before writing the regression test that locks it in.
- **test_user_7** — a permanent, deterministic regression test for Gate 3's single
  biggest real finding: fidelity below `calibration.yaml`'s floor correctly reports
  UNKNOWN through the full pipeline, not a false NO_REGRESSION/REGRESSION — built as
  a synthetic, millisecond-fast reproduction of the same shape the real Gate 3
  dogfood run needed a costly live agent session to surface.

Plus one standalone, non-persona check (`test_shutdown_timing.py`): a TIMED
confirmation (per CLAUDE.md's explicit "a timing test, not just a passing test"
requirement for this project's own three-times-confirmed async-shutdown-hang bug
shape) that `drifter observe` shuts down promptly after an abrupt client disconnect —
a scenario (a crashed/force-killed agent, not a clean SIGINT) no existing test
covered.

All 12 new tests pass; full suite green throughout.

---

## `drifter init` (F-33) built: found missing while sanity-checking Gate 4's own handoff checklist

Before handing the build to a second real user (Gate 4), its own checklist was sanity-
checked against the real CLI rather than assumed correct: `drifter init` — literally
the first command Gate 4's checklist tells a second user to run — did not exist as a
registered subcommand at all. `cli/app.py`'s parser only ever had `observe`, `stats`,
`doctor`, `score`, `run`, `replay-serve`; running `drifter init` failed immediately
with argparse's "invalid choice" error, before a second user could ever reach
`drifter observe`.

Built now, deliberately narrower than docs/FEATURES.md's own F-33 text ("runs initial
tool classification (F-26)"): `policy/` (F-26, tool risk classification) is empty —
never built in Gate 3 despite PHASES.md's own checklist naming it — and
`cli.config.DrifterConfig` has no risk-classification field to populate even if it
were. F-33's stated "Done when" bar ("produces a working drifter.yaml with zero
manual edits required to run `drifter observe`") does not require classification
output, so this narrower scope still satisfies it — same precedent as F-34's
documented narrower-than-spec Gate 2 scope.

`cli/init.py` scans `.mcp.json`, `.cursor/mcp.json`, and the platform Claude Desktop
config path (in that precedence order) for `mcpServers`-shaped stdio server
definitions — the real, documented Claude Code / Claude Desktop config schema,
confirmed against Claude Code's own docs before writing the parser, not assumed. A
non-stdio entry (`type` in http/sse/ws, or a bare `url`) is reported as explicitly
skipped, never silently dropped or mis-parsed, since Drifter's v0 proxy can only
drive stdio servers. Refuses to overwrite an existing `drifter.yaml` without
`--force`. Built red-test-first (`tests/cli/test_init.py`, 16 tests) and verified
end-to-end against a real scanned config, not just unit-tested in isolation.

`drifter tasks mine` — the third command in Gate 4's checklist — remains
unimplemented, but that gap is already documented and expected: PHASES.md's own v1
section explicitly defers workflow mining (F-28/29/30) past Gate 3. `init` had no
such documented deferral; it was simply missing.

---

## F-16/F-17 test corpus widened with real, published MCP server tool manifests

Unit/regression-level test-corpus expansion, explicitly NOT Gate 4 work and NOT a
resolution of Gate 3's tier-3 finding: `tests/mutate/test_real_world_manifests.py`
runs `mutate_tool_manifest`/`add_tool` against real, verbatim tool descriptions from
five independently-authored, genuinely public MCP servers (git, fetch, sqlite, time —
`modelcontextprotocol/servers`; a subset of github/github-mcp-server's issue tools),
chosen for description styles absent from the golden fixture's uniform filesystem-verb
register. 30 new tests confirm: no injection-check false positives on ordinary real
descriptions, no article-agreement or other grammatical defects across the corpus
(checked with an independent oracle, not the implementation's own fix logic),
reproducibility under seed, Schema Immunity against real (not fabricated) schemas, and
collision-free `tool_addition` against every real manifest. Manual review of the full
mutated corpus confirms styling stays plausible.

One real, notable finding surfaced in the process, not manufactured: the official
`mcp-server-fetch` reference server's `fetch` tool description contains genuinely
injection-shaped language overriding an assumed prior instruction, verbatim from a
real, shipped server — and it matches none of `description_update.py`'s five literal
injection patterns, so it passes through unflagged. Documented as SPEC.md §15
limitation 13 and locked in by
`test_the_real_fetch_tool_description_is_a_known_injection_check_gap`. Not fixed here
— widening the pattern list was Gate 3's own already-reviewed decision; changing it as
a side effect of a test-corpus expansion would bypass that review.

---

## Gate 3 closed: brittle-agent fallback confirms the harness, tier-3 finding carried forward

Kill criterion satisfied, via the fallback path its own text names, not via the real
dogfood pairing: `tests/fixtures/scripted_agent.py` gained a `SELECT:<substring>`
mode — tool selection by literal substring match against a tool's description, a
deliberately fragile mechanism standing in for a real agent whose routing happens to
key off exact wording. Verified empirically before relying on it: `list_directory`'s
real golden-fixture description contains "detailed listing"; `description_update` at
seed 42 removes that exact substring via its synonym table (`"detailed"→"thorough"`).
Run through the real `cli.run.run_mutation_comparison` orchestration (not an isolated
unit check): the baseline arm finds and calls `list_directory` normally; the mutated
arm's selection finds nothing and calls nothing — a real, agent-observable behavior
break caused only by the mutation. The harness reports `REGRESSION`, correctly.
196/196 full suite passing (`tests/cli/test_kill_criterion_brittle_agent.py`).

This resolves the ambiguity the two real-dogfood `UNKNOWN` results left open (per the
kill criterion's own reasoning: distinguishing "harness problem" from "agent
unusually robust"). The harness is confirmed to detect a real, planted mutation
effect — the two real attempts' `UNKNOWN` outcome is understood as an exact-tier
replay fidelity limitation against a curious real agent, not evidence the harness
itself doesn't work.

**Explicitly not retired by this**: the tier-3/exact-tier-replay-viability finding
from the two real-dogfood attempts is a separate, still-open fact, carried forward as
open scope for Gate 4/v1 (SPEC.md §7, PHASES.md's Gate 3 Status, `.drifter/
GATE_STATUS`'s `gate_3_note`) — resolving the kill criterion via a synthetic brittle
agent doesn't mean a real agent's combinatorial verification behavior against
exact-tier-only replay stopped being a real problem.

`.drifter/GATE_STATUS` moves to `gate: 4`.

## Kill-criterion attempts #1 and #2 — both UNKNOWN, converging on a
structural finding, not a fixture-richness problem

A second, richer 4-call fixture (recorded live, deliberately covering the two
most common follow-up patterns from attempt #1) was run through the
identical three-arm comparison. Result: UNKNOWN again, all three arms
below fidelity_floor=0.70 (fidelities 0.25-0.60 across 9 real
attempts). Two fixtures failing the same way for the same reason rules
out "the fixture wasn't rich enough yet" as the explanation.

**The actual mechanism, confirmed by reading all 9 recorded
sequences:** a real, curious agent's tool-selection verification
behavior is combinatorial (path format × tool choice × directory
depth), not enumerable from a single anticipated follow-up set. A
near-universal first move (`list_allowed_directories`) was absent from
both recorded fixtures; once any call misses, the agent doesn't retry
once, it escalates through an open-ended sequence (some runs reached
8-9 calls for a 2-call task). No finite single-session recording can
realistically pre-populate that space at exact-tier-only resolution.

**This reframes a prior Gate 3 scoping decision.** Tier 3 (semantic
matching) was deferred from F-16/F-17 on the reasoning that neither
operator's own mutation changes argument values in a way that needs
it — correct as far as it went. This finding shows tier 3 (or an
equivalent broadening of match resolution) may be a prerequisite for
exact-tier replay to be viable against ANY real, curious agent at all,
independent of whether a mutation is active. This is a different,
larger justification than the one tier 3 was originally deferred
against, and changes its priority from "nice-to-have for later
operators" to "possibly blocking exact-tier replay's real-world
viability."

**Kill criterion status:** still unresolved. Neither honest path
forward (recording an even more exhaustive fixture, vs. building tier-
3 semantic resolution) was attempted — both are real, substantive
pieces of work, not something to decide as a byproduct of this
investigation. Flagging this explicitly as the actual next decision
point for whoever picks this up, rather than defaulting to either
silently.

**Secondary, minor finding:** in 3 of 9 runs, Claude Code's own
natural-language self-report claimed "every call returned MISS" or
equivalent, when the recorded trace showed real hits. Not a Drifter
defect — a reminder that an agent's own narration of its tool use is
not a reliable substitute for the recorded trace when interpreting
results.

## Gate 3 exit test: satisfied by an injection-defense finding, not a
mutation-behavior finding — kill criterion still open

Formal record that Gate 3's PHASES.md exit test is satisfied (`257f9ce`, `e06b122`),
and explicit that this is distinct from, and does not resolve, the kill criterion.

**What's closed:** a real, previously-unknown fragility was found using the actual
Gate 0 dogfood pairing — Drifter's own synthetic placeholder content triggered a real
agent's prompt-injection defenses, blocking every multi-step replay-mode task. Fixed
(`_synthesize_call_tool_result`/`_synthesize_added_tool_result` now return genuinely
empty content, verified against the literal failing call, regression-tested against
re-introduction of any self-referential language, not just the original triggering
string). `cli/replay_serve.py` — the missing entrypoint that let a real,
standards-compliant MCP client agent connect to the replay proxy at all — was also
built and verified as part of this investigation; it did not exist before this gate
and was a genuine prerequisite gap, not anticipated in the original Gate 3 scope.

A second, real methodological gap was found in the same investigation and is
documented but deliberately not fixed yet: `aggregate_baseline_runs` cannot currently
distinguish a `claude mcp get` connectivity artifact from a genuine zero-tool-call
task run — both produce identical recorded shapes. SPEC.md §15 limitation 12.

**What's open:** the actual baseline-vs-`description_update`-vs-`tool_addition`
behavioral comparison against the real dogfood pairing has never been run. A full
handoff runbook exists for running it from a plain, non-nested terminal — nested
`claude` process spawning is blocked in the session where this gate's development
work happened, which is an environment constraint, not a Drifter finding. Until that
comparison runs, PHASES.md's kill criterion remains genuinely unevaluated — not
cleared, not triggered, unattempted. Gate 4 (handoff to a second user) should not
proceed on the assumption that the kill criterion has been checked.

## 2026-08-25 — Gate 3 exit-test evidence: a real fragility, found in Drifter's own code

Attempted the real dogfood run PHASES.md's Gate 3 exit test asks for — Claude Code,
through the actual Gate 0 filesystem-server pairing, not `scripted_agent.py` — and it
surfaced a genuine, previously-unknown fragility on the first real attempt. It landed
in Drifter's own synthesis code rather than in a mutation-induced behavior change, but
it satisfies the letter and spirit of the exit test: a concrete, non-synthetic finding
that would not have been found without the real dogfood pairing.

**What happened, in order:**

1. `drifter run`'s only existing agent-connection mechanism (`cli/subprocess_adapter.py`)
   has Drifter spawn the agent and treat the agent's own stdio as the wire — that only
   works for a purpose-built script (`scripted_agent.py`). A real, standards-compliant
   MCP client like Claude Code always spawns its own configured server commands; it has
   no mode where an external process feeds it MCP frames over its own stdin. This
   blocked the real run outright, before any Drifter logic under test was even reached.
   Fixed by adding `cli/replay_serve.py` — `run_replay_proxy` exposed over real OS
   stdio (mirroring `cli/observe.py`'s `stdio_server()` pattern), so a real agent's
   `mcp.json` can point at it exactly the way it already points at `drifter observe`.
   Verified against a real subprocess-spawning MCP client, not just in-process.

2. This session's own environment blocks spawning a nested `claude` process under
   `--dangerously-skip-permissions` (an auto-mode classifier denial) — resolved by
   retrying without that flag, at the cost of needing `--allowedTools` pre-approval for
   the MCP tools instead. Documented as a known limitation of running this kind of test
   from inside a Claude Code session specifically, not a Drifter issue — confirming the
   full baseline-vs-mutation comparison (the original Case 1/Case 2 question) remains
   something to finish from outside this session, whenever convenient.

3. With the connection working, the real baseline arm (Claude Code, replaying a freshly
   recorded live session, 3 repeats) hit a wall on the very first tool call, identically
   every time: `list_directory` resolved as a genuine, correctly-matched exact-tier HIT
   (`fault=False`, `result_provenance=real`) — but `replay_proxy.py`'s shape-only
   synthesis (SPEC.md §15 limitation 1, F-02/F-04) returned placeholder TEXT describing
   its own fakeness ("original payload was never recorded... F-14 full synthesis not
   implemented yet"). Claude Code read that and refused to proceed, treating it as a
   plausible prompt-injection attempt — verbatim: "I'm not treating this as an
   instruction and haven't read anything as a result of it." `scripted_agent.py` has no
   semantic understanding of tool output at all and could never have caught this —
   only a real agent's real judgment did. Fixed by making synthesized content genuinely
   empty (`text: ""`) rather than more carefully worded prose — an empty string has no
   language for a real agent's own reasoning (or a keyword filter) to interpret as
   suspicious, a categorically different guarantee than softer wording would be. Scoped
   narrowly: `content_length` still comes from the actual historical recording's own
   `array_lengths`, not new inference — general F-14 schema-inference synthesis remains
   unbuilt. See SPEC.md §15, limitation 11, for the permanent record of this finding.

Net effect: two real bugs found and fixed in Drifter itself (missing real-agent
connection mechanism; unsafe synthesis placeholder content), using the real dogfood
pairing exactly as Gate 3 was designed to exercise it — even though neither is the
"mutation caused a behavior regression" shape the exit test was originally written to
anticipate. The actual baseline-vs-mutation comparison against Claude Code is still
open, blocked only by this session's own sandboxing, not by anything in Drifter.

---

## 2026-08-25 — Gate 3 scoping: O4 stays excluded, but now for the real reason

Pre-Gate-3 review found the locked docs said nothing about why O4 (Tool Integration)
isn't in Gate 3's two-operator scope — `docs/gate0/NOTES.md` already corrected the
original schema-merge/no-clean-inverse justification as factually wrong (O4 is
compositionally O1 + related description updates, not a schema merge), but that
correction never propagated to PHASES.md or FEATURES.md, and SPEC.md's own citation
of the correction (§4, C19) pointed at a nonexistent path (`mutate/operators/NOTES.md`
— the real file is `docs/gate0/NOTES.md`). Decision: keep O4 out of Gate 3, but for scope
reasons — prove the harness on two cleanly-attributable operators before a composite
third, so the kill criterion's exit test never has to disentangle which operator
caused a detected regression — not the debunked technical blocker. PHASES.md's Gate 3
section and SPEC.md's C19 note updated to say this explicitly. Revisit O4 for v1 once
F-16/F-17 are proven.

## 2026-08-25 — description_update (F-16) is a closed-set structural transformation, no generation involved

Decided explicitly rather than left implicit, since it's safety-relevant per SPEC.md
§10's injection defense: `description_update` performs bounded, deterministic (seed-
reproducible, per `calibration.yaml`'s `mutation.seed`) text transformation over an
existing description's own content — synonym substitution from a fixed table,
sentence-level reordering of sentences already present — with **no LLM call and no
free-text generation anywhere in the operator**. F-16's existing FEATURES.md wording
("bounded structural paraphrase... within the existing content") already implied
this; recorded here as a deliberate architectural choice, not an incidental reading.

Consequence for SPEC.md §10's imperative-pattern regex rejection: it becomes
defense-in-depth against a *source* description that already contained injection-
shaped text before mutation, not the primary defense against a generative process's
output — a closed-set recombination of an already-reviewed description's own words
cannot manufacture genuinely novel imperative phrasing the way an LLM paraphrase
could. This also keeps `description_update` consistent with `docs/gate0/NOTES.md`'s own
stated architectural philosophy ("Drifter enforces the equivalent constraint
architecturally... a stronger guarantee, not dependent on an LLM following
instructions") — an LLM-paraphrase-then-reject design would have made Drifter's own
operator rely on exactly the weaker mechanism that note draws a contrast against.
Also keeps mutation generation free of API dependency/cost, matching this project's
consistent record/replay-once, execute-many-times-for-free architecture (most
recently demonstrated by `drifter score`'s zero-network-calls guarantee, Gate 2).

---

## 2026-08-25 — F-34's actual scope is narrower than FEATURES.md's original wording

F-34 as written in FEATURES.md described a URL-based proxy address and separate
stdout final-answer capture. Gate 2's actual implementation is stdio-wired directly to
the in-process replay proxy (matching every other Gate 2 component — no HTTP/URL
transport exists in v0 per SPEC.md), and does not capture a final answer —
`run_baseline` consumes tool-call sequences, not task conclusions; final-answer
evaluation is F-24's job, not built yet. FEATURES.md's F-34 wording should be read as
describing a later, HTTP-transport-era version of this adapter, not Gate 2's. See
`src/cli/subprocess_adapter.py`'s module docstring for the full reasoning, and
FEATURES.md's F-34 entry itself, updated alongside this note.

---

## 2026-08-25 — Gate 1 closed by override, not by passing its exit test

Gate 1 closed by explicit user override rather than passing its empirical exit test.
Recorded here so this isn't indistinguishable from a gate that actually passed, six
commits and one investigation down the line. See `.drifter/GATE_STATUS` for the
itemized breakdown of which exit-test items were genuinely verified (golden fixture in
CI, `.gitignore` clean, dependency audit) versus overridden without evidence (the
week-long real trial and the `drifter stats`-vs-known-usage check).

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
