"""parameter_rename mutation operator (F-40), docs/SPEC.md §7/§10.

The third Level 0/1 structural mutation operator, added specifically to
give F-12 (inverse-mutation key resolution) a real inverse to resolve
against — F-12's own docs/FEATURES.md entry had sat unbuilt as a stub since
Gate 2 for exactly that reason ("needs a real mutation's recorded inverse
to resolve against"). `description_update` (F-16) is schema-immune and
`tool_addition` (F-17) has no prior recording to invert against at all
(docs/SPEC.md §7's own text) — neither could ever exercise tier 2, by
construction, so building this operator and F-12 together, in the same
change, is the only way either becomes real rather than aspirational.

Closed-set, deterministic, structural — same standard as the other two
operators: exactly one JSON-Schema top-level property per tool is renamed,
snake_case to camelCase (`customer_id` -> `customerId`, docs/SPEC.md §13's
own illustrative report example), never touching a tool's name, its
description, or any property's own type/description/nesting. No table to
review here (unlike description_update's synonym table or tool_addition's
archetype pool) — the transformation is a pure, mechanical, structurally
invertible string rule, not chosen content, so there's nothing "generated"
to safety-review the way those two operators' own output is.

Scope decisions, stated explicitly:

1. Exactly ONE property per tool, not every eligible one. Renaming every
   snake_case property would maximize inverse-resolution coverage, but
   this operator's whole purpose is to give F-12 something concrete and
   checkable to resolve against — one deterministic, unambiguous rename
   per tool is enough to prove tier 2 works, and matches this project's
   established "narrow, real, working scope" precedent (F-14 scoped to
   tool_addition only, F-13 scoped to exact value multisets) over
   speculative breadth.

2. The alphabetically-first ELIGIBLE property, not the first one in
   dict/JSON insertion order. Property order in a `dict` reflects
   whatever order the recording or config file happened to serialize it
   in — not a meaningful signal — so choosing by it would make "which
   property gets renamed" depend on incidental JSON formatting rather
   than the tool's own content. Alphabetical order is deterministic given
   only the schema itself, matching every other operator's "same input,
   same seed, same output" contract. `seed` here selects nothing (there is
   only one deterministic choice once eligibility is decided) — kept as a
   parameter anyway, matching `mutate_tool_manifest`'s/`add_tool`'s own
   signatures, so `cli/run.py`'s dispatch can treat all three operators
   uniformly.

3. "Eligible" means: the name contains an underscore (there is something
   to convert), AND the resulting camelCase name doesn't collide with an
   existing sibling property on the SAME tool. A tool with zero eligible
   properties (no properties at all, or every name is already a single
   word / already camelCase / every conversion collides) is left
   completely untouched — reported honestly via `MutationLogEntry` (`
   after="(no eligible parameter)"`, `inverse=None`), never forced into a
   no-op-shaped rename.

4. `required` is updated in lockstep when the renamed property was
   required — an incoming call's structural validity against the SERVED
   schema must still hold after the rename, or a well-behaved agent's own
   client-side schema validation could reject the mutated tool outright
   before ever calling it, which would confound this operator's real
   experimental purpose (does an agent's OBSERVED BEHAVIOR change) with an
   unrelated client-side validation failure.

Inverse mapping (F-12's actual consumer): `MutationLogEntry.inverse` is
`{new_name: old_name}` for the one renamed property (see that field's own
widened docstring in `mutate/description_update.py`), or `None` when
nothing was renamed. `inverse_map_from_log` below extracts `{tool_name:
{new_name: old_name}}` across a whole mutation log — generic across
whichever operator produced it (only entries with a real dict `inverse`
contribute), so `cli/run.py` doesn't need to special-case which operator
is active to build the map `replay.replay_store.ReplayStore.lookup` needs.
"""

from __future__ import annotations

import re

from mcp_drifter.mutate.description_update import MutationLogEntry
from mcp_drifter.record.schema import ToolDescriptor

