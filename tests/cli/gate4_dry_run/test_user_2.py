"""Gate 4 pre-handoff dry run — test_user_2: adversarial config, real
`description_update` regression through the full CLI pipeline.

See tests/cli/gate4_dry_run/test_user_1.py's module docstring for the
scope note shared by every file in this package (dry run, not Gate 4
closure).

test_user_2's setup is deliberately messier than test_user_1's: both
`.mcp.json` and `.cursor/mcp.json` declare a server named "shared" with
DIFFERENT commands (tests init's documented earlier-location-wins
precedence for real, not just in a unit test), one malformed entry with
no `command` field, and an already-existing `drifter.yaml` on disk from
a previous attempt (tests the overwrite refusal, then `--force`). The
scripted agent uses the brittle `SELECT:<substring>` tool-selection mode
against a real tool description containing a real `description_update`
synonym-table word ("detailed" -> "thorough"), so the mutated arm
provably calls nothing — a real, planted REGRESSION discovered via
`drifter init` -> `drifter observe` -> `drifter run` as the actual entry
points, not via `run_mutation_comparison` called directly the way Gate
3's own kill-criterion test did.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from cli.config import ConfigError
from cli.init import run_init
from cli.run import run_run
from record.reader import read_session
from record.schema import ToolCall

ANNOTATED_SERVER = str(Path(__file__).parent.parent.parent / "fixtures" / "fake_server_annotated.py")
SCRIPTED_AGENT = str(Path(__file__).parent.parent.parent / "fixtures" / "scripted_agent.py")


def _write_configs(home: Path) -> None:
    (home / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "shared": {"command": sys.executable, "args": [ANNOTATED_SERVER]},
                    "broken": {"args": ["no-command-field"]},
                }
            }
        ),
        encoding="utf-8",
    )
    cursor_dir = home / ".cursor"
    cursor_dir.mkdir()
    (cursor_dir / "mcp.json").write_text(
        json.dumps({"mcpServers": {"shared": {"command": "this-command-does-not-exist-anywhere"}}}),
        encoding="utf-8",
    )


async def _record_persona_session(config_path: Path) -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "cli", "observe", "--config", str(config_path), "--server", "shared"],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            await session.call_tool("get_status", {})


def test_user_2_adversarial_config_precedence_and_overwrite_protection(tmp_path):
    home = tmp_path
    _write_configs(home)

    config_path = home / "drifter.yaml"
    config_path.write_text("# a real, already-hand-edited config from a prior attempt\n", encoding="utf-8")

    # Overwrite protection must actually refuse, not silently clobber.
    with pytest.raises(ConfigError):
        run_init(output_path=config_path, search_root=home)
    assert "already-hand-edited" in config_path.read_text(encoding="utf-8")

    run_init(output_path=config_path, search_root=home, force=True)
    written = config_path.read_text(encoding="utf-8")

    # .mcp.json is checked before .cursor/mcp.json -- the REAL executable
    # (fake_server_annotated.py) must win, not the bogus one.
    assert "fake_server_annotated.py" in written
    assert "this-command-does-not-exist-anywhere" not in written
    assert "broken" not in written


def test_user_2_real_regression_via_the_full_pipeline(tmp_path):
    home = tmp_path
    _write_configs(home)

    config_path = home / "drifter.yaml"
    run_init(output_path=config_path, search_root=home, force=True)

    runs_dir = home / ".drifter" / "runs"
    config_text = config_path.read_text(encoding="utf-8").replace(".drifter/runs", str(runs_dir.as_posix()))
    config_path.write_text(config_text, encoding="utf-8")

    anyio.run(_record_persona_session, config_path)

    session_files = list(runs_dir.glob("*.jsonl"))
    assert len(session_files) == 1
    fixture_path = session_files[0]
    recorded_calls = [r for r in read_session(fixture_path) if isinstance(r, ToolCall)]
    assert [c.tool_name for c in recorded_calls] == ["get_status"]

    # Confirmed, not assumed (same discipline as Gate 3's own
    # kill-criterion test): the real, recorded manifest's get_status
    # description contains "detailed", the exact substring SELECT below
    # keys off, and mutate_description reliably removes it (this
    # description is a single sentence, so every seed's substitution
    # applies unconditionally -- no reorder randomness to account for).
    from mutate.description_update import mutate_description
    from replay.replay_proxy import tools_served_from_session

    served = {t.name: t.description for t in tools_served_from_session(fixture_path)}
    assert "detailed" in served["get_status"]
    assert "detailed" not in mutate_description(served["get_status"], seed=42).mutated

    agent_command = json.dumps([sys.executable, SCRIPTED_AGENT, "SELECT:detailed|{}"])
    config_text_with_agent = config_text + f"\nagent:\n  command: {agent_command}\n"
    config_path.write_text(config_text_with_agent, encoding="utf-8")

    out = io.StringIO()
    run_run(
        config_path=config_path,
        fixture_path=fixture_path,
        server_name="shared",
        task_id="test_user_2",
        operator="description_update",
        runs_dir=home / "run_sessions",
        seed=42,
        repeats=1,
        timeout_s=30.0,
        output_stream=out,
    )
    output = out.getvalue()
    assert "test_user_2" in output
    assert "REGRESSION" in output
    assert "NO_REGRESSION" not in output
