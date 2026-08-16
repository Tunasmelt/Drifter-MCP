# Drifter — Gate 1 Prompt Pack

> Operational build aid, not authoritative — SPEC.md/FEATURES.md/PHASES.md govern
> if this ever disagrees with them.

Sequenced prompts for an agentic coding session (Claude Code or equivalent) to build
Gate 1. Run them **in order** — each assumes the previous one's tests are green.
Don't batch multiple prompts into one turn; the whole point of the gate structure is
catching problems early, and that only works if each step is actually verified before
the next one starts.

Every prompt below assumes CLAUDE.md, SPEC.md, and FEATURES.md are in the repo root
and readable. Prompt 0 has the agent confirm that before anything else.

---

### Prompt 0 — Orientation and scaffolding

```
Read CLAUDE.md, SPEC.md, and FEATURES.md in full before doing anything else.
Confirm you understand: the no-new-planning-documents rule, the module build order
(record/ → replay/ → mutate/ → evaluate/ → mine/ → policy/ → cli/), and that we are
starting Gate 1 per PHASES.md.

Then scaffold the project:
- pyproject.toml for a package named `mcp-drifter`, console script `drifter`,
  Python 3.11+, using uv for dependency management
- Package layout exactly matching CLAUDE.md's module list: record/, replay/,
  mutate/, evaluate/, mine/, policy/, cli/ — empty __init__.py in each, even
  though only record/ and part of cli/ get built in this gate
- tests/ directory with a fixtures/ subdirectory
- .gitignore covering standard Python artifacts AND a `.drifter/` entry —
  this second one is not optional boilerplate, it's a specified requirement
  from PHASES.md Gate 1 (see SECURITY.md if present)
- pip-audit (or uv's native equivalent) wired into a CI config that fails the
  build on high/critical CVEs — also a specified Gate 1 requirement, not later
  cleanup

Do not write any record/, proxy/, or replay/ logic yet. This prompt is scaffolding
only. Stop and show me the structure before continuing.
```

---

### Prompt 1 — Record schema (F-... foundational, blocks everything else)

```
Build record/schema.py per SPEC.md §6 and the field-inclusion rule stated there:
"record it only if it cannot be derived later from what is recorded."

Required as Pydantic models with schema_version="0.1" on every model, and
model_config = ConfigDict(extra="allow") on all of them — this is a forward-
compatibility requirement from CLAUDE.md, not a style preference.

Models needed: SessionStart, ToolsList, ToolCall, TrajectoryEnd (see SPEC.md §6 for
the exact JSON shapes). Include the fields SPEC.md explicitly says cannot be added
retroactively: references, result_provenance, tools_raw AND tools_served (both),
environment.fingerprint, seq, risk, raw_frame_offset, mutation_inverse,
classification_source, baseline_fidelity — even though most of these won't be
populated by anything until later gates. The schema must exist in full now; the
logic that fills every field does not.

Write a test that constructs one of each model with realistic values and round-trips
it through .model_dump_json() and back, asserting equality. This is the test that
must never break as the schema evolves — treat it as the seed of the golden fixture
test mentioned in FEATURES.md F-01... er, in the record/ module generally.

Do not build the proxy or the writer yet. Schema only, and its round-trip test.
```

---

### Prompt 2 — Proxy passthrough (F-01)

```
Build proxy/stdio.py per FEATURES.md F-01: Drifter is invoked as the MCP server
command in the client's config, spawns the real server as a child process, and
forwards all JSON-RPC frames bidirectionally, completely unmodified, in both
directions.

Use the official Python MCP SDK for the stdio transport layer rather than
hand-rolling JSON-RPC framing.

No recording yet — this prompt is passthrough only. The "done when" for F-01 is
explicit in FEATURES.md: "an agent using the proxied server behaves identically to
using the server directly, with zero added latency the user would notice." Write an
integration test using a minimal fake MCP server (a small test fixture server that
responds to tools/list and one tools/call) and confirm a test client gets identical
responses whether talking to the fake server directly or through the Drifter proxy.

Do not add logging, recording, or any transformation of the frames in this prompt.
```

---

### Prompt 3 — Structured recording (F-02) + raw mirroring (F-03)