# Converts one snake_case underscore-boundary into a camelCase hump:
# "customer_id" -> "customerId". Deliberately narrow (ASCII lowercase/
# digit word chars only, matching real-world JSON-Schema property naming
# conventions observed in the golden fixture) -- not a general
# identifier-casing library.
_SNAKE_BOUNDARY_RE = re.compile(r"_([a-z0-9])")


def _snake_to_camel(name: str) -> str | None:
    """Returns the camelCase form of `name`, or `None` if there's nothing
    to convert (no underscore at all -- already single-word or already
    camelCase) -- same "nothing to do, report honestly" convention
    `mutate.description_update.mutate_description` already uses for a
    description with nothing to substitute or reorder."""
    if "_" not in name:
        return None
    camel = _SNAKE_BOUNDARY_RE.sub(lambda m: m.group(1).upper(), name)
    return camel if camel != name else None


def camel_to_snake(name: str) -> str:
    """The exact inverse of `_snake_to_camel` — used only by tests to
    confirm round-tripping; `ReplayStore.lookup`'s own inverse resolution
    doesn't recompute this at all, it just looks up the literal
    `{new_name: old_name}` mapping this module already recorded at
    mutation time, which is more precise than re-deriving it (a real
    camelCase name can be ambiguous to reverse — e.g. consecutive capitals
    — the recorded mapping never is).
    """
    return re.sub(r"[A-Z]", lambda m: "_" + m.group(0).lower(), name)


def _rename_first_eligible_property(tool: ToolDescriptor) -> tuple[ToolDescriptor, str | None, str | None]:
    """Returns `(possibly-mutated tool, old_name, new_name)` —
    `old_name`/`new_name` are both `None` when nothing was eligible."""
    schema = tool.input_schema if isinstance(tool.input_schema, dict) else {}
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        return tool, None, None

    for prop_name in sorted(properties):
        new_name = _snake_to_camel(prop_name)
        if new_name is None or new_name in properties:
            continue

        new_properties = dict(properties)
        new_properties[new_name] = new_properties.pop(prop_name)
        new_schema = dict(schema)
        new_schema["properties"] = new_properties
        required = new_schema.get("required")
        if isinstance(required, list) and prop_name in required:
            new_schema["required"] = [new_name if r == prop_name else r for r in required]

        mutated_tool = tool.model_copy(update={"input_schema": new_schema})
        return mutated_tool, prop_name, new_name

    return tool, None, None


def rename_tool_parameters(
    tools: list[ToolDescriptor], seed: int
) -> tuple[list[ToolDescriptor], list[MutationLogEntry]]:
    """Applies `_rename_first_eligible_property` to every tool in the
    manifest. Tool name, description, and every property besides the one
    renamed are untouched — matching `description_update`'s own "Schema
    Immunity"/"description only" boundary, mirrored here as "one property
    only, everything else immune."
    """
    mutated_tools: list[ToolDescriptor] = []
    log_entries: list[MutationLogEntry] = []

    for tool in tools:
        mutated_tool, old_name, new_name = _rename_first_eligible_property(tool)
        mutated_tools.append(mutated_tool)
        log_entries.append(
            MutationLogEntry(
                tool_name=tool.name,
                operator="parameter_rename",
                before=old_name,
                after=new_name or "(no eligible parameter)",
                inverse={new_name: old_name} if new_name is not None else None,
                seed=seed,
                injection_flagged=False,
            )
        )

    return mutated_tools, log_entries


def inverse_map_from_log(log_entries: list[MutationLogEntry]) -> dict[str, dict[str, str]]:
    """Extracts `{tool_name: {new_param_name: old_param_name}}` from a
    mutation log produced by ANY operator, not just this one — an entry
    whose `inverse` is `None` (description_update/tool_addition, always;
    parameter_rename, when nothing was eligible) simply contributes
    nothing. This is what lets `cli/run.py` build `ReplayStore.lookup`'s
    `inverse_param_map` uniformly regardless of which `--operator` is
    active, rather than hardcoding "only parameter_rename has an inverse"
    into the orchestration layer.
    """
    return {entry.tool_name: entry.inverse for entry in log_entries if entry.inverse}
