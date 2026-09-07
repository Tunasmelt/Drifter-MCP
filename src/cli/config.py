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
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from evaluate.assertions import TaskAssertions


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


class PolicyConfig(BaseModel):
    """F-26/F-25 (docs/SPEC.md §10/§11): `destructive` is the tier-4 USER
    OVERRIDE `policy/classify.py` reads — a tool name listed here is
    classified `destructive` unconditionally, regardless of what MCP
    annotations, name heuristics, or observed behavior would otherwise
    say. Deliberately the highest-priority tier, not the lowest, despite
    being listed last in docs/SPEC.md §10's own prose ("MCP annotations →
    name/schema heuristics → observed behavior → user policy override") —
    a real ambiguity in that text, resolved explicitly (docs/CHANGELOG.md):
    "override" only means something if it wins over the automated tiers,
    not if it's itself the last, weakest fallback. `confirmation_required`
    is a separate, later (F-25) safety control, not a classification
    input — parsed here now so both fields exist together, matching
    docs/SPEC.md §11's own example, but not yet consumed by anything
    (`policy/` doesn't have a safety-verdict module until F-25).
    """

    model_config = ConfigDict(extra="allow")

    destructive: list[str] = []
    confirmation_required: list[str] = []


class TaskAssertConfig(BaseModel):
    """docs/SPEC.md §8's Task-axis oracle, as authored in `drifter.yaml`
    (F-24). Every field optional — a task with none of them set is a task
    with no oracle, which yields UNKNOWN rather than a vacuous PASS.

    `result_contains` from docs/SPEC.md §8's own list is deliberately absent
    and is REJECTED with an actionable message rather than silently
    ignored: recording is shape-only by design (F-02/F-04, a docs/SPEC.md §3
    non-negotiable), so no recorded session carries the payload such an
    assertion would need to read. `result_has_keys` and `no_errors` are the
    recordable checks offered in its place — see
    `evaluate/assertions.py`'s module docstring and docs/CHANGELOG.md.
    """

    model_config = ConfigDict(extra="allow")

    calls: list[str] = []
    # Each entry is a two-element [earlier, later] pair.
    calls_before: list[list[str]] = []
    never_calls: list[str] = []
    result_has_keys: dict[str, list[str]] = {}
    no_errors: bool = False

    @field_validator("calls_before")
    @classmethod
    def _pairs_are_pairs(cls, v: list[list[str]]) -> list[list[str]]:
        for pair in v:
            if len(pair) != 2:
                raise ValueError(
                    f"each calls_before entry must be exactly [earlier, later]; got {pair!r} "
                    f"with {len(pair)} element(s)"
                )
        return v

    @model_validator(mode="after")
    def _reject_unevaluable_result_contains(self) -> "TaskAssertConfig":
        """`extra="allow"` (this project's convention, so a config written
        against a later version still loads) would otherwise accept
        `result_contains` and silently never check it — a user would
        reasonably believe their assertion was being evaluated when nothing
        was reading it. Silently ignoring an authored assertion is precisely
        the "doesn't crash, just calmly reports success" failure mode the
        Task axis's own UNKNOWN-by-default invariant exists to prevent, so
        this one unevaluable key is rejected loudly and by name.
        """
        if "result_contains" in (self.model_extra or {}):
            raise ValueError(
                "`result_contains` cannot be evaluated: Drifter records result SHAPE only "
                "(type/keys/array lengths), never payloads — a docs/SPEC.md §3 invariant, not a "
                "gap. Use `result_has_keys: {tool: [key, ...]}` to assert on the recorded shape, "
                "or `no_errors: true` to assert no call reported is_error."
            )
        return self


class TaskConfig(BaseModel):
    """One authored task: what to ask the agent, and what success means.

    Fills in docs/SPEC.md §11's `tasks: [...]`, which had been listed in the
    config schema since the spec was written but never given a real shape
    (`cli/run.py`'s own docstring recorded that gap). `id` is what
    `drifter run --task-id` selects; `prompt` is what gets substituted into
    `agent.command`'s `{task.prompt}`, so an authored task no longer needs
    `--prompt` passed alongside it.

    This is deliberately NOT mining-derived task discovery — F-24's
    docs/FEATURES.md entry lists "Depends on: task definitions (F-30)",
    which is about where task CANDIDATES come from automatically. Authoring
    one by hand needs no mining at all, and that dependency was blocking a
    whole verdict axis on an unrelated unbuilt feature.
    """

    model_config = ConfigDict(extra="allow")

    id: str
    prompt: str = ""
    assert_: TaskAssertConfig = Field(default_factory=TaskAssertConfig, alias="assert")

    def assertions(self) -> TaskAssertions:
        """This authored task's oracle, as `evaluate/assertions.py`'s own
        types. Lives here rather than in either command so `drifter run` and
        `drifter report` cannot drift apart in how they read a task — a
        report that silently disagreed with the run it re-renders would be
        worse than one that refused to render. Deliberately NOT in
        `cli/run.py`: `cli/report.py` needs it too and must never import
        `cli.run` (that path transitively reaches real subprocess-spawning
        code, breaking its zero-execution guarantee — `test_report.py`
        asserts this by AST).
        """
        return TaskAssertions(
            calls=tuple(self.assert_.calls),
            calls_before=tuple((pair[0], pair[1]) for pair in self.assert_.calls_before),
            never_calls=tuple(self.assert_.never_calls),
            result_has_keys={tool: tuple(keys) for tool, keys in self.assert_.result_has_keys.items()},
            no_errors=self.assert_.no_errors,
        )


def find_task(tasks: list[TaskConfig], task_id: str) -> TaskConfig | None:
    """The authored task `task_id` names, or None if this is a bare label.

    Not an error when nothing matches: `--task-id` predates authored tasks
    and has always been usable as a free-form label alongside `--prompt`.
    Raising would break every existing invocation for the sake of a feature
    that is opt-in by design.
    """
    return next((t for t in tasks if t.id == task_id), None)


def assertions_for(tasks: list[TaskConfig], task_id: str) -> TaskAssertions:
    """`find_task` + `TaskConfig.assertions()`, or an empty oracle when this
    id names no authored task — the single call both commands use."""
    task = find_task(tasks, task_id)
    return task.assertions() if task is not None else TaskAssertions()


class DrifterConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    version: int
    servers: list[ServerConfig]
    # docs/SPEC.md §11's `tasks: [...]`, real as of F-24. Empty is the
    # honest default: no authored tasks means the Task axis reports
    # UNKNOWN, which is the correct answer rather than a missing feature.
    tasks: list[TaskConfig] = []
    record: RecordConfig = RecordConfig()
    # None (not a default AgentConfig()) since there's no sensible
    # default agent command -- absence must stay distinguishable from
    # "configured to run nothing," matching this project's nullable-
    # field discipline. cli/run.py reports an actionable ConfigError
    # when it's needed but missing, rather than a bare validation
    # traceback (same reasoning as ConfigError's own docstring below).
    agent: AgentConfig | None = None
    # Unlike `agent`, a real, sensible default (empty overrides, nothing
    # requires confirmation) exists and is exactly what "no policy: block
    # written" should mean -- so this stays a default PolicyConfig(), not
    # None, matching RecordConfig's own precedent rather than AgentConfig's.
    policy: PolicyConfig = PolicyConfig()

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
