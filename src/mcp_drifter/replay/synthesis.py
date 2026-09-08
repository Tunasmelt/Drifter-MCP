"""F-14 general synthetic response generation.

Builds a structurally valid, content-empty response for a tool from its
recorded `outputSchema` (`ToolDescriptor.output_schema`), for the case
where replay has no recording to answer a call with at all.

Three rules define the boundary, and they are the whole point of this
module rather than incidental caution:

1. **Never invent a value.** Every generated leaf is the zero value for
   its declared type -- `""`, `0`, `False`, `[]`, `{}`. A synthesized
   `"/home/user/report.pdf"` would be a fabricated claim about a world
   this recording never observed; `""` is the absence of a claim. The
   single exception is `enum`, where no zero value is a member and the
   first declared one is the only option that is simultaneously
   schema-valid and not a judgement about likelihood.

2. **Never emit prose.** Nothing here produces explanatory text, and
   `replay_proxy.py`'s content blocks stay empty. During Gate 3
   dogfooding a real agent (Claude Code) read an earlier explanatory
   placeholder and refused to proceed, calling it a prompt-injection
   attempt -- see that module's own note. Empty text makes no claim to
   evaluate; softened prose is still prose.

3. **Never let this look like a hit.** Synthesis here is a response to
   a MISS, and the caller records it with its own `"synthetic_miss"`
   provenance, which counts in the fidelity denominator. This is
   deliberately NOT F-17's `"synthetic"` provenance, which is excluded
   from that denominator because a mutation-injected tool has no prior
   recording by definition. A general miss had a recording available in
   principle and did not get one. Conflating them would mean a run that
   missed every call computed fidelity 1.0 vacuously and produced a
   confident verdict on no evidence -- exactly the defect DEC-027
   closed. See docs/SPEC.md §7.

What this does NOT do, per DEC-027: it does not improve the miss RATE,
only what a miss does to the session (the agent can continue instead of
receiving a protocol error). Limitation 16 is untouched by it.
"""

from __future__ import annotations

from typing import Any

# Zero value per JSON Schema primitive type. `null` maps to None, which is
# also this module's "I cannot synthesize anything" signal -- harmless
# because both mean "no structured content to offer", and a schema whose
# declared type IS null wants exactly that.
_ZERO_BY_TYPE: dict[str, Any] = {
    "string": "",
    "integer": 0,
    "number": 0,
    "boolean": False,
    "array": [],
    "object": {},
    "null": None,
}


def synthesize_structured_content(schema: dict | None) -> Any:
    """Returns a zero-valued instance conforming to `schema`, or `None`
    when nothing can be synthesized (no schema, or no declared type).

    `None` for an untyped schema is deliberate rather than a fallback to
    `{}`: an empty object is a positive claim that the tool returns an
    object with no required fields, which an untyped schema does not
    say. Returning None lets the caller fall back to a content-empty
    result and record honestly that it had no output contract to work
    from -- see `ToolDescriptor.output_schema` on why `None` and `{}`
    must stay distinguishable all the way down.
    """
    if not schema:
        return None

    # `enum` is checked before `type` because an enum member is the
    # binding constraint -- a schema can declare both, and the zero value
    # for the type is very unlikely to be a member.
    enum = schema.get("enum")
    if enum:
        return enum[0]

    declared = schema.get("type")
    if isinstance(declared, list):
        # A union (commonly `["string", "null"]`). The first named type is
        # taken rather than preferring "null", so the synthesized value
        # exercises the shape the tool actually describes.
        declared = declared[0] if declared else None
    if not isinstance(declared, str):
        return None

    if declared == "object":
        return _synthesize_object(schema)
    if declared == "array":
        # Always empty, never one synthesized element: an empty array is
        # valid against every `items` schema, and `minItems` is not
        # honored on purpose -- padding to satisfy it would mean emitting
        # fabricated elements, which rule 1 forbids. A schema with
        # minItems > 0 is one of the cases where synthesis cannot be
        # fully faithful, and under-claiming is the right failure.
        return []
    return _ZERO_BY_TYPE.get(declared)


def _synthesize_object(schema: dict) -> dict:
    """Emits exactly the `required` properties, recursively.

    Optional properties are omitted rather than zero-filled: leaving one
    out is valid against the schema, whereas including it asserts the
    tool returned a field the recording never showed it returning.
    """
    properties = schema.get("properties") or {}
    required = schema.get("required") or []
    return {name: synthesize_structured_content(properties.get(name)) for name in required if name in properties}
