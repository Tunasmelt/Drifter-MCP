"""Tests for policy/classify.py (F-26), docs/SPEC.md §10.

Real, planted MCP tool descriptors throughout -- name/annotations shapes
chosen to match what an actual `tools/list` response looks like, not
invented shorthand.
"""

from record.schema import ToolDescriptor
from policy.classify import (
    Classification,
    _classify_from_observed_behavior,
    classify_manifest,
    classify_tool,
)


def _tool(name: str, annotations: dict | None = None) -> ToolDescriptor:
    return ToolDescriptor(name=name, description="d", input_schema={}, annotations=annotations)


# --- tier 4: user policy override, wins unconditionally ---------------------


def test_user_override_wins_even_against_a_read_only_annotation():
    """The whole point of "override": a tool the SERVER claims is safe
    (readOnlyHint: true) must still classify as destructive when the
    USER has explicitly listed it -- the user is trusted, the server's
    own hint is not (docs/SPEC.md §10's own framing)."""
    tool = _tool("get_customer", annotations={"readOnlyHint": True})
    result = classify_tool(tool, destructive_override=frozenset({"get_customer"}))
    assert result == Classification(risk="destructive", source="user_override")


def test_a_tool_not_in_the_override_list_is_unaffected():
    tool = _tool("get_customer", annotations={"readOnlyHint": True})
    result = classify_tool(tool, destructive_override=frozenset({"some_other_tool"}))
    assert result.source != "user_override"


# --- tier 1: MCP annotations -------------------------------------------------


def test_read_only_and_open_world_classifies_as_read_only_external():
    tool = _tool("weather_lookup", annotations={"readOnlyHint": True, "openWorldHint": True})
    assert classify_tool(tool) == Classification(risk="read_only_external", source="mcp_annotation")


def test_read_only_without_open_world_classifies_as_read_only_local():
    tool = _tool("weather_lookup", annotations={"readOnlyHint": True})
    assert classify_tool(tool) == Classification(risk="read_only_local", source="mcp_annotation")


def test_not_read_only_and_destructive_classifies_as_destructive():
    tool = _tool("purge_cache", annotations={"readOnlyHint": False, "destructiveHint": True})
    assert classify_tool(tool) == Classification(risk="destructive", source="mcp_annotation")


def test_not_read_only_not_destructive_and_idempotent_is_reversible_write():
    tool = _tool("set_flag", annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True})
    assert classify_tool(tool) == Classification(risk="reversible_write", source="mcp_annotation")


def test_not_read_only_not_destructive_and_not_idempotent_is_irreversible_write():
    tool = _tool("append_log", annotations={"readOnlyHint": False, "destructiveHint": False})
    assert classify_tool(tool) == Classification(risk="irreversible_write", source="mcp_annotation")


def test_read_only_false_with_no_destructive_hint_falls_through_to_heuristic():
    """destructiveHint absent is genuinely ambiguous at tier 1 -- must NOT
    guess using the MCP SDK's own client-facing default (true) on the
    server's behalf; falls to tier 2, where this tool's name resolves it."""
    tool = _tool("delete_record", annotations={"readOnlyHint": False})
    result = classify_tool(tool)
    assert result.source == "heuristic"
    assert result.risk == "destructive"


def test_no_annotations_at_all_falls_through_to_heuristic():
    tool = _tool("get_customer", annotations=None)
    result = classify_tool(tool)
    assert result.source == "heuristic"
    assert result.risk == "read_only_local"


def test_empty_annotations_dict_falls_through_to_heuristic():
    tool = _tool("get_customer", annotations={})
    result = classify_tool(tool)
    assert result.source == "heuristic"


# --- tier 2: name heuristics --------------------------------------------------


def test_get_prefix_is_read_only():
    assert classify_tool(_tool("get_customer")).risk == "read_only_local"


def test_list_prefix_is_read_only():
    assert classify_tool(_tool("list_files")).risk == "read_only_local"


def test_create_prefix_is_reversible_write():
    assert classify_tool(_tool("create_invoice")).risk == "reversible_write"


def test_send_prefix_is_irreversible_write():
    assert classify_tool(_tool("send_email")).risk == "irreversible_write"


def test_delete_prefix_is_destructive():
    assert classify_tool(_tool("delete_customer")).risk == "destructive"


def test_heuristic_checks_most_severe_prefix_first():
    """A name matching more than one table (shouldn't happen with a
    reviewed, disjoint prefix set, but checked defensively per this
    module's own docstring) must resolve to the more cautious result --
    confirmed here by construction, not just claimed."""
    from policy.classify import _DESTRUCTIVE_PREFIXES, _READ_ONLY_PREFIXES

    assert not any(d.startswith(r) or r.startswith(d) for d in _DESTRUCTIVE_PREFIXES for r in _READ_ONLY_PREFIXES)


def test_prefix_match_is_precise_not_substring_happy():
    """"undelete_x" must NOT match the "delete_" prefix (it doesn't START
    with it) -- str.startswith, not a loose substring search, matching
    description_update.py's own "instead" vs "instead of" precedent."""
    result = classify_tool(_tool("undelete_customer"))
    assert result.risk != "destructive"


def test_an_unrecognized_name_with_no_annotations_is_unknown_and_unresolved():
    result = classify_tool(_tool("frobnicate_widget"))
    assert result == Classification(risk="unknown", source="unresolved")


# --- tier 3: observed behavior (documented stub) -----------------------------


def test_observed_behavior_tier_always_declines():
    """Locks in the deliberate, documented gap: no signal currently
    recorded can honestly distinguish a write from a read-only call, so
    this tier must never produce a confident answer -- if it ever does,
    that's a real behavior change that needs its own review, not a
    silent one."""
    tool = _tool("mystery_tool")
    assert _classify_from_observed_behavior(tool) is None


# --- classify_manifest --------------------------------------------------------


def test_classify_manifest_keys_by_tool_name():
    tools = [_tool("get_customer"), _tool("delete_customer")]
    result = classify_manifest(tools)
    assert result["get_customer"].risk == "read_only_local"
    assert result["delete_customer"].risk == "destructive"


def test_classify_manifest_applies_the_override_list_across_the_whole_manifest():
    tools = [_tool("get_customer"), _tool("list_files")]
    result = classify_manifest(tools, destructive_override=["get_customer"])
    assert result["get_customer"].source == "user_override"
    assert result["list_files"].source == "heuristic"
