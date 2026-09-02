# CLAUDE.md — Working instructions for Drifter

You're working in the Drifter repository. Read this before touching code. It assumes
you've read (or will read as part of Gate 0) `docs/SPEC.md` and `docs/FEATURES.md` — this file
is *how to work*, those are *what to build*.

## The one rule above all others

**No new planning documents.** docs/SPEC.md, docs/FEATURES.md, docs/PHASES.md, docs/HANDOFF.md, and
docs/CHANGELOG.md are the complete, locked planning surface (DEC-026). If something seems
wrong or underspecified once you're in the code, the move is:

1. Check if it's actually addressed and you missed it (these documents are long)
2. If genuinely missing or wrong: fix it in place with a docs/CHANGELOG.md entry
   explaining why, in the same PR as the code change that needed the fix
3. Never create a new `*.md` planning file, a "v2 spec," a "notes" file, or an
   "alternative approach" document. If you feel the pull to write one, that pull is
   exactly what this rule exists to stop. Write code or write a CHANGELOG entry.

## Where you are

Check docs/PHASES.md for the current gate. Do not start work belonging to a later gate
before the current gate's exit test has passed — each gate's exit test exists
specifically to catch a foundational problem before more is built on top of it. If
you're unsure which gate is active, check `.drifter/GATE_STATUS` (create it at
`gate: 0` if it doesn't exist yet) rather than guessing from what code happens to
exist.

## Build order within a gate

Follow docs/FEATURES.md's dependency chain, not convenience. F-numbers indicate build
order within a module, and modules have a strict dependency order:
`record/` → `replay/` → `mutate/` → `evaluate/` → `mine/` → `policy/` → `cli/`.
Don't start `mutate/` work while `record/`'s golden fixture test is still red.

## Non-negotiable invariants (docs/SPEC.md §3) — check these on every relevant PR

- Recording never writes payload data by default, only shapes. If a change to
  `record/` touches what gets written to disk, the secret-redaction fixture test
  (F-04's done-criterion) must still pass with zero leaked values.
- Mutation testing never forwards a live call under a mutated schema. If you're
  touching `mutate/` or the proxy's resolve step, verify this is still structurally
  true, not just true by default configuration.
- Every mutated `tools/list` response sets `ttlMs: 0` and a private `cacheScope`
  (F-19). This is easy to accidentally regress if the response-building code gets
  refactored — there should be a test that fails loudly if this is dropped.
- Task verdict defaults to UNKNOWN, never PASS, when no assertion is configured.
  Getting this backwards is the single easiest way to make Drifter quietly
  untrustworthy — a bug here doesn't crash, it just lies calmly.
- Every constant in `calibration.yaml` is a guess until Gate 2's re-derivation step.
  Don't write code that treats them as authoritative research values, and don't add
  new unlabeled constants elsewhere — put them in the calibration file too.

## Testing discipline

Every feature in docs/FEATURES.md has an explicit "Done when" criterion — that's your test
target, not a vague suggestion. For F-01 through F-15 especially, prefer fixture-based
tests over mocks: record a real (or realistically synthetic) session once, commit it,
and test against it. The golden fixture (`tests/fixtures/golden_v0.1.jsonl`) is
never modified in place — a schema change produces a new versioned fixture alongside
it, and both are tested, to catch backward-compatibility breaks.

If you're implementing `evaluate/` (F-21–F-27): read the AgentAssay paper
(arXiv:2603.02601) for the statistical approach before writing adaptive-sampling or
verdict logic. Do not read or reference their source code — it's AGPL-3.0 licensed
and importing or closely mirroring it would obligate this project under that license.
Implementing the same published statistical technique independently is fine and
expected; copying their expression of it is not.

This project's recurring bug pattern (Prompts 1, 3, 5) is not missing fields but
fields populated with plausible-but-wrong values due to ordering/timing — prefer
tests that assert exact expected values over tests that assert presence/non-null.

A second, related recurring bug pattern lives specifically in schema evolution — how a
newly-added field behaves against records written before it existed. When adding any
new ToolCall/record field: (1) it must be nullable with no non-null default unless
every historical record format is guaranteed to have it, (2) track its "unknown" count
separately from its "false"/"zero" count in any aggregate stat, (3) write a test
against a hand-built pre-change corpus BEFORE implementing the field, confirm it
fails, then implement. This exact sequence has now correctly caught a real bug three
times (is_error/duration_ms in v1.0.7→v1.0.8, fault in this change) — treat it as
required procedure for new fields, not optional extra caution.

This project has hit the same async-shutdown-hang shape three times now
(`record/proxy.py` Prompt 6, `cli/observe.py` Prompt 7's Ctrl+C handling,
`subprocess_adapter.py`'s test fixture this gate): a blocking call (stdin read,
subprocess wait, thread join) that doesn't honor anyio/asyncio cancellation, causing
shutdown to hang until something external force-kills it. When writing any code that
spawns a process, reads a stream, or waits on a thread: confirm explicitly (with a
timing test, not just a passing test) that cancellation/interruption during that
operation completes promptly, not just that the happy path works. "It passed" is not
sufficient evidence for shutdown-path code in this codebase specifically — it has to
be timed, per this pattern's three confirmed occurrences.

## When something in the spec turns out to be wrong

It will happen — docs/SPEC.md's calibration register exists because several of its
constants are known-uncertain by design. The distinction that matters:

- **A calibration constant needs adjusting** → update `calibration.yaml`, note it in
  docs/CHANGELOG.md under the current version with the data that justified the change. No
  architecture discussion needed.
- **An architectural invariant (docs/SPEC.md §3) seems wrong** → this is rare and serious.
  Stop, write up specifically what broke and why in a docs/CHANGELOG.md entry, and treat
  it as a decision requiring the same scrutiny the original decision got — not a
  quick patch. Check docs/SPEC.md §15 first; it may already be a documented, accepted
  limitation rather than a bug.

## What "done" looks like for this session

If you're starting fresh: Gate 0's five checklist items in docs/PHASES.md, none of which
are code. Do them in order; the AgentAssay read and the competitor survey both
directly affect what Gate 1 should look like.

If Gate 0 is already done: pick up the next unchecked task in the active gate's
checklist in docs/PHASES.md, in dependency order per docs/FEATURES.md.

Do not skip ahead to a feature that looks more interesting than the next one in
sequence. The dependency ordering in docs/FEATURES.md and the gate ordering in docs/PHASES.md
are both load-bearing — several were specifically designed to surface foundational
problems (Gate 1's proxy stability, Gate 2's free-replay proof) before time is spent
on things that would be built on top of a broken foundation.
