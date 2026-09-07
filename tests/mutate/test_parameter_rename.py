"""Tests for the parameter_rename mutation operator (F-40), docs/SPEC.md
§7/§10 — the third Level 0/1 operator, built alongside F-12 (inverse-
mutation key resolution) specifically to give it a real inverse to
resolve against.
"""

from record.schema import ToolDescriptor
from mutate.parameter_rename import (
    camel_to_snake,
    inverse_map_from_log,
    rename_tool_parameters,
)


def _tool(name: str, properties: dict, required: list[str] | None = None) -> ToolDescriptor:
    schema = {"type": "object", "properties": properties}
    if required is not None:
        schema["required"] = required
    return ToolDescriptor(name=name, description="d", input_schema=schema)


# --- the core rename transformation ------------------------------------


def test_a_snake_case_property_is_renamed_to_camel_case():
    tool = _tool("get_customer", {"customer_id": {"type": "string"}})
    mutated, log = rename_tool_parameters([tool], seed=1)

    assert mutated[0].input_schema["properties"] == {"customerId": {"type": "string"}}
    assert log[0].before == "customer_id"
    assert log[0].after == "customerId"


def test_required_list_is_updated_in_lockstep_with_the_rename():
    tool = _tool("get_customer", {"customer_id": {"type": "string"}}, required=["customer_id"])
    mutated, _log = rename_tool_parameters([tool], seed=1)

    assert mutated[0].input_schema["required"] == ["customerId"]


def test_tool_name_and_description_and_other_properties_are_never_touched():
    tool = _tool(
        "get_customer",
        {"customer_id": {"type": "string"}, "verbose": {"type": "boolean"}},
    )
    mutated, _log = rename_tool_parameters([tool], seed=1)

    assert mutated[0].name == "get_customer"
    assert mutated[0].description == "d"
    # alphabetically "customer_id" < "verbose" -- only the first eligible one renamed
    assert mutated[0].input_schema["properties"]["verbose"] == {"type": "boolean"}


def test_only_one_property_is_renamed_even_when_several_are_eligible():
    tool = _tool("multi", {"a_b": {"type": "string"}, "c_d": {"type": "string"}})
    mutated, log = rename_tool_parameters([tool], seed=1)

    props = mutated[0].input_schema["properties"]
    assert "aB" in props and "a_b" not in props
    assert "c_d" in props  # untouched -- alphabetically second, not chosen
    assert log[0].before == "a_b"


def test_a_tool_with_no_properties_is_left_untouched():
    tool = _tool("get_status", {})
    mutated, log = rename_tool_parameters([tool], seed=1)

    assert mutated[0] == tool
    assert log[0].before is None
    assert log[0].after == "(no eligible parameter)"
    assert log[0].inverse is None


def test_a_tool_whose_only_property_is_already_single_word_is_left_untouched():
    tool = _tool("search", {"query": {"type": "string"}})
    mutated, log = rename_tool_parameters([tool], seed=1)

    assert mutated[0].input_schema["properties"] == {"query": {"type": "string"}}
    assert log[0].inverse is None


def test_a_camel_case_collision_with_an_existing_sibling_property_is_skipped():
    """"a_b" would naively rename to "aB", but "aB" already exists as a
    sibling property -- renaming would silently merge two distinct
    parameters into one. Must skip to the next eligible candidate, not
    produce a colliding schema."""
    tool = _tool("collide", {"a_b": {"type": "string"}, "aB": {"type": "integer"}, "c_d": {"type": "string"}})
    mutated, log = rename_tool_parameters([tool], seed=1)

    props = mutated[0].input_schema["properties"]
    assert props["aB"] == {"type": "integer"}  # untouched, not overwritten
    assert "c_d" not in props
    assert "cD" in props
    assert log[0].before == "c_d"


def test_rename_is_deterministic_given_the_same_input_independent_of_seed():
    """Unlike description_update/tool_addition, seed selects nothing here
    -- there's only one deterministic candidate once eligibility is
    decided. Confirmed directly rather than just asserted in the
    docstring."""
    tool = _tool("get_customer", {"customer_id": {"type": "string"}})
    mutated_a, _ = rename_tool_parameters([tool], seed=1)
    mutated_b, _ = rename_tool_parameters([tool], seed=999)

    assert mutated_a[0].input_schema == mutated_b[0].input_schema


# --- the inverse mapping, F-12's actual consumer ------------------------


def test_inverse_is_the_new_to_old_mapping_for_a_renamed_property():
    tool = _tool("get_customer", {"customer_id": {"type": "string"}})
    _mutated, log = rename_tool_parameters([tool], seed=1)

    assert log[0].inverse == {"customerId": "customer_id"}


def test_inverse_map_from_log_extracts_per_tool_mappings():
    tools = [
        _tool("get_customer", {"customer_id": {"type": "string"}}),
        _tool("search", {"query": {"type": "string"}}),  # nothing eligible
    ]
    _mutated, log = rename_tool_parameters(tools, seed=1)

    assert inverse_map_from_log(log) == {"get_customer": {"customerId": "customer_id"}}


def test_inverse_map_from_log_is_generic_across_operators_with_no_inverse():
    """description_update/tool_addition's own MutationLogEntry.inverse is
    always None -- inverse_map_from_log must not choke on or misreport
    those, since cli/run.py calls it unconditionally regardless of which
    operator produced the log."""
    from mutate.description_update import MutationLogEntry

    log = [
        MutationLogEntry(
            tool_name="t", operator="description_update", before="a", after="b",
            inverse=None, seed=1, injection_flagged=False,
        )
    ]
    assert inverse_map_from_log(log) == {}


def test_camel_to_snake_round_trips_the_simple_case():
    assert camel_to_snake("customerId") == "customer_id"
