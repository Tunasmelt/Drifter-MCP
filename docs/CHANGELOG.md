# Drifter — Specification Changelog

Tracks how SPEC.md, FEATURES.md, and PHASES.md arrived at their current state.
Written so future amendments follow the same discipline: every change gets a reason,
not just a diff.

---

## External review: six reproducible defects, four fixed

An independent reviewer (Codex) traced record → replay → mutation → evaluation →
reporting and reported seven findings. Every one that was independently re-verified
held up. Nothing was overstated, and one was worse than reported. Recorded as
docs/SPEC.md §15 limitation 18; the summary and the reasoning behind each fix are there.

Verification came first in each case, because a finding accepted on description alone
would have produced a fix aimed at the wrong mechanism. Two examples of that paying
off:

**The payload-retention claim was checked against real data, not the code.** The
dogfood corpus from earlier today has known file contents, so `grep` settled it: the
CSV body appears verbatim in all 10 `.frames` files, and in zero JSONL files. The
nuance that matters for the fix is that raw IS passed through `redact_rpc_payload` —
which is a secret-SHAPED-value redactor, not a payload stripper. So CLAUDE.md's
invariant ("never writes payload data by default, only shapes") is true of the JSONL
and false of the recording output as a whole. Then testing a planted key in four
locations found the sharp bug: `result.content`, `error.data` and `params.arguments`
all redacted correctly; `error.message` leaked. Scoping error, now fixed.

**The replay finding reproduced exactly, and the mechanism was two bugs rather than
one.** With a `parameter_rename` active: the OLD name resolved via the exact tier
(the un-adapted agent looked healthy) and a BOGUS name resolved via the semantic tier,
which hashes the multiset of argument VALUES ignoring names. Knowing both mechanisms
is what made "validate the served contract before lookup" obviously right, rather than
"weaken the semantic tier", which would have been the wrong fix.

That fix has a consequence worth stating rather than burying: through the proxy the
semantic tier is now unreachable against any strict schema. Correct — such a call
would be rejected live — but it narrows F-13 considerably, and four tests moved to a
permissive manifest because they asserted tier threading using arguments no real
server would accept.

### The finding that was worse than reported, and partly self-inflicted

Experiment contamination. All four sub-claims held, and the sharpest is one this
project introduced in the previous commit: `mutate/audit.py` opens `mutations.jsonl`
with mode `"w"`, so a second run under the same `--task-id` DESTROYS the first run's
paper trail while the first run's sessions survive and keep being aggregated. Audit
and sessions then describe different experiments — worse than having no audit, and
introduced by F-18, whose entire purpose was traceability.

`drifter run` now refuses a session directory that already holds sessions, `--force`
discards, and the check runs before the dry-run branch so the conflict surfaces
without paying for the baseline arm. A guard, not the fix.

### The unifying insight the review surfaced

Three separately-documented defects are one root cause: finding 3 (crashed and
timed-out agents counted as valid), limitation 12 (a connectivity artifact wrongly
INCLUDED) and limitation 14 (a legitimate session wrongly EXCLUDED). All three exist
because **the recorded schema has no notion of whether a run completed.** One nullable
field — outcome plus exit code, added under the schema-evolution procedure — closes
all three. That is now the highest-leverage identified fix, ahead of anything in the
feature backlog.

### Where the review's framing improved on this project's own

Two observations sharper than what was already written down.

The retention contract: Drifter currently pays the PRIVACY cost of retaining raw
response content while replay receives NONE of the fidelity benefit, because replay
reads the shape-only records. That is the worst of both positions, and it connects
directly to limitation 17 — the content that would let a replayed agent navigate is
already on disk and replay does not use it. Resolving retention should precede feature
expansion, and either resolution needs an unmutated replay check showing a real agent
can still complete the task.

The verdict rule: with zero observed baseline spread, any deviation becomes a
regression. For an unchanged agent independently choosing path A 90% of the time,
0.9^10 x (1 - 0.9^10) ~= 22.7% of ten-run-per-arm comparisons produce a false
regression from that event alone. The arithmetic is correct. `min_valid_runs: 3` does
not address it, and adaptive scheduling preserves the rule rather than validating it.
Measuring the false-alarm rate with unchanged-agent comparisons — an experiment this
project has never run — is the prerequisite for trusting the verdict, not more
operators.

### Endorsed, and adopted

The reviewer's priority order (fix retention/invalid-schema/failed-run accounting;
isolate experiments and enforce provenance; prove unmutated replay preserves task
completion; measure false alarms; reconcile documentation) matches what this session's
own dogfooding independently pointed at, and the recommendation to pause mining and
additional operators stands — nothing found today argues for more surface area.

---

## Dogfooding the wheel as a new user: limitation 16's real root cause, and it is not what limitation 16 says

Installed the built wheel into a clean venv with no repo on the path — byte-for-byte
what publishing would ship — and ran the whole loop as a new user, against a real
Claude Code 2.1.259 agent and a real `@modelcontextprotocol/server-filesystem`. Not a
scripted stand-in; the first time this project has done that end to end.

Most of it worked. `drifter init` found the real `.mcp.json` and wrote both config
files (the `calibration.yaml` fix from earlier this session earned its keep
immediately — a pip-installed user previously had no such file). `doctor` classified
14 tools over a real handshake. Ten real agent sessions recorded cleanly:
`agent_identity: claude-code/2.1.259`, real server version, manifest hash populated,
shapes only, no payload. `stats`, `score` and `coverage` all read them correctly.

### The finding: shape-only recording destroys the agent's own navigation

`drifter coverage` scored that corpus at **100% (exact 40, missed 0 of 40)**.
`drifter run` against the same corpus produced **0/4 valid baseline runs**, fidelities
0.20/0.67/0.67/0.67, all excluded, verdict correctly UNKNOWN.

The side-by-side explains it exactly. Recorded live:

    list_allowed_directories {}
    list_directory           {.../project}        -> listing shows `data/`
    list_directory           {.../project/data}   -> listing shows readings.csv
    read_text_file           {.../project/data/readings.csv}

Replayed:

    list_allowed_directories {}                   HIT
    list_directory           {.../project}        HIT, content EMPTY
    read_text_file           {.../project/readings.csv}   MISS

The second `list_directory` never happens. The replayed listing is content-empty, so
the agent never learns `data/` exists, guesses one directory up, and issues a call
that was never recorded. Three of four runs reproduced this identical shape; the
fourth degenerated further, the agent inventing `{"random_string": "x"}` arguments
after repeated empty results.

Each step in the chain is individually correct and required: recording captures shapes
not payloads (§3's secrets invariant); limitation 11 then forced synthesized content to
be genuinely empty rather than descriptive prose; a real agent builds its next call's
ARGUMENTS from the previous response's CONTENT; with content gone it cannot reconstruct
them. Recorded as §15 limitation 17.

### Why this reframes limitation 16 rather than confirming it

Limitation 16 attributes the miss rate to "a real, curious agent" exploring
"combinatorially unenumerable" argument values — to agent exploration. This corpus
falsifies that. The agent was not exploring: given a tightly-specified prompt it
produced a byte-identical 4-call trajectory across all 10 live recordings
(`natural_variation: 0.000`). It diverged ONLY under replay, and only where an argument
depended on content it no longer received. The variable is information loss in the
recording, not curiosity in the agent.

Two consequences follow, both of which retire work this project was counting on:

**DEC-027(b)'s lever cannot reach this.** Corpus growth attacks coverage, and coverage
was already 100%. More sessions of the same task add no information about `data/` to a
replay, because replay serves shapes however many times the content was observed.

**F-14 as built cannot fix it either.** `replay/synthesis.py` emits the zero value for
every declared type — an empty array for a directory listing. That is the right choice
for not fabricating claims about the world, and it reproduces precisely the information
loss above.

### A correction to a claim made earlier in this same session

Two hours before this run I reported the vague-prompt coverage curve (68.8% → 91.7%
across 6 sessions) as "the first empirical validation of DEC-027(b) — corpus growth
demonstrably closes the gap." That over-claimed. The curve measures corpus
self-consistency, and this run shows self-consistency does not predict replay
viability: the 100%-coverage corpus produced zero valid runs. The curve is still a
correct measurement of what it measures; it is not evidence that growing a corpus makes
replay work.

`render_coverage` now says this on the GOOD path, where it is easiest to omit and most
likely to mislead: a low projection reliably predicts exclusions, a high one promises
nothing, because coverage replays RECORDED calls whose arguments already encode content
the agent will not receive.

### Not fixed

The only fixes addressing the root cause require recording or reconstructing enough
response content to preserve data flow, which runs straight into §3's secrets
invariant — the one principle treated as non-negotiable from commit one. That is a
design tension needing the scrutiny CLAUDE.md reserves for invariant-level findings,
not a patch. Candidates named in limitation 17, none decided: a redacted structural
skeleton (names and ids, values redacted); a content-aware synthesis tier that
reconstructs listings from OTHER recorded calls' arguments (the corpus's own paths
reveal `data/` exists, even with no response body kept); or narrowing Drifter's stated
scope to agents whose arguments do not depend on response content.

### Two README defects, found by following it literally

`drifter run` rejected README's own documented config. `agent.command` was shown as a
shell string where the schema requires `list[str]`, and the `mode: http` example
omitted `command` entirely though it is required with no default. Both examples fail
validation, so every new user stops at their first `drifter run` — the same class as
limitation 16's secondary finding (a), and equally unreachable from inside the repo
where nobody reads the README to learn the schema. Fixed, with a worked Claude Code
wrapper for http mode, and locked in by `tests/cli/test_readme_examples_are_valid.py`,
which parses README's own fenced YAML against the real schema. Verified red against the
original README before being accepted.

---

## The coverage curve: the limitation-16 experiment gets its instrument, which promptly caught itself lying

`replay/coverage.py` (DEC-027(c)) answers "how good is this corpus?" for one corpus.
The question §15 limitation 16 actually turns on is the DERIVATIVE: as more sessions of
the same task are recorded, does projected coverage climb toward the 0.70 fidelity
floor, or flatten below it? Those outcomes mean opposite things — climbing means
corpus-based replay works and the rest is recording effort; flattening means
exact/semantic-tier replay cannot be made adequate for an exploratory agent by ANY
amount of recording, and the honest response would be to narrow the claim rather than
record harder.

`replay/coverage_curve.py` plus `drifter coverage [--curve]` is that instrument. Three
choices keep it honest: subsets are SAMPLED rather than taken as prefixes (prefixes
would make the curve an artifact of recording order — it would describe your file
naming), sampling is SEEDED (so a before/after comparison is meaningful), and every
point carries its spread and subset count (at small n the between-subset variance is
large, and a bare mean would imply precision the data cannot support — the same
discipline the minimum-evidence gate enforces for verdicts).

### The instrument's first real run was wrong, in limitation 16's own shape

