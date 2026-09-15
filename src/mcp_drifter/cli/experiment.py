"""Experiment identity (docs/PHASES.md R2).

Before R2 a `drifter run`'s sessions lived in `run/<task_id>/`, keyed by task id
alone. The report, the safety scan, the mutation audit and the aggregation each
read that directory as ONE experiment, so a rerun mixed into its predecessor, and
`--force` "fixed" that by deleting the predecessor. Reports also rebuilt policy
and assertions from whatever the config said at report time, and lost both when
`--runs-dir` was passed without a config.

Now every invocation gets `run/<task_id>/<experiment_id>/`, and `experiment.json`
is written there before any agent runs. It binds the sessions to the corpus (by
content hash), the assertions, policy and calibration actually used, and the run
parameters. Environment fingerprints live in the sessions themselves and are
enforced by `evaluate/baseline.py`.

A pre-R2 directory, with sessions directly under `run/<task_id>/` and no
`experiment.json`, is still read as a single experiment with no id.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import secrets
from collections.abc import Sequence
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from mcp_drifter.cli.config import ConfigError
from mcp_drifter.evaluate.assertions import TaskAssertions
from mcp_drifter.record.redact import redact_secrets

EXPERIMENT_FILE = "experiment.json"
_ARM_DIRS = ("baseline", "mutated")


def new_experiment_id(now: datetime | None = None) -> str:
    """Sortable by creation time (UTC, second resolution), unique within the second."""
    now = now or datetime.now(timezone.utc)
    return f"{now.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _drifter_version() -> str:
    try:
        return version("mcp-drifter")
    except PackageNotFoundError:
        return "unknown"


def write_experiment_settings(
    experiment_dir: Path,
    *,
    task_id: str,
    operator: str,
    seed: int,
    repeats: int,
    prompt: str,
    server: str,
    agent_command: Sequence[str],
    agent_mode: str,
    corpus_paths: Sequence[Path],
    response_fixture: Path | None,
    assertions: TaskAssertions,
    policy,
    calibration,
    adaptive: bool,
    budget: int | None,
    max_wall_time_s: float | None,
    replay_discovered_values: bool,
) -> dict:
    settings = {
        "experiment_id": experiment_dir.name,
        "task_id": task_id,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "drifter_version": _drifter_version(),
        "operator": operator,
        "seed": seed,
        "repeats": repeats,
        "prompt": prompt,
        "server": server,
        # An agent command can carry credentials in its argv; stored with the
        # same secret-shape redaction the recorder applies to arguments.
        "agent_command": redact_secrets(list(agent_command)),
        "agent_mode": agent_mode,
        "corpus": [{"path": str(p), "sha256": _sha256(p)} for p in corpus_paths],
        "response_fixture": (
            {"path": str(response_fixture), "sha256": _sha256(response_fixture)} if response_fixture else None
        ),
        "assertions": dataclasses.asdict(assertions),
        "policy": policy.model_dump(mode="json"),
        "calibration": calibration.model_dump(mode="json"),
        "adaptive": adaptive,
        "budget": budget,
        "max_wall_time_s": max_wall_time_s,
        "replay_discovered_values": replay_discovered_values,
    }
    experiment_dir.mkdir(parents=True, exist_ok=True)
    (experiment_dir / EXPERIMENT_FILE).write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return settings


def load_experiment_settings(experiment_dir: Path) -> dict | None:
    path = experiment_dir / EXPERIMENT_FILE
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def assertions_from_settings(data: dict) -> TaskAssertions:
    """Rebuilds the frozen dataclass from its JSON form (lists back to tuples)."""
    return TaskAssertions(
        calls=tuple(data.get("calls", ())),
        calls_before=tuple(tuple(pair) for pair in data.get("calls_before", ())),
        never_calls=tuple(data.get("never_calls", ())),
        result_has_keys={tool: tuple(keys) for tool, keys in (data.get("result_has_keys") or {}).items()},
        no_errors=bool(data.get("no_errors", False)),
        answer_matches=data.get("answer_matches"),
    )


def _is_experiment_dir(path: Path) -> bool:
    return (path / EXPERIMENT_FILE).exists() or any((path / arm).is_dir() for arm in _ARM_DIRS)


def resolve_experiment_dir(runs_dir: Path, task_id: str, experiment: str | None = None) -> Path:
    """The directory a report reads: the named experiment, else the latest one,
    else a pre-R2 task directory that holds sessions directly."""
    task_dir = runs_dir / "run" / task_id
    if not task_dir.exists():
        raise ConfigError(
            f"no recorded `drifter run` found for task {task_id!r} under {runs_dir} "
            f"(expected {task_dir}) — run `drifter run --task-id {task_id}` first."
        )
    experiments = sorted(
        p for p in task_dir.iterdir() if p.is_dir() and p.name not in _ARM_DIRS and _is_experiment_dir(p)
    )
    if experiment is not None:
        target = task_dir / experiment
        if target in experiments:
            return target
        available = ", ".join(p.name for p in experiments) or "(none)"
        raise ConfigError(f"no experiment {experiment!r} for task {task_id!r}; available: {available}")
    if experiments:
        return experiments[-1]
    if _is_experiment_dir(task_dir):
        return task_dir
    raise ConfigError(
        f"no recorded `drifter run` found for task {task_id!r} under {runs_dir} "
        f"(expected {task_dir}) — run `drifter run --task-id {task_id}` first."
    )
