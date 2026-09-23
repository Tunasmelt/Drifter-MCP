# Security Policy

## Reporting a vulnerability

Please report security vulnerabilities privately, not in a public issue.

Use [GitHub Security Advisories](https://github.com/Tunasmelt/Drifter-MCP/security/advisories/new)
for this repository. Include what you found, how to reproduce it, and its impact. We'll
acknowledge receipt and follow up with next steps.

## Supported versions

Drifter is pre-1.0 (alpha). Security fixes land on `master` and are released promptly;
there is no separate maintenance branch for older versions yet.

## Threat model

Drifter is a single-user, local CLI tool: no accounts, no hosted server, no telemetry.
Everything it does runs as the invoking user's own process, with the invoking user's own
filesystem and network access. The relevant boundaries are:

- **Data written to disk.** `drifter observe` records tool-use trajectories to
  `.drifter/runs/*.jsonl` (shapes) and `.drifter/raw/*.frames` (full wire frames, off by
  default). Payload *values* are never written unless you explicitly opt into
  `--record-full`; string values likely to be secrets (API keys, tokens, credentials
  matching common high-entropy patterns) are redacted before anything touches disk.
  Recorded data still includes tool names, call frequency, timing, and error rates —
  real operational detail about your server, not a secret in the redaction sense, but
  not something to publish either. `.drifter/` is `.gitignore`d by default; don't
  override that for a real project without thinking about what you're publishing.
- **The loopback HTTP listener** (`agent.mode: http`, used when your agent is driven by
  a client — like the Claude Code CLI — that speaks MCP over its own subprocess rather
  than yours). Binds to `127.0.0.1` only, with no config option to widen that. Validates
  the `Origin` header on every request per the MCP Streamable HTTP transport's own
  security requirements, closing the DNS-rebinding path that section names explicitly.
  Uses an OS-assigned ephemeral port, never a fixed or predictable one. Carries no
  authentication token: the listener's lifetime is bounded to one `drifter run`
  invocation, it serves only replayed or synthesized data — never a live tool call under
  a mutated schema, a non-negotiable invariant enforced structurally, not just by
  default configuration — and the process holding the port is torn down the same way the
  spawned agent subprocess itself is.
- **Mutation output.** All three mutation operators (`description_update`,
  `tool_addition`, `parameter_rename`) are closed-set and structural: no free-text
  generation anywhere in the output space. Generated text is also checked against
  imperative-instruction patterns (`ignore`, `always call`, `you must`, `disregard`,
  `instead of`) and rejected if it matches, so a mutation can't accidentally produce
  something that reads as a prompt injection. Every mutation is logged with an exact
  before/after and an inverse mapping.
- **Dependencies.** `pip-audit` runs in CI on every push and pull request, failing the
  build on known high/critical-severity vulnerabilities in the dependency tree.

**Out of scope**, by design, not oversight: authorization boundaries, record-level access
control, and rate limiting don't apply to a single-user local CLI tool with no accounts
and no persistent, network-exposed server. A genuine hosted mode (multi-user, persistent,
network-exposed) is not planned for v1 or v1.5 and would need this document revisited
before it shipped, not just the code.

## Design history

This file previously documented the design-level security review done at Gate 0, before
any code existed — the reasoning behind the `.gitignore` requirement above, the CI
dependency-audit requirement, and the loopback-HTTP-listener decisions were all made and
recorded there ahead of the code that implements them. That history, and the reasoning
behind each decision, is preserved in `docs/CHANGELOG.md` and `docs/SPEC.md` §3/§15 for
anyone who wants the full account rather than the current-state summary above.
