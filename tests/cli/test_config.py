"""Unit tests for cli/config.py's minimal drifter.yaml loader (F-09)."""

import pytest
from mcp.client.stdio import StdioServerParameters

from mcp_drifter.cli.config import ConfigError, ServerConfig, assertions_for, find_task, load_config, server_target

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


def test_agent_defaults_to_none_when_not_specified(tmp_path):
    """None, not a default AgentConfig -- absence must stay
    distinguishable from "configured to run nothing" (nullable-field
    discipline, see AgentConfig's own docstring in cli/config.py)."""
    config = load_config(_write(tmp_path, VALID_YAML))
    assert config.agent is None


def test_agent_command_honored_when_specified(tmp_path):
    text = VALID_YAML + '\nagent:\n  command: ["python", "agent.py", "--task", "{task.prompt}"]\n'
    config = load_config(_write(tmp_path, text))
    assert config.agent is not None
    assert config.agent.command == ["python", "agent.py", "--task", "{task.prompt}"]


def test_agent_with_empty_command_raises_config_error(tmp_path):
    text = VALID_YAML + "\nagent:\n  command: []\n"
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, text))


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


# --- agent.mode / agent.env_var (F-38) --------------------------------------


def test_agent_mode_defaults_to_subprocess_for_backward_compatibility(tmp_path):
    """Every drifter.yaml written before F-38 has no `mode` key at all --
    it must keep behaving exactly as it always did, not start requiring
    a new field."""
    text = VALID_YAML + '\nagent:\n  command: ["python", "agent.py"]\n'
    config = load_config(_write(tmp_path, text))
    assert config.agent.mode == "subprocess"


def test_agent_env_var_defaults_to_drifter_proxy_url(tmp_path):
    text = VALID_YAML + '\nagent:\n  command: ["python", "agent.py"]\n'
    config = load_config(_write(tmp_path, text))
    assert config.agent.env_var == "DRIFTER_PROXY_URL"


def test_agent_mode_http_is_honored(tmp_path):
    text = VALID_YAML + '\nagent:\n  command: ["python", "agent.py"]\n  mode: http\n'
    config = load_config(_write(tmp_path, text))
    assert config.agent.mode == "http"


def test_agent_env_var_is_honored_when_specified(tmp_path):
    text = VALID_YAML + '\nagent:\n  command: ["python", "agent.py"]\n  mode: http\n  env_var: MY_CUSTOM_URL\n'
    config = load_config(_write(tmp_path, text))
    assert config.agent.env_var == "MY_CUSTOM_URL"


def test_agent_mode_rejects_unknown_values(tmp_path):
    text = VALID_YAML + '\nagent:\n  command: ["python", "agent.py"]\n  mode: carrier_pigeon\n'
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, text))


def test_unknown_top_level_keys_do_not_break_loading(tmp_path):
    # Later-gate blocks (baseline, mutations, tasks, policy) already
    # present in a hand-written drifter.yaml must not be rejected —
    # this loader just doesn't read them yet.
    text = VALID_YAML + "\nmutations:\n  profile: quick\n  seed: 42\n"
    config = load_config(_write(tmp_path, text))
    assert config.servers[0].name == "crm"


# --- servers[].url (F-39) ----------------------------------------------------


def test_server_url_is_honored_when_specified(tmp_path):
    text = "version: 1\nservers:\n  - name: remote\n    url: https://mcp.example.com/mcp\n"
    config = load_config(_write(tmp_path, text))
    assert config.servers[0].url == "https://mcp.example.com/mcp"
    assert config.servers[0].command is None


def test_server_with_both_command_and_url_raises_config_error(tmp_path):
    text = 'version: 1\nservers:\n  - name: bad\n    command: ["npx", "-y", "@mcp/server"]\n    url: https://mcp.example.com/mcp\n'
    with pytest.raises(ConfigError, match="exactly one of"):
        load_config(_write(tmp_path, text))


def test_server_with_neither_command_nor_url_raises_config_error(tmp_path):
    text = "version: 1\nservers:\n  - name: bad\n"
    with pytest.raises(ConfigError, match="exactly one of"):
        load_config(_write(tmp_path, text))


def test_server_with_empty_url_raises_config_error(tmp_path):
    text = "version: 1\nservers:\n  - name: bad\n    url: ''\n"
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, text))


