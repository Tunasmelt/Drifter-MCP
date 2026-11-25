# Drifter — Handoff

Read this first if you're picking this project up — a new Claude Code session, a
collaborator, or yourself in three months having forgotten the details.

## What this is, in 30 seconds

Drifter watches how a real AI agent uses its MCP tools, records it, then deliberately
changes the tool descriptions the agent sees (never the real tools) and re-runs the
same workflows to see if behavior changed, whether the task still worked, and whether
anything unsafe happened. It exists because MCP tools change under agents constantly
and nothing currently tests a user's *actual* agent against their *actual* tools under
that kind of change — see SPEC.md §2 and §4 for the verified research behind that
claim.

## Document map — read in this order

1. **SPEC.md** — what is true. Architecture, invariants, schemas, the replay-key
   design (the one genuinely hard technical problem, now solved), the claims ledger.
   This is the source of truth for *what Drifter is*. Amend only with a CHANGELOG
   entry and a reason.
2. **FEATURES.md** — every feature (F-01 through F-49; F-41–F-49 are planned for 0.3.0/1.0.0), what it does, why, its
   dependencies, and what "done" means for it. This is the source of truth for
   *what gets built*.
3. **PHASES.md** — the gated build order with exit tests and kill criteria. This is
   the source of truth for *what gets built first, and when to stop and reconsider*.
4. **CHANGELOG.md** — how the spec arrived at its current state, including the
   corrections made along the way. Worth reading once for context on *why* certain
   decisions look the way they do (several survived being challenged).

## Where we actually are right now

Check PHASES.md's checkboxes for current status — that's the live source of truth
for what's built, not this file. This file's job is orientation and things-easy-to-
get-wrong, not a progress snapshot, because a snapshot here will always drift out of
date as work lands. If PHASES.md's Gate 1 checklist is partially checked, you're
mid-Gate-1; read CHANGELOG.md's most recent entries for anything that changed
reasoning along the way (e.g. the O4 operator correction, v1.0.3).

## Things that are easy to get wrong if you skim instead of read

- **The package name is `mcp-drifter`, not `drifter`.** Bare `drifter` is taken on
  PyPI (verified directly, not assumed). Brand name stays "Drifter."
- **HTTP exists (F-38/F-39); authentication is partly built.** `url` servers can use a static
  token (`auth: {type: bearer_env, token_env: NAME}`, F-41) and token storage exists (F-42), but
  `drifter auth login | status | logout` exist (F-43), and `observe`, `doctor` and `fixture` use a stored
  login (F-44). `client_credentials` (F-45) connects for CI with `secret_env` or `private_key_env` and a required `issuer`. Planned for 0.3.0 (SPEC.md §5.2, F-43–F-47). Don't let
  marketing copy get ahead of what's actually built.
- **Drifter is its own OAuth client; it never forwards the agent's token.** Tokens are
  audience-bound, so passthrough can't work, and under stdio there's no agent token
  anyway. Login is `drifter auth login`, run before `observe` — `observe`'s stdout is the
  protocol channel and must never block on a browser flow.
- **Use the SDK's `OAuthClientProvider`; don't write an OAuth client.** Confirmed in
  `mcp` 2.3.0. Drifter adds only storage, CLI, the S256 refusal and token non-disclosure.
- **No token ever reaches `.drifter/`, logs or error text.** F-47's canary test is
  written before any auth code, the same way F-04's redaction test preceded the recorder.
- **Replay is the default even during Gate 3 mutation testing.** Live-server mode
  never exists before v1, and even then it never runs under a mutated schema. This
  isn't a corner that got cut for time — it's DEC-005/DEC-... level, a core safety
  invariant.
- **Three mutation operators exist**: `description_update` and `tool_addition` (the two
  empirically worst in MCPEvol-Bench, C5) plus `parameter_rename` (F-40, added to give
  F-12 a real inverse). No new operators are planned for 0.3.0 or 1.0.0.
- **1.0.0 is a stability promise, not a validation claim.** Gate 4 closed by override with
  no second user. 1.0.0 freezes the record schema, config and CLI (F-48/F-49); it says
  nothing about outside adoption.
- **Every number in the calibration register is a guess**, not a research finding.
  SPEC.md §9 lists them explicitly so nobody mistakes `fidelity_floor: 0.70` for
  something measured. They get re-derived from real data at Gate 2 — don't defend
  them as-is before then.
- **AgentAssay's code cannot be imported or ported.** AGPL-3.0. Read it, don't copy
  it. If evaluation-engine code in `evaluate/` ever looks suspiciously close to
  something in that repo, that's a problem, not a coincidence to shrug off.

## The four documents' relationship to each other

```
SPEC.md      →  what is true (architecture, invariants, schema, claims)
FEATURES.md  →  what gets built (49 features, dependencies, done-criteria)
PHASES.md    →  when it gets built, and when to stop and reconsider
CHANGELOG.md →  why it looks like this now (corrections, retracted claims)
```

If you ever find yourself wanting to write a new planning document instead of code,
that instinct is the thing PHASES.md's Gate 0 kill criteria and DEC-026 (see
CHANGELOG) exist to catch. Don't.

## If you're an AI agent picking this up in a coding session

See CLAUDE.md in the repo root for session-specific working instructions
(build order, testing discipline, what not to do). It's shorter than this file on
purpose — it assumes you've read SPEC.md and FEATURES.md already, or will as part of
Gate 0.
