"""F-14 general synthetic response generation: `replay/synthesis.py`.

Scope, stated up front because F-14 has been narrowed twice before and
the boundary is what keeps it honest: this builds a structurally valid,
CONTENT-EMPTY response for a tool from its recorded `outputSchema`. It
never invents values, never calls an LLM, and never emits prose (see
`replay/replay_proxy.py`'s module note on why synthesized text is empty
rather than explanatory -- a real agent flagged explanatory placeholder
text as a prompt-injection attempt during Gate 3 dogfooding).

The fidelity contract is the part that matters most. F-17's
`tool_addition` synthesis is EXCLUDED from the fidelity denominator,
because no prior recording can exist for an injected tool by
definition. A general F-14 miss is a different thing: a recording could
have existed and did not. Reusing the `"synthetic"` provenance for it
would exclude it too, and a run that missed every single call would
then compute fidelity 1.0 vacuously, sail past the 0.70 floor, and
produce a confident verdict founded on nothing -- the exact failure
DEC-027's minimum-evidence work closed, re-entered through a different
door. Hence a distinct `"synthetic_miss"` provenance that counts in the
denominator and never as a hit.
"""

from __future__ import annotations

import jsonschema
import pytest

from mcp_drifter.replay.synthesis import synthesize_structured_content


class TestZeroValuesByType:
    """Every generated value is the empty/zero value for its declared
    type -- never a plausible-looking sample. A synthesized `"path"`
    of `"/tmp/example.txt"` would be a fabricated claim about the
    world; `""` is not.
    """

    @pytest.mark.parametrize(
        "schema,expected",
        [
            ({"type": "string"}, ""),
            ({"type": "integer"}, 0),
            ({"type": "number"}, 0),
            ({"type": "boolean"}, False),
            ({"type": "array", "items": {"type": "string"}}, []),
            ({"type": "object"}, {}),
            ({"type": "null"}, None),
        ],
    )
    def test_scalar_and_container_zero_values(self, schema, expected):
        assert synthesize_structured_content(schema) == expected


def test_only_required_properties_are_emitted():
    """An optional property left out is valid against the schema; an
    optional property invented is a claim the recording never made.
    """
    schema = {
        "type": "object",
        "properties": {"content": {"type": "string"}, "encoding": {"type": "string"}},
        "required": ["content"],
    }

    assert synthesize_structured_content(schema) == {"content": ""}


def test_an_object_with_no_required_list_emits_an_empty_object():
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}

    assert synthesize_structured_content(schema) == {}


def test_nested_required_objects_recurse():
    schema = {
        "type": "object",
        "properties": {
            "result": {
                "type": "object",
                "properties": {"lines": {"type": "array", "items": {"type": "string"}}, "count": {"type": "integer"}},
                "required": ["lines", "count"],
            }
        },
        "required": ["result"],
    }

    assert synthesize_structured_content(schema) == {"result": {"lines": [], "count": 0}}


def test_an_enum_uses_its_first_declared_value_since_empty_would_be_invalid():
    """The one place a zero value is not available: `""` is not a member
    of the enum, so it would fail the very validation F-14's "Done when"
    requires. The first declared member is the only choice that is both
    schema-valid and not a judgement about which value is likely.
    """
    schema = {"type": "object", "properties": {"status": {"enum": ["ok", "error"]}}, "required": ["status"]}

    assert synthesize_structured_content(schema) == {"status": "ok"}


def test_an_untyped_schema_yields_none_rather_than_a_guessed_shape():
    assert synthesize_structured_content({}) is None
    assert synthesize_structured_content(None) is None


def test_a_union_type_takes_the_first_named_type():
    assert synthesize_structured_content({"type": ["string", "null"]}) == ""


def test_synthesized_output_validates_against_its_own_schema():
    """F-14's literal "Done when" criterion: the synthesized response
    passes the tool's own declared schema validation.
    """
    schema = {
        "type": "object",
        "properties": {
            "content": {"type": "string"},
            "lines": {"type": "array", "items": {"type": "string"}},
            "status": {"enum": ["ok", "error"]},
            "count": {"type": "integer"},
        },
        "required": ["content", "lines", "status", "count"],
    }

    jsonschema.validate(instance=synthesize_structured_content(schema), schema=schema)
