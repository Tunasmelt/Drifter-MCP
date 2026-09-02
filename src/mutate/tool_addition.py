"""tool_addition mutation operator (F-17), docs/SPEC.md §10.

Mechanism, decided explicitly (same discipline as description_update's
closed-set decision — a real architectural choice, not a formality):
a small, fixed set of complete tool ARCHETYPES (name + description +
input_schema, each authored and reviewed as a unit), one selected per
call via a seed. No free-text generation, no LLM, matching this gate's
zero-generation architectural stance — the "structural, not free-text"
framing that motivated description_update's own mechanism applies
identically here. "Styled plausibly consistent with sibling tools"
(F-17's own docs/FEATURES.md wording) is achieved by each archetype already
being written in an ordinary, generic "MCP server utility tool" register
(snake_case verb_noun naming, a short declarative description, a small
JSON-Schema `input_schema`) — the same register real MCP servers
actually use (confirmed against the golden fixture's own 14 real tools:
short imperative-toned descriptions, snake_case names, simple object
schemas) — not by adapting per-manifest at call time. Keeping the pool
small and fully pre-reviewed (like description_update's synonym table)
is what makes the safety story tractable: every possible output is
something a human already read before this operator ever ran.

Safety: unlike description_update, there is no SOURCE text here to
launder — every archetype is this operator's own, first-party content.
So the injection check is closer to primary defense than description_
update's defense-in-depth framing: a flagged archetype would be this
operator's own authoring mistake, not something carried through from
elsewhere. Confirmed empirically, not just claimed: the rejection tests
in tests/mutate/test_tool_addition.py were run against a first,
deliberately naive implementation (an archetype pool with "You must
call this tool first..." and no check at all) and failed on both the
static pool-review check and the runtime belt-and-suspenders check —
see the module's own git history / session record for that red run.

Name collision: resolved deterministically, never silently. add_tool
tries archetypes in a seeded order, skipping any whose name already
belongs to a sibling tool in the manifest being mutated — a collision
must never produce a tool that shares a real sibling's name (which
would make the injected tool indistinguishable from — and able to
shadow calls intended for — a real one). If every archetype collides
(not possible with the current pool against any real manifest observed
so far, but not structurally impossible), add_tool raises
NameCollisionError explicitly rather than falling back to something
unreviewed.

Audit logging: reuses mutate.description_update.MutationLogEntry
(imported, not redefined — same shape, same rationale: F-18's own
docs/FEATURES.md entry states "Depends on: F-16, F-17," so a general log
format can't exist before both operators do). `before=None` here
specifically — nothing existed before an addition, not a placeholder.
`inverse=None`, confirmed this round: docs/SPEC.md §7's own text excludes
tool_addition from tier 2's applicability outright (no prior recording
to invert against, by definition).

F-14 (synthetic response generation), scoped narrowly to THIS operator's
own added tool, lives in replay/replay_proxy.py's `synthetic_tool_names`
parameter — not here. That module owns serving; this module only
produces the tool definition and its audit record. See replay_proxy.py's
own docstring for why the wire response is a single, generic placeholder
rather than anything schema-derived (there is no prior recording to
derive a plausible shape from, by definition — docs/SPEC.md §7).
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from mutate.description_update import _INJECTION_RE, MutationLogEntry
from record.schema import ToolDescriptor


@dataclass(frozen=True)
class ToolArchetype:
    name: str
    description: str
    input_schema: dict


class NameCollisionError(Exception):
    """Every archetype's name collided with an existing sibling tool."""


class InjectionFlaggedError(Exception):
    """An archetype's description matched docs/SPEC.md §10's injection-
    pattern check. Should be unreachable against the real, reviewed
    _ARCHETYPES pool below (see test_no_archetype_description_
    contains_an_injection_pattern) — this exists so a future archetype
    added without re-running that check fails loudly at call time
    instead of silently shipping flagged content.
    """


# Fixed, committed, reviewable as data -- not generated, not fetched.
# Deliberately small: five generic "server utility tool" archetypes,
# each already reviewed for style (matches real MCP servers' own
# register — confirmed against the golden fixture's 14 real tools) and
# safety (verified injection-free below, at both import time via the
# test suite and defensively at call time — see add_tool).
_ARCHETYPES: tuple[ToolArchetype, ...] = (
    ToolArchetype(
        "get_status",
        "Returns the current operational status of the server, including readiness and any active warnings.",
        {"type": "object", "properties": {}, "required": []},
    ),
    ToolArchetype(
        "clear_cache",
        "Clears any internal cache maintained by the server, so subsequent operations re-read from source.",
        {"type": "object", "properties": {}, "required": []},
    ),
    ToolArchetype(
        "validate_config",
        "Checks the server's current configuration for errors and returns a list of any problems found.",
        {
            "type": "object",
            "properties": {"strict": {"type": "boolean", "description": "Treat warnings as errors."}},
            "required": [],
        },
    ),
    ToolArchetype(
        "refresh_index",
        "Rebuilds the server's internal index from the current state of its underlying data source.",
        {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Optional subtree to limit the refresh to."}},
            "required": [],
        },
    ),
    ToolArchetype(
        "list_capabilities",
        "Returns a summary of the optional features this server currently has enabled.",
        {"type": "object", "properties": {}, "required": []},
    ),
)


def add_tool(siblings: list[ToolDescriptor], seed: int) -> tuple[ToolDescriptor, MutationLogEntry]:
    """Selects one archetype (seeded, reproducible) whose name doesn't
    collide with any tool already in `siblings`, and returns it as a
    real `ToolDescriptor` plus its audit-log entry.

    Injection check runs on the SELECTED archetype at call time (not
    only relied on at import time) — see InjectionFlaggedError's own
    docstring for why this exists even though the current pool is
    already verified clean.
    """
    order = list(range(len(_ARCHETYPES)))
    random.Random(seed).shuffle(order)
    sibling_names = {s.name for s in siblings}

    for i in order:
        archetype = _ARCHETYPES[i]
        if archetype.name in sibling_names:
            continue

        if _INJECTION_RE.search(archetype.description):
            raise InjectionFlaggedError(
                f"archetype {archetype.name!r} matched an injection pattern -- refusing to add it"
            )

        tool = ToolDescriptor(name=archetype.name, description=archetype.description, input_schema=archetype.input_schema)
        entry = MutationLogEntry(
            tool_name=tool.name,
            operator="tool_addition",
            before=None,
            after=tool.model_dump_json(),
            inverse=None,
            seed=seed,
            injection_flagged=False,
        )
        return tool, entry

    raise NameCollisionError(
        f"every archetype name collides with an existing sibling tool: {sorted(a.name for a in _ARCHETYPES)}"
    )
