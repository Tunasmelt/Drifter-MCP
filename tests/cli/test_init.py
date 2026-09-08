"""Tests for `drifter init` (F-33), docs/SPEC.md §11.

Built red-test-first per this project's own hard requirement, not
skipped as "just a config scanner": found while sanity-checking Gate
4's own handoff checklist that `drifter init` -- literally the first
command a second user is told to run -- didn't exist as a CLI
subcommand at all (docs/CHANGELOG.md's corresponding entry).

Deliberately NARROWER than docs/FEATURES.md's F-33 text ("runs initial
tool classification (F-26)"): `policy/` (F-26, tool risk
classification) is empty -- not built in Gate 3 despite PHASES.md's
checklist naming it -- and `cli.config.DrifterConfig` has no
risk-classification field to populate even if it were. F-33's own
stated "Done when" bar ("produces a working drifter.yaml with zero
manual edits required to run drifter observe") does not require
classification output, so this narrower scope still satisfies it. Same
precedent as F-34's own documented narrower-than-spec Gate 2 scope
(cli/subprocess_adapter.py's docstring) -- built and tested standalone
against what actually exists today, not against an aspirational shape.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from mcp_drifter.cli.config import ConfigError, load_config
from mcp_drifter.cli.init import ServerConfig, run_init, scan_mcp_configs


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


# --- scan_mcp_configs: extraction, skip rules, precedence -------------------


def test_scan_extracts_a_real_shaped_stdio_server_from_mcp_json(tmp_path):
    _write_json(
        tmp_path / ".mcp.json",
        {"mcpServers": {"filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]}}},
    )
    servers, skipped, found_files = scan_mcp_configs(tmp_path)

    assert len(servers) == 1
    assert servers[0].name == "filesystem"
    assert servers[0].command == ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
    assert skipped == []
    assert (tmp_path / ".mcp.json") in found_files


def test_scan_extracts_from_cursor_mcp_json_too(tmp_path):
    _write_json(tmp_path / ".cursor" / "mcp.json", {"mcpServers": {"git": {"command": "uvx", "args": ["mcp-server-git"]}}})
    servers, skipped, found_files = scan_mcp_configs(tmp_path)

    assert len(servers) == 1
    assert servers[0].name == "git"
    assert servers[0].command == ["uvx", "mcp-server-git"]


def test_scan_server_with_no_args_field(tmp_path):
    _write_json(tmp_path / ".mcp.json", {"mcpServers": {"time": {"command": "mcp-server-time"}}})
    servers, _, _ = scan_mcp_configs(tmp_path)
    assert servers[0].command == ["mcp-server-time"]


def test_scan_skips_non_stdio_transports_with_an_explicit_reason(tmp_path):
    """Drifter's v0 architecture is stdio-only (docs/SPEC.md) -- an http/sse/ws
    entry must never be silently dropped OR silently mis-parsed as if it
    were a stdio command; it's reported as skipped with a reason."""
    _write_json(
        tmp_path / ".mcp.json",
        {
            "mcpServers": {
                "remote-api": {"type": "http", "url": "https://mcp.example.com/mcp"},
                "sse-server": {"type": "sse", "url": "https://api.example.com/sse"},
            }
        },
    )
    servers, skipped, _ = scan_mcp_configs(tmp_path)

    assert servers == []
    assert {s.name for s in skipped} == {"remote-api", "sse-server"}
    for s in skipped:
        assert "stdio" in s.reason.lower()


def test_scan_skips_malformed_entries_without_crashing(tmp_path):
    _write_json(tmp_path / ".mcp.json", {"mcpServers": {"broken": {"args": ["no-command-field"]}}})
    servers, skipped, _ = scan_mcp_configs(tmp_path)

    assert servers == []
    assert len(skipped) == 1
    assert skipped[0].name == "broken"


def test_scan_tolerates_a_config_file_with_no_mcpservers_key(tmp_path):
    _write_json(tmp_path / ".mcp.json", {"unrelated": "content"})
    servers, skipped, found_files = scan_mcp_configs(tmp_path)
    assert servers == []
    assert skipped == []


def test_scan_tolerates_invalid_json_without_crashing(tmp_path):
    path = tmp_path / ".mcp.json"
    path.write_text("{not valid json", encoding="utf-8")
    servers, skipped, found_files = scan_mcp_configs(tmp_path)
    assert servers == []
    # The unreadable file is still reported found, so the user knows why
    # nothing came out of it, rather than the scan silently ignoring it.
    assert path in found_files


def test_scan_returns_no_servers_and_no_found_files_when_nothing_exists(tmp_path, monkeypatch):
    import mcp_drifter.cli.init as init_mod

    monkeypatch.setattr(init_mod, "claude_desktop_config_path", lambda: None)
    servers, skipped, found_files = scan_mcp_configs(tmp_path)
    assert servers == []
    assert skipped == []
    assert found_files == []


