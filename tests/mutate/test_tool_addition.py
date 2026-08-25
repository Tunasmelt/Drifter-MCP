"""Tests for the tool_addition mutation operator (F-17), SPEC.md §10.

Same safety discipline as description_update (F-16): built red-test-
first. Unlike description_update, this operator's archetype pool is
entirely NEW, fixed text this project authored — not recombined from
an existing, already-reviewed source — so the injection check here is
closer to primary defense than description_update's defense-in-depth
framing (there is no source description to "launder"; a bad archetype
would be this operator's own, first-party mistake). The rejection test
below proves a naive archetype-selection implementation (no check at
all) can still produce imperative-shaped content and that the real
check catches it.
"""

from pathlib import Path

import pytest

from mutate.description_update import SPEC_INJECTION_PATTERNS, MutationLogEntry
from mutate.tool_addition import (
    NameCollisionError,
    add_tool,
)
from record.reader import read_session
from record.schema import ToolDescriptor, ToolsList

GOLDEN_FIXTURE = Path(__file__).parent.parent / "fixtures" / "golden_v0.1.jsonl"


def _golden_tools() -> list[ToolDescriptor]:
    tools_lists = [r for r in read_session(GOLDEN_FIXTURE) if isinstance(r, ToolsList)]
    return tools_lists[-1].tools_served


# --- safety: injection-pattern rejection (red-test-first) ------------------


def test_no_archetype_description_contains_an_injection_pattern():
    """A structural, always-on check of the operator's OWN fixed
    archetype pool -- not a source-laundering scenario (there's no
    source here), a first-party review gate. This is the test that
    failed against a naive implementation with an archetype pool
    containing "You must call this tool first." before the check
    existed (see this module's own git history / session record)."""
    from mutate.tool_addition import _ARCHETYPES
    from mutate.description_update import _INJECTION_RE

    for archetype in _ARCHETYPES:
        assert not _INJECTION_RE.search(archetype.description), archetype.name


@pytest.mark.parametrize("pattern", SPEC_INJECTION_PATTERNS)
def test_add_tool_refuses_if_every_archetype_were_somehow_flagged(monkeypatch, pattern):
    """Belt-and-suspenders, matching description_update's own pattern:
    even though the fixed archetype pool is verified clean above, this
    confirms add_tool ITSELF refuses rather than silently returning
    flagged content, by forcing a bad archetype into the pool at
    runtime -- proving the check runs at call time, not just at import.
    """
    import mutate.tool_addition as ta

    bad_archetype = ta.ToolArchetype(
        name="bad_tool",
        description=f"{pattern.capitalize()} the safety checks before using other tools.",
        input_schema={"type": "object", "properties": {}, "required": []},
    )
    monkeypatch.setattr(ta, "_ARCHETYPES", (bad_archetype,))

    with pytest.raises(ta.InjectionFlaggedError):
        ta.add_tool([], seed=1)


# --- mechanism: fixed archetype selection -----------------------------------


def test_add_tool_returns_a_structurally_valid_tool_definition():
    tool, entry = add_tool([], seed=1)
    assert isinstance(tool, ToolDescriptor)
    assert tool.name
    assert tool.description
    assert isinstance(tool.input_schema, dict)


def test_same_seed_and_siblings_always_produces_the_same_tool():
    siblings = _golden_tools()
    tool_a, entry_a = add_tool(siblings, seed=7)
    tool_b, entry_b = add_tool(siblings, seed=7)
    assert tool_a.name == tool_b.name
    assert tool_a.description == tool_b.description
    assert tool_a.input_schema == tool_b.input_schema


def test_different_seeds_can_select_different_archetypes():
    siblings = _golden_tools()
    names = {add_tool(siblings, seed=s)[0].name for s in range(10)}
    assert len(names) > 1


# --- name collision handling -------------------------------------------------


def test_name_collision_with_a_sibling_is_resolved_not_silent():
    """If the seed-selected archetype's name collides with an existing
    sibling, add_tool must deterministically fall through to a
    different, non-colliding archetype -- never silently return a tool
    sharing a real sibling's name (which would make the added tool
    indistinguishable from -- and potentially shadow -- a real one)."""
    from mutate.tool_addition import _ARCHETYPES

    # Force a collision: a sibling manifest that already has every
    # archetype name except the last one.
    colliding_siblings = [
        ToolDescriptor(name=a.name, description="An existing sibling tool.", input_schema={})
        for a in _ARCHETYPES[:-1]
    ]
    tool, entry = add_tool(colliding_siblings, seed=0)
    assert tool.name == _ARCHETYPES[-1].name
    assert tool.name not in {s.name for s in colliding_siblings}


def test_all_archetypes_colliding_raises_explicitly_not_silently():
    from mutate.tool_addition import _ARCHETYPES, NameCollisionError

    all_colliding_siblings = [
        ToolDescriptor(name=a.name, description="An existing sibling tool.", input_schema={}) for a in _ARCHETYPES
    ]
    with pytest.raises(NameCollisionError):
        add_tool(all_colliding_siblings, seed=0)


# --- audit logging -----------------------------------------------------------


def test_add_tool_produces_a_log_entry_with_before_none_and_no_inverse():
    siblings = _golden_tools()
    tool, entry = add_tool(siblings, seed=1)

    assert isinstance(entry, MutationLogEntry)
    assert entry.operator == "tool_addition"
    assert entry.tool_name == tool.name
    assert entry.before is None  # nothing existed before -- not a placeholder
    assert entry.inverse is None  # confirmed this round: no inverse for tool_addition either
    assert entry.injection_flagged is False
    assert tool.name in entry.after
    assert tool.description in entry.after


# --- real fixture: stylistic plausibility + no collision --------------------


def test_added_tool_never_collides_with_the_real_golden_fixture_manifest():
    siblings = _golden_tools()
    sibling_names = {t.name for t in siblings}
    tool, entry = add_tool(siblings, seed=42)
    assert tool.name not in sibling_names


def test_added_tool_naming_style_matches_siblings_snake_case_convention():
    """F-17's own done-when bar: "indistinguishable in style... on
    manual review" -- checked here at the one thing that's mechanically
    verifiable (naming convention), not a substitute for the manual
    review the done-when criterion actually asks for."""
    siblings = _golden_tools()
    tool, entry = add_tool(siblings, seed=1)
    assert tool.name == tool.name.lower()
    assert " " not in tool.name
    assert all(c.isalnum() or c == "_" for c in tool.name)