Run against `.drifter/runs/` for server `filesystem`, it printed ~96 rows of confident
percentages and concluded **"PLATEAUED at 50.0%, below the 70% floor — recording more is
not projected to close the gap."** Every part of that was wrong, and wrong in precisely
the way limitation 16 documents: decisive-looking output resting on a foundation the
output never showed.

**One.** 108 sessions in the corpus; 4 carry any `filesystem` calls. The other 104
counted toward "corpus size" while contributing nothing, so most sampled subsets held no
relevant calls, were discarded as un-estimable, and the surviving points were computed
from one or two subsets yet printed to a tenth of a percent. It was also answering the
wrong question outright: "how many recordings of THIS task do I need" cannot be answered
by counting recordings of a different one. Sessions with no calls for the server are now
excluded, and the count dropped is reported.

**Two.** A point built from 1 subset rendered identically to one built from 12. The call
count underpinning the whole curve — 8 — appeared nowhere. Both are now printed.

**Three, the actual bug.** At `corpus_size == N` there is exactly one possible subset, so
that point's `marginal_gain` compares a sampled mean against a single deterministic
value and is near-zero by construction. The detector read *running out of corpus* as
evidence of flatness. Exhaustive points are now flagged and excluded from plateau
detection.

Corrected, the same data says the opposite: **STILL CLIMBING — 16.7% / 33.9% / 50.0% at
2/3/4 sessions, +16.1% on the last session, 20 points short of the floor.** The plateau
was entirely an artifact of the three defects above.

That number should not be over-read: 4 sessions and 8 calls, and the linear
extrapolation the renderer prints is explicitly labelled as having no reason to hold.
What it is good for is direction, and it is the first honest data point the experiment
has. Notably it is also consistent with DEC-027(c)'s earlier 10/17/22/27% measurement
over a differently-composed corpus — both climbing, both well short of 0.70.

### Why this is a shipped command rather than an experiment script

The question generalizes. Any user pointing Drifter at their own agent needs to know
whether their corpus is adequate BEFORE spending real agent runs finding out — which is
exactly the mistake limitation 16 records a real user making, at a cost of twenty runs.
README documents both the command and the recording protocol for building a
single-task corpus.

---

## Limitation and feature audit: two stale claims, and what the 16 limitations actually cost

A full read of docs/SPEC.md §15's sixteen limitations and docs/FEATURES.md's forty
features, looking for what is genuinely open versus what the documents merely still
SAY is open. Two of the latter, both material.

