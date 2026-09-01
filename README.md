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
tool — replayed from the recording, at zero marginal cost per run. If your agent's
behavior changes, Drifter tells you, with a real effect size, not a vibe.

## How it works

```
agent ──MCP──▶ drifter ──MCP──▶ real server      (drifter observe: record)
agent ──MCP──▶ drifter (replayed, mutated)        (drifter replay-serve / drifter run)
```

1. **Record** (`drifter observe`) — a transparent passthrough proxy between your agent
   and a real MCP server. Records the trajectory (which tools, in what order, with
   what shapes of arguments and results) to a local JSONL corpus. Payloads are never
   written by default — only shapes — and secrets are pattern-matched and redacted.
2. **Replay** — an already-recorded session becomes a offline stand-in for the real
   server: exact-match tool calls resolve instantly from the recording, for free,
   with no live connection and no API cost.
3. **Mutate** — two structural operators, both closed-set and reviewed as data, never
   free-text generation: `description_update` (bounded synonym substitution and
   sentence reordering) and `tool_addition` (a small, fixed pool of generic tool
   archetypes). Neither touches a tool's name or schema.
4. **Evaluate** — runs your agent against the same task through the unmutated and
   mutated manifests, and compares the resulting behavior. A verdict defaults to
   `UNKNOWN`, never a false pass, when there isn't enough data to say more.

## Status

Pre-v1, under active gated development (see [`PHASES.md`](PHASES.md)). Gates 0–3 are
closed:

- **Record & replay** (`drifter observe`, exact-key replay, redaction, trajectory
  segmentation) — built and tested.
- **Baseline analysis & re-scoring** (`drifter score`) — re-analyzes already-recorded
  data with zero new agent execution and zero API calls.
- **Mutation** (`description_update`, `tool_addition`) — built, safety-reviewed
  (red-test-first against prompt-injection-shaped output), tested against a real
  agent.
- **Orchestration** (`drifter run`) — baseline + one mutation operator + behavioral
  comparison, run against a real dogfood pairing (Claude Code + a real filesystem MCP
  server).

Task assertions, safety verdicts, mutation mining/approval, and the full report
format are not built yet — see [`FEATURES.md`](FEATURES.md) for the complete
per-feature breakdown and [`SPEC.md` §15](SPEC.md) for known limitations, stated
plainly, including two found only by testing against a real agent rather than a
scripted stand-in.

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
drifter run --fixture <recorded.jsonl> --server my-server \
            --task-id my-task --prompt "..." --operator description_update
```

`drifter run`'s current scope is deliberately minimal (see its own module docstring)
— one task, one operator, a behavioral comparison. It is not yet the full orchestrated
`v1` command surface.

## Design principles

The short version (full list in [`SPEC.md` §3](SPEC.md)):

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

The planning surface (`SPEC.md`, `FEATURES.md`, `PHASES.md`, `CHANGELOG.md`) is
locked — see [`CLAUDE.md`](CLAUDE.md) for the working process this project follows,
including why there are no other planning documents and won't be. Module layout
mirrors the pipeline: `record/` → `replay/` → `mutate/` → `evaluate/` → `mine/` →
`policy/` → `cli/`.

## License

[MIT](LICENSE).
