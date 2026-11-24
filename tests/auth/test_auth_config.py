"""F-41, config half (docs/FEATURES.md F-41, docs/SPEC.md §11): `servers[].auth`.

Written before the implementation. What "done" means here:
  - `auth` is a typed, validated block, not a key `extra="allow"` swallows. Until now a
    user who wrote `auth:` under a server got NO authentication and no warning at all.
  - `auth` on a `command` (stdio) entry is a config error: stdio servers take credentials
    from their own environment, so accepting it would silently do nothing.
  - `_env` fields hold the NAME of an environment variable. A value that looks like a
    secret is rejected at load, so a token pasted into drifter.yaml fails loudly instead
    of being committed -- and the rejection must not echo the value it is refusing.
  - Fields that do not belong to the chosen `type` are errors, not ignored: a typo or a
    leftover field must never quietly change what is sent.
"""

from __future__ import annotations

import pytest

from mcp_drifter.cli.config import ConfigError, load_config

URL_SERVER = """
version: 1
servers:
  - name: remote-crm
    url: "https://mcp.example.com/mcp"
{auth}
"""

STDIO_SERVER = """
version: 1
servers:
  - name: local
    command: ["python", "server.py"]
{auth}
"""


def _write(tmp_path, template: str, auth_block: str = "") -> object:
    path = tmp_path / "drifter.yaml"
    path.write_text(template.format(auth=auth_block), encoding="utf-8")
    return path


def _auth(body: str) -> str:
    """An `auth:` block indented under the (single) server entry."""
    lines = body.strip("\n").splitlines()
    return "    auth:\n" + "\n".join("      " + line for line in lines)


# --- defaults ---------------------------------------------------------------------------


def test_a_server_with_no_auth_block_has_no_auth(tmp_path):
    config = load_config(_write(tmp_path, URL_SERVER))
    assert config.servers[0].auth is None


def test_an_explicit_none_type_is_accepted_on_a_url_server(tmp_path):
    config = load_config(_write(tmp_path, URL_SERVER, _auth("type: none")))
    assert config.servers[0].auth is not None and config.servers[0].auth.type == "none"


def test_the_type_defaults_to_none_when_the_block_omits_it(tmp_path):
    # An `auth:` block that only lists scopes would otherwise be a no-op in disguise.
    with pytest.raises(ConfigError, match="scopes"):
        load_config(_write(tmp_path, URL_SERVER, _auth("scopes: [read]")))


# --- bearer_env ---------------------------------------------------------------------------


def test_bearer_env_loads_with_a_token_env_name(tmp_path):
    config = load_config(_write(tmp_path, URL_SERVER, _auth("type: bearer_env\ntoken_env: CRM_TOKEN")))
    auth = config.servers[0].auth
    assert auth is not None and (auth.type, auth.token_env) == ("bearer_env", "CRM_TOKEN")


@pytest.mark.parametrize("name", ["CRM_TOKEN", "crm_token", "_PRIVATE", "MCP_API_KEY_2", "T", "A1"])
def test_ordinary_environment_variable_names_are_accepted(tmp_path, name):
    config = load_config(_write(tmp_path, URL_SERVER, _auth(f"type: bearer_env\ntoken_env: {name}")))
    assert config.servers[0].auth.token_env == name


def test_bearer_env_requires_token_env(tmp_path):
    with pytest.raises(ConfigError, match="token_env"):
        load_config(_write(tmp_path, URL_SERVER, _auth("type: bearer_env")))


# --- auth on a stdio server -----------------------------------------------------------------


@pytest.mark.parametrize("body", ["type: bearer_env\ntoken_env: CRM_TOKEN", "type: none", "type: oauth"])
def test_auth_on_a_command_server_is_a_config_error_naming_the_server(tmp_path, body):
    with pytest.raises(ConfigError) as failure:
        load_config(_write(tmp_path, STDIO_SERVER, _auth(body)))
    message = str(failure.value)
    assert "local" in message and "url" in message and "command" in message, message


# --- literal secrets in _env fields --------------------------------------------------------

LITERAL_SECRETS = [
    pytest.param("sk-AbCdEf0123456789AbCdEf0123456789", id="openai-style-key"),
    pytest.param("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r", id="jwt"),
    pytest.param("Bearer abcdefgh12345678", id="bearer-header-value"),
    pytest.param("ghp_Ab12Cd34Ef56Gh78Ij90Kl12Mn34Op56Qr78", id="mixed-case-token-with-underscore"),
    pytest.param("3f9a1c5e7b2d4f6a8c0e1b3d5f7a9c2e4b6d8f0a", id="lowercase-hex-40"),
    pytest.param("3F9A1C5E7B2D4F6A8C0E1B3D5F7A9C2E4B6D8F0A", id="uppercase-hex-40"),
    pytest.param("my secret value", id="contains-spaces"),
    pytest.param("abc.def.ghi", id="contains-dots"),
    pytest.param("token-with-hyphens", id="contains-hyphens"),
    pytest.param("123456", id="starts-with-a-digit"),
]


