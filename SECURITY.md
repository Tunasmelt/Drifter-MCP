# Drifter — Security

Written pre-code, at Gate 0, because a review against docs/SPEC.md's design surface found
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

**The fix.** docs/PHASES.md Gate 1 now requires the `.gitignore` entry for `.drifter/` to
be added in the *same commit* that first creates the directory — not as a follow-up,
not as something `drifter init` politely suggests. Gate 1's exit test now explicitly
checks `git status` shows nothing staged under `.drifter/` after a full observe
session. See docs/CHANGELOG.md for the exact diff.

**What this doesn't cover.** A user who explicitly runs `--record-full` (the
documented, warned-against opt-out in F-04) and then commits the result anyway has
overridden two separate warnings deliberately. That's a documented, accepted
limitation — docs/SPEC.md §15 territory — not a bug to engineer around.

### 2. No dependency scanning was specified anywhere in the build plan

**The problem.** docs/PHASES.md listed testing discipline, golden fixtures, and CI checks
for correctness — nothing for known-vulnerable dependencies. Since Gate 1 is also
where `pyproject.toml` first exists, this was the correct place to specify it, and it
was missing.

**The fix.** `pip-audit` (or the `uv`-native equivalent, whichever has better
first-party support at build time) runs in CI from the first commit with a dependency
tree, failing the build on high/critical CVEs. Added to Gate 1's task list and exit
test in docs/PHASES.md.

### 3. (2026-09-04, pre-code, F-38) An HTTP agent adapter opens Drifter's first-ever
   network listener

**The problem.** Everything above assumes "no server" (docs/SPEC.md §3 principle 10) —
true through Gate 4, false the moment F-38 (`docs/FEATURES.md`, `docs/PHASES.md`'s v1
section) serves `run_replay_proxy` over real Streamable HTTP so a spawned agent can
reach it by URL instead of piped stdio. Even loopback-bound and single-user, this is a
real, new attack-surface class this project has never had: any other local process, or
a malicious webpage open in a browser on the same machine, can attempt to reach a
listening localhost port. The MCP spec's own Streamable HTTP security section names
the exact risk (DNS rebinding) and the exact mitigations — this section exists so
those mitigations are a design decision made now, not a gap discovered after F-38
ships.

**The fix, decided now, before F-38's code exists:**
- Bind to `127.0.0.1` explicitly, never `0.0.0.0` — no config option to widen this in
  v1; a real remote-access use case is out of scope, not a follow-up flag.
- Validate the `Origin` header on every request per the spec's own requirement — a
  request whose `Origin` isn't absent-or-loopback is rejected, closing the DNS-
  rebinding path the spec names explicitly.
- No authentication token for v1, and this is a stated, reviewed trade-off, not an
  oversight: the listener's lifetime is bounded to one `drifter run` invocation, it
  serves only replayed/synthetic data (never live tool execution — the mutation-under-
  replay invariant, docs/SPEC.md §3, is completely unaffected by this feature), and
  the process holding the port is killed the same way the agent subprocess itself
  already is (F-38's task list, `cli/subprocess_adapter.py`'s existing terminate/kill
  discipline). Revisit if F-38 ever grows a longer-lived or multi-agent server mode —
  that would be a different threat model, matching this file's own standing
  instruction to revisit itself, not just the checklist, when the model changes.
- Port selection: ephemeral (OS-assigned), never a fixed, predictable port — nothing
  about this feature should make it easier for another local process to guess where
  to connect.

**What this doesn't cover.** F-39 (the *other* HTTP feature — Drifter connecting OUT
to a real, remote HTTP MCP server) is a client, not a listener, and has no equivalent
new attack surface of its own; standard TLS/cert validation via the SDK's own HTTP
client is assumed sufficient and not re-litigated here.

## What's deliberately not addressed yet

Everything in the *manual-review* half of the standard `/security-check` skill —
authorization boundaries, record-level access control, rate limiting — doesn't apply
to a single-user local CLI tool with no accounts and no *hosted* server
(docs/SPEC.md §3, principle 10; DEC-003 in earlier drafts). This is narrower than
"no server" as of F-38 (gap 3 above) — a loopback-only, single-user, single-invocation
listener is not a hosted mode and doesn't change this section's conclusion, but it's
also not literally "no server" anymore, which is why gap 3 above exists as its own
entry rather than being silently folded into this paragraph's original framing. A
genuine hosted mode (multi-user, persistent, network-exposed) remains explicitly out
of scope through v2+ (docs/FEATURES.md, "Deliberately excluded") and would still
require revisiting this document itself, not just the checklist.

## Ownership going forward

Once Gate 1 produces real source code, run the actual `/security-check` skill against
it as part of that gate's own review, in addition to (not instead of) this document.
This file stays as the design-level record of *why* certain repo-hygiene and CI
choices exist; the skill's automated output is the code-level enforcement of them.
Update this file, with a dated entry, any time a new design-level security question
gets resolved before code exists to check it automatically — the same discipline
docs/CHANGELOG.md applies to the rest of the spec.
