# Drifter

A proxy-based regression-testing harness that sits on the MCP connection between your
agent and its tools. It records real tool-use trajectories, replays them safely
offline, mutates the tool interface in controlled ways, and reports behavioral, task,
and safety regressions with explicit uncertainty — never a silent guess.

`mcp-drifter` on PyPI. Console command: `drifter`.

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
tool — replayed from your recordings, at zero marginal cost per replayed call. If
your agent's behavior changes, Drifter tells you, with a real effect size, not a
vibe — and when your recordings don't cover enough of what your agent actually does
to support a verdict, it tells you that instead of guessing.

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
   surviving runs. See [`docs/SPEC.md` §15, limitation 16](docs/SPEC.md) for the real
   measurements behind that, and DEC-027 in
   [`docs/CHANGELOG.md`](docs/CHANGELOG.md) for what is and isn't being done about it.
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

Pre-v1, under active gated development (see [`docs/PHASES.md`](docs/PHASES.md)).
Gates 0–3 are closed:

- **Record & replay** (`drifter observe`, exact-key replay, redaction, trajectory
  segmentation) — built and tested.
- **Baseline analysis & re-scoring** (`drifter score`) — re-analyzes already-recorded
  data with zero new agent execution and zero API calls.
- **Mutation** (`description_update`, `tool_addition`, `parameter_rename`) — built,
  safety-reviewed (red-test-first against prompt-injection-shaped output), tested
  against a real agent.
- **Orchestration** (`drifter run`) — baseline + one mutation operator + behavioral
  comparison, run against a real dogfood pairing (Claude Code + a real filesystem MCP
  server).
- **Setup** (`drifter init`) — scans `.mcp.json`/`.cursor/mcp.json`/Claude Desktop's
  config for existing stdio MCP servers and writes a starter `drifter.yaml`, so you
  don't have to hand-write your server list.

Task assertions, safety verdicts, mutation mining/approval, and the full report
format are not built yet — see [`docs/FEATURES.md`](docs/FEATURES.md) for the
complete per-feature breakdown and [`docs/SPEC.md` §15](docs/SPEC.md) for known
limitations, stated plainly, including two found only by testing against a real
agent rather than a scripted stand-in.

## Install

```
pip install mcp-drifter    # not yet published — see pyproject.toml for local install
```

For now, from a checkout:

```
uv sync
uv run drifter --help
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
[`docs/SPEC.md` §11](docs/SPEC.md)):

```yaml
# drifter.yaml, in addition to `servers:` above
agent: {mode: subprocess, command: "python agent.py --task '{task.prompt}'"}
```

`mode: subprocess` fits an agent that itself speaks MCP directly over the process
you spawn. If your agent instead launches its own MCP client subprocess (e.g. the
Claude Code CLI, which spawns servers from its own `--mcp-config` rather than
speaking MCP on its own stdio), use `mode: http` instead — Drifter serves the proxy
over a loopback HTTP URL and injects it into the environment your own launch
mechanism reads, rather than spawning anything itself:

```yaml
agent: {mode: http, env_var: DRIFTER_PROXY_URL}
```

Then:

```
drifter run --fixture <recorded.jsonl> --server my-server \
            --task-id my-task --prompt "..." --operator description_update
```

`drifter run`'s current scope is deliberately minimal (see its own module docstring)
— one task, one operator, a behavioral comparison. It is not yet the full orchestrated
`v1` command surface. **Known limitation, confirmed against a real, non-scripted
agent** (see [`docs/SPEC.md` §15, limitation 16](docs/SPEC.md)): exact-match replay
frequently fails to match a real agent's actual call pattern, which can exclude most
runs for low fidelity and leave a verdict computed from very little surviving data —
always check the report's `N/10 valid runs` lines, not just its headline verdict,
before trusting a `REGRESSION`/`NO_REGRESSION` result.

`drifter run` shows a blast-radius preview (planned agent runs, estimated tool calls
by risk level) and asks for confirmation before spawning any real agent process —
pass `--yes`/`-y` to skip the prompt for scripted/non-interactive use, `--dry-run` to
see the preview without running anything, or `--budget N` (a tool-call ceiling, not
literally model calls — this proxy can't see those) / `--max-wall-time SECONDS` to
cap real cost, checked before each repeat starts.

Want that report again later without spending anything? `drifter report --task-id
my-task` re-renders the exact same BEHAVIOR/TASK/SAFETY output from the sessions
`drifter run` already recorded — zero new agent execution. It can't show what was
actually mutated (that detail isn't persisted to disk yet), but everything else is
identical to the original run's own output.

Both `drifter run` and `drifter report` exit with a verdict-specific code for
scripting/CI use (see [`docs/SPEC.md` §12](docs/SPEC.md)): `0` clean, `1`
behavior regression, `3` safety violation, `5` budget exceeded, `4` config/
connectivity error. `2` (assertion failure) is defined but can't fire yet —
TASK always reports `UNKNOWN` until task assertions ship as an authored
feature.

## Design principles

The short version (full list in [`docs/SPEC.md` §3](docs/SPEC.md)):

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
