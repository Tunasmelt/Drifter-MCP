"""README's config examples must actually parse.

Found by dogfooding the published wheel as a genuinely new user: every
`agent:` example in README was INVALID, and `drifter run` rejected the
documented config before doing anything.

Two separate defects, both of which would stop a new user at their first
`drifter run`:

  1. `command:` was shown as a shell string
     (`command: "python agent.py --task '{task.prompt}'"`), but
     `AgentConfig.command` is `list[str]`. Drifter never invokes a shell,
     so argv has to be split by the author.
  2. The `mode: http` example omitted `command` entirely. It reads as
     though http mode needs no command -- Drifter does not spawn the
     agent's MCP client in that mode -- but the field is required with no
     default, so the example fails validation on a missing field.

This is the same class of defect as docs/SPEC.md §15 limitation 16's
secondary finding (a): documentation that cannot be followed. That one
was found by a blind test agent, this one by installing the wheel and
following README literally. Neither was reachable from inside the repo,
where nobody reads the README to learn the schema.

The test parses README's own fenced YAML rather than a copy, so the
examples cannot drift from the schema again without going red.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from mcp_drifter.cli.config import AgentConfig

README = Path(__file__).resolve().parents[2] / "README.md"


def _yaml_blocks_declaring_an_agent() -> list[str]:
    """Every fenced yaml block in README that configures `agent:`."""
    text = README.read_text(encoding="utf-8")
    blocks = re.findall(r"```ya?ml\n(.*?)```", text, re.DOTALL)
    return [b for b in blocks if re.search(r"^agent:", b, re.MULTILINE)]


def test_readme_actually_contains_agent_examples():
    """Guards the guard: if the extraction regex silently stops matching,
    every test below would vacuously pass over an empty list.
    """
    assert len(_yaml_blocks_declaring_an_agent()) >= 2


@pytest.mark.parametrize("block", _yaml_blocks_declaring_an_agent())
def test_each_documented_agent_block_validates_against_the_real_schema(block):
    parsed = yaml.safe_load(block)

    # Pydantic raises here if `command` is a string, or is missing -- the
    # two defects this test was written for.
    agent = AgentConfig.model_validate(parsed["agent"])

    assert isinstance(agent.command, list)
    assert all(isinstance(part, str) for part in agent.command)
    assert agent.command, "a documented command must have at least an executable"


def test_the_http_example_still_documents_a_command():
    """`mode: http` means Drifter does not spawn the agent's MCP client --
    but it DOES spawn `command`, a user-written wrapper that reads
    $DRIFTER_PROXY_URL. Dropping `command` from that example is the exact
    mistake this file exists to prevent, and it is a tempting one because
    the mode's whole point is that Drifter spawns less.
    """
    http_blocks = [b for b in _yaml_blocks_declaring_an_agent() if "mode: http" in b]

    assert http_blocks, "README no longer documents the http agent mode"
    for block in http_blocks:
        agent = AgentConfig.model_validate(yaml.safe_load(block)["agent"])
        assert agent.mode == "http"
        assert agent.command
