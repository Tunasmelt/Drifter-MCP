"""Unit tests for cli/config.py's minimal drifter.yaml loader (F-09)."""

import pytest

from cli.config import ConfigError, load_config

VALID_YAML = """
version: 1
servers:
  - name: crm
    command: ["npx", "-y", "@mcp/server-crm"]
"""


def _write(tmp_path, text, name="drifter.yaml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_valid_config(tmp_path):
    config = load_config(_write(tmp_path, VALID_YAML))
    assert config.version == 1
    assert len(config.servers) == 1
    assert config.servers[0].name == "crm"
    assert config.servers[0].command == ["npx", "-y", "@mcp/server-crm"]


def test_record_dir_defaults_when_not_specified(tmp_path):
    config = load_config(_write(tmp_path, VALID_YAML))
    assert config.record.dir == ".drifter/runs"
    assert config.record.redact == "shape"


def test_record_dir_honored_when_specified(tmp_path):
    text = VALID_YAML + "\nrecord:\n  dir: custom/runs\n"
    config = load_config(_write(tmp_path, text))
    assert config.record.dir == "custom/runs"


def test_missing_file_raises_config_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "does_not_exist.yaml")


def test_malformed_yaml_raises_config_error(tmp_path):
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(_write(tmp_path, "servers: [this is: not: valid"))


def test_empty_servers_list_raises_config_error(tmp_path):
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, "version: 1\nservers: []\n"))


def test_missing_servers_key_raises_config_error(tmp_path):
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, "version: 1\n"))


def test_server_with_empty_command_raises_config_error(tmp_path):
    text = "version: 1\nservers:\n  - name: crm\n    command: []\n"
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, text))


def test_unknown_top_level_keys_do_not_break_loading(tmp_path):
    # Later-gate blocks (baseline, mutations, tasks, policy) already
    # present in a hand-written drifter.yaml must not be rejected —
    # this loader just doesn't read them yet.
    text = VALID_YAML + "\nmutations:\n  profile: quick\n  seed: 42\n"
    config = load_config(_write(tmp_path, text))
    assert config.servers[0].name == "crm"