@pytest.mark.parametrize("secret", LITERAL_SECRETS)
@pytest.mark.parametrize("field", ["token_env", "secret_env"])
def test_a_literal_secret_in_an_env_field_is_rejected_and_never_echoed(tmp_path, secret, field):
    type_ = "bearer_env" if field == "token_env" else "client_credentials"
    extra = "" if field == "token_env" else "\nclient_id: my-client"
    body = f"type: {type_}\n{field}: '{secret}'{extra}"
    with pytest.raises(ConfigError) as failure:
        load_config(_write(tmp_path, URL_SERVER, _auth(body)))
    message = str(failure.value)
    assert field in message, message
    assert "environment variable" in message and "NAME" in message, message
    # Not the whole value, and not a recognizable slice of it either.
    assert secret not in message
    assert secret[:10] not in message and secret[-10:] not in message, message


def test_a_rejected_secret_is_not_echoed_by_any_other_config_error_either(tmp_path):
    """`hide_input_in_errors` on the root model: pydantic otherwise prints `input_value=...`
    for ANY invalid field, and drifter.yaml values (a `--api-key` in a command, say) can be
    secrets too. `observe` is launched unattended by an agent, so stderr ends up in its logs."""
    secret = "sk-AbCdEf0123456789AbCdEf0123456789"
    path = tmp_path / "drifter.yaml"
    path.write_text(f'version: 1\nservers:\n  - name: crm\n    command: "{secret}"\n', encoding="utf-8")
    with pytest.raises(ConfigError) as failure:
        load_config(path)
    assert secret not in str(failure.value) and "input_value" not in str(failure.value)


def test_other_config_errors_keep_their_actionable_message_without_the_input(tmp_path):
    path = tmp_path / "drifter.yaml"
    path.write_text("version: 1\nservers:\n  - name: crm\n    url: 'ftp://example.com/mcp'\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="must start with http:// or https://"):
        load_config(path)


# --- fields that do not belong to the chosen type --------------------------------------------


@pytest.mark.parametrize(
    "body, offending",
    [
        ("type: bearer_env\ntoken_env: CRM_TOKEN\nsecret_env: CRM_SECRET", "secret_env"),
        ("type: bearer_env\ntoken_env: CRM_TOKEN\nclient_id: abc", "client_id"),
        ("type: bearer_env\ntoken_env: CRM_TOKEN\nscopes: [read]", "scopes"),
        ("type: oauth\ntoken_env: CRM_TOKEN", "token_env"),
        ("type: oauth\nsecret_env: CRM_SECRET", "secret_env"),
        ("type: client_credentials\nclient_id: abc\nsecret_env: S\ntoken_env: T", "token_env"),
        ("type: none\ntoken_env: CRM_TOKEN", "token_env"),
        ("type: none\nscopes: [read]", "scopes"),
    ],
)
def test_a_field_that_does_not_apply_to_the_type_is_rejected_by_name(tmp_path, body, offending):
    with pytest.raises(ConfigError) as failure:
        load_config(_write(tmp_path, URL_SERVER, _auth(body)))
    assert offending in str(failure.value), failure.value


def test_a_misspelled_key_is_rejected_not_ignored(tmp_path):
    with pytest.raises(ConfigError, match="token_envv"):
        load_config(_write(tmp_path, URL_SERVER, _auth("type: bearer_env\ntoken_env: CRM_TOKEN\ntoken_envv: X")))


def test_an_unknown_type_is_rejected_and_the_valid_ones_are_listed(tmp_path):
    with pytest.raises(ConfigError) as failure:
        load_config(_write(tmp_path, URL_SERVER, _auth("type: basic\ntoken_env: CRM_TOKEN")))
    message = str(failure.value)
    for valid in ("oauth", "bearer_env", "client_credentials", "none"):
        assert valid in message, message


# --- the other types parse (they connect in F-43/F-44/F-45) -------------------------------------


def test_oauth_parses_with_scopes_and_a_preregistered_client_id(tmp_path):
    config = load_config(_write(tmp_path, URL_SERVER, _auth("type: oauth\nscopes: [crm.read]\nclient_id: my-client")))
    auth = config.servers[0].auth
    assert (auth.type, auth.scopes, auth.client_id) == ("oauth", ["crm.read"], "my-client")


def test_client_credentials_parses_and_requires_a_client_id_and_a_secret_env(tmp_path):
    ok = load_config(
        _write(tmp_path, URL_SERVER, _auth("type: client_credentials\nclient_id: my-client\nsecret_env: CRM_SECRET"))
    )
    assert ok.servers[0].auth.secret_env == "CRM_SECRET"
    for missing, body in (
        ("client_id", "type: client_credentials\nsecret_env: CRM_SECRET"),
        ("secret_env", "type: client_credentials\nclient_id: my-client"),
    ):
        with pytest.raises(ConfigError, match=missing):
            load_config(_write(tmp_path, URL_SERVER, _auth(body)))


def test_scopes_must_be_a_list_of_non_empty_strings(tmp_path):
    for bad in ("scopes: read", "scopes: ['']", "scopes: [1]"):
        with pytest.raises(ConfigError, match="scopes"):
            load_config(_write(tmp_path, URL_SERVER, _auth(f"type: oauth\n{bad}")))
