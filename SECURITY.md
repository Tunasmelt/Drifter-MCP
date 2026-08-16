# Drifter — Security

Written pre-code, at Gate 0, because a review against SPEC.md's design surface found
two gaps that needed fixing in the plan before they became gaps in a shipped repo.
This is a living document — update it, don't replace it, as the automated checklist
in `/security-check` becomes runnable against real code from Gate 1 onward.

## Why this exists now, with no code written

The standard `/security-check` workflow greps source for hardcoded keys, injection
patterns, missing headers, and vulnerable dependencies. None of that applies yet —
there's no source to grep. But two of its underlying categories (data-at-rest
exposure, dependency scanning) are things you specify *before* writing code, not
things you bolt on after. Waiting for Gate 1 to be "done" to think about them means
retrofitting instead of designing them in.

## Gaps found and closed (Gate 0)

### 1. `.drifter/` was never covered by `.gitignore` guidance

**The problem.** F-04 (secret redaction) ensures payload *values* never get written —
but tool names, server topology, call frequency, timing patterns, and error rates all
still land in `.drifter/runs/*.jsonl` and `.drifter/raw/*.frames`, by design, because
that's the entire point of the recorder. None of that is a "secret" in the redaction
sense, but it's exactly the kind of operational detail nobody means to publish —
internal service names, which vendor APIs a team actually uses, roughly how often a
payment tool gets called. Left unspecified, the realistic failure mode is someone
running `drifter observe` in a real project, then `git add .` out of habit.

**The fix.** PHASES.md Gate 1 now requires the `.gitignore` entry for `.drifter/` to
be added in the *same commit* that first creates the directory — not as a follow-up,
not as something `drifter init` politely suggests. Gate 1's exit test now explicitly
checks `git status` shows nothing staged under `.drifter/` after a full observe
session. See CHANGELOG.md for the exact diff.

**What this doesn't cover.** A user who explicitly runs `--record-full` (the
documented, warned-against opt-out in F-04) and then commits the result anyway has
overridden two separate warnings deliberately. That's a documented, accepted
limitation — SPEC.md §15 territory — not a bug to engineer around.

### 2. No dependency scanning was specified anywhere in the build plan

**The problem.** PHASES.md listed testing discipline, golden fixtures, and CI checks
for correctness — nothing for known-vulnerable dependencies. Since Gate 1 is also
where `pyproject.toml` first exists, this was the correct place to specify it, and it
was missing.

**The fix.** `pip-audit` (or the `uv`-native equivalent, whichever has better
first-party support at build time) runs in CI from the first commit with a dependency
tree, failing the build on high/critical CVEs. Added to Gate 1's task list and exit
test in PHASES.md.

## What's deliberately not addressed yet

Everything in the *manual-review* half of the standard `/security-check` skill —
authorization boundaries, record-level access control, rate limiting — doesn't apply
to a single-user local CLI tool with no accounts and no server (SPEC.md §3, principle
10; DEC-003 in earlier drafts). These become relevant only if a hosted mode is ever
built, which is explicitly out of scope through v2+ (FEATURES.md, "Deliberately
excluded"). Revisit this document itself, not just the checklist, if that changes —
a hosted mode changes the threat model, not just the feature list.

## Ownership going forward

Once Gate 1 produces real source code, run the actual `/security-check` skill against
it as part of that gate's own review, in addition to (not instead of) this document.
This file stays as the design-level record of *why* certain repo-hygiene and CI
choices exist; the skill's automated output is the code-level enforcement of them.
Update this file, with a dated entry, any time a new design-level security question
gets resolved before code exists to check it automatically — the same discipline
CHANGELOG.md applies to the rest of the spec.
