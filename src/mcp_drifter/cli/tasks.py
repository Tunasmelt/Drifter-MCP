"""`drifter tasks mine` and `drifter tasks approve` (F-28-F-30, docs/SPEC.md §12).

`mine` reads the recorded corpus, groups trajectories into signatures (F-28), mines
recurring workflows (F-29) and writes them as editable candidates (F-30). `approve`
promotes one the user has edited. Approved candidates are then ordinary tasks:
`cli/config.py`'s `load_config` merges them into `config.tasks`.

Mining proposes; it never decides. It cannot recover what the agent was ASKED from a
sequence of calls, so a candidate's `prompt` starts empty and `approve` refuses until
the user has written one. Both commands read only recorded sessions -- no server is
contacted, no agent is run, no recording is modified.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TextIO

from pydantic import ValidationError

from mcp_drifter.cli.config import (
    ConfigError,
    DrifterConfig,
    approved_task_from_entry,
    load_config,
    resolve_tasks_file,
)
from mcp_drifter.cli.stats import resolve_runs_dir
from mcp_drifter.mine.candidates import (
    CandidateFileError,
    append_entries,
    approve_in_text,
    approved_entries,
    build_entries,
    read_candidates,
    render_file,
    uncovered_tools,
)
from mcp_drifter.mine.prefixspan import mine_patterns
from mcp_drifter.mine.signature import group_signatures, load_corpus_trajectories
from mcp_drifter.record.calibration import Calibration, load_calibration


def _only_server(config: DrifterConfig | None, server: str | None) -> str:
    if server is not None:
        return server
    if config is None or not config.servers:
        raise ConfigError("No server given and none found in config — pass --server.")
    if len(config.servers) > 1:
        names = ", ".join(s.name for s in config.servers)
        raise ConfigError(f"More than one server is configured ({names}) — pass --server to choose one.")
    return config.servers[0].name


def _coverage_lines(tools: tuple[str, ...], approved: list[dict]) -> list[str]:
    uncovered = uncovered_tools(tools, approved)
    if not tools:
        return []
    return [
        f"TOOLS IN NO APPROVED TASK ({len(uncovered)}): "
        + (", ".join(uncovered) if uncovered else "none — every tool is asserted by an approved task")
    ]


def run_tasks_mine(
    config_path: Path | None = None,
    runs_dir: Path | None = None,
    server: str | None = None,
    output: Path | None = None,
    calibration: Calibration | None = None,
    output_stream: TextIO = sys.stdout,
) -> None:
    config: DrifterConfig | None = None
    if runs_dir is None or server is None or output is None:
        config = load_config(config_path)
    if runs_dir is None:
        runs_dir = resolve_runs_dir(config, config_path)
    server = _only_server(config, server)
    path = output if output is not None else resolve_tasks_file(config, config_path)
    settings = (calibration or load_calibration()).mine

    session_paths = sorted(runs_dir.glob("*.jsonl")) if runs_dir.exists() else []
    if not session_paths:
        raise ConfigError(f"No recorded sessions found in {runs_dir}. Run `drifter observe` first.")

    corpus = load_corpus_trajectories(session_paths, server=server)
    total = len(corpus.trajectories)
    out = output_stream
    if not total:
        out.write(
            f"TASK MINING  no trajectories on {server!r} in {runs_dir} "
            f"({corpus.sessions} session(s) matched that server).\n"
        )
        return

    groups = group_signatures(corpus.trajectories)
    patterns = mine_patterns(
        groups,
        min_support=settings.min_support,
        min_length=settings.min_length,
        max_length=settings.max_length,
    )
    out.write(
        f"TASK MINING  {total} trajectories from {corpus.sessions} session(s) on {server!r}\n"
        f"             {len(groups)} distinct workflow(s); {len(patterns)} recurring pattern(s) "
        f"(support >= {settings.min_support}, length {settings.min_length}-{settings.max_length})\n"
    )
    if corpus.unsegmented_calls:
        out.write(
            f"             {corpus.unsegmented_calls} call(s) fell outside any recorded "
            "trajectory and were not mined\n"
        )

    existing_text = path.read_text(encoding="utf-8") if path.exists() else None
    try:
        existing = read_candidates(existing_text) if existing_text is not None else None
    except CandidateFileError as exc:
        raise ConfigError(f"{path} is invalid: {exc}") from exc

    if not patterns:
        out.write(
            f"             no workflow recurs in at least {settings.min_support} trajectories, so "
            "nothing was proposed. Record more sessions of the same task, or lower "
            "`mine.min_support` in calibration.yaml.\n"
        )
        approved = approved_entries(existing) if existing is not None else []
        for line in _coverage_lines(corpus.tools, approved):
            out.write(line + "\n")
        return

    listed = {tuple(e.get("pattern") or ()) for e in existing.entries} if existing else set()
    fresh = [p for p in patterns if p.items not in listed][: settings.max_candidates]
    taken = {e["id"] for e in existing.entries} if existing else set()
    entries = build_entries(fresh, total_trajectories=total, taken_ids=taken)

    try:
        if existing_text is None:
            new_text = render_file(server, entries)
        else:
            new_text = append_entries(existing_text, server, entries)
    except CandidateFileError as exc:
        raise ConfigError(f"{path}: {exc}") from exc

    if existing_text is not None and new_text == existing_text:
        out.write(f"             no new candidates: every recurring pattern is already in {path}\n")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_text, encoding="utf-8")
        verb = "wrote" if existing_text is None else "appended"
        out.write(f"             {verb} {len(entries)} candidate(s) to {path}\n\nCANDIDATES\n")
        for entry in entries:
            share = 100 * entry["support"] // entry["of_trajectories"]
            out.write(
                f"  {entry['id']}\n"
                f"    in {entry['support']} of {entry['of_trajectories']} trajectories ({share}%), "
                f"{entry['sessions']} session(s)\n"
                f"    {' -> '.join(entry['pattern'])}\n"
            )
        out.write(
            f"\nNEXT  write `prompt` (and review `assert`) in {path}, then "
            "`drifter tasks approve <id>`. Nothing is a task until you do.\n"
        )

    doc = read_candidates(new_text)
    for line in _coverage_lines(corpus.tools, approved_entries(doc)):
        out.write(line + "\n")


def run_tasks_approve(
    candidate: str,
    config_path: Path | None = None,
    file: Path | None = None,
    output_stream: TextIO = sys.stdout,
) -> None:
    config = load_config(config_path)
    configured = resolve_tasks_file(config, config_path)
    path = file if file is not None else configured
    if not path.exists():
        raise ConfigError(f"{path} not found. Run `drifter tasks mine` first to write candidates.")

    text = path.read_text(encoding="utf-8")
    try:
        doc = read_candidates(text)
        new_text = approve_in_text(text, candidate)
    except CandidateFileError as exc:
        raise ConfigError(f"{path}: {exc}") from exc

    entry = next(e for e in doc.entries if e["id"] == candidate)
    if not str(entry.get("prompt") or "").strip():
        raise ConfigError(
            f"candidate {candidate!r} has no `prompt`. Mining cannot know what the agent should be "
            f"asked — write it in {path}, review `assert`, then approve again."
        )
    if any(t.id == candidate for t in config.tasks):
        raise ConfigError(
            f"task id {candidate!r} is already defined under `tasks:` in your drifter.yaml; "
            "rename the candidate's `id` first."
        )
    try:
        task = approved_task_from_entry(entry)
    except ValidationError as exc:
        raise ConfigError(f"candidate {candidate!r} has an invalid `assert` block: {exc}") from exc

    path.write_text(new_text, encoding="utf-8")
    out = output_stream
    out.write(
        f"APPROVED  {task.id}\n"
        f"          prompt: {task.prompt}\n"
        f"          `drifter run --task-id {task.id} ...` now uses this prompt and its assertions.\n"
    )
    if path.resolve() != configured.resolve():
        out.write(
            f"NOTE      {path} is not this project's configured tasks_file ({configured}), so "
            f"`drifter run` will not see it until you set `tasks_file: {path}` in drifter.yaml.\n"
        )
