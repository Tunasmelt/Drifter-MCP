"""Gate 4 pre-handoff dry run — test_user_3: zero configs found, then a
bad hand-written config caught by `drifter doctor` before `observe` ever
starts.

See tests/cli/gate4_dry_run/test_user_1.py's module docstring for the
shared scope note (dry run, not Gate 4 closure).

test_user_3 has no `.mcp.json`, `.cursor/mcp.json`, or Claude Desktop
config anywhere -- `drifter init` must fail with an actionable message
telling them to write `drifter.yaml` by hand, not crash or produce an
empty/invalid file. They then do exactly that, but make a real,
plausible mistake (a command that doesn't exist) -- `drifter doctor`
must catch it with a specific, actionable reason before they ever waste
time on `drifter observe`.
"""

from __future__ import annotations

import io

import pytest

from mcp_drifter.cli.config import ConfigError
from mcp_drifter.cli.doctor import run_doctor
from mcp_drifter.cli.init import run_init


def test_user_3_init_fails_actionably_with_zero_configs_found(tmp_path, monkeypatch):
    import mcp_drifter.cli.init as init_mod

    # No real platform config should leak into this scenario -- this
    # persona genuinely has nothing configured anywhere.
    monkeypatch.setattr(init_mod, "claude_desktop_config_path", lambda: None)

    output_path = tmp_path / "drifter.yaml"
    with pytest.raises(ConfigError, match="No known MCP client config files found"):
        run_init(output_path=output_path, search_root=tmp_path)

    # A failed init must never leave a partial/empty file behind.
    assert not output_path.exists()


def test_user_3_doctor_catches_a_nonexistent_server_command_before_observe(tmp_path):
    config_path = tmp_path / "drifter.yaml"
    config_path.write_text(
        "version: 1\n"
        "servers:\n"
        "  - name: my-server\n"
        "    command: ['this-executable-does-not-exist-anywhere-12345']\n",
        encoding="utf-8",
    )

    out = io.StringIO()
    ok = run_doctor(config_path=config_path, output_stream=out)

    assert ok is False
    output = out.getvalue()
    assert "[FAIL]" in output
    assert "my-server" in output
    # Actionable, not a stack trace (CLAUDE.md's F-37 invariant).
    assert "could not start command" in output


def test_user_3_doctor_reports_ok_for_config_parsing_even_when_connectivity_fails(tmp_path):
    """The config itself is well-formed -- doctor's two checks (parse,
    connectivity) must be reported separately, so a user can tell "my
    YAML is fine, it's the command that's wrong" from the output alone."""
    config_path = tmp_path / "drifter.yaml"
    config_path.write_text(
        "version: 1\nservers:\n  - name: bad-cmd\n    command: ['nonexistent-cmd-xyz']\n",
        encoding="utf-8",
    )

    out = io.StringIO()
    run_doctor(config_path=config_path, output_stream=out)
    output = out.getvalue()

    assert "[ OK ] config" in output
    assert "1 server(s) declared" in output
    assert "[FAIL] server 'bad-cmd'" in output
