"""Minimal drifter.yaml config loader (docs/SPEC.md §11).

`servers:`, `record.dir` (F-09), and now `agent.command` (F-35,
`drifter run`) — the full config surface (`tasks:`, `mutations:`,
`policy:`) is still later-gate scope per docs/PHASES.md; this loader is
deliberately narrow, not the final shape. `extra="allow"` on every
model means a drifter.yaml already written with later-gate blocks in
it won't be rejected — those blocks just aren't read yet.

`agent.command` is a list of argv tokens, NOT docs/SPEC.md §11's example
shell string (`"python agent.py --task '{task.prompt}'"`) — matching
`ServerConfig.command`'s existing convention (also a list) rather than
inventing shell-parsing (shlex) for one field and not the other. A
deliberate, small deviation from the example syntax, not a new format:
`{task.prompt}` still templates per-token (`cli/run.py`), just without
a shell-quoting step in between.

`agent.mode` (F-38, docs/SPEC.md §5.1/§11): added now that a second real
mode exists to distinguish (`subprocess`, the only one through Gate 3,
vs. `http`) — CLAUDE.md's simplicity principle blocked this field until
there was a genuine second value for it, not before. Defaults to
`"subprocess"` so every drifter.yaml written before this field existed
keeps behaving identically, with no migration needed. `agent.env_var`
(default `"DRIFTER_PROXY_URL"`) names the environment variable
`cli/subprocess_adapter.py`'s `mode: http` path injects the replay
proxy's real, loopback-bound URL into — only meaningful when
`mode: http`, but always present (with its default) so a caller doesn't
need to branch on `mode` just to read it.

`ServerConfig.url` (F-39, docs/SPEC.md §5.1/§11): the *real server's* own
transport, separate from and unrelated to `agent.mode` above (that's the
agent-under-test's transport, this is the real MCP server Drifter connects
to on the agent's behalf). Mutually exclusive with `command` on the same
entry — `server_target()` below is the one place that distinction turns
into what `record/proxy.py` actually consumes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from mcp.client.stdio import StdioServerParameters
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    # F-39 (docs/SPEC.md §5.1/§11): `command` (local, spawned over stdio) and
    # `url` (a real, network-reachable Streamable HTTP endpoint — the
    # "change one config line" onboarding story) are mutually exclusive on
    # the same entry, enforced below. Both default to None rather than one
    # being required, so the validator (not pydantic's own required-field
    # error) reports whichever real problem occurred -- neither given, or
    # both given -- with an actionable message instead of a generic
    # "field required" that doesn't explain the exclusivity rule.
    command: list[str] | None = None
    url: str | None = None

    @field_validator("command")
    @classmethod
    def _command_not_empty(cls, v: list[str] | None) -> list[str] | None:
        if v is not None and not v:
            raise ValueError("server command must have at least one element (the executable)")
        return v

    @field_validator("url")
    @classmethod
    def _url_not_empty(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("server url must not be empty")
        return v

    @model_validator(mode="after")
    def _exactly_one_of_command_or_url(self) -> "ServerConfig":
        if (self.command is None) == (self.url is None):
            raise ValueError(
                f"server {self.name!r} must declare exactly one of `command` (local, stdio) or "
                "`url` (a real, network-reachable Streamable HTTP endpoint), not both or neither"
            )
        return self


def server_target(server: ServerConfig) -> StdioServerParameters | str:
    """The one place `command` vs `url` gets turned into what `record/
    proxy.py`'s `run_passthrough_proxy`/`_connect_to_server` actually
    consumes (`record.proxy.ServerTarget`) — every caller (`cli/observe.py`,
    `cli/doctor.py`) goes through this rather than re-deriving the same
    branch, so a third transport later only needs a change here. The
    `_exactly_one_of_command_or_url` validator above guarantees exactly one
    of `server.command`/`server.url` is set by the time this runs — no
    third "neither" case to handle here.
    """
    if server.url is not None:
        return server.url
    return StdioServerParameters(command=server.command[0], args=server.command[1:])


class RecordConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    dir: str = ".drifter/runs"
    redact: str = "shape"


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    command: list[str]
    mode: Literal["subprocess", "http"] = "subprocess"
    env_var: str = "DRIFTER_PROXY_URL"

    @field_validator("command")
    @classmethod
    def _command_not_empty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("agent.command must have at least one element (the executable)")
        return v


class DrifterConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    version: int
    servers: list[ServerConfig]
    record: RecordConfig = RecordConfig()
    # None (not a default AgentConfig()) since there's no sensible
    # default agent command -- absence must stay distinguishable from
    # "configured to run nothing," matching this project's nullable-
    # field discipline. cli/run.py reports an actionable ConfigError
    # when it's needed but missing, rather than a bare validation
    # traceback (same reasoning as ConfigError's own docstring below).
    agent: AgentConfig | None = None

    @field_validator("servers")
    @classmethod
    def _at_least_one_server(cls, v: list[ServerConfig]) -> list[ServerConfig]:
        if not v:
            raise ValueError("drifter.yaml must declare at least one server under `servers:`")
        return v


class ConfigError(ValueError):
    """drifter.yaml is missing, malformed, or fails validation.

    A plain pydantic.ValidationError traceback isn't an "actionable
    error" (CLAUDE.md's non-negotiable invariant for drifter doctor,
    F-37) — wrapping it here means every caller gets a message a user
    can act on without reading a stack trace, even before doctor exists.
    """


def load_config(path: Path | None = None) -> DrifterConfig:
    path = path or Path("drifter.yaml")
    if not path.exists():
        raise ConfigError(
            f"{path} not found. `drifter observe` needs a drifter.yaml with at least one "
            "server defined under `servers:` (docs/SPEC.md §11) — see drifter.yaml at the repo "
            "root for the expected shape."
        )
    with path.open("r", encoding="utf-8") as f:
        try:
            data = yaml.safe_load(f) or {}
        except yaml.YAMLError as e:
            raise ConfigError(f"{path} is not valid YAML: {e}") from e
    try:
        return DrifterConfig.model_validate(data)
    except ValidationError as e:
        raise ConfigError(f"{path} is invalid: {e}") from e
