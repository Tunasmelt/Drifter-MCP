"""Minimal drifter.yaml config loader (SPEC.md §11).

`servers:`, `record.dir` (F-09), and now `agent.command` (F-35,
`drifter run`) — the full config surface (`tasks:`, `mutations:`,
`policy:`) is still later-gate scope per PHASES.md; this loader is
deliberately narrow, not the final shape. `extra="allow"` on every
model means a drifter.yaml already written with later-gate blocks in
it won't be rejected — those blocks just aren't read yet.

`agent.command` is a list of argv tokens, NOT SPEC.md §11's example
shell string (`"python agent.py --task '{task.prompt}'"`) — matching
`ServerConfig.command`'s existing convention (also a list) rather than
inventing shell-parsing (shlex) for one field and not the other. A
deliberate, small deviation from the example syntax, not a new format:
`{task.prompt}` still templates per-token (`cli/run.py`), just without
a shell-quoting step in between. No `mode` field — F-34's subprocess
adapter is the only agent adapter that exists; nothing branches on it
yet, and adding an unused field now would be exactly the kind of
speculative surface CLAUDE.md's simplicity principle warns against.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    command: list[str]

    @field_validator("command")
    @classmethod
    def _command_not_empty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("server command must have at least one element (the executable)")
        return v


class RecordConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    dir: str = ".drifter/runs"
    redact: str = "shape"


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    command: list[str]

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
            "server defined under `servers:` (SPEC.md §11) — see drifter.yaml at the repo "
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
