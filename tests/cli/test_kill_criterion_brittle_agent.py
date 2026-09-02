"""Gate 3's kill criterion (docs/PHASES.md), the actual final piece: "before
proceeding to Gate 4 or v1, deliberately construct a known-brittle test
agent and confirm the harness *can* detect a planted regression."

Deliberately NOT the real dogfood pairing — that's the whole point
(docs/PHASES.md's own text: "isolating whether the issue is the harness or
simply that this particular agent is unusually robust"). This uses
`tests/fixtures/scripted_agent.py`'s `SELECT:<substring>` mode: a
description-text-dependent tool-selection mechanism, planted to break
under `description_update`'s synonym substitution, run through the
real `cli.run.run_mutation_comparison` orchestration (not an isolated
unit check of any one piece) — the same path a real run would take,
with a synthetic agent standing in for "the thing under test" instead
of Claude Code.

The planted break is verified empirically before being relied on, not
assumed: `list_directory`'s real golden-fixture description contains
the literal substring "detailed listing"; `description_update` at
seed=42 substitutes "detailed"→"thorough" (among other changes),
confirmed directly (see test_the_planted_substring_is_verified_broken_
by_the_real_mutation below) to remove that substring from the mutated
description. The brittle agent's SELECT mode finds `list_directory` in
the baseline arm (unmutated) and finds nothing at all in the mutated
arm — a real, agent-observable behavior break caused by nothing but
the mutation, with zero live-agent involvement and zero cost.
"""

import json
import sys
from pathlib import Path

from cli.run import run_mutation_comparison
from mutate.description_update import mutate_tool_manifest
from replay.replay_proxy import tools_served_from_session

GOLDEN_FIXTURE = Path(__file__).parent.parent / "fixtures" / "golden_v0.1.jsonl"
SCRIPTED_AGENT = Path(__file__).parent.parent / "fixtures" / "scripted_agent.py"
GOLDEN_SERVER = "filesystem"
BRITTLE_SEED = 42
PLANTED_SUBSTRING = "detailed listing"
REAL_LIST_DIRECTORY_ARGS = {
    "path": "C:\\Users\\user\\AppData\\Local\\Temp\\claude\\c--Users-user-Desktop-Drifter-MCP\\4b0618ee-e102-49e3-a472-c0b4a62fd678\\scratchpad\\golden_fixture_content"
}


def test_the_planted_substring_is_verified_broken_by_the_real_mutation():
    """Confirms the premise this whole test file depends on, rather than
    assuming it: the chosen substring is present pre-mutation and absent
    post-mutation, at the exact seed used below. If a future synonym-
    table edit changes this, this test fails loudly and explains why,
    rather than the kill-criterion test below silently passing for the
    wrong reason (e.g. the agent finding nothing because of an unrelated
    bug, not because the mutation broke the substring match)."""
    original_tools = tools_served_from_session(GOLDEN_FIXTURE)
    original = next(t for t in original_tools if t.name == "list_directory")
    assert PLANTED_SUBSTRING in original.description

    mutated_tools, _log = mutate_tool_manifest(original_tools, seed=BRITTLE_SEED)
    mutated = next(t for t in mutated_tools if t.name == "list_directory")
    assert PLANTED_SUBSTRING not in mutated.description


def test_harness_reports_regression_against_a_known_planted_break(tmp_path):
    """The actual kill-criterion confirmation: run the real
    cli.run.run_mutation_comparison orchestration -- the same path a
    real drifter run would take -- with the brittle agent as the
    spawned process. Baseline arm: SELECT finds list_directory (its
    description still contains the substring) and makes a real,
    exact-tier-matching call. Mutated arm (description_update, same
    seed the premise test verified breaks the substring): SELECT finds
    nothing, calls nothing, producing an empty tool-call path -- a
    genuine, deterministic behavior deviation caused only by the
    mutation. This must report REGRESSION, not UNKNOWN and not
    NO_REGRESSION.
    """
    command = [
        sys.executable,
        str(SCRIPTED_AGENT),
        f"SELECT:{PLANTED_SUBSTRING}|{json.dumps(REAL_LIST_DIRECTORY_ARGS)}",
    ]

    result = run_mutation_comparison(
        task_id="kill_criterion_brittle_agent",
        prompt="",
        fixture_path=GOLDEN_FIXTURE,
        server_name=GOLDEN_SERVER,
        agent_command=command,
        operator="description_update",
        session_dir=tmp_path / "runs",
        raw_dir=tmp_path / "raw",
        seed=BRITTLE_SEED,
        repeats=1,  # the scripted agent is fully deterministic -- no natural variation to average over
        timeout_s=30.0,
    )

    assert result.baseline.has_data is True
    assert result.baseline.dominant_path == ("list_directory",)

    assert result.mutated.has_data is True
    assert result.mutated.dominant_path == ()  # SELECT found nothing -- the planted break, observed

    assert result.effect.verdict == "REGRESSION"