def test_earlier_config_location_wins_on_a_duplicate_server_name(tmp_path):
    """.mcp.json is checked before .cursor/mcp.json (docs/SPEC.md §11's own
    location order) -- a same-named server in both is deterministic, not
    "whichever the filesystem happens to iterate last." """
    _write_json(tmp_path / ".mcp.json", {"mcpServers": {"git": {"command": "from-mcp-json"}}})
    _write_json(tmp_path / ".cursor" / "mcp.json", {"mcpServers": {"git": {"command": "from-cursor"}}})

    servers, _, _ = scan_mcp_configs(tmp_path)
    assert len(servers) == 1
    assert servers[0].command == ["from-mcp-json"]


# --- run_init: writing behavior, overwrite protection, actionable errors ----


def test_run_init_writes_a_drifter_yaml_that_load_config_accepts(tmp_path):
    _write_json(tmp_path / ".mcp.json", {"mcpServers": {"filesystem": {"command": "npx", "args": ["-y", "server-filesystem"]}}})
    output_path = tmp_path / "drifter.yaml"

    run_init(output_path=output_path, search_root=tmp_path)

    config = load_config(output_path)
    assert config.servers[0].name == "filesystem"
    assert config.servers[0].command == ["npx", "-y", "server-filesystem"]


def test_run_init_refuses_to_overwrite_an_existing_drifter_yaml_without_force(tmp_path):
    _write_json(tmp_path / ".mcp.json", {"mcpServers": {"a": {"command": "x"}}})
    output_path = tmp_path / "drifter.yaml"
    output_path.write_text("# a real, already-hand-edited config\n", encoding="utf-8")

    with pytest.raises(ConfigError):
        run_init(output_path=output_path, search_root=tmp_path)

    # Never touched -- an overwrite-protection refusal must not partially
    # write, matching the "reversible over destructive" default this
    # project's own operating instructions require for any hard-to-reverse
    # local action.
    assert output_path.read_text(encoding="utf-8") == "# a real, already-hand-edited config\n"


def test_run_init_force_overwrites_an_existing_drifter_yaml(tmp_path):
    _write_json(tmp_path / ".mcp.json", {"mcpServers": {"a": {"command": "x"}}})
    output_path = tmp_path / "drifter.yaml"
    output_path.write_text("old content", encoding="utf-8")

    run_init(output_path=output_path, search_root=tmp_path, force=True)

    assert "old content" not in output_path.read_text(encoding="utf-8")


def test_run_init_raises_actionable_config_error_when_no_servers_found(tmp_path, monkeypatch):
    import mcp_drifter.cli.init as init_mod

    monkeypatch.setattr(init_mod, "claude_desktop_config_path", lambda: None)
    output_path = tmp_path / "drifter.yaml"

    with pytest.raises(ConfigError):
        run_init(output_path=output_path, search_root=tmp_path)
    assert not output_path.exists()


def test_run_init_reports_skipped_non_stdio_servers_to_the_status_stream(tmp_path):
    import io

    _write_json(
        tmp_path / ".mcp.json",
        {"mcpServers": {"ok": {"command": "x"}, "remote": {"type": "http", "url": "https://example.com"}}},
    )
    output_path = tmp_path / "drifter.yaml"
    out = io.StringIO()

    run_init(output_path=output_path, search_root=tmp_path, status_stream=out)

    report = out.getvalue()
    assert "remote" in report
    assert "ok" in report


def test_run_init_output_is_valid_yaml_with_expected_top_level_keys(tmp_path):
    _write_json(tmp_path / ".mcp.json", {"mcpServers": {"a": {"command": "x", "args": ["y"]}}})
    output_path = tmp_path / "drifter.yaml"

    run_init(output_path=output_path, search_root=tmp_path)

    data = yaml.safe_load(output_path.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert data["servers"] == [{"name": "a", "command": ["x", "y"]}]
    assert "record" in data


def test_run_init_handles_windows_style_backslash_paths_without_yaml_corruption(tmp_path):
    """Real regression precedent (tests/cli/test_observe.py's _drifter_yaml,
    cited in the repo's own root drifter.yaml comment): a double-quoted YAML
    scalar containing backslashes gets corrupted by escape-sequence
    interpretation. A real Windows command arg is exactly this shape."""
    _write_json(
        tmp_path / ".mcp.json",
        {"mcpServers": {"filesystem": {"command": "cmd", "args": ["/c", "npx", "-y", "server-filesystem", "C:\\Users\\user\\Desktop"]}}},
    )
    output_path = tmp_path / "drifter.yaml"

    run_init(output_path=output_path, search_root=tmp_path)

    config = load_config(output_path)
    assert config.servers[0].command[-1] == "C:\\Users\\user\\Desktop"