def test_server_target_returns_stdio_params_for_a_command_entry():
    server = ServerConfig(name="local", command=["npx", "-y", "@mcp/server"])
    target = server_target(server)
    assert isinstance(target, StdioServerParameters)
    assert target.command == "npx"
    assert target.args == ["-y", "@mcp/server"]


def test_server_target_returns_the_url_string_for_a_url_entry():
    server = ServerConfig(name="remote", url="https://mcp.example.com/mcp")
    target = server_target(server)
    assert target == "https://mcp.example.com/mcp"
    assert isinstance(target, str)


# --- policy: (F-26) -----------------------------------------------------------


def test_policy_defaults_to_empty_lists_when_not_specified(tmp_path):
    config = load_config(_write(tmp_path, VALID_YAML))
    assert config.policy.destructive == []
    assert config.policy.confirmation_required == []


def test_policy_destructive_is_honored_when_specified(tmp_path):
    text = VALID_YAML + "\npolicy:\n  destructive: [delete_all, wipe_db]\n"
    config = load_config(_write(tmp_path, text))
    assert config.policy.destructive == ["delete_all", "wipe_db"]


def test_policy_confirmation_required_is_honored_when_specified(tmp_path):
    text = VALID_YAML + "\npolicy:\n  confirmation_required: [send_email]\n"
    config = load_config(_write(tmp_path, text))
    assert config.policy.confirmation_required == ["send_email"]


# --- F-24: authored tasks (docs/SPEC.md §11's `tasks:`) ---------------------


def test_an_authored_task_parses_with_its_assertions(tmp_path):
    path = tmp_path / "drifter.yaml"
    path.write_text(
        "version: 1\n"
        "servers:\n"
        "  - name: s\n"
        "    command: ['echo']\n"
        "tasks:\n"
        "  - id: invoice\n"
        "    prompt: make an invoice\n"
        "    assert:\n"
        "      calls: [search, create]\n"
        "      calls_before: [[search, create]]\n"
        "      never_calls: [delete]\n"
        "      no_errors: true\n",
        encoding="utf-8",
    )
    config = load_config(path)

    assert [t.id for t in config.tasks] == ["invoice"]
    assertions = assertions_for(config.tasks, "invoice")
    assert assertions.calls == ("search", "create")
    assert assertions.calls_before == (("search", "create"),)
    assert assertions.never_calls == ("delete",)
    assert assertions.no_errors is True


def test_a_config_with_no_tasks_yields_an_empty_oracle(tmp_path):
    """The honest default: no authored task means the Task axis reports
    UNKNOWN, which is a correct answer rather than a missing feature."""
    path = tmp_path / "drifter.yaml"
    path.write_text("version: 1\nservers:\n  - name: s\n    command: ['echo']\n", encoding="utf-8")
    config = load_config(path)

    assert config.tasks == []
    assert assertions_for(config.tasks, "anything").empty is True


def test_an_unmatched_task_id_is_a_bare_label_not_an_error(tmp_path):
    """`--task-id` predates authored tasks and stays usable as a free-form
    label alongside `--prompt` -- raising here would break every existing
    invocation for an opt-in feature."""
    path = tmp_path / "drifter.yaml"
    path.write_text(
        "version: 1\nservers:\n  - name: s\n    command: ['echo']\n"
        "tasks:\n  - id: known\n    assert:\n      calls: [a]\n",
        encoding="utf-8",
    )
    config = load_config(path)

    assert find_task(config.tasks, "unknown") is None
    assert assertions_for(config.tasks, "unknown").empty is True


def test_result_contains_is_rejected_loudly_not_silently_ignored(tmp_path):
    """`extra="allow"` would otherwise accept an assertion nothing reads --
    a user would believe it was being checked. Recording is shape-only by
    design (docs/SPEC.md §3), so this one can never be evaluated."""
    path = tmp_path / "drifter.yaml"
    path.write_text(
        "version: 1\nservers:\n  - name: s\n    command: ['echo']\n"
        "tasks:\n  - id: t\n    assert:\n      result_contains: ['invoice-42']\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="result_has_keys"):
        load_config(path)


def test_calls_before_rejects_a_malformed_pair(tmp_path):
    path = tmp_path / "drifter.yaml"
    path.write_text(
        "version: 1\nservers:\n  - name: s\n    command: ['echo']\n"
        "tasks:\n  - id: t\n    assert:\n      calls_before: [[only_one]]\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="earlier, later"):
        load_config(path)