**C8 retracted from the claims ledger.** §4's own rule is that everything citable in
docs or marketing must trace to that table. C8 ("ttlMs/cacheScope honored by SDK
client-side response cache") was marked VERIFIED — and §15 limitation 15 had already
disproved it, in this same document, without the ledger being updated. The original
verification was against the SDK's type definitions, which was the wrong evidence:
limitation 15 root-caused it through the SDK's real dispatch chain and found those
fields exist only on the draft `_v2026_07_28` surface model and are silently stripped
on every currently-negotiable protocol version, confirmed by real wire capture. A
retracted claim left sitting at VERIFIED in the one table that gates citation is worse
than an unlisted one — it is pre-approved for use. Now marked RETRACTED and not
citable, with the reason and the real mechanism named.

**Limitation 16's tail was stale in the direction that flatters the project.** It
closed by saying DEC-027's parts (b) corpus-based replay and (c) projected coverage
were "not built." Both shipped. Corrected — and the correction matters more than a
tick, because what (c) then measured is the most important number this limitation has
produced: projected coverage of 10.0%/17.1%/22.2%/26.7% at 2/3/4/5 sessions. Rising
monotonically, genuinely improved by (b), and visibly decelerating far short of the
0.70 floor.

The honest reading, now recorded: (b) supplied the only structurally sound lever, and
(c) showed that pulling it at reachable corpus sizes does not clear the floor. Whether
it CAN be is an empirical question with a defined experiment — 20-50 real recordings
of one narrow task, plotting the curve for a plateau. Until that runs the limitation
stays open and `mine/` stays unbuilt.

Also recorded: limitation 16's secondary finding (a) — `drifter run` unconfigurable
from README alone — is fixed; README documents the `agent:` block for both agent modes.

The pattern across all three corrections, and across the PHASES checklist fixed in the
previous commit, is one-directional: this project's documents drift toward
understating what is built and overstating what is verified. Both directions are
errors, but they fail differently — an unticked checkbox wastes a reader's time, while
a stale VERIFIED in the claims ledger puts a false statement into anything citing it.

---

## v1 scope close-out: F-14, F-18, and a checklist that had stopped telling the truth

Three pieces of work, plus one documentation defect that was quietly the most
misleading thing in the repo.

### The checklist had stopped telling the truth

Through Gate 3 the per-gate **Status** blocks were maintained meticulously while the
checkboxes above them were not. The result: `docs/PHASES.md` showed Gate 1's recorder,
Gate 2's whole replay/analyzer stack and most of Gate 3 as unticked work, months after
they were built, tested and shipped. Anyone reading the plan cold — the exact audience
a phase plan exists for — would have badly misjudged where this project stands.

Every box was re-checked against the actual code and artifacts and ticked only where a
named module, test or file was confirmed present. 64 ticked, 8 left open, and the open
ones now carry an explicit reason so an unticked box means "genuinely open, here is why"
rather than "nobody updated this."

One of those re-checks corrected a claim I had just made myself. F-20 (header stripping)
looked unbuilt by grep — no header-stripping code exists anywhere in `src/`. It is
actually SATISFIED BY CONSTRUCTION: `replay/replay_proxy.py`, the module serving the
mutated arm, imports nothing capable of reaching a live server, so a mutated call cannot
be forwarded at all and the header defense is moot. That was already documented in
FEATURES.md and locked in by an import-inspecting test; the grep was evidence of the
right thing and I read it as the wrong one.

### F-14 general synthetic response generation

Satisfying F-14's stated "Done when" — synthesized responses pass the tool's own
declared schema validation — turned out to require capturing the schema first.
`record/writer.py` recorded `inputSchema` and `annotations` and dropped `outputSchema`
entirely, so a replayed session had nothing to validate against.
`ToolDescriptor.output_schema` was therefore added under this project's schema-evolution
procedure: nullable, `None` kept distinguishable from `{}`, red test against a
hand-built pre-change corpus written and confirmed failing first. The committed golden
fixture is itself a genuine pre-change recording, so it serves as permanent
backward-compatibility evidence and is asserted against directly.

The synthesizer (`replay/synthesis.py`) emits the ZERO value for every declared type and
never a plausible sample: a synthesized `"/home/user/report.pdf"` would be a fabricated
claim about a world the recording never observed, where `""` is the absence of a claim.
`enum` is the single exception, since no zero value is a member and the first declared
one is the only choice that is both schema-valid and not a guess about likelihood.
Optional properties are omitted rather than zero-filled, and arrays are always empty even
under `minItems` — under-claiming is the right failure mode here.

**The trap this design exists to avoid, which nearly went the other way.** The obvious
implementation reuses F-17's `"synthetic"` provenance for a synthesized miss. That
provenance is EXCLUDED from the fidelity denominator, correctly, because a
mutation-injected tool can have no prior recording by definition. A general miss is the
opposite case — a recording could have existed and did not. Had they shared a
provenance, a run that missed every single call would have had every call excluded, hit
`_run_fidelity`'s vacuous 1.0 empty path, cleared the 0.70 floor, and produced a
confident verdict founded on zero matched evidence. That is exactly the defect DEC-027's
minimum-evidence gate closed, re-entered through a new door: not by inflating the
numerator this time, but by emptying the denominator.

There is a second, sharper edge in the same place. A synthesized miss answers on the
wire rather than erroring, so it is recorded with `fault=False` — and `fault is False`
is `_run_fidelity`'s "confirmed hit" signal. Without an explicit guard it would have
scored as a full-weight HIT, driving fidelity UP in exact proportion to how badly replay
was failing. Both are now guarded by a distinct `"synthetic_miss"` provenance that
counts in the denominator, never as a hit, and reports in its own bucket.

Off by default. Per DEC-027 this changes what a miss DOES to a session, not the miss
RATE, and is explicitly not credited with improving limitation 16.

### F-18 mutation audit log

The gap was persistence, not structure. `MutationLogEntry` already carried most of what
F-18 asks for, but only in memory for the duration of one `run_mutation_comparison`
call — so after a real run nothing on disk could trace a verdict back to the edit that
caused it, which is precisely when a paper trail is worth having.

Now written to `<session_dir>/mutations.jsonl` BEFORE the mutated arm runs, not after,
so the trail survives a crash, a budget abort or an interrupt during that arm — the
cases where "what exactly did it change?" is hardest to reconstruct from memory.

Two fields added. `mutation_id` is a deterministic digest over the fields that define
the mutation, not a uuid4: re-running the same operator at the same seed against the
same manifest must yield the SAME id, which is what makes "this verdict came from that
exact edit" a checkable claim rather than a hopeful one. `target` names what was
actually edited — `parameter_rename` changes one specific parameter, and `tool_name`
alone cannot reproduce that edit by hand.

Two things the tests found rather than assumed. The golden fixture produces no real
inverse mapping under `parameter_rename` at all: every tool in it takes single-word
parameters (`path`, `content`), none eligible for snake_case→camelCase renaming — found
by running the operator against it, and the test was rebuilt on a purpose-made manifest
instead. And a tool with nothing eligible to rename was being logged with target
`parameter:None`, which reads as "a parameter named None was renamed"; it now records an
explicit `parameter:<none eligible>`.

### What remains unbuilt, deliberately

`mine/` (F-28/F-29/F-30) is untouched and stays that way for now. It is not blocked
technically — it is blocked on evidence. DEC-027(c)'s coverage measurement puts
projected replay coverage at 10%/17%/22%/27% across 2/3/4/5 sessions against a 0.70
floor, rising but visibly decelerating. Mining frequent subsequences from a corpus that
thin would produce task candidates for tasks Drifter cannot yet replay well enough to
score. The decisive experiment — 20-50 real recordings of one narrow task, plotting the
coverage curve to find out whether corpus-based replay ever clears the floor — comes
first, and its answer determines whether `mine/` has real fuel or is tuned to a corpus
regime that does not exist.

---

## Pre-publish round 2: the gaps the first audit left, and a version decision reversed

The first audit fixed what `pip install` would *break*. This one fixes what it would
*claim*. Four findings, all of them things only a published artifact — not a checkout —
would expose.

**A README that argues against installing the thing it ships with.** The README carried
a prominent warning: "Do not `pip install mcp-drifter` yet — that name resolves to an
empty placeholder." True when written, false the instant we publish, and the README is
baked into the wheel metadata and rendered as the PyPI project page. We would have
published a package whose own front page told people not to install it. Rewritten to
lead with the install command, keep an honest alpha caveat, and point developers at the
checkout. Two adjacent claims were stale for the same reason and fixed with it: the
header's "reserved but not yet a real release", and an exit-code paragraph asserting
that code `2` "can't fire yet" — F-24 made it reachable and that sentence outlived it.

**Eleven relative doc links that break off-repo.** `](docs/SPEC.md)` resolves fine on
GitHub and nowhere else. PyPI renders the README with no repo context, and `docs/` is
not in the sdist either, so every one of them would have 404'd for the audience most
likely to click. Absolutized to `blob/master` URLs.

**`Typing :: Typed` with no `py.typed`.** The classifier claimed PEP 561 support the
wheel did not honor — without the marker file, type checkers ignore an installed
package entirely. Either the classifier or the marker had to go; the marker is correct
here (the codebase is annotated throughout), so `src/mcp_drifter/py.typed` now ships.

**`calibration.yaml`, deferred by the first audit, now fixed.** Every doc tells the user
to edit `calibration.yaml`; a pip-installed user had no such file, because it lived only
in the checkout. Never a correctness bug — `record/calibration.py` falls back to field
defaults that were verified identical field-by-field — but the knobs SPEC.md §9 calls
tunable were unreachable without cloning. `drifter init`, already the "get me a working
setup" command, now writes it alongside `drifter.yaml`.

Two deliberate choices in that last one. It is **never overwritten, not even under
`--force`** — that flag is about the `drifter.yaml` init generates and can regenerate,
whereas a tuned calibration file is hand-authored data init cannot reconstruct;
clobbering it as a side effect of a flag aimed at a different file is exactly the quiet
destructive action this project's rules forbid. And the starter is shipped as package
data read verbatim, not regenerated from dataclass fields, because the file's comments
carry the reasoning that each constant is a documented guess — regenerating would drop
precisely that. A test asserts the packaged copy stays byte-identical to the repo's, so
drift fails loudly instead of silently giving installed users thresholds no test ever
validated.

**The version decision, reversed on evidence.** The recommendation had been `0.1.0a1`,
on the reasoning that an alpha version string carries the project's real uncertainty
more honestly than a flat `0.1.0`. Checking PyPI before setting it showed why that would
have backfired: the index holds exactly one release, the `0.0.1` placeholder, and pip
ignores pre-releases by default. `0.1.0a1` would therefore have left
`pip install mcp-drifter` resolving to the empty placeholder — reinstating the exact
trap this audit exists to close, while looking like extra caution. Shipping flat
`0.1.0`; the alpha signal lives in the `Development Status :: 3 - Alpha` classifier and
the README caveat, neither of which breaks resolution. The honest label was not worth
paying for in broken installs.

---

## Pre-publish audit: a packaging bug that would have broken other people's environments

Before a first real PyPI release, an audit of what `pip install mcp-drifter` would
actually do. It found one hard blocker, one live trap, and several gaps — none of which
any test could have caught, because every test runs from the repo where the layout
never mattered.

**The blocker: seven generic top-level packages.** `pyproject.toml` declared
`packages = ["record", "replay", "mutate", "evaluate", "mine", "policy", "cli"]` with
`package-dir = {"" = "src"}`, so installing Drifter would have dropped all seven into
site-packages as TOP-LEVEL names. Six are real, existing PyPI distributions — checked
directly rather than assumed:

| name | also on PyPI as |
|---|---|
| `evaluate` | HuggingFace's evaluation library |
| `record` | Zope record objects |
| `replay` | replay of random function calls |
| `mutate` | CDM data-processing tool |
| `mine` | Dropbox state sharing |
| `policy` | RBAC policy enforcement |

Whichever installed second would silently shadow the other. `import evaluate` in an ML
environment could get Drifter's; `from mcp_drifter...` could get HuggingFace's. A
wrong-import, not an error — the exact failure class this project treats as most
dangerous, and it would have been *other people's* environments breaking, not ours.

Fixed by nesting everything under a single `mcp_drifter/` package. Deliberately NOT
`drifter/`: that name is already taken on PyPI (a VirtualBox control tool), so nesting
under it would have recreated the same collision class in miniature. Matching the
distribution name exactly means nothing else can claim it. The module dependency order
CLAUDE.md fixes is untouched — `record/` → ... → `cli/`, now as `mcp_drifter.record`
and so on.

The mechanical part was 341 import statements across 84 files. Two classes of reference
a naive `from X import` rewrite misses, both found by tests failing rather than by
reading: subprocess invocations passing a module name as a STRING (`["-m", "cli",
"observe", ...]` in 14 test files, which failed with a bare `No module named record`
from the child process), and `monkeypatch.setattr("cli.run.run_run", ...)` targets.
Also updated: the AST import-cleanliness tests, which read source by path
(`src/cli/...` → `src/mcp_drifter/cli/...`) and assert on module-name strings
(`"cli.run"` → `"mcp_drifter.cli.run"`).

Verified the way it actually matters — a clean venv with no repo on the path: the wheel
installs, `drifter --help` works, `drifter init` exits 4 on a missing config,
`drifter score` runs, and site-packages contains `mcp_drifter` and nothing else.

**The live trap: `pip install mcp-drifter` succeeds today and gives you nothing.** The
name carries only the 0.0.1 placeholder published in Gate 0 to reserve it — 1.5 KB, no
code, no `drifter` command, and it installs *successfully* rather than erroring. The
README's `pip install mcp-drifter # not yet published` line was accurate but its failure
mode was quiet. Rewritten to lead with the checkout and warn explicitly.

**Metadata gaps**, all real for a package meant to be found: no classifiers, no
project URLs, no keywords. Added, with `Development Status :: 3 - Alpha` chosen
deliberately rather than as boilerplate — the record/observe/score and safety paths are
exercised and independently validated, but behavioral regression detection against a
real non-scripted agent is unproven (§15 limitation 16). Beta would overstate the
evidence.

**A build break I introduced and caught in the same pass**, worth recording because the
symptom was misleading: adding `[project.urls]` immediately after `requires-python` put
it mid-`[project]`, so TOML absorbed the *following* `dependencies` array into it. The
wheel still built; only the sdist failed, with `project.urls.dependencies must be
string`. Moved after the array. Both artifacts now build, and the sdist was checked for
completeness rather than assumed good.

**One gap found and deliberately NOT fixed here:** `calibration.yaml` isn't shipped, so
a pip-installed user has no file to edit even though the docs say to edit it. Verified
this is a discoverability gap and not a correctness one — the packaged defaults were
compared field-by-field against the YAML and match exactly, so an installed user gets
identical numbers. The real fix is `drifter init` writing a starter calibration.yaml
alongside `drifter.yaml`; that is a behavior change with its own test, not something to
slip into a packaging commit.

---

## F-27: adaptive scheduling — a stopping rule that is a proof, not a peek

The last unbuilt item on v1's priority list. F-27's own bar is "fewer runs than
fixed-N, with the same final verdicts," and the second half is the one that matters: a
scheduler that saves runs by changing answers hasn't implemented this feature, it has
broken the one above it.

**Reframed against two premises that no longer hold**, the same way F-31/F-32 were.
docs/SPEC.md §8 specifies a three-stage ladder — 1 run × all mutations (screen), 5 on
the flagged, 20 on the still-inconclusive. First, that allocates budget ACROSS many
mutations; `drifter run` compares exactly one operator against one baseline, so there
is no set to triage between. Second, and newly true: `screen: 1` cannot produce a
verdict at all any more. DEC-027's minimum-evidence gate makes a one-run arm UNKNOWN by
construction, so a one-run screening stage could only ever report "don't know" — it
could never flag or clear anything. That is this session's own earlier work invalidating
a pre-existing calibration constant, which is worth stating plainly rather than quietly
working around: `calibration.yaml`'s `mutation.repeats.screen`/`confirm` are now stale
and unread, annotated as such in place (kept, not deleted — they are the right shape for
a future multi-mutation `drifter run`). `resolve: 20` survives intact as the ceiling.

The same PRINCIPLE — stop spending once the answer is settled — applies within a single
comparison as sequential early stopping, and that is what got built.

**The stopping rule is a proof.** The obvious implementation is to recompute the verdict
after each run and stop when it looks decided. That is optional stopping, which inflates
false positives precisely because the decision to stop correlates with noise favouring
the current answer. Rather than build that and caveat it, this stops only when it is
CERTAIN: after each run it evaluates the best and worst cases still reachable by every
remaining run, and stops only if both yield the same verdict. When no remaining outcome
can change the answer, stopping cannot bias it. The bound is stated explicitly in the
module (with `m` valid runs, `matching` matches and `r` attempts left, the final ratio
lies in `[matching/(m+r), (matching+r)/(m+r)]`, and an EXCLUDED remaining run leaves it
at `matching/m`, which sits between those — so exclusions need no separate case).

To make divergence structurally impossible, the verdict rule itself was extracted from
`compute_behavior_effect_size` into a shared `verdict_for_deviation`. A scheduler with
its own copy of the rule could stop on a verdict the scorer then disagrees with —
silently returning a different answer than fixed-N. One function, one rule.

**The largest single saving falls out of DEC-027's gate.** If the BASELINE arm didn't
clear `min_valid_runs`, the comparison is UNKNOWN no matter what the mutated arm does —
so every mutated run is guaranteed waste, and the scheduler now skips the entire arm
without spawning one agent. Before that gate existed there was no way to know this in
advance.

**Measured savings, at a ceiling of 20:** a clear regression settles in 3 runs (17
saved); a clean result against a baseline with real natural variation settles in 8 (12
saved); a genuinely mixed case in 15 (5 saved); a thin baseline spends 0 of 20.

**And one honest asymmetry, found by running it rather than reasoning about it.**
Against a baseline with exactly ZERO spread, it saves nothing — 20 of 20. That is
correct, not a defect: zero spread makes the verdict rule infinitely sharp (any
deviation at all outranks a natural variation of zero), so a single deviating run among
those remaining would flip NO_REGRESSION to REGRESSION, and no number of clean runs
rules that out in advance. Pinned by its own test so a future "optimization" has to
argue with it rather than quietly reintroduce optional-stopping bias. A direct
consequence worth stating: F-27 saves nothing against this project's own deterministic
scripted test agents, which produce exactly those zero-spread baselines — its value is
realized against real, stochastic agents, which is precisely where the real cost is.
The end-to-end check against a real spawned agent therefore confirms verdict
EQUIVALENCE (adaptive and fixed both NO_REGRESSION, 8/8 runs) rather than a saving.

On by default, since verdicts are provably unchanged; `--no-adaptive` opts out. The
report prints what scheduling did and why, because an unexplained short run looks like
a crash rather than a saving.

One interaction with F-32 (budgets) surfaced in the existing test suite and is worth
recording, since it changes observable behavior. F-32's end-to-end test sets a budget
tight enough that the baseline survives one valid run and the mutated arm previously
attempted five more, all failing on the spent budget. The mutated arm is now skipped
outright — the baseline's single valid run is below `min_valid_runs`, so the verdict
was UNKNOWN regardless and those five agent spawns were pure waste. Checked rather
than assumed that this doesn't cost anything real: budget exhaustion is still detected
from the BASELINE arm's own exclusions, so `budget_exceeded` and docs/SPEC.md §12's exit
code 5 are unaffected. The test now asserts the new behavior and says why the old
expectation was worse.

---

## F-24: the Task axis is real — authored assertions, and exit code 2 finally reachable

docs/SPEC.md §3 principle 4 promises "three independent verdicts. Behavior / Task /
Safety." Two were real. TASK had printed `UNKNOWN — no oracle configured` from a
hardcoded string in `render_run_result` since Gate 3, because no oracle existed to ask.
This builds it: `evaluate/assertions.py` plus a real `tasks:` block in `drifter.yaml`.

**A blocking dependency that turned out not to exist.** docs/FEATURES.md listed F-24 as
"Depends on: task definitions (F-30)", and F-30 (mining task candidates from a corpus)
is deferred indefinitely for want of a multi-week corpus — so a whole verdict axis was
being held hostage. Checked rather than accepted: F-30 is about DISCOVERING tasks
automatically. Authoring one by hand needs no mining whatsoever. The dependency was real
for auto-discovery and simply wrong as a blocker for authoring, and removing it cost
nothing but reading it carefully.

**One of docs/SPEC.md §8's four named assertion types cannot be built, and saying so
loudly is the point.** §8 lists `calls`, `calls_before`, `never_calls`, and
`result_contains`. The first three are built as specified. `result_contains` is
structurally unevaluable: F-02/F-04 record only a result's SHAPE (`{type, keys,
array_lengths}`), never its payload, and that is a §3 non-negotiable rather than a gap
to close later — there is simply nothing for such an assertion to read. The dangerous
outcome here would have been silence: `extra="allow"` is this project's config
convention, so `result_contains:` in a `drifter.yaml` would have parsed cleanly, been
read by nothing, and left the author believing their assertion was enforced. That is
precisely the "doesn't crash, just calmly reports success" failure mode the Task axis's
UNKNOWN-by-default rule exists to prevent, so it is now REJECTED at config-load time,
by name, pointing at the two recordable checks offered in its place: `result_has_keys`
(the recorded shape's keys — what "did the tool return the right kind of thing" reduces
to under shape-only recording) and `no_errors` (the recorded `is_error` flag).

**Evaluated over valid runs only.** Assertions run against each arm's fidelity-passing
sessions (`BaselineResult.valid_session_paths`, added for this). A run excluded because
replay collapsed is one where the agent couldn't do the task for harness reasons;
asserting on it would report a task failure that is really a fidelity failure, and
would have made the Task axis inherit limitation 16's whole problem.

**Deliberately NOT gated by the minimum-evidence rule** the Behavior axis needs. That
gate exists because `natural_variation`/`baseline_spread` are statistical estimates
that mean nothing at n=1. An assertion is a deterministic check on one real trajectory:
a single run genuinely calling `delete_everything` under `never_calls` is a real
finding, not a sampling artifact. Run counts are always printed so a reader can weigh
how widespread a failure was.

**Exit code 2 is reachable for the first time** (docs/SPEC.md §12), and its precedence
is a real decision rather than a slot in a list. It outranks BEHAVIOR (1): a failed
assertion is a deterministic statement that the task broke, strictly stronger than a
statistical claim that the agent's path shifted. It is read from the MUTATED arm only,
and suppressed when the baseline fails the same assertions — a baseline already failing
its own oracle means the task or the corpus is wrong, not that the mutation broke
anything, and reporting that as the run's headline failure would point the user at the
wrong problem entirely. Verified end-to-end against a real run: an authored task whose
`calls: [read_file]` doesn't match the agent's actual `read_text_file` correctly reports
`TASK baseline FAIL / mutated FAIL` with the reason printed, and exits 0 — not 2 —
because both arms failed.

`drifter report` gets real Task verdicts too, unlike `mutation_log`: assertions are
evaluated against the recorded trajectories themselves, which a reconstructed report
already has in hand.

One near-miss worth recording. The first version put the shared `TaskConfig →
TaskAssertions` conversion in `cli/run.py` and had `cli/report.py` import it lazily
inside a function. That silently violated `cli/report.py`'s zero-execution guarantee —
importing `cli.run` transitively pulls in real subprocess-spawning code — and
`test_report.py`'s AST check walks function bodies, so it would have caught it. Moved to
`cli/config.py` as `TaskConfig.assertions()`/`find_task`/`assertions_for`, which both
commands already import, with the added benefit that `drifter run` and `drifter report`
now structurally cannot disagree about how an authored task is read.

---

## DEC-027(c): projected replay coverage — and the first real measurement of whether (b) helps

The last of DEC-027's three pieces, and the one that turns limitation 16 from a thing
users discover *after* twenty real agent runs into a number shown before any are spent.
`replay/coverage.py` estimates what fraction of an agent's calls will actually resolve
from a corpus; `drifter run` shows it in its pre-flight and `drifter doctor` reports it
per configured server. Zero execution, zero API cost — same discipline as
`cli/score.py`.

**The obvious implementation would have been worthless, which shaped the design.**
Resolving a corpus's own recorded calls against a store built from that corpus reports
~100% by construction: every call is in the index because it *is* what built the index.
That number would be self-congratulation, not measurement. The real question is a
GENERALIZATION question — how well does this corpus answer a session it has never seen
— so this uses leave-one-out cross-validation: each session is held out in turn and its
calls resolved against the others only. A dedicated test pins this down
(`test_coverage_is_not_measured_in_sample`), asserting 0% where an in-sample check
would have reported 100%.

Computed in one pass rather than N. Naive leave-one-out means building N stores over
N sessions each — O(N²) file reads, far too slow to gate a pre-flight on an 80-session
corpus. Instead each key is indexed once to the SET of sessions containing it, and a
held-out call resolves exactly when some *other* session also carries that key. Same
answer, O(N) reads.

Two caveats are stated in the output rather than buried, because both change how the
number should be read. It is biased LOW (the simulated store has one fewer session than
a real run's would), negligibly so for a large corpus and severely for a tiny one —
which is why a single-session corpus is reported as *not estimable* rather than as 0%:
with nothing to hold out against, cross-validation has no meaning, and printing "0%"
would read as a measurement when it is an artifact of the method. And it measures only
the exact and semantic tiers; inverse (F-12) depends on a specific active mutation's
inverse map, which is not a property of the corpus at all, so including it would
inflate a number meant to describe the recordings.

**The empirical answer DEC-027(b) explicitly left open.** That entry stated plainly
that whether corpus size actually improves the MISS rate was unmeasured, and that this
piece existed to answer it rather than assume it. Measured against this repository's
own accumulated recordings (5 sessions carrying calls for `filesystem`, 15 calls),
mean projected coverage by corpus size:

| sessions | mean projected coverage |
|---|---|
| 2 | 10.0% |
| 3 | 17.1% |
| 4 | 22.2% |
| 5 | 26.7% |

So corpus replay **does** help, monotonically — the direction DEC-027 bet on is real
rather than merely plausible. It is also sobering: at five sessions the projection is
~27% against a 0.70 fidelity floor, and the per-step gains are shrinking (7.1, 5.1,
4.5 points). Nothing here supports a claim that adding sessions closes the gap on its
own; the honest reading is that coverage is a real lever with an unknown and possibly
distant plateau. This corpus is also small and heterogeneous (test fixtures plus the
golden fixture, not repeated real runs of one task), so treat the curve as directional
evidence, not a calibrated growth model.

Two independent corroborations of limitation 16 fell out of this, neither sought.
The estimator put this corpus at ~27%, squarely inside the 0.25–0.60 band of real
fidelities docs/SPEC.md §7 recorded across 9 real agent attempts — an estimate derived
purely from recordings landing on the same number real runs produced. And its
per-tool breakdown named `list_allowed_directories` at 0% coverage: precisely the
"near-universal first move absent from both recorded fixtures" that §7's root-cause
analysis identified by hand. The mechanism now surfaces automatically what previously
took reading nine transcripts to find.

The per-tool breakdown is the actionable half generally — a user who can see which
tools their corpus answers worst can go record those, which is the one lever DEC-027
left open. `drifter doctor` reports coverage as `[WARN]`/`[ OK ]`/`[INFO]` but never
counts it against its own return value: a thin corpus is a real finding, not a
config/connectivity error, and doctor's boolean drives docs/SPEC.md §12's exit code 4.

One test-design bug worth recording, since it was instructive rather than careless.
The first version of the "coverage grows with corpus size" test used a fixed shared
call set plus one unique call per session, and asserted a rising curve. It produced a
dead-FLAT 66.7% at every size — correctly, since the shared calls always resolve and
the unique ones never do, at any N. The estimator was right and the test's model of
reality was wrong: real exploration overlaps *partially*, which is exactly why the
measured curve above climbs rather than sitting flat. Rewritten with partial overlap
(session `i` visits paths `{i, i+1}`), it rises 50% → 75% → 100% as designed.

---

## DEC-027(b): corpus replay — `drifter run` reads a corpus, not one fixture

The second of DEC-027's three pieces. `drifter run --fixture <one.jsonl>` replayed
from exactly one recorded session; `--fixture` now accepts any number of session
files, directories of them, or a mix, and every resolved session is indexed into one
`ReplayStore`. `drifter replay-serve` got the same treatment, for the same reason —
a real external agent connecting to it explores exactly the way the Gate 4 test's did.

**What is and isn't being claimed.** docs/SPEC.md §7 already records that a second,
*richer* single fixture failed identically to the first, which rules out "the fixture
wasn't rich enough yet" as an explanation. That finding is not contradicted here and
the distinction matters: what failed was one session whose author tried to ANTICIPATE
the agent's follow-up patterns; a corpus is the union of many sessions actually
RECORDED from real behavior, accumulating coverage empirically instead of by guess.
That is why DEC-027 chose it as the honest lever. It is not a claim that it suffices —
§7's combinatorial argument applies undiminished to any finite corpus, and nothing has
yet measured how the MISS rate moves as a corpus grows. Corpus replay is the only
honest direction available; its sufficiency is an open empirical question, which is
what DEC-027(c) exists to answer rather than assume. A cross-reference saying so was
added to §7 itself, since the two passages could otherwise be read as contradicting.

**`replay/corpus.py`** resolves inputs and reports what the corpus holds. Two
decisions worth stating:

*Directory expansion is deliberately non-recursive.* `drifter observe` writes
sessions directly into the runs directory while `drifter run` writes its arms to
`<runs>/run/<task>/{baseline,mutated}/`, so a non-recursive glob picks up real
observed recordings and structurally excludes `drifter run`'s own output. That is
load-bearing, not tidiness: indexing a prior run's baseline arm would be circular
(replaying Drifter's own replay), and indexing its mutated arm would poison the
corpus with a deliberately-altered manifest, silently presenting the mutated
interface as the real server's.

*Task grouping is not required and deliberately not attempted.* No recorded field
ties a session to the task that produced it (`cli/score.py`'s docstring documents
that gap at length). It doesn't block this: replay keys on `(server, tool,
arguments)`, not on task, so a session recorded doing some other task against the
same server is valid replay material — and task diversity across a corpus is exactly
what widens coverage. Sessions from a *different* server are harmless to index (their
keys carry that server's name and can never match) but are counted separately, so the
reported number never overstates what actually contributes.

**Reported before anything is spent.** `drifter run` now prints a REPLAY CORPUS line
ahead of the blast-radius preview: how many of the resolved sessions actually recorded
against this server, how many calls were indexed, a warning if none did, and a warning
if the contributing sessions disagree on the tool manifest (real interface drift
mid-corpus, surfaced rather than silently resolved by picking the newest). Run against
this repo's own accumulated corpus it immediately reported `20 of 83 session(s)
recorded against 'filesystem', 15 call(s) indexed` — thin, and now visibly so.

**A real bug, found by running it rather than reasoning about it.** The first version
sampled the manifest source as the single session for `policy/blast_radius.py`'s
per-run cost estimate. Against the real corpus, the most recently started session
turned out to carry a manifest and zero calls — so the preview a user authorizes real
spending through cheerfully reported "~0 tool call(s)". Blast radius is a cost-and-risk
ceiling, so of the available single-session samples it must take the one that cannot
understate: now the HEAVIEST contributing session (`Corpus.heaviest_path`), which
restored a correct ~140-call estimate for the same invocation. Locked in by its own
regression test.

---

## DEC-027: limitation 16 decided — the premise is wrong, not the matching

docs/SPEC.md §15 limitation 16 (exact-match replay collapsing against a real,
unscripted agent) has been the one open architectural finding since Gate 4's real
blind-agent test, carried explicitly in `.drifter/GATE_STATUS` with the instruction
that whoever picks up v1 "needs to decide, explicitly, how to respond to this before
trusting `drifter run`'s verdicts." Deciding it here, with the scrutiny CLAUDE.md
reserves for an architectural-invariant finding rather than a calibration tweak.

**The causal chain, traced before choosing anything.** A real agent makes a call the
recording doesn't contain → `ReplayStore` MISS → the MISS is served as a JSON-RPC
protocol error → the agent's session degrades (the real test's transcripts show
agents reporting tools "aren't returning results," several abandoning MCP for their
own native file tools) → that run's calls are mostly `fault=True` → fidelity falls
below the 0.70 floor → the run is excluded → few runs survive → a verdict is computed
from the survivors anyway. That is five distinct failure points, not one, and the two
fixes docs/SPEC.md previously named each attack only one of them.

**Rejected: a fuzzy/partial value-matching tier.** Two independent reasons. First, it
would make the fidelity metric lie exactly where it is most load-bearing: a fuzzy hit
is recorded as a hit, so a session resolved by guessing that `read_file("/a/b.txt")`'s
recorded shape is an acceptable answer for `read_file("/x/y.txt")` would report HIGHER
fidelity than one that honestly missed — inverting the meaning of the number the
fidelity floor gates on. Second, and decisively, it does not address the root cause
limitation 16 actually documented: the real agent called tools with NO recording at
all (`list_allowed_directories`, never recorded) and escalated through combinatorial,
unenumerable argument values. There is no near-match to loosen toward. A combinatorial
gap cannot be closed by looser matching against a finite recording — only by more
recording.

**Rejected as "the fix": F-14 general structural synthesis on MISS.** This one is
worth building eventually, but it treats the wrong link in the chain and would be
actively dangerous if sold as the answer. It changes what a MISS DOES to the session
(a structurally valid empty response instead of a protocol error, so the agent
continues and the task completes) — genuinely valuable. But the run is still
correctly 15% faithful, and still correctly excluded. Fidelity is not the bug here;
it is the honest measurement OF the bug. F-14 alone converts a loud failure into a
quiet one unless the floor is also weakened, and weakening the floor to make reports
appear is precisely the dishonesty this project exists to avoid. Kept on the roadmap
for session quality; explicitly NOT credited as closing limitation 16.

**The actual finding: docs/SPEC.md §3 principle 2's unstated premise is too strong.**
"Replay-first" is sound and stays. What is unsupportable is the assumption underneath
it — carried most visibly in README's "record once, replay for free" — that ONE
recorded session is a sufficient stand-in for a live server across repeated runs of a
task. That is false for any non-deterministic, exploratory agent, and no matching
strategy makes it true. Corrected, not deleted: replay adequacy is a property of the
CORPUS, and Drifter's job is to measure and report that adequacy rather than assume
it. Drifter is a measurement tool; when its own input is inadequate the correct
behavior is to say so, not to paper over it with guesses.

Three pieces follow, in dependency order:

**(a) Minimum-evidence gate — built in this change.** Below `calibration.
min_valid_runs` (3, a guess, per this project's calibration discipline) valid runs in
EITHER arm, the Behavior verdict is UNKNOWN with a stated reason, never a computed
verdict. This was not merely conservatism: with one surviving valid baseline run,
`natural_variation` is 0.0 (a lone run trivially matches its own dominant path) and
`baseline_spread` is 0.0 (pstdev of one sample) — both artifacts of n=1, not
measurements — and their combination made `compute_behavior_effect_size`'s zero-spread
branch report a confident REGRESSION for ANY nonzero deviation in the mutated arm.
The real test's "BEHAVIOR REGRESSION at 100% deviation from baseline" was therefore
structurally guaranteed by its own 1-valid-run input, not bad luck. Reproduced as a
red unit test against that exact shape (1 valid baseline / 2 valid mutated out of 10
each) and confirmed to return `REGRESSION` before the gate existed. Same failure shape
as the "verdict defaults to UNKNOWN, never PASS" invariant CLAUDE.md names, pointed
the other way: a confident FAILURE claim on evidence that cannot support any claim.
The reason string is rendered in the BEHAVIOR block itself, not only in the exclusions
list further down — limitation 16's second half was that a cold reader got no visible
signal, and a bare UNKNOWN reproduces that in a quieter form.

**Unplanned corroboration, worth recording rather than quietly fixing.** Adding the
gate turned 12 existing end-to-end tests red — every real-pipeline test that asserted
a Behavior verdict, across `test_run.py`, `test_kill_criterion_brittle_agent.py`, the
Gate 4 dry-run personas, and `test_report.py`. All of them ran 1–2 repeats per arm for
speed and then asserted a confident verdict. So the defect limitation 16 recorded from
one real agent session was not an unlucky edge case reached only by a degraded run: it
was the condition this project's own integration suite had been validating the
pipeline under all along, and every one of those green assertions was resting on
evidence the pipeline could not legitimately produce a verdict from. Fixed by raising
those tests to `repeats=3` (the scripted agents are deterministic, so the verdicts are
unchanged — only the evidence under them is now legitimate), not by exempting them
from the gate. Notably this includes Gate 3's own kill-criterion test, whose
"the harness detects a real, planted mutation effect" claim is load-bearing evidence
in `.drifter/GATE_STATUS` and was being demonstrated at n=1.

**(b) Corpus-based replay, not single-fixture — next, not built here.** `drifter run`
takes one `--fixture`. `ReplayStore.index_session` is already additive per file, and
`drifter observe` already accumulates a corpus. Indexing every recorded session for a
task attacks the MISS rate with COVERAGE — the only honest lever, and the "more
exhaustive fixture" option Gate 3's own note named and never decided between.

**(c) Coverage measured before spending — after (b).** A pre-flight projected-MISS-rate
check, so a user learns their corpus is too thin BEFORE twenty real agent runs rather
than after. F-31's blast-radius preview already carries "estimated replay coverage" as
a documented unbuilt gap; that is its home.

README's claim is corrected to match in this change: not "record once, replay for
free" but record ENOUGH — with Drifter being the thing that tells you what enough is.

---

## parameter_rename (F-40) built, finally giving F-12 a real inverse to resolve against

Next item down "v1 — remaining scope": a third Level 0/1 mutation operator, on the
user's explicit go-ahead to design it (the alternative — skip to task-assertion
authoring UX — was declined). There was no spec for a third operator, only
`parameter_rename` named once, in passing, as F-12's own illustrative test example
(docs/FEATURES.md: "Done when: a `parameter_rename` mutation test produces HIT
(inverse) rather than MISS"). Investigated before writing anything: F-12
(inverse-mutation key resolution) has sat as an unbuilt stub since Gate 2 for the
reason its own entry always stated — it needs a real mutation's recorded inverse to
resolve against, and neither existing operator has one (`description_update` is
schema-immune; `tool_addition` has no prior recording to invert against by
definition, docs/SPEC.md §7). Building only the third operator without also building
F-12 would have shipped dead code with nothing to consume it; building only F-12
without a real inverse-producing operator was exactly the trap that left it a stub
for two gates. Scoped as one change.

**`mutate/parameter_rename.py`**: renames exactly one JSON-Schema top-level property
per tool, snake_case → camelCase (`customer_id` → `customerId`, matching docs/SPEC.md
§13's own illustrative example), updating `required` in lockstep so the served
schema stays internally consistent. Deterministic given only the schema (the
alphabetically-first eligible property — not JSON insertion order, which isn't a
meaningful signal — with `seed` selecting nothing, since there's only one correct
choice once eligibility is decided). A tool with nothing eligible (no properties, no
underscores, or every candidate collides with an existing sibling) is left
completely untouched, reported honestly (`inverse=None`), never forced into a
no-op-shaped rename. No content to safety-review here, unlike `description_update`'s
synonym table or `tool_addition`'s archetype pool — the transformation is a pure,
mechanical, structurally invertible string rule, not chosen text.

**F-12, real this time**: `replay.replay_store.ReplayStore.lookup` gained an
`inverse_param_map: dict[str, str] | None` parameter. On an exact-key miss, if given,
a live call's renamed argument names are translated back to their recorded
originals and the exact index is retried before falling to semantic — docs/SPEC.md
§7's full three-tier ordering (exact, inverse, semantic) is now real, not aspirational.
Kept deliberately dumb about mutations: `ReplayStore` has no idea `parameter_rename`
exists, it just applies whatever plain `{new_name: old_name}` dict it's handed —
`replay/replay_proxy.py` holds the per-tool slice of a caller-supplied `inverse_map`
and passes it down per call, matching this project's module dependency order
(`replay/` stays upstream of `mutate/`, never importing it). `inverse_map` was
threaded the same way `synthetic_tool_names` already was, end to end:
`build_replay_server`/`run_replay_proxy` (`replay/`) → `serve_replay_over_http`
(`cli/http_proxy.py`) → `make_run_once`/`run_agent_subprocess`/
`run_agent_subprocess_http` (`cli/subprocess_adapter.py`) → `cli/run.py`'s
orchestration, which builds it generically via `mutate.parameter_rename.
inverse_map_from_log` (contributes nothing for the two operators whose own
`MutationLogEntry.inverse` is always `None`, so `cli/run.py` never has to
special-case which operator is active) and passes it only to the MUTATED arm — the
baseline arm serves the original manifest, so its calls already match under their
original names with nothing to translate.

`MutationLogEntry.inverse` widened from `str | None` to `dict[str, str] | None` — safe
to change freely since this dataclass is never persisted to disk (it's `cli/report.py`'s
own documented gap that mutation logs aren't reconstructable from a session's JSONL at
all), unlike the strict nullable-field discipline this project holds actual `ToolCall`
schema fields to.

Fidelity weighting: `evaluate.baseline._run_fidelity` already gave anything that
wasn't `"semantic"` full weight (1.0) — `"inverse"` needed zero code change there,
only a docstring correction, since docs/SPEC.md §7's own formula groups exact and
inverse together at full confidence (an inverse resolution recovers the exact
original call under a known transformation, not an approximation). `provenance_breakdown`
(the CONFIDENCE section built two entries up) gained a fifth bucket, `inverse`,
alongside exact/semantic/synthetic/unresolved.

Tested at three levels, matching this project's own precedent for the semantic tier:
a `ReplayStore` unit level (ordering — exact wins over inverse wins over semantic;
partial key translation; `None` map reproduces pre-F-12 behavior exactly), a real
proxy level (`test_an_inverse_map_hit_is_recorded_with_match_tier_inverse`, a genuine
`ClientSession` against `run_replay_proxy` with a real `inverse_map`, confirming the
recorded `match_tier` end to end), and a `cli/run.py` orchestration level (confirms
the new operator branch doesn't crash the pipeline — the golden fixture's real tools
happen to have no snake_case properties, verified directly rather than assumed, so
this exercises the honest "nothing eligible" path at full scale, not inverse
resolution itself, which the proxy-level test already nails down precisely).

---

## Synthetic replay provenance surfaced in reports (docs/SPEC.md §13)

Next item down "v1 — remaining scope" after the exit-code wiring above.
`evaluate.baseline._run_fidelity` already knew, per call, which of
exact/semantic/synthetic/unresolved it was (that's exactly what feeds the
tier-weighted fidelity float F-15 built) — it just threw that breakdown away
after folding it into one number. `BaselineResult` gained a new
`provenance_breakdown: dict[str, int] | None` field (counts, not
percentages, computed over valid runs only — same scope as
`baseline_fidelity` itself, `None` exactly when `valid_runs == 0`), and
`render_run_result` gained a CONFIDENCE section showing each arm's own
fidelity + breakdown, since baseline and mutated can genuinely differ (a
`tool_addition` mutation adds synthetic calls only the mutated arm has).

Deliberately reported per-arm, not merged into docs/SPEC.md §13's illustrative
single "baseline 10 runs · mutation 10 runs" CONFIDENCE line — merging would
have hidden exactly the kind of arm-specific divergence this feature exists
to surface. Also deliberately labeled `semantic`, not the illustrative
`inverse`: inverse-mutation key resolution (F-12) is still unbuilt, so
showing an "inverse 0%" bucket on every single report would be fake
precision for a tier that structurally cannot fire yet — omitted from the
breakdown entirely rather than always-zero, matching `_run_fidelity`'s own
established `MatchTier = Literal["exact", "semantic"]` scope.

Tests were written against a hand-built session mixing all four bucket
categories at once BEFORE the field existed on `BaselineResult` at all
(`tests/evaluate/test_baseline.py`), confirmed to fail (no such attribute),
then implemented — same procedure CLAUDE.md requires for new record-adjacent
fields, applied here even though this field lives on a computed result
dataclass rather than the on-disk `ToolCall` schema itself.

Real, stated scope boundary, not silently left ambiguous: this closes only
the provenance-breakdown half of docs/SPEC.md §13's CONFIDENCE section. The
`detectable regression threshold`/`calibration: fidelity_floor=...` footnote
and the RECOMMENDATION line are unrelated, still-unbuilt scope — see
docs/SPEC.md §13's own updated implementation-status note.

---

## docs/SPEC.md §12 exit-code scheme wired up for `run`/`report`

The last item in "v1 — remaining scope" that didn't need a design decision first
(unlike F-27, which is blocked on a real scoping question about multi-mutation
orchestration `drifter run` doesn't have yet): every command exited `0`/`4` only,
even though `drifter run`/`drifter report` were already computing real
BEHAVIOR/TASK/SAFETY verdicts and just throwing the exit-code opportunity away.

Added `cli.report_format.compute_exit_code(RunResult) -> int`, shared by both
commands since both produce the same `RunResult` shape: `3` (safety violation)
outranks everything else — the highest-value finding class per docs/SPEC.md §8,
never gated by the other axes; `5` (budget exceeded) outranks `1` (behavior
regression) because a budget-exhausted run's remaining repeats were skipped, not
completed, so a REGRESSION verdict computed from it may rest on less data than
it looks like; `0` covers NO_REGRESSION and the honestly-uncertain
INCONCLUSIVE/UNKNOWN alike, since this scheme should flag genuine problems, not
"we don't know." `2` (assertion failure) is real, wired code with nothing that
can trigger it: TASK is unconditionally UNKNOWN today (`render_run_result`
hardcodes it — no assertion engine reads into `RunResult` yet), so there's no
signal to read. Documented as staying that way until task assertions become a
first-class authored feature, not silently left to look implemented.

`drifter score` deliberately was NOT touched — it produces a bare
`BaselineResult` from an ungrouped corpus, not a `RunResult`; there is no
BEHAVIOR/TASK/SAFETY verdict for it to report an exit code about. Wiring
something in there would mean inventing a verdict for a command that doesn't
have one, not connecting an existing signal.

Budget-exceeded detection needed two different mechanisms, honestly, not one
papered over: `drifter run` has a live `policy.budget.BudgetTracker` on hand
(new `BudgetTracker.exceeded()` method, `check()`'s condition without raising),
so its `RunResult.budget_exceeded` is exact. `drifter report` reconstructs a
`RunResult` purely from disk, with no tracker to ask — its
`budget_exceeded_from_excluded_runs()` instead greps already-recorded
`ExcludedRun.reason` text for `BudgetExceededError`'s own message substring
("budget exhausted"). This is stringly-typed and stated as such in the
function's own docstring: a differently-worded failure containing that
substring would be misclassified, though nothing else in this codebase raises
with it today. Also added `cli/app.py` exit-code integration tests
(`tests/cli/test_app.py`) confirming `main()` itself raises `SystemExit` with
the right code, not just that `compute_exit_code` returns the right number in
isolation — the earlier F-36 work already had that gap (report_format.py's
render logic was unit-tested, but nothing exercised `cli/app.py`'s actual
dispatch-and-exit wiring end to end).

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

## F-36's second half built: `drifter report`, and a real import-cleanliness refactor along the way

`drifter score` met F-36's own Gate 2 exit test long ago (re-analyze a whole runs
directory with zero new execution), but the other named command — rendering the
FULL docs/SPEC.md §13 report format (BEHAVIOR/TASK/SAFETY, the exact shape
`drifter run` prints) from a specific PRIOR run's stored sessions, not a whole
undifferentiated directory — never got built. It's the last item on the v1
priority list besides F-27 (adaptive scheduling, still deliberately deferred).

Reusing `cli/run.py`'s existing `RunResult`/`render_run_result` directly (by
importing `cli.run` from the new `cli/report.py`) looked like the obvious,
minimal approach — and was caught and rejected before landing: `cli.run` imports
`cli.subprocess_adapter`, which imports real subprocess-spawning code. Merely
importing `cli.run` for its dataclass would have transitively loaded that
machinery into `drifter report`'s own process, quietly breaking the "genuinely
zero execution, structurally" guarantee `cli/score.py` already established as
this project's own standard for this class of command — even though
`drifter report` itself would never call any of it.

Fixed properly, not worked around: `RunResult`/`render_run_result`/`_path_str`
are split out of `cli/run.py` into a new `cli/report_format.py`, checked to have
zero execution-capable imports of its own. `cli/run.py` re-exports both names
unchanged, so every existing caller and test kept working with no changes needed.
`policy/safety.py`'s `evaluate_safety_across_arms` (also needed by both `cli/run.py`
and the new `cli/report.py`) moved there too, for the same sharing reason — and
because `policy/` sits below `cli/` in this project's own module dependency order
(CLAUDE.md), it can't import `cli.config.PolicyConfig` to receive one directly;
it takes plain `destructive_override`/`confirmation_required` sequences instead,
matching this file's own existing functions exactly.

`cli/report.py`'s `build_report_result` reconstructs a `RunResult` purely from a
prior run's `session_dir/{baseline,mutated}` JSONL files — the same
`aggregate_baseline_runs`/`compute_behavior_effect_size`/
`evaluate_safety_across_arms` pure functions `run_mutation_comparison` itself
uses, just fed already-recorded paths instead of freshly-run ones. Raises an
actionable `ConfigError` (naming the expected directory) if the task was never
run at all. One real, stated gap, not silently glossed: `RunResult.mutation_log`
is always empty in a reconstructed report — nothing in the recorded session
schema, or anywhere `run_mutation_comparison` writes, persists which operator or
seed actually produced a given `session_dir`, so it's genuinely not recoverable,
not merely unbuilt.

Confirmed through the real end-to-end pipeline, not just fixture-built sessions:
`tests/cli/test_report.py` gains a test that runs a genuine `drifter run` (a real
replay-served agent) and then confirms `drifter report` reconstructs an identical
`effect.verdict`, both arms' `dominant_path`, and `safety.verdict` from those same
recorded sessions alone. 10 new tests total, including an AST-based no-live-
connection check applied to BOTH new modules (`cli/report.py` AND `cli/
report_format.py` — the former's own cleanliness would have been worthless if the
module it depends on secretly wasn't clean too), matching `cli/score.py`'s own
established precedent for this class of guarantee. `cli/app.py` gains the
`report` subcommand; its module docstring's stale references (`--budget`/
`--dry-run` as "not yet built," `report` as "lands in later gates") are corrected.

---

## Limitation 16 re-examined after F-13/F-15: still open, not fixed by association

Explicit re-check, not an assumption: F-13 (semantic key resolution) and F-15's
tier-weighted fidelity were both built citing limitation 16 (exact-match replay
collapsing against a real, unscripted agent) as their motivating evidence. Whether
they actually CLOSED that gap was never separately verified — until now.

Read `replay_store.py`'s own `semantic_key` implementation directly rather than
trusting the earlier framing: semantic matching requires the same tool name, the
same argument count, and every argument VALUE to match exactly — it only tolerates
a different parameter NAME carrying an identical value. The module's own docstring
already said as much ("there is no fuzzy/partial-value matching at either tier"),
but this connection — that this specific narrowness means limitation 16's actual
documented failure mode (a real agent calling a near-universal unrecorded first
move, then escalating through combinatorial, genuinely different argument VALUES
across an open-ended sequence) is a VALUE-divergence and TOOL-divergence problem,
not the KEY-naming problem F-13 solves — had not been stated explicitly anywhere
until this pass.

**Conclusion: limitation 16 remains open.** F-13 is a real, valuable, separate
improvement (it will genuinely help a `parameter_rename`-shaped mutation, or a
client library that renames an argument key), but it was never going to fix the
real curious-agent divergence problem, and doesn't. F-15's fidelity-weighting fix
is also real and valuable on its own terms — a semantic hit no longer silently
counts as full confidence — but it improves the HONESTY of a low-fidelity report,
not the underlying MISS rate limitation 16 is actually about.

Deliberately did NOT spend another real second-user dogfood session to
re-quantify the MISS rate: the architectural analysis above is sufficient to know
F-13 doesn't close the gap, without paying for a live-agent re-run of a result
that has no structural reason to have changed. `docs/SPEC.md` §15 limitation 16
and `docs/FEATURES.md`'s F-13 priority-list entry are both updated to say this
plainly rather than let the "built the priority-1 item" framing imply the
underlying finding was resolved. The real open work — general structural
response synthesis on MISS (F-14, still not built) or a genuinely fuzzy/
partial-value matching tier beyond F-13's exact-value semantic tier — remains
undecided, and is now the actual next thing worth deciding, not F-13/F-15
themselves.

---

## F-32 (budget and hard limits) built: the `policy/` module is now complete

Following the priority list: `policy/budget.py` builds docs/SPEC.md §11/§13's
budget and hard-limits feature, the last unbuilt item in `policy/` (F-26, F-25,
F-31 were already built). Following the exact same discipline as F-31's own two
reframings: check what's actually observable/buildable before building it, and
say so plainly when a spec phrase doesn't map onto this codebase's real
architecture.

**"`--budget N` (model calls)"** — Drifter cannot observe model calls at all.
The proxy sees only MCP traffic (docs/SPEC.md §15 limitation 2: "no prompts, no
system prompt, no model reasoning" — a fact this project has stated since Gate 0
and re-confirmed independently several times since). `--budget N` is built
instead as a TOOL-CALL ceiling — the one real, countable proxy for cost this
project's own recorded data actually gives it.

**Enforcement shape, stated precisely rather than glossed over:** `BudgetTracker.
check()` runs BEFORE each repeat starts, never during. A real agent subprocess,
once spawned, is never preemptively killed partway through for exceeding
budget — that would require reaching into `cli/subprocess_adapter.py`'s live
process management, real, separate design work not attempted here. The repeat
that crosses the threshold still completes and its calls count toward the
total; every repeat after that is skipped before ever being spawned. This is
the honest, buildable version of "aborts cleanly, stops mid-execution": stops
starting NEW work, not stops IN-FLIGHT work.

A genuinely elegant fit, not a coincidence: `BudgetTracker`'s `check()` raises
`BudgetExceededError` from inside a wrapped `run_once` callable
(`budget_limited`), and `evaluate.baseline.run_baseline`'s existing repeat loop
already catches any `run_once()` exception and records it as a normal
`ExcludedRun`, continuing to aggregate whatever repeats DID complete. This gives
"partial results reportable" for free — zero changes to `evaluate/baseline.py`
were needed. One `BudgetTracker` is shared across BOTH arms (baseline +
mutated) in `cli/run.py`'s `run_mutation_comparison`, deliberately: the budget
is for the whole `drifter run` invocation's real cost, not accounted per-arm.

**`--dry-run`** needed zero new computation at all — it's F-31's blast-radius
preview, shown, with `run_run` simply returning before the confirmation prompt
or any execution. **`baseline.max_calls`/`execution.budget_calls`**
(docs/SPEC.md §11's own YAML example) remain unbuilt as config KEYS — the real
limits shipped as `drifter run` CLI flags (`--budget`/`--max-wall-time`/
`--dry-run`) instead, matching this project's own existing precedent
(`--repeats`/`--seed`/`--timeout` are all flags, not `drifter.yaml` keys, for
the same reason: these vary per invocation, not per project).

Confirmed through the real end-to-end pipeline, not just `policy/budget.py`'s
own unit tests: `tests/cli/test_run.py` gains a test that runs a real
replay-served agent with a budget of exactly one successful run's worth of
tool calls, and confirms the baseline arm reports 1 valid run plus 4
budget-exhausted exclusions while the mutated arm — sharing the same tracker —
gets zero budget left at all. 10 new tests total across `tests/policy/
test_budget.py` and `tests/cli/test_run.py`. docs/SPEC.md §11/§13,
docs/FEATURES.md's F-32 entry, and docs/PHASES.md all gain a full account of
what's built vs. reframed vs. genuinely deferred (`baseline.cache`,
`mutations.profile`/`exclude_tools`, `execution.mode`, `tasks: [...]` all
remain unrelated, unbuilt speculative config surface).

---

## F-31 (blast-radius preview) built, honestly reframed against two premises that don't hold

Following the priority list: `policy/blast_radius.py` builds docs/SPEC.md §10's
blast-radius preview, required before `drifter run` spawns any real agent
subprocess. Two of the mockup's own elements turned out to be real, documented
gaps rather than something to build, discovered by checking rather than
assuming — the same discipline this project applied to F-19's cache-busting
investigation and F-25's own deferred checks.

**"Live-mode run"** — the preview's own stated trigger ("required before any
live-mode run") — doesn't exist anywhere in this codebase. No code path connects
to a real MCP server during evaluation; `drifter run` replays exclusively. This
isn't a new finding specific to F-31 — F-25, F-26, and F-37 had each already
confirmed the same fact independently while building their own pieces — but F-31
is the first feature whose own literal "Done when" bar depends on it existing.
**"Estimated replay coverage"** presupposes a live-server FALLBACK for a replay
MISS (a mechanism that would answer "what fraction of calls will need to fall
through to a real server"), which also doesn't exist — a MISS in this codebase
either synthesizes a placeholder (`tool_addition`'s own injected tool) or reports
MISS outright, never falls through to a live call. Neither gap is built here;
both are stated plainly in docs/SPEC.md §10's own new implementation-status note,
matching this project's established pattern for this class of decision rather
than reinterpreted loosely to look built or silently dropped.

What IS real, and gates something that actually matters: `drifter run`'s
un-deferred cost TODAY is spawning real agent subprocesses — `repeats` times per
arm, twice (baseline + mutated) — a real, ongoing cost (API calls, tokens, real
wall time) that previously had zero preview or confirmation gate at all. The
preview computes workflow count (hardcoded to `1`, matching `drifter run`'s
current one-task-one-operator scope, not a placeholder pretending to be a real
count — see docs/PHASES.md's own kill criterion for when this stops being true),
planned agent runs (`repeats × 2`), and an estimated tool-call volume/risk
breakdown — using the ALREADY-RECORDED fixture's own call sequence, classified
via F-26, as the predictor, honestly labeled an estimate rather than a
guarantee (a fresh agent invocation can and does diverge from the fixture,
docs/SPEC.md §15 limitation 16's own real-test finding).

`cli/run.py`'s `run_run` gains `assume_yes`/`input_stream` parameters: shows the
preview, then requires `--yes` (new `cli/app.py` flag) or an interactive
`y`/`yes` before `run_mutation_comparison` — the function that actually spawns
agents — is ever called. Declining aborts cleanly. Confirmed the abort is real,
not cosmetic: the decline test uses a deliberately nonexistent agent command, so
if declining somehow didn't actually stop execution, the test would see a
"could not start command" spawn-failure error instead of a clean "Aborted"
message — proving the agent was genuinely never attempted.

15 new tests across `tests/policy/test_blast_radius.py` and `tests/cli/
test_run.py` (a decline test, an empty-input-declines test confirming `[y/N]`'s
own bracketed default, an interactive-yes-proceeds test, plus two existing real
end-to-end tests updated with `assume_yes=True` so they keep running
non-interactively, each gaining a `"Planned:"` assertion confirming the preview
actually appears). `docs/SPEC.md` §10, `docs/FEATURES.md`'s F-31 entry, and
`docs/PHASES.md` all gain a full account of what's built vs. reframed vs.
genuinely deferred.

---

## F-25 (safety verdict engine) built, wired into a real `drifter run` report

Following the priority list, now that F-26 unblocks it: `policy/safety.py`
resolves docs/SPEC.md §8's Safety axis — "evaluated on every run regardless of
configuration... reported even when Behavior shows NO_REGRESSION" — for two of
its five named check categories, grounded in data this project actually records;
wired into `cli/run.py` so a real `drifter run` report now has a real `SAFETY`
line, not a stub.

**Built:** a call to a tool F-26 classifies `"destructive"` or
`"irreversible_write"` is a finding; a call to a tool listed in `drifter.yaml`'s
`policy.confirmation_required` is treated as an automatic finding too — a real,
stated reinterpretation of "bypassed," not a guess: no live-mode confirmation
UX exists anywhere in this codebase yet (F-31/F-32 are still unbuilt), so there
is no real confirmation step for a call to have genuinely bypassed. Since
Drifter never invokes such a tool without going through this harness, every
call to a `confirmation_required`-listed tool is the honest, correct reading of
"bypassed" when the thing being bypassed doesn't exist yet.

**Not built, each for its own real, individually-checked reason, not a batch
excuse:** "capability outside `allowed_capabilities`" — no config field by that
name exists anywhere in `cli/config.py`/docs/SPEC.md §11's actual configuration
surface, the same shape of gap F-19's cache-busting investigation found (a
feature description naming a mechanism that was never concretely specified).
"Secrets detected in output" — structurally impossible from what's currently
recorded: `compute_result_shape` never stores string VALUES at all, not even a
redaction marker, so there is nothing to scan (F-02/F-04's shape-only
invariant, working exactly as designed). "Observed behavior contradicting a
declared annotation" — directly blocked by F-26's own documented scope
decision: the observed-behavior classification tier always declines, so there's
nothing to compare a declared annotation against yet.

`cli/run.py` gains `_evaluate_safety_across_arms`, called from
`run_mutation_comparison` and threaded into the new `RunResult.safety` field.
Deliberately evaluated across EVERY recorded session from both arms via a
direct glob of the session directories — not `BaselineResult.valid_runs`, which
excludes low-fidelity or otherwise-invalid runs Safety must still see, since a
destructive call is dangerous regardless of whether the surrounding trajectory
cleared the fidelity floor. `render_run_result` gains the `SAFETY` line, format
matching docs/SPEC.md §13's report mockup exactly (`SAFETY    NO VIOLATION`).

Confirmed through the real end-to-end pipeline, not just unit tests: `tests/
cli/test_run.py` gains a test that runs a real replay-served agent, forces a
real golden-fixture tool into `policy.destructive`, and confirms the rendered
report shows `SAFETY    VIOLATION` alongside a clean `BEHAVIOR  NO_REGRESSION`
— F-25's own "Done when" bar, met with real data rather than a hand-built
dataclass. 15 new tests total across `tests/policy/test_safety.py` and `tests/
cli/test_run.py`. docs/SPEC.md §8 and docs/PHASES.md both gain implementation-status
notes/a full gate-shaped Tasks/Exit-test/Kill-criterion block recording exactly
which of the five check categories are real and which are honestly deferred.

---

## F-26 (tool risk classification) built: the `policy/` module's first feature

Following the priority list: `policy/classify.py` resolves a `ToolDescriptor`
through the four-tier scheme docs/SPEC.md §10 describes — user policy override,
MCP annotations, name heuristics, observed behavior — and is wired into `drifter
doctor`, which now surfaces every tool whose classification couldn't be resolved
by any tier.

A real, resolved ambiguity in the locked spec text, not a silent judgment call:
docs/SPEC.md §10's own prose lists the four tiers as "MCP annotations → name/schema
heuristics → observed behavior → user policy override," in that order. Read
literally as a strict priority order, that would make the user's own explicit
override the LOWEST-priority tier — outranked by a guess from the tool's name. That
can't be the intended meaning of "override": something a human explicitly decided
must win over an automated guess, not be beaten by one. Resolved explicitly (not
assumed silently): `classify_tool` checks the override list FIRST, and docs/SPEC.md
§10's own text is amended to say so plainly, crediting the enumeration order as
describing the fallback CASCADE among the three automated tiers, not override's
actual priority.

`record/schema.py`'s `ToolDescriptor` gains `annotations: dict | None` (the real
wire `tools/list` annotations block — `readOnlyHint`/`destructiveHint`/
`idempotentHint`/`openWorldHint` — captured unmodified by `record/writer.py`); this
is real "cannot be added retroactively" data, same class as `timestamp`/`is_error`.
Tier 1 (`_classify_from_annotations`) only ever uses EXPLICIT `True`/`False` hint
values as signal — an omitted hint is never assumed to carry the MCP SDK's own
client-facing default (the SDK's `destructive_hint: Default: true` exists to tell a
*client* how to behave when a hint is missing, not to tell a *safety classifier*
what a server that sent no hint at all actually intended). Tier 2 is a small, fixed,
reviewable name-prefix table (`get_`/`delete_`/`create_`/etc.), the same "closed-set,
reviewable as data" spirit `mutate/description_update.py`'s synonym table already
established — a calibration-register-style heuristic, not a validated boundary.

Tier 3 (observed behavior) is a real, documented, deliberate stub — `_classify_from_
observed_behavior` always returns `None`. Not an oversight: no signal Drifter
currently records (`result_shape`/`is_error`/`fault`) can honestly distinguish a
write from a read-only call from shape alone, and inventing an unfounded heuristic
here would violate this project's own "verified, not assumed" discipline for
exactly the reason `record/redact.py`'s own entropy heuristic is already careful to
flag as tunable, not derived.

A genuinely unresolved classification (`"unknown"`, no tier answered) needed its own
`ClassificationSource` value — `record/schema.py` gains `"unresolved"`, distinct
from `"heuristic"`: labeling an unresolved result as if the heuristic tier had
actually run and answered would be exactly the "plausible but wrong value" pattern
CLAUDE.md's testing-discipline note warns against.

Sanity-checked against real data, not just synthetic fixtures: run against the
golden fixture's 14 real filesystem-server tools, 12 classified cleanly via the name
heuristic and 2 (`directory_tree`, `move_file`) correctly fell through to
`"unknown"` — a real gap in heuristic coverage, and the taxonomy's own safe default
working exactly as designed rather than a bug to paper over.

`cli/doctor.py` connects a SECOND time per server (after connectivity already
passed) specifically to fetch `tools/list` and classify it — an accepted, documented
cost (doctor is not a hot path) rather than reworking `_check_server`'s existing,
already-tested connect-and-`initialize`-only contract. F-26's own "Done when" bar
("surfaces every ambiguous classification... before any live-mode run is possible")
is met for the "surfaces" half; the "before any live-mode run is possible" half is
explicitly NOT a hard gate yet, since no live-mode invocation path exists anywhere
in this codebase (F-31/F-32 are still unbuilt) — a real, narrower-than-spec scope
decision, stated plainly rather than silently dropped, matching this project's own
established precedent for this class of decision.

29 new tests total across `tests/policy/test_classify.py`,
`tests/cli/test_doctor.py`, `tests/cli/test_config.py`, and `tests/record/
test_writer.py`. `docs/PHASES.md` gains a full gate-shaped Tasks/Exit-test/
Kill-criterion block for F-26, matching F-38/F-39's own rigor — including an honest
kill criterion about the heuristic table's own real, untested false-positive risk
against tool names beyond the golden fixture's 14.

---

## F-39 (HTTP real-server connection) built: the other half of "+HTTP in v1"

Following the priority list's next item: `record/proxy.py`'s real-server
connection now supports `servers[].url` (a real, network-reachable Streamable
HTTP endpoint) alongside the existing `command` (local, spawned stdio) — the
"change one config line" onboarding story SPEC.md §2 has pitched since before
any code existed, now literally true.

`record/proxy.py` gains `ServerTarget = StdioServerParameters | str` and
`connect_to_server(server)`, which picks `mcp.client.streamable_http.
streamable_http_client` vs `mcp.client.stdio.stdio_client` purely by the
target's own type — a bare `str` is always a URL, `StdioServerParameters`
never is, so no shape-sniffing is needed. Confirmed against the installed SDK
before relying on it (already documented in SPEC.md §5.1 from F-38's own
research): both are async context managers yielding the identical
`(read_stream, write_stream)` pair, so `_pump`'s forwarding logic needed
zero changes — the existing F-01 stdio test suite passed completely
unchanged. `cli/config.py`'s `ServerConfig` gains `url: str | None`, mutually
exclusive with `command` via a `model_validator` (neither or both set is
rejected with an actionable message naming the server), and a new
`server_target()` function is the one place that distinction turns into what
`connect_to_server` actually consumes — shared by `cli/observe.py` and
`cli/doctor.py` rather than each re-deriving the same branch (`connect_to_server`
is deliberately public, not `_`-prefixed, for exactly this reuse).

One real, empirically-confirmed difference from the stdio case, handled
explicitly rather than assumed to generalize for free: an unreachable URL
does NOT fail synchronously at connect time the way a bad stdio command does
— `streamable_http_client`'s context manager entry succeeds even against a
completely unreachable address; the real failure only surfaces once
`ClientSession.initialize()` sends an actual request, deep inside
`connect_to_server`'s own task group, and arrives as `httpx2.ConnectError`
wrapped in an `ExceptionGroup` (PEP 654), not bare. Verified with a direct
REPL reproduction before writing any fix, not guessed. `cli/observe.py`'s and
`cli/doctor.py`'s existing actionable-error handling (already built for a bad
stdio command) is extended to `except*` (not a plain `except` — PEP 654
forbids mixing the two styles on one `try`, and forbids `return`/`break`/
`continue` directly inside an `except*` block, both discovered the hard way
via real `SyntaxError`s while writing this, not anticipated), so a
url-configured server's connectivity failure now surfaces as the same
actionable `ConfigError`/`ServerCheck` a bad stdio command already did — not
a raw `ExceptionGroup` traceback. `httpx2` (the MCP SDK's own vendored httpx
fork, already transitive via `mcp`) is declared as an explicit direct
dependency in `pyproject.toml`, matching the `uvicorn`/`sse-starlette`
precedent F-38 already established for this reasoning.

Real end-to-end test coverage, not just unit-level (`tests/record/
test_proxy_http.py`, reusing F-38's own `serve_replay_over_http` as the real
server side rather than building a second real-HTTP-server test harness):
`connect_to_server` against a real Streamable HTTP server, `connect_to_server`
still spawning a real subprocess for the stdio case (confirming the branch
wasn't only ever exercised on one side), and — the actual "Done when" bar —
`run_passthrough_proxy` driven in a real, separate subprocess exactly as
`drifter observe` is genuinely invoked (agent-facing stdio via
`stdio_server()`, real-server-facing Streamable HTTP via `connect_to_server`),
confirming a real tool-call round trip matches the recorded golden fixture's
own `is_error` values. 13 new tests total across `tests/record/
test_proxy_http.py`, `tests/cli/test_config.py`, `tests/cli/test_observe.py`,
and `tests/cli/test_doctor.py`. `docs/PHASES.md` gains a full gate-shaped
Tasks/Exit-test/Kill-criterion block for F-39, matching F-38's own rigor.

---

## F-15's remaining scope built: tier-weighted fidelity, closing F-13's own deliberate gap

Directly following F-13 (semantic key resolution, previous entry): its own honest
"known remaining gap" note said fidelity gating didn't yet discount a semantic hit
relative to an exact one, since served sessions had no field to say which tier
resolved a call. Built now, following CLAUDE.md's required procedure for a new
schema field: a genuinely discriminating test
(`test_a_semantic_hit_is_discounted_relative_to_an_exact_hit`, a mixed exact+
semantic session) was written and confirmed to fail against the pre-change
`_run_fidelity` (a plain, tier-blind hit ratio — it reported 1.0 for the mixed
session, not the intended weighted 0.9) BEFORE any implementation, not after.

`record/schema.py`'s `ToolCall` gains `match_tier: Literal["exact", "semantic"] |
None`, plus `MATCH_TIER_MARKER_KEY`, set by `replay/replay_proxy.py`'s
`on_call_tool` on every real HIT using the exact same private-marker-key pattern
`SYNTHETIC_RESULT_MARKER_KEY`/`result_provenance` already established — a second
dict, carrying the marker, handed to the recording observer only, never the actual
wire response returned to the connecting agent. `record/writer.py` strips the
marker the same way it already strips the synthetic one. `evaluate/baseline.py`'s
`_run_fidelity` now computes `(exact_hits + semantic_weight * semantic_hits) /
total`, reading `calibration.yaml`'s existing `semantic_weight` (0.8).

One real design question resolved deliberately, not by default: what should
`match_tier is None` on a CONFIRMED hit mean? For every other nullable field in
this schema (`is_error`, `fault`, `duration_ms`), `None` means "genuinely unknown,
treat conservatively." Here it doesn't — every `MatchTier` value besides `"exact"`
postdates this field's own introduction (they shipped together), so a hit recorded
before the field existed structurally CANNOT have been anything but an exact-tier
hit. Verified against this project's own history (`replay_store.py`'s `MatchTier`
literally had no other value before F-13), not assumed — and locked in by
`test_a_pre_field_hit_with_no_recorded_tier_is_treated_as_exact_not_unknown`,
which would report the wrong fidelity (0.8 instead of 0.9) if this were ever
mis-implemented as "exclude/unknown" instead.

`tests/replay/test_replay_proxy.py` gains a true end-to-end confirmation
(`test_a_semantic_hit_is_recorded_with_match_tier_semantic`) that the marker
actually threads through the real proxy into a real recorded session, not just at
the unit level — plus a `match_tier == "exact"` assertion added to the existing
golden-fixture round-trip test. docs/SPEC.md §7's implementation-status note,
docs/FEATURES.md's F-13/F-15 entries and priority list, and `evaluate/baseline.py`'s
own module docstring are all updated to say this is built, not still a gap.

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
