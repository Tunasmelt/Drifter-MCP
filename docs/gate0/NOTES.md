# MCPEvol-Bench Mutation Operator Taxonomy — Design Notes

**Source:** arXiv:2607.14642, Section 5.2 and Figure 4. Extracted 2026-08-16 as a Gate 0
deliverable per PHASES.md. This is a design reference for `mutate/`, not a spec — Drifter's
operators are proxy-based rewrites of `tools/list`, not source-code AST mutations like the
paper's. Definitions below describe *what changes*; Drifter's implementation of *how* differs
by necessity (SPEC.md §7 — inverse-mutation replay resolution assumes proxy-layer, not
source-layer, mutation).

## Full taxonomy — three hierarchical levels

Per the paper: "higher-level mutations encompass lower-level ones" (Fig. 4 caption).

### TOOL level (4 operators) — structural, whole-tool changes

| ID | Operator | Effect on task fulfillment | Drifter status |
|---|---|---|---|
| O1 | **Tool Addition** | −0.96 (worst) | **Gate 3, F-17** |
| O2 | Tool Replacement | +0.10 | v1+ |
| O3 | Tool Removal (paper: "Tool Deletion") | −0.13 | v1+ |
| O4 | **Tool Integration** | −0.90 (2nd worst) | v1 (deferred from Gate 3 — see note below) |

### PARAM level (4 operators) — parameter structure/constraint changes

| Operator | Description (paper §5.2) | Score | Status |
|---|---|---|---|
| Flexible Expansion | Add optional parameters, preserving backward compatibility | not individually reported | v1+ |
| Constraint Mutation | Change validation constraints on existing parameters | not individually reported | v1+ |
| Parameter Pruning | Remove unused/redundant parameters | +0.08 | v1+ |
| Interface Refactoring | Restructure parameter types/required attributes | not individually reported | v1+ |

Note: `parameter_rename` and `parameter_type_change`, referenced throughout SPEC.md §7's
replay-key design, are Drifter's own decomposition of what the paper groups under
"Interface Refactoring" — the paper doesn't isolate rename as its own operator. Drifter's
inverse-mutation replay tier (SPEC.md §7, tier 2) is specifically viable for rename and
type-change because they're structurally invertible; treat "Interface Refactoring" broadly
as a v1+ superset once more of it is decomposed.

### DESC level (3 operators) — natural-language description changes

| Operator | Description | Score | Status |
|---|---|---|---|
| **Tool Description Update** | Rewrites a tool's top-level description | **−0.81 (3rd worst)** | **Gate 3, F-16** |
| Parameter Description Update | Rewrites individual parameter descriptions | not individually reported | v1+ |
| Joint Description Update | Combined tool + parameter description rewrite | not individually reported | v1+ |

## Full operator definitions (Table 11, Appendix F.3) — extracted verbatim

Previously only names and scored-subset numbers were captured. Full definitions,
independently re-verified against the primary source on 2026-08-16:

| # | Level | Name | Definition (verbatim) |
|---|---|---|---|
| O1 | TOOL | Tool Addition | "Adds a new tool to extend the server's functionality. Existing tools are kept unchanged to reduce regression risk and preserve backward compatibility." |
| O2 | TOOL | Tool Replacement | "Replaces an existing tool with an updated version to improve capability or design. The new tool should cover the original tool's main use cases, while other tools remain unaffected." |
| O3 | TOOL | Tool Removal | "Removes an obsolete tool and transfers its essential behavior into another existing tool. The aim is to reduce the number of tools while retaining key functionality." |
| **O4** | TOOL | **Tool Integration** | **"Adds a new tool and refines related tool descriptions to improve overall consistency. The goal is clearer tool roles, less overlap, and easier discovery for users."** |
| O5 | PARAM | Flexible Expansion | "Extends a tool interface by adding a small number of optional parameters. These parameters increase flexibility while keeping existing calls working as before." |
| O6 | PARAM | Constraint Mutation | "Changes parameter constraints, required/optional status, or data types to make the interface contract more accurate." |
| O7 | PARAM | Parameter Pruning | "Simplifies the interface by removing redundant or low-value parameters." |
| O8 | PARAM | Interface Refactoring | "Updates the tool description together with parameter additions/removals." |
| O9 | DESC | Tool Description Update | "Edits the tool description to better reflect what the tool does. It improves clarity and accuracy without changing behavior." |
| O10 | DESC | Parameter Description Update | "Edits parameter descriptions to make their meaning and intended usage clearer." |
| O11 | DESC | Joint Description Update | "Improves both tool and parameter descriptions to present a consistent and accurate specification." |

