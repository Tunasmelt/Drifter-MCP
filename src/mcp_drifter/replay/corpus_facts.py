"""R0: FUTURE-ARGUMENT HINTS derived from a corpus, per call.

These are hints with SYNTHETIC provenance, not witnessed response content.
The distinction is the correction an external reviewer forced, and it
matters: this module knows only that a value was used LATER in the same
session, which is temporal ordering, not causation. The value may have come
from the prompt, from the agent's prior knowledge, from a different
response, or from the agent inventing it. Presenting these as "what the
response showed" would be a stronger claim than the data supports.


docs/SPEC.md §15 limitation 17 recorded the failure this exists to answer.
Recorded against a live server an agent did:

    list_directory {.../project}        -> the listing showed `data/`
    list_directory {.../project/data}   -> the listing showed readings.csv
    read_text_file {.../project/data/readings.csv}

Replayed, the first `list_directory` was an exact HIT. Lookup was never the
problem. Its CONTENT came back empty (recording is shape-only, and
limitation 11 then required synthesized content be genuinely empty rather
than descriptive), so the agent never learned `data/` existed, guessed one
directory up, and missed. A corpus measured at 100% projected coverage
produced 0/4 valid runs.

**The observation this module rests on: the corpus holds values the agent
used later — as the ARGUMENTS of its own subsequent calls.** If a session
shows `list_directory {P}` and then a call carrying `P/data`, then `P/data`
was AVAILABLE to the agent at that point. Whether this listing is what
surfaced it is an inference the corpus cannot settle. Nothing new needs
recording and no payload needs retaining, which is why this is the cheapest
possible test of R0's hypothesis: it does not touch docs/SPEC.md §3's
secrets invariant at all.

**What it does and does not guarantee.** Every value returned appears
verbatim as an argument in some recorded call, so nothing is invented. That
is weaker than faithful attribution: a hint can be attached to the wrong
response, and avoiding invented strings does not make the attribution
correct. Values that are multi-line or oversized are excluded because they
are prose rather than identifiers — `write_file.content` qualified under the
first version, which made the original "never emits prose" claim false, and
prose is exactly what a live agent flagged as prompt injection (limitation
11). The length cap is a heuristic, not a proof: single-line prose under it
still passes, so provenance labelling is the real mitigation.

**Deliberately general, not filesystem-shaped.** The rule is "argument
values that appeared later in the same session", not "paths". A search tool
returning ids, a database tool returning keys and a filesystem tool
returning paths are one problem, and encoding path semantics here would
solve exactly one server.

**What it does not do.** It offers values, not a faithful response body. It
cannot reproduce ordering, counts, contents, or anything the agent inferred
from a payload rather than from an identifier. It is a hypothesis test for
R0, not a claim that replay is now faithful.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from mcp_drifter.record.reader import read_session
from mcp_drifter.record.schema import ToolCall
from mcp_drifter.replay.replay_store import replay_key


@dataclass(frozen=True)
class CorpusFacts:
    """Per-call, the values the corpus witnessed being used afterwards.

    Keyed by `replay_key` — the same exact key `ReplayStore` indexes by, so
    a call that resolves to a recording resolves to its facts identically,
    with no second notion of identity to drift out of step.
    """

    by_key: dict[str, tuple[str, ...]] = field(default_factory=dict)


# A hint longer than this is not an identifier. A cap is a HEURISTIC, not a
# proof: single-line prose under the cap still passes, which is why
# provenance labelling rather than filtering is the real mitigation. Named
# so a reader can disagree with the number instead of reverse-engineering it.
MAX_HINT_LENGTH = 512


def _string_values(arguments: dict) -> list[str]:
    """Argument values usable as discoverable identifiers.

    Strings only, and deliberately not recursed into nested containers for
    now: a nested value's role is much harder to justify offering back, and
    R0 is a hypothesis test that should fail honestly rather than succeed
    by breadth. Numbers and booleans are excluded because they are not
    identifiers — handing back `head: 6` as navigational content would be
    noise at best and a misleading claim at worst.
    """
    return [
        v
        for v in arguments.values()
        # Multi-line and oversized values are excluded because they are
        # prose, not identifiers. `write_file.content` was the reviewer's
        # reproduction: it qualified as a "string argument" and would have
        # been emitted verbatim into a replayed response, which is exactly
        # the instruction-shaped text limitation 11 established a real agent
        # treats as a prompt-injection attempt. The earlier claim that this
        # module "never emits prose" was false for that case.
        if isinstance(v, str) and v and "\n" not in v and len(v) <= MAX_HINT_LENGTH
    ]


def build_corpus_facts(session_paths: Sequence[Path], server: str) -> CorpusFacts:
    """Indexes, for every recorded call, the string argument values that
    appear in LATER calls of the same session.

    "Later in the same session" is the whole warrant. A value used before a
    call was not revealed by it, and a value from a different session is
    still evidence (sessions pool, per DEC-027(b)'s corpus premise) but only
    ever attributed to the call it actually followed.

    Restricted to `server`: a replay key includes the server name, and
    pooling across servers would hand an agent identifiers from a system it
    is not talking to.
    """
    accumulated: dict[str, set[str]] = {}

    for path in session_paths:
        calls = [r for r in read_session(path) if isinstance(r, ToolCall) and r.server == server]
        # Walk backwards, carrying the set of values seen so far. Each call
        # is credited with everything that came after it, in one pass.
        # Everything the agent had used at or before position i. A value in
        # here was demonstrably already available, so a later call cannot be
        # what surfaced it -- the reviewer's reproduction was a file read
        # BEFORE a listing and written again AFTER it, which the previous
        # version re-offered as newly discovered because it only subtracted
        # the current call's own arguments.
        prior: list[set[str]] = []
        running: set[str] = set()
        for call in calls:
            running = running | set(_string_values(call.arguments))
            prior.append(running)

        seen_later: set[str] = set()
        for i in range(len(calls) - 1, -1, -1):
            call = calls[i]
            key = replay_key(server, call.tool_name, call.arguments)
            already_known = prior[i]
            accumulated.setdefault(key, set()).update(seen_later - already_known)
            seen_later |= set(_string_values(call.arguments))

    return CorpusFacts(by_key={key: tuple(sorted(values)) for key, values in accumulated.items()})


def successor_values(facts: CorpusFacts, server: str, tool_name: str, arguments: dict) -> tuple[str, ...]:
    """Hint values the corpus shows being used after this exact call.

    Keyed on the INCOMING arguments, so a call resolved via the inverse or
    semantic tier gets no hints even though its source recording has them --
    a known gap, not a design choice, and one that makes this useless for
    exactly the mutated arm `parameter_rename` produces.

    Sorted, so a replayed response is byte-identical across runs — an
    agent's behaviour must not vary for reasons unrelated to the mutation
    under test. Returns `()` for a call the corpus has never seen, rather
    than guessing from a similar one: R0's hypothesis is about restoring
    WITNESSED navigation, and a near-match would be exactly the fabrication
    DEC-027 rejected.
    """
    return facts.by_key.get(replay_key(server, tool_name, arguments), ())
