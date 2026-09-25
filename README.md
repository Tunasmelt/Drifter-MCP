# Drifter

A proxy that sits on the MCP connection between your agent and its tools and records
real tool-use trajectories — shapes only, secrets redacted. On top of that recorder it
offers **experimental** structural mutation and offline replay of the tool interface.

> **Scope of this release.** Recording is the dependable part. Mutation and replay
> run, but replay serves recorded response *shapes*, not contents, so an agent whose
> task depends on reading what a tool returned cannot complete that task under replay
> ([`docs/SPEC.md` §15 limitations 17 and 19](https://github.com/Tunasmelt/Drifter-MCP/blob/master/docs/SPEC.md)).
> Treat behavioral verdicts as experimental, and do not use them for release decisions.
>
> Authored response fixtures (`--response-fixture`) close that gap for a task you can
> write fixtures for: against a real Claude Code agent on a content-dependent task,
> shape-only replay produced 0/4 valid runs and 0/4 correct answers, while authored
> fixtures produced 8/8 valid runs and 8/8 correct answers in both arms
> ([limitation 20](https://github.com/Tunasmelt/Drifter-MCP/blob/master/docs/SPEC.md)).
> That is one task, one operator, 4 runs per arm, and it shows capability preservation
> being measured correctly — not yet that a real breaking change is detected.

Console command: `drifter`. Install with `uv tool install mcp-drifter` — see
[Install](#install).

## The problem

MCP tools change under agents without errors. A three-month study of 515 servers found
54.6% of tools modified or deprecated; frontier models degraded 13.7–14.4% under
simulated tool evolution, with damage concentrated in planning and reasoning, not
tool-call syntax. The MCP spec's own 12-month deprecation policy guarantees continued
churn. Nobody tests a user's *actual agent* against their *actual tools* under
controlled interface change — existing tools test models, servers, or agents
generically, not this specific triangle.

Drifter does that: it wraps whatever MCP server you already use, records what your
agent actually does with it, then reruns the same tasks against a deliberately
mutated version of that server's interface — a reworded tool description, an added
tool — replayed from your recordings, at zero marginal cost per replayed call.

The evidence so far is genuine but partial. In a real run against Claude Code, replay
let the agent reach the right file on every attempt while its request-match coverage
scored 0.88 — yet no attempt answered the task, because tool contents aren't replayed
by default. Drifter reports that honestly rather than papering over it: a baseline that
can't perform the task is flagged as inadequate, and an authored `answer_matches`
oracle turns a wrong answer into TASK FAIL. A clean behavioral verdict is not yet, on
its own, evidence that your agent still works — see [Status](#status) for what is and
isn't independently validated today.

## How it works

```
agent ──MCP──▶ drifter ──MCP──▶ real server      (drifter observe: record)
agent ──MCP──▶ drifter (replayed, mutated)        (drifter replay-serve / drifter run)
```

1. **Record** (`drifter observe`) — a transparent passthrough proxy between your agent
   and a real MCP server. Records the trajectory (which tools, in what order, with
   what shapes of arguments and results) to a local JSONL corpus. Payloads are never
   written by default — only shapes — and secrets are pattern-matched and redacted.
2. **Replay** — your recorded sessions become an offline stand-in for the real
   server: matching tool calls resolve instantly from the recording, for free, with
   no live connection and no API cost. **How much you need to record is the catch,
   and Drifter is honest about it:** a real agent explores, so one recorded session
   rarely covers what it does on the next run. Calls Drifter has no recording for
   MISS, those runs are excluded for low fidelity, and if too few survive you get
   `UNKNOWN` with the counts shown — never a confident verdict resting on two
   surviving runs. See [`docs/SPEC.md` §15, limitation 16](https://github.com/Tunasmelt/Drifter-MCP/blob/master/docs/SPEC.md) for the real
   measurements behind that, and DEC-027 in
   [`docs/CHANGELOG.md`](https://github.com/Tunasmelt/Drifter-MCP/blob/master/docs/CHANGELOG.md) for what is and isn't being done about it.
3. **Mutate** — three structural operators, all closed-set: `description_update`
   (bounded synonym substitution and sentence reordering, never touches a tool's
   name or schema), `tool_addition` (a small, fixed pool of generic tool
   archetypes), and `parameter_rename` (renames one input parameter, snake_case to
   camelCase — Drifter still recognizes the resulting call via inverse-mutation key
   resolution, so replay keeps working even though the schema changed).
4. **Evaluate** — runs your agent against the same task through the unmutated and
   mutated manifests, and compares the resulting behavior. A verdict defaults to
   `UNKNOWN`, never a false pass, when there isn't enough data to say more.

## Status

**Alpha, v1 feature-complete.** Every gate in the build plan is closed, including the
release gate — a fresh install of the built package, outside this repository, running
the full `init → doctor → observe → prepare fixture → baseline replay → mutation →
report` workflow against three agents (an unchanged client, a deliberately unadapted
one, and a real Claude Code session that reads a changed schema and recovers). Full
history in [`docs/PHASES.md`](https://github.com/Tunasmelt/Drifter-MCP/blob/master/docs/PHASES.md). Published to
[PyPI](https://pypi.org/project/mcp-drifter/) as `mcp-drifter` 0.1.0.

- **Record & replay** (`drifter observe`, tiered replay resolution, redaction,
  trajectory segmentation) — built and tested.
- **Baseline analysis & re-scoring** (`drifter score`, `drifter coverage`) —
  re-analyzes already-recorded data with zero new agent execution and zero API calls,
  including a projected-coverage estimate you can check before spending anything.
- **Mutation** (`description_update`, `tool_addition`, `parameter_rename`) — built,
  safety-reviewed (red-test-first against prompt-injection-shaped output), tested
  against a real agent.
- **Orchestration** (`drifter run`) — baseline + one mutation operator + behavioral
  comparison, with a per-call budget enforced during execution (not just between
  repeats) and a blast-radius preview required before any real agent process spawns.
- **Setup** (`drifter init`) — scans `.mcp.json`/`.cursor/mcp.json`/Claude Desktop's
  config for existing stdio MCP servers and writes a starter `drifter.yaml`, so you
  don't have to hand-write your server list.
- **Three verdict axes, one of them experimental** — Behavior (a pre-registered
  interval rule vs. a corpus-derived baseline path; **experimental**, see the scope
  note above), Task (opt-in assertions you author, including an `answer_matches`
  outcome oracle on the agent's final answer), Safety (evaluated on every run, never
  gated by the others, and explicit about calls it couldn't classify rather than
  silently passing them through). Set `policy.max_risk` in `drifter.yaml` (for example
  `reversible_write`) and any call to a tool classified above that ceiling is a safety
  finding too.
- **Cost controls** — blast-radius preview, budget/wall-time ceilings enforced live,
  projected replay coverage before you spend, and adaptive scheduling that stops once
  the verdict is provably settled.
- **Tested across the matrix that matters** — both MCP protocol eras, both transports
  (stdio and HTTP), and Python 3.11–3.13.

Task mining (`drifter tasks mine` / `approve`) turns recurring workflows in your recordings
into editable task candidates. It works, and on a small corpus it has little to find — see
[`docs/FEATURES.md`](https://github.com/Tunasmelt/Drifter-MCP/blob/master/docs/FEATURES.md) for the complete per-feature breakdown and
[`docs/SPEC.md` §15](https://github.com/Tunasmelt/Drifter-MCP/blob/master/docs/SPEC.md) for known limitations, stated plainly, including
several found only by testing against a real agent rather than a scripted stand-in.

## Install

Requires Python 3.11 or newer.

```
uv tool install mcp-drifter    # or: pip install mcp-drifter
drifter --help
```

Then generate a starter config and calibration file in the directory you want to work
in:

```
drifter init
```

> **This is an alpha release.** The recording, replay, scoring and safety paths are
> exercised end to end and independently validated, including a full release-gate run
> against a real headless Claude Code agent. Detecting a genuine regression via a real
> (non-scripted) agent — as opposed to preserving capability, which the release gate
> did prove — is *not yet* separately validated; see
> [limitation 16](https://github.com/Tunasmelt/Drifter-MCP/blob/master/docs/SPEC.md) and the "What is and isn't validated" section above.
> Expect the interface to change.

To work on Drifter itself:

```
git clone https://github.com/Tunasmelt/Drifter-MCP
cd Drifter-MCP
uv sync
uv run pytest
```

## Quickstart

Already have the server registered with Claude Code, Cursor, or Claude Desktop? Let
`drifter init` find it instead of writing `drifter.yaml` by hand:

```
drifter init
```

Otherwise, write it directly:

```
# drifter.yaml
version: 1
servers:
  - name: my-server
    command: ["npx", "-y", "@my/mcp-server"]
```

Point your agent's MCP client config at `drifter observe` instead of your server
directly, then use your agent normally — Drifter records every session transparently:

```
drifter observe --server my-server
```

Once you have a recorded corpus:

```
drifter stats                          # per-tool call frequency, error/fault rate, latency
drifter score                          # re-analyze already-recorded sessions, free, instant
```

`drifter run` additionally needs an `agent:` block in `drifter.yaml` — it doesn't know
how to spawn or reach the agent under test otherwise (full schema:
[`docs/SPEC.md` §11](https://github.com/Tunasmelt/Drifter-MCP/blob/master/docs/SPEC.md)):

```yaml
# drifter.yaml, in addition to `servers:` above
agent:
  mode: subprocess
  # A LIST of argv elements, not a shell string -- Drifter never invokes a
  # shell, so quoting and word-splitting are yours to do here.
  command: ["python", "agent.py", "--task", "{task.prompt}"]
```

`mode: subprocess` fits an agent that itself speaks MCP directly over the process
you spawn. If your agent instead launches its own MCP client subprocess (e.g. the
Claude Code CLI, which spawns servers from its own `--mcp-config` rather than
speaking MCP on its own stdio), use `mode: http` instead — Drifter serves the proxy
over a loopback HTTP URL and injects it into the environment your own launch
mechanism reads, rather than spawning anything itself:

```yaml
agent:
  mode: http
  env_var: DRIFTER_PROXY_URL
  # Still required in http mode. Drifter does not spawn your MCP client, but
  # it does spawn THIS -- a wrapper of yours that reads $DRIFTER_PROXY_URL
  # and points your agent at it. For Claude Code that means writing a small
  # --mcp-config with {"type": "http", "url": "<that url>"} and exec'ing
  # `claude -p`.
  command: ["python", "agent_wrapper.py", "{task.prompt}"]
```

If `agent_wrapper.py` launches Claude Code specifically, pass the `claude -p` call an
explicit `cwd` outside any directory with its own `CLAUDE.md` or auto-memory setup (the
wrapper's own directory works well). Without it, a session whose working directory sits
under such a project picks up that project's unrelated context instead of treating the
prompt as an isolated task. `--bare` is not a substitute: it also disables OAuth/keychain
auth, so an install authenticated via OAuth (rather than `ANTHROPIC_API_KEY`) fails every
run with "Not logged in."

Then:

```
drifter run --fixture .drifter/runs --server my-server \
            --task-id my-task --prompt "..." --operator description_update
```

Content-dependent tasks can opt into an explicitly authored response fixture:

```yaml
# responses.yaml — test data you review and maintain, never captured by observe
version: 1
server: my-server
responses:
  - tool_name: read_text_file
    arguments: {path: /fixtures/readings.csv}
    result:
      content:
        - {type: text, text: "sensor,value\na,10\nb,20\n"}
      isError: false
```

Pass it with `--response-fixture responses.yaml`. Every entry must match an exact
request already present in the recorded corpus; inverse parameter-renames are also
supported, while semantic matches deliberately receive no authored payload. The file
may contain sensitive test data, so keep it synthetic or sanitized and review it before
committing. Calls served this way are recorded with `authored_fixture` provenance and
cannot later be indexed as observed server responses.

`--fixture` takes as many recorded sessions as you have — individual files,
directories of them, or a mix — and replays from all of them at once. **Point it at
your whole corpus, not one session.** A real agent explores, so any single recording
covers very little of what it does next; more recordings means more calls resolve
instead of MISSing. `drifter run` prints what it actually found before spending
anything:

```
REPLAY CORPUS  20 of 83 session(s) recorded against 'filesystem', 15 call(s) indexed
REPLAY COVERAGE  ~27% projected (exact 4, semantic 0, missed 11 of 15 calls
                 across 5 sessions, leave-one-out)
                 WARNING: below the 0.70 coverage floor — most runs are likely to be
                 EXCLUDED and the verdict to come back UNKNOWN.
                 worst-covered tools:
                   list_allowed_directories: 0% (1/1 calls unresolved)
```

That coverage number is an honest estimate of how well your recordings answer a run
they've *never seen* (each session held out in turn and resolved against the others),
not a self-scoring of the calls that built the index — so it tells you whether to
record more before spending agent runs, and which tools to go exercise.
`drifter doctor` reports the same figure per server, so you can check readiness
without setting up a run.

`drifter run`'s current scope is deliberately minimal (see its own module docstring)
— one task, one operator, a behavioral comparison. It is not yet the full orchestrated
`v1` command surface. **Known limitation, confirmed against a real, non-scripted
agent** (see [`docs/SPEC.md` §15, limitation 16](https://github.com/Tunasmelt/Drifter-MCP/blob/master/docs/SPEC.md) and DEC-027 in
[`docs/CHANGELOG.md`](https://github.com/Tunasmelt/Drifter-MCP/blob/master/docs/CHANGELOG.md)): replay frequently fails to match a real
agent's actual call pattern, which excludes runs for low fidelity. Drifter no longer
reports a confident verdict on top of that — below `calibration.min_valid_runs` per
arm you get `UNKNOWN` with the surviving counts — but a thin corpus still means fewer
usable runs, so check the `N/10 valid runs` and `REPLAY CORPUS` lines, not just the
headline verdict.

### Telling Drifter what success looks like

By default the TASK axis reports `UNKNOWN` — Drifter won't guess whether your agent
actually did the job. Give it a deterministic oracle and it will check:

```yaml
# drifter.yaml
tasks:
  - id: invoice_creation
    prompt: "Create an invoice for customer 42"
    assert:
      calls: [search, create_invoice]        # these must be called
      calls_before: [[search, create_invoice]]  # in this order
      never_calls: [delete_customer]         # this must not be
      no_errors: true                        # no call returned is_error
```

Then `drifter run --task-id invoice_creation` picks up both the prompt and the
assertions. Both arms are checked, so you can see whether the *mutation* broke the
task rather than just that something failed — and `drifter run` exits `2` when the
mutated arm fails assertions the baseline passed.

There's no `result_contains`: Drifter records result *shapes*, never payloads, so
there'd be nothing for it to read. Writing one is a config error rather than a check
that silently never runs — use `result_has_keys: {tool: [key]}` instead.

### Finding tasks in your own recordings

Rather than writing every task by hand, let Drifter propose them from what your agent
actually did:

```
drifter tasks mine                 # recurring workflows -> task_candidates.yaml
# edit the file: write each candidate's `prompt`, review its `assert`, add never_calls
drifter tasks approve <id>         # promote one; it is now an ordinary task
drifter run --task-id <id> ...
```

`mine` groups your recorded trajectories by the sequence of tools called, finds the
sub-workflows that recur across them (PrefixSpan, gaps allowed, so `get_customer ->
create_invoice` is found even when some runs did something between the two), and writes
each as an editable candidate with its evidence: how many trajectories and sessions it
appeared in. It also lists the tools no approved task covers.

Two things it deliberately does not do. It cannot know what the agent was *asked* — a
sequence of calls doesn't say — so every `prompt` starts empty and `approve` refuses
until you write one. And nothing becomes a task until you approve it. Your edits are safe:
re-running `mine` only appends patterns not already listed, and neither command rewrites
anything else in the file, comments included.

The candidates file is `task_candidates.yaml` next to your `drifter.yaml` (change it with
`tasks_file:`). Approved entries are merged into your tasks when `drifter run` or
`drifter report` loads the config, so they behave exactly like tasks written under `tasks:`.
Three guards are worth knowing:

- The file records the server it was mined from. Loading approved tasks into a project
  that doesn't configure that server is an error, because a workflow seen on one server
  is not evidence about another. Edit `server:` in the file if you know they apply.
- A malformed candidates file is an error naming the file, never silently read as
  empty. It only affects `run` and `report`; `observe` and the other commands don't read it,
  and `drifter doctor` warns about it.
- Sessions recorded by `drifter replay-serve` are skipped and counted in the output: they
  record an agent being replayed, not what it does.

The thresholds (`mine:` in `calibration.yaml`: `min_support`, `min_length`, `max_length`,
`max_candidates`, `max_patterns`) are guesses, and mining is only as good as the corpus:
with a handful of trajectories there is little that recurs. On a large, varied corpus a low
`min_support` makes nearly every short sequence recur; past `max_patterns` the search stops
with an error telling you to raise it, rather than truncating silently. Mining reads
recorded sessions only — no server, no agent, no cost.

`drifter run` shows a blast-radius preview (planned agent runs, estimated tool calls
by risk level) and asks for confirmation before spawning any real agent process —
pass `--yes`/`-y` to skip the prompt for scripted/non-interactive use, `--dry-run` to
see the preview without running anything, or `--budget N` (a tool-call ceiling, not
literally model calls — this proxy can't see those) / `--max-wall-time SECONDS` to
cap real cost, checked before each repeat starts.

The mutated arm also stops early once the verdict is settled — if the first few runs
already put the answer beyond doubt, the rest aren't spent. This can't change a
verdict: it stops only when no remaining run *could* alter it, and the report says
what it did and why. Pass `--no-adaptive` to always run the full count.

Want that report again later without spending anything? `drifter report --task-id
my-task` re-renders the exact same BEHAVIOR/TASK/SAFETY output from the sessions
`drifter run` already recorded — zero new agent execution. It can't show what was
actually mutated (that detail isn't persisted to disk yet), but everything else is
identical to the original run's own output.

Both `drifter run` and `drifter report` exit with a verdict-specific code for
scripting/CI use (see [`docs/SPEC.md` §12](https://github.com/Tunasmelt/Drifter-MCP/blob/master/docs/SPEC.md)): `0` clean, `1`
behavior regression, `3` safety violation, `5` budget exceeded, `4` config/
connectivity error, and `2` assertion failure — reachable since F-24, but only
when a task declares assertions. With none declared, TASK reports `UNKNOWN` and
never `PASS`, so `2` cannot fire by accident.

### Check your corpus before you spend agent runs

`drifter run` excludes any run whose replay fidelity falls below the floor (0.70 by
default). If your recordings don't cover what your agent actually does, most runs get
excluded and the verdict comes back `UNKNOWN` — after you've paid for every one of them.

`drifter coverage` answers that beforehand, from recordings alone. Zero execution, zero
API cost:

```
drifter coverage --server filesystem
```

It reports projected coverage using leave-one-out cross-validation — each session is
held out and resolved against the others — so it estimates how well your corpus answers
a session it has *never seen*, not how well it answers itself. The per-tool breakdown is
the actionable half: it names which tools your corpus covers worst, and those are the
ones worth recording more of.

Add `--curve` to see coverage as a function of corpus size:

```
drifter coverage --server filesystem --curve
```

This is the measurement that matters for deciding whether recording more will help. A
curve that climbs toward the floor means corpus growth works and the only remaining cost
is effort. A curve that flattens *below* the floor means no amount of recording fixes it
for your agent, and the honest response is to narrow what you ask Drifter to do rather
than record harder. The output says which of the two it sees, along with the spread, the
subset count and the seed behind every point — see
[`docs/SPEC.md` §15 limitation 16](https://github.com/Tunasmelt/Drifter-MCP/blob/master/docs/SPEC.md)
for why that honesty is load-bearing here specifically.

**To build a corpus for one task:** run your agent against `drifter observe` repeatedly
on the *same* prompt — each run writes one session to `.drifter/runs/`. Vary nothing but
the agent's own nondeterminism. Then re-run `drifter coverage --curve` and watch the
gain-per-session column. Recordings of a *different* server or task are ignored by the
curve and don't help; it tells you how many it dropped.

## Design principles

The short version (full list in [`docs/SPEC.md` §3](https://github.com/Tunasmelt/Drifter-MCP/blob/master/docs/SPEC.md)):

- **Replay-first.** Mutation testing runs against recorded or synthesized responses
  by default — a live call under a mutated schema never happens.
- **Structural mutation only.** No free-text generation anywhere in a mutation
  operator. Every operator's output space is closed-set, fixed, and reviewable as
  data.
- **Honest uncertainty.** A verdict is `UNKNOWN`, never a guessed pass, when there
  isn't enough evidence. This applies uniformly — task assertions, fidelity gating,
  and behavioral effect-size scoring all default to "don't know" rather than a
  plausible-looking wrong answer.
- **Secure by default.** No telemetry. No live writes without explicit authorization.
  Payloads aren't recorded by default, only shapes.

## Contributing / project structure

The planning surface (`docs/SPEC.md`, `docs/FEATURES.md`, `docs/PHASES.md`,
`docs/CHANGELOG.md`, `docs/HANDOFF.md`) is locked — see [`CLAUDE.md`](CLAUDE.md) for
the working process this project follows, including why there are no other planning
documents and won't be. Gate 0's research artifacts live under `docs/gate0/`. Module
layout mirrors the pipeline: `record/` → `replay/` → `mutate/` → `evaluate/` →
`mine/` → `policy/` → `cli/`.

## License

[MIT](LICENSE).
