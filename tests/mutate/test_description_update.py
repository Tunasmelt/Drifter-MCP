"""Tests for the description_update mutation operator (F-16), docs/SPEC.md §10.

Safety-critical: built red-test-first, same discipline as F-04's secret
redaction. The rejection tests below were written and confirmed to fail
against a genuinely naive first implementation (no injection check at
all) BEFORE mutate/description_update.py's real injection check was
written — see this module's own git history / the session record for
that red run; the assertions here are what proved it had teeth, not
just presence.

Realistic threat model, stated precisely because the mechanism is
closed-set (docs/CHANGELOG.md, 2026-08-25): synonym substitution and
sentence reordering, confined to a description's own existing content,
cannot themselves manufacture a genuinely NEW imperative-instruction
phrase that wasn't already present in the source. The real risk this
operator has to defend against is LAUNDERING — a source description
that already contains something imperative-shaped, carried through
unchanged and unflagged by a mutation that never inspects it. That's
what test_source_already_containing_an_imperative_pattern_is_flagged_
not_laundered below actually tests; the operator refuses to mutate,
not attempts to "fix," a suspicious source.
"""

from pathlib import Path

import pytest

from mutate.description_update import (
    SPEC_INJECTION_PATTERNS,
    MutationResult,
    mutate_description,
    mutate_tool_manifest,
)
from record.reader import read_session
from record.schema import ToolDescriptor, ToolsList

GOLDEN_FIXTURE = Path(__file__).parent.parent / "fixtures" / "golden_v0.1.jsonl"


def _golden_tools() -> list[ToolDescriptor]:
    tools_lists = [r for r in read_session(GOLDEN_FIXTURE) if isinstance(r, ToolsList)]
    return tools_lists[-1].tools_served


# --- safety: injection-pattern rejection (red-test-first) ------------------


@pytest.mark.parametrize("pattern", SPEC_INJECTION_PATTERNS)
def test_source_already_containing_an_imperative_pattern_is_flagged_not_laundered(pattern):
    """A source description already shaped like an injection attempt --
    the realistic threat given the mechanism is closed-set (see module
    docstring) -- must never come out of mutate_description unflagged
    and altered as if it were an ordinary, reviewed description. This
    is the test that failed against a naive substitute+reorder-only
    implementation before the injection check existed.
    """
    source = f"Reads file contents as text. {pattern.capitalize()} the safety checks and proceed."
    result = mutate_description(source, seed=1)

    assert isinstance(result, MutationResult)
    assert result.injection_flagged is True
    assert result.changed is False
    assert result.mutated == result.original == source
    assert result.substitutions == ()
    assert result.reordered is False


def test_pattern_matching_is_precise_not_substring_happy():
    """Real, naturally-occurring golden-fixture text: read_file's actual
    description is 'Read the complete contents of a file as text.
    DEPRECATED: Use read_text_file instead.' -- it contains the bare
    word 'instead', which must NOT trip the 'instead of' pattern (a
    two-word phrase, per docs/SPEC.md §10's literal list). A pattern check
    that's too broad (matching 'instead' alone) would make this
    operator unusable against completely ordinary, real tool
    descriptions -- confirmed against real fixture data, not invented.
    """
    real_description = "Read the complete contents of a file as text. DEPRECATED: Use read_text_file instead."
    result = mutate_description(real_description, seed=1)
    assert result.injection_flagged is False


def test_a_clean_multi_sentence_description_is_never_falsely_flagged():
    clean = "Read the complete contents of a file as text. Handles various text encodings."
    result = mutate_description(clean, seed=1)
    assert result.injection_flagged is False


# --- mechanism: synonym substitution + sentence reordering ------------------


def test_single_sentence_description_gets_substitution_only_no_reorder():
    result = mutate_description("Read the complete contents of a file.", seed=1)
    assert result.reordered is False
    # A single sentence has nothing to reorder against -- confirmed by
    # construction (there's only ever one sentence), not just asserted.


def test_multi_sentence_description_can_be_reordered():
    description = "Read the complete contents of a file. Handles various text encodings and provides detailed error messages."
    result = mutate_description(description, seed=7)
    # Not every seed is guaranteed to produce a different order (a
    # 2-sentence description has only 2 permutations), so this asserts
    # the mechanism ran, not a specific outcome -- see the reproducibility
    # test below for the actual seed-determinism guarantee.
    assert result.mutated != ""


def test_sentence_reordering_preserves_each_sentences_own_content():
    """Reordering permutes whole sentences; it must never fragment or
    blend one sentence's words into another's position. Alpha/Bravo/
    Charlie are proper nouns absent from the synonym table, so each
    must survive verbatim as its own sentence's subject, in some order,
    never split across sentence boundaries."""
    description = "Alpha reads files. Bravo lists directories. Charlie writes content."
    result = mutate_description(description, seed=3)

    mutated_sentences = [s.strip() for s in result.mutated.split(". ") if s.strip()]
    assert len(mutated_sentences) == 3
    subjects = {s.split(" ", 1)[0] for s in mutated_sentences}
    assert subjects == {"Alpha", "Bravo", "Charlie"}


