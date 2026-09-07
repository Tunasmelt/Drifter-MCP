"""Tool risk classification (F-26), docs/SPEC.md §10.

Four-tier resolution over a `ToolDescriptor`, first-confident-tier-wins:

1. **User policy override** (`drifter.yaml`'s `policy.destructive`) — wins
   unconditionally when a tool name is listed, regardless of what the other
   three tiers would say. Checked FIRST, not last, despite docs/SPEC.md §10's
   own prose listing it after the three automated tiers — that ordering
   names the automated FALLBACK CASCADE, not override's priority; see this
   module's own `classify_tool` docstring and docs/CHANGELOG.md for the
   full reasoning. An override that could be outranked by a heuristic
   guess wouldn't be one.
2. **MCP annotations** (`ToolDescriptor.annotations`, the real wire
   `tools/list` `annotations` block) — untrusted hints per the MCP spec
   itself ("Clients should never make tool use decisions based on
   ToolAnnotations received from untrusted servers"), used here only as
   the FIRST, weakest-trust automated signal, never alone for anything
   destructive-adjacent without note. Only EXPLICIT `True`/`False` hint
   values are used as signal — an omitted hint is never assumed to carry
   the MCP SDK's own client-side default value on the server's behalf
   (that default exists for a different purpose: telling a *client* how
   to behave when a hint is missing, not telling a *safety classifier*
   what a server that didn't send a hint at all intended).
3. **Name heuristics** — a small, fixed, reviewable prefix table (same
   "closed-set, reviewable as data" spirit as `mutate/description_update.py`'s
   synonym table), not a general NLP guess. Real, documented limitation:
   this is a heuristic in `docs/SPEC.md` §9's calibration-register sense — an
   engineering default calibrated against common MCP naming conventions
   (`get_`/`list_`/`delete_`/etc.), not a validated boundary against a real
   tool-name corpus. A tool whose name doesn't match any known prefix falls
   through to `"unknown"`.
4. **Observed behavior** — docs/SPEC.md §10 names this as a real tier, but no
   well-founded signal exists yet in what Drifter actually records
   (`result_shape`/`is_error`/`fault`) to infer WRITE vs READ-ONLY effect
   from — shape alone cannot reveal semantic effect, and inventing an
   unfounded heuristic here would violate this project's own "verified, not
   assumed" discipline for exactly the reason `record/redact.py`'s own
   entropy heuristic is careful to flag as tunable, not derived. Left as a
   real, documented, narrower-than-spec gap (matching this project's
   established precedent for this class of decision — F-14, F-19, etc.),
   not silently skipped: `classify_tool` calls into this tier explicitly
   and it always declines, rather than the tier not existing in code at all.

`"unknown"` — the taxonomy's own safe default (docs/SPEC.md §10: "unsafe by
default, never mutated, never live-invoked") — is what every tool gets when
no tier above produces a confident answer, never silently upgraded to
something more permissive.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from record.schema import ClassificationSource, RiskLevel, ToolDescriptor

# Tier 3 (name heuristics): checked in this order — most severe first — so
# a name matching more than one table (shouldn't happen with a reviewed,
# disjoint prefix set, but checked defensively) resolves to the more
# cautious classification, not the first table that happens to be iterated.
# `str.startswith`, not a substring/regex search: description_update.py's
# own "instead" vs "instead of" precedent is the reason this project checks
# prefixes precisely rather than loosely, here too.
_DESTRUCTIVE_PREFIXES = ("delete_", "remove_", "drop_", "purge_", "destroy_", "wipe_", "truncate_", "terminate_", "kill_")
_IRREVERSIBLE_WRITE_PREFIXES = ("send_", "post_", "publish_", "notify_", "email_", "execute_", "run_", "invoke_", "submit_")
_REVERSIBLE_WRITE_PREFIXES = (
    "create_", "add_", "update_", "set_", "insert_", "write_", "put_", "patch_", "edit_", "append_", "rename_"
)
_READ_ONLY_PREFIXES = (
    "get_", "list_", "search_", "find_", "read_", "fetch_", "describe_", "show_", "query_", "check_", "count_"
)


@dataclass(frozen=True)
class Classification:
    """One tool's resolved risk level, and why — `source` is always
    recorded (docs/SPEC.md §10: "classification_source recorded per
    tool"), never left implicit, so a caller can tell a confident
    annotation-derived answer apart from an unresolved `"unknown"` guess."""

    risk: RiskLevel
    source: ClassificationSource


def _classify_from_annotations(tool: ToolDescriptor) -> Classification | None:
    annotations = tool.annotations
    if not annotations:
        return None

    read_only = annotations.get("readOnlyHint")
    if read_only is True:
        open_world = annotations.get("openWorldHint")
        risk: RiskLevel = "read_only_external" if open_world is True else "read_only_local"
        return Classification(risk=risk, source="mcp_annotation")

    if read_only is False:
        destructive = annotations.get("destructiveHint")
        if destructive is True:
            return Classification(risk="destructive", source="mcp_annotation")
        if destructive is False:
            idempotent = annotations.get("idempotentHint")
            risk = "reversible_write" if idempotent is True else "irreversible_write"
            return Classification(risk=risk, source="mcp_annotation")
        # destructiveHint absent: read_only is explicitly False but nothing
        # says whether it's destructive -- genuinely ambiguous, not a case
        # to guess at using the MCP SDK's own client-facing default value.
        return None

    # readOnlyHint absent entirely: no usable signal from this tier.
    return None


def _classify_from_name_heuristic(tool: ToolDescriptor) -> Classification | None:
    name = tool.name.lower()
    if name.startswith(_DESTRUCTIVE_PREFIXES):
        return Classification(risk="destructive", source="heuristic")
    if name.startswith(_IRREVERSIBLE_WRITE_PREFIXES):
        return Classification(risk="irreversible_write", source="heuristic")
    if name.startswith(_REVERSIBLE_WRITE_PREFIXES):
        return Classification(risk="reversible_write", source="heuristic")
    if name.startswith(_READ_ONLY_PREFIXES):
        return Classification(risk="read_only_local", source="heuristic")
    return None


def _classify_from_observed_behavior(tool: ToolDescriptor) -> Classification | None:
    """Real, documented gap (see module docstring) — always declines. Not
    an oversight: no signal currently recorded (`result_shape`/`is_error`/
    `fault`) can honestly distinguish a write from a read-only call."""
    return None


def classify_tool(tool: ToolDescriptor, destructive_override: frozenset[str] = frozenset()) -> Classification:
    """Resolves one tool's risk classification.

    `destructive_override` is `drifter.yaml`'s `policy.destructive` list
    (as a set, for O(1) membership — callers classifying a whole manifest
    should build it once, not per tool; see `classify_manifest` below).
    Wins unconditionally over every automated tier when `tool.name` is a
    member — this is tier 4 checked FIRST, not last; see this module's own
    docstring for why that's the correct reading of docs/SPEC.md §10's
    prose despite its enumeration order.
    """
    if tool.name in destructive_override:
        return Classification(risk="destructive", source="user_override")

    for tier in (_classify_from_annotations, _classify_from_name_heuristic, _classify_from_observed_behavior):
        result = tier(tool)
        if result is not None:
            return result

    return Classification(risk="unknown", source="unresolved")


def classify_manifest(
    tools: list[ToolDescriptor], destructive_override: Sequence[str] = ()
) -> dict[str, Classification]:
    """Classifies every tool in a manifest, keyed by name. `drifter doctor`
    (F-26's own "Done when" bar) reads this to surface every `"unknown"`
    result for one-time user confirmation before any live-mode run.
    """
    override_set = frozenset(destructive_override)
    return {tool.name: classify_tool(tool, override_set) for tool in tools}