## CORRECTION (2026-08-16, post independent re-verification) — O4 was mischaracterized

The original version of this document described Tool Integration as merging two
tools' schemas, and used that as the reason to defer it from Gate 3 to v1 (replay-key
tier-3 fallback, no clean inverse). **That description was never verified against the
primary source and is wrong.** Per Table 11 above and the paper's own Appendix H.1
prompt for this operator ("Only modify description/text fields; DO NOT change name,
inputSchema, or outputSchema"), Tool Integration is compositionally just **Tool
Addition (O1) + description-only updates to related tools (O9-adjacent)** — the two
operators already scoped for Gate 3. There is no schema-merge and no tier-3 replay
problem.

**Consequence:** the stated reason for deferring O4 to v1 no longer holds. Whether O4
belongs in Gate 3 after all (as a near-free composition of F-16 + F-17) is a live
question for whoever scopes Gate 3's final task list — not resolved here, but the
prior reasoning for excluding it should not be relied upon.

## Corroborating evidence for SPEC.md principle 7 (structural mutations only)

Appendix H.1's prompt for the Tool Description Update operator enforces, verbatim:
"Schema Immunity: You MUST NOT alter the tool's name, inputSchema, outputSchema...
Your modifications are strictly limited to the description text field." The paper's
authors impose this via LLM prompt instruction; Drifter enforces the equivalent
constraint architecturally, at the proxy layer, which is a stronger guarantee (not
dependent on an LLM following instructions). Worth citing as prior-art corroboration
that unconstrained mutation of tool-facing text is a recognized risk, not a Drifter
invention.

## Source-document note (not a Drifter error)

§6.3 of the paper references "Table 6" when describing the historical-server
performance drop, but that data is actually in Table 5 (Table 6 is the BGE-M3
similarity table). This is the paper's own internal cross-reference error — flagged
here so it isn't later mistaken for a citation mistake in Drifter's own docs.



FEATURES.md F-16/F-17 map to **O9 (Tool Description Update)** and **O1 (Tool Addition)** —
the two most damaging *individually-scored* operators. O4 (Tool Integration, −0.90) is the
second-worst overall but was deliberately left for v1 (PHASES.md), not Gate 3, because it's
structurally harder: merging two tools' schemas has no clean inverse for replay-key
resolution (SPEC.md §7 tier 2 explicitly notes `tool_integration` degrades to tier 3
semantic matching, not tier 2 exact inverse). Building it alongside two invertible
operators in the same one-week gate risked conflating "genuinely hard to replay" with
"regression the harness correctly detected" — cleaner to prove the harness on two clean
cases first.

## What's NOT reusable from the paper as-is

- The paper's mutations are LLM-driven AST-anchored **source code** edits (Claude-Opus-4-5
  selecting operators, DeepSeek-Chat validating via generated test cases). Drifter's
  mutations are **proxy-layer** rewrites of the served `tools/list` response — the server's
  actual source is never touched (SPEC.md principle 3). This is the core architectural
  divergence, not an implementation detail: it's what lets Drifter test servers nobody
  controls, at the cost of not being able to model a tool whose *behavior* changed while its
  schema stayed identical (SPEC.md §15, limitation 4).
- The paper's per-operator scores come from an LLM judge (DeepSeek-Chat, 1–10 rubric) on
  201 synthesized tasks, not from Drifter's behavior/task/safety framework. Cite the
  *ranking* (which operators hurt most) as directional evidence; do not imply Drifter's
  own effect-size numbers will match these magnitudes — different measurement instrument
  entirely (SPEC.md §15, limitation 3, re: AgentAssay overlap applies a similar caution
  here).

## New evidence found during this extraction, not previously in the claims ledger

Real (not simulated) historical-version degradation, Table 5 of the paper — 50 real
historical server versions, 86 tasks: GPT-5.4 −12.3%, Claude-Sonnet-4-6 −11.7%,
Claude-Opus-4-6 −4.1%. Stronger evidence than the simulated-evolution numbers already in
SPEC.md's claims ledger, because it's not LLM-simulated mutation at all — see
CHANGELOG.md v1.0.2 for the claims-ledger update this triggered.