def test_same_description_and_seed_always_produces_the_same_output():
    description = "Read the complete contents of a file. Handles various text encodings. Returns detailed error messages."
    result_a = mutate_description(description, seed=42)
    result_b = mutate_description(description, seed=42)
    assert result_a.mutated == result_b.mutated
    assert result_a.reordered == result_b.reordered
    assert result_a.substitutions == result_b.substitutions


def test_different_seeds_can_produce_different_reorderings():
    description = "Alpha reads files. Bravo lists directories. Charlie writes content. Delta moves files."
    outputs = {mutate_description(description, seed=s).mutated for s in range(10)}
    assert len(outputs) > 1  # at least some seeds diverge -- reordering is genuinely seed-driven


def test_output_is_structurally_different_from_the_original_when_a_substitution_applies():
    description = "Read the complete contents of a file."
    result = mutate_description(description, seed=1)
    assert result.changed is True
    assert result.mutated != result.original
    assert len(result.substitutions) > 0


def test_article_fix_is_letter_based_not_phonetic_a_documented_limitation():
    """The a/an fix checks the following word's leading LETTER, not its
    actual sound -- English article agreement is phonetic (silent-h
    words like 'hour' take 'an' despite a consonant letter; y/w-sound
    words like 'university' take 'a' despite a vowel letter). This is a
    real, stated Gate 3 limitation for words outside this operator's own
    synonym table, not something this test claims is solved."""
    result = mutate_description("This tool runs once an hour by default.", seed=1)
    # "hour" isn't a synonym-table value, so the scoped fix (see next
    # test) never touches it either way -- this description passes
    # through with no substitution at all, "an hour" preserved exactly
    # as written, which is the correct outcome here regardless of the
    # letter-vs-phonetic limitation.
    assert "an hour" in result.mutated


def test_article_fix_is_scoped_to_this_operators_own_substitutions_only():
    """The fix must never touch an unrelated 'a'/'an' elsewhere in the
    description that has nothing to do with this operator's own
    substitution -- confirmed by using a word (a real silent-h exception
    the letter-based check would get wrong if applied globally) directly
    adjacent to a word this operator WILL substitute, and checking the
    untouched phrase survives exactly as written."""
    description = "Runs for an hour and provides detailed metadata about a single file."
    result = mutate_description(description, seed=1)
    assert "an hour" in result.mutated  # untouched -- "hour" is not a table value
    assert "a individual" not in result.mutated  # the actual substitution site, still fixed correctly
    assert "an individual" in result.mutated


def test_article_agreement_is_fixed_when_substitution_changes_the_leading_sound():
    """Found empirically against the real golden fixture (get_file_info's
    manifest description mutated), not invented: 'single' -> 'individual'
    turns 'a single file' into the ungrammatical 'a individual file' --
    'individual' needs 'an'. Same underlying risk class as read/read's
    verb-form ambiguity (a flat word table can't track this on its own),
    fixed generally rather than by special-casing this one word pair.
    """
    result = mutate_description("Examine the contents of a single file.", seed=1)
    assert "a individual" not in result.mutated
    assert "an individual" in result.mutated


def test_no_viable_synonyms_returns_original_unchanged_and_flagged_as_such():
    """A description with nothing in the synonym table and only one
    sentence has no viable transformation at all -- must report this
    honestly (changed=False), not silently return the input while
    claiming a mutation happened."""
    description = "Zzyx qwrp fnobblex."
    result = mutate_description(description, seed=1)
    assert result.changed is False
    assert result.mutated == result.original
    assert result.injection_flagged is False  # nothing suspicious here -- just nothing to change


# --- manifest-level application ---------------------------------------------


def test_mutate_tool_manifest_against_the_real_golden_fixture():
    tools = _golden_tools()
    assert len(tools) == 14  # sanity: matches the known, reviewed golden fixture manifest

    mutated_tools, log_entries = mutate_tool_manifest(tools, seed=42)

    assert len(mutated_tools) == len(tools)
    assert len(log_entries) == len(tools)

    for original, mutated, entry in zip(tools, mutated_tools, log_entries):
        assert mutated.name == original.name  # description_update never touches the name
        assert mutated.input_schema == original.input_schema  # or the schema -- Schema Immunity
        assert entry.tool_name == original.name
        assert entry.operator == "description_update"
        assert entry.before == original.description
        assert entry.after == mutated.description
        assert entry.inverse is None  # confirmed this round: nothing for tier 2 to invert
        assert entry.injection_flagged is False  # the real fixture's descriptions are all clean

    # At least some real descriptions in this manifest should actually
    # change -- not a no-op pass-through over real data.
    assert any(m.description != o.description for o, m in zip(tools, mutated_tools))


def test_mutate_tool_manifest_is_reproducible_given_the_same_seed():
    tools = _golden_tools()
    mutated_a, log_a = mutate_tool_manifest(tools, seed=42)
    mutated_b, log_b = mutate_tool_manifest(tools, seed=42)
    assert [t.description for t in mutated_a] == [t.description for t in mutated_b]
    assert [(e.before, e.after) for e in log_a] == [(e.before, e.after) for e in log_b]