```
Now wire recording into the proxy from Prompt 2. Build record/writer.py and
record/reader.py as SEPARATE modules — not a shared read/write module. CLAUDE.md
and PHASES.md are explicit about this: a shared module invites silent format drift
between what's written and what's read, and keeping them separate is a deliberate
structural safeguard, not an oversight to "clean up" later.

writer.py: parses each intercepted tools/list and tools/call frame into the Pydantic
models from Prompt 1, appends to a session JSONL file per SPEC.md §6's format.
result_shape is computed (type, keys, array length) — never store the actual
response payload at this stage, that's Prompt 4's job to explicitly guard.

Also implement raw frame mirroring (F-03): every literal JSON-RPC frame gets written
to .drifter/raw/, and each parsed record in the JSONL carries a raw_frame_offset
pointing back to its exact position in the raw log.

reader.py: reads a session's JSONL back into the Pydantic models, validating against
schema_version.

Test: extend the fake-server integration test from Prompt 2 so it now also asserts
the resulting JSONL, once read back via reader.py, exactly reconstructs the sequence
of tool calls that occurred (tool names, args, result shapes, in order).
```

---

### Prompt 4 — Secret redaction (F-04) — security-critical, test first

```
Build the secret redaction layer that sits in front of writer.py from Prompt 3.
This is F-04 and it is explicitly test-enforced per FEATURES.md, not
documentation-enforced — meaning I want you to write the test BEFORE the
implementation, and the test must fail red before you write the redaction logic.

The test: construct a fixture tool call containing planted fake secrets in its
arguments — an OpenAI-style key (sk-...), a bearer token, and a JWT-shaped string —
run it through the full write path (both the JSONL writer AND the raw frame
mirror from Prompt 3), read every byte of both output files, and assert none of
the planted secret values appear anywhere in either file.

Once that test is red, implement pattern-based redaction covering common token
formats (sk-*, Bearer *, JWT three-part base64 structure, and a general
high-entropy-string heuristic as a catch-all) applied to argument values and
headers in both the parsed writer path and the raw frame mirror — the raw
mirror needs the SAME redaction, this is a common place to accidentally leave a
gap since it's "just a backup copy."

Do not implement --record-full yet (that opt-out with its warning is a smaller
follow-up, not required to close F-04's core guarantee). Confirm the test goes
from red to green and show me the diff.
```

---

### Prompt 5 — Environment fingerprinting (F-05)

```
Build environment fingerprinting per FEATURES.md F-05 and SPEC.md's principle 9
(non-negotiable design principle, see SPEC.md §3). Every session should hash agent
identity, model name, MCP server names/versions, and a tool-manifest hash into a
single environment.fingerprint field on the SessionStart record from Prompt 1.

Add the comparison guard: write a function that takes two session records and
returns whether their fingerprints match, and if not, produces an explicit error
identifying which sub-fields differed (not just "fingerprints don't match" — say
whether it was the model, a server version, or the tool manifest that changed).
This matters because SPEC.md is explicit that this must "block comparison with an
explicit error, not a silent wrong answer" — that specificity is required, a bare
boolean mismatch isn't enough.

Test: two fixture sessions with an intentionally different tool-manifest hash
should produce a clear, specific mismatch error when compared, not silently
proceed.
```

---

### Prompt 6 — Trajectory segmentation (F-06, F-07, F-08)

```
Build trajectory segmentation, covering three related pieces from FEATURES.md:

F-06 (trace-context detection): check _meta on every request for W3C trace context
(traceparent). When present, group calls sharing a trace ID into one trajectory at
confidence 0.99.

F-07 (heuristic fallback): when no trace context exists, segment on idle gap
(default from calibration.yaml, see SPEC.md §9 — do not hardcode 30 seconds inline,
read it from the calibration file) combined with a data-flow connectivity check.

F-08 (data-flow references): as a call's arguments are recorded, check whether any
argument value matches a value that appeared in a prior call's result within the
same candidate trajectory. If so, record it in the `references` field pointing back
to that prior call and the matched path.

Build calibration.yaml now if it doesn't exist yet, per SPEC.md §9's table of
constants, with idle_gap_seconds as one of its first entries — this is the first
gate where a calibration constant is actually consumed by code, so this is the
right moment to establish the pattern of reading from that file rather than
hardcoding.

Test with three fixtures: one with trace context present (should segment at 0.99
confidence with zero manual correction), one without trace context but with a clear
idle gap and a data-flow dependency between two calls (heuristic should segment
correctly AND capture the reference), and one adversarial case — no trace context,
no idle gap, but two calls that are obviously unrelated by content — document
this as a known limitation if the heuristic gets it wrong, per SPEC.md §15's
practice of stating limitations plainly rather than hiding them.
```

---

### Prompt 7 — `drifter observe` (F-09)

```
Build the CLI entrypoint drifter observe, wiring together everything from Prompts
2 through 6 into a long-running passthrough session. Per FEATURES.md F-09: live
terminal feedback showing trajectory count, call count, and error count as they
happen — not just a silent process.

This command needs a real drifter.yaml to read server definitions from. Build the
minimal config loader needed for the `servers:` block from SPEC.md §11 — just
enough to support observe mode, not the full config surface (baseline, mutations,
tasks blocks come in later gates).

I am going to run this myself against a real MCP server as part of Gate 1's actual
exit test (per PHASES.md: "one full week of the author's own daily agent work...
zero crashes and no perceptible latency"). Before I do that, write whatever
automated tests you can for the CLI wiring itself, but understand that the real
exit test here is manual and happens outside this session over the following days.
Tell me clearly what I need to do to start that week-long trial once this prompt
is done.
```

---

### Prompt 8 — `drifter stats` and `drifter doctor` (F-10, connectivity only)

```
Build two more CLI commands:

drifter stats (F-10): reads the JSONL corpus produced by observe mode and reports
tool call frequency, unused tools (present in the server's manifest but never
called), retry rate, error rate, and latency percentiles per tool. This should
work against whatever corpus exists at the time it's run, including a very small
one — don't assume a full week of data, since I'll want to check this partway
through the trial too.

drifter doctor: for Gate 1, connectivity checks only (per PHASES.md — full
classification-sanity checks come in a later gate once policy/ exists). It should
verify the configured servers are reachable and the config file parses, and give a
specific, actionable error for common failures (bad server command, missing
config file, malformed drifter.yaml) rather than a raw stack trace.

Test both against the fixture sessions built in earlier prompts, and confirm
drifter doctor produces a clean pass against a valid Gate 1-era drifter.yaml.
```

---

### Prompt 9 — Golden fixture and CI closeout

```
This is the closing task for Gate 1. Two things:

1. Take one real recorded session — either from my week-long observe trial if it's
   done by now, or a carefully hand-constructed realistic one if not — and commit
   it as tests/fixtures/golden_v0.1.jsonl. Per PHASES.md, this file is never
   modified in place going forward; a future schema change produces a new versioned
   fixture alongside it, both tested. Write the test that loads this fixture
   through reader.py and asserts every field parses without error.

2. Run through PHASES.md Gate 1's full exit test checklist explicitly, one item at
   a time, and report status on each:
   - drifter observe ran a full week with zero crashes / no perceptible latency
     (this one needs my confirmation, not just yours)
   - drifter stats output matches what I already know about my own tool usage
     (also needs my confirmation)
   - golden fixture parses cleanly in CI
   - git status after a full observe session shows nothing staged under .drifter/
   - CI dependency audit (pip-audit) is green

Do not mark Gate 1 complete yourself — per verification-before-completion
discipline, show me the evidence for each item and let me confirm the two items
that need my judgment before we consider Gate 1 closed and move to Gate 2.
```

---

## Notes on using this pack

- **If a prompt's tests don't go green**, stop and fix before moving to the next
  prompt. The dependency chain in FEATURES.md is real — Prompt 6 genuinely can't be
  trusted if Prompt 3's recording is subtly wrong, and building on top of it anyway
  just moves the bug somewhere harder to find.
- **Prompt 4 is the one to be strictest about.** It's the only Gate 1 task tied
  directly to SECURITY.md. If the coding session tries to skip the red-test-first
  step or waters down the planted-secret fixture, push back.
- **Prompt 7's exit test is partly manual and takes real calendar time** (a week).
  Everything from Prompt 8 onward can start once observe mode is stable, without
  waiting for the full week to elapse — but Gate 1 itself isn't actually closed
  (Prompt 9) until that week's evidence is in.
- If at any point the agent suggests writing a new planning document, a "notes.md,"
  or an "alternative approach" file — that's CLAUDE.md's rule getting tested. Point
  back to it.
