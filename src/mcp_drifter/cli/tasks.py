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
from mcp_drifter.mine.prefixspan import PatternLimitError, mine_patterns, supporting_tools
from mcp_drifter.mine.signature import group_signatures, load_corpus_trajectories
from mcp_drifter.policy.classify import classify_manifest
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


def _read(path: Path) -> str:
    """Read WITHOUT newline translation. `Path.read_text` turns CRLF into LF, so writing the
    text back changed every line ending of a file the user had touched in one place --
    "your edits survive byte for byte" was false for every line."""
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def _write(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)


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
        config = load_config(config_path, merge_tasks=False)
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
    try:
        patterns = mine_patterns(
            groups,
            min_support=settings.min_support,
            min_length=settings.min_length,
            max_length=settings.max_length,
            max_patterns=settings.max_patterns,
        )
    except PatternLimitError as exc:
        raise ConfigError(str(exc)) from exc
    out.write(
        f"TASK MINING  {total} trajectories from {corpus.sessions} session(s) on {server!r}\n"
        f"             {len(groups)} distinct workflow(s); {len(patterns)} recurring pattern(s) "
        f"(support >= {settings.min_support}, length {settings.min_length}-{settings.max_length})\n"
    )
    if corpus.replayed_sessions:
        out.write(
            f"             {corpus.replayed_sessions} session(s) recorded by `drifter replay-serve` were "
            "skipped: they record an agent being replayed, not what it does\n"
        )
    if corpus.unsegmented_calls:
        out.write(
            f"             {corpus.unsegmented_calls} call(s) fell outside any recorded "
            "trajectory and were not mined\n"
        )

    existing_text = _read(path) if path.exists() else None
    try:
        existing = read_candidates(existing_text) if existing_text is not None else None
    except CandidateFileError as exc:
        raise ConfigError(f"{path} is invalid: {exc}") from exc

    if not patterns:
        # Distinguish "nothing recurs" from "something recurs but is shorter than min_length":
        # on real data a one-call task repeated 46 times produced the first message, which
        # sent the user to the wrong setting.
        too_short = (
            mine_patterns(
                groups,
                min_support=settings.min_support,
                min_length=1,
                max_length=settings.max_length,
                max_patterns=settings.max_patterns,
            )
            if settings.min_length > 1
            else []
        )
        if too_short:
            shown = ", ".join(" -> ".join(p.items) + f" (x{p.support})" for p in too_short[:3])
            out.write(
                f"             {len(too_short)} workflow(s) recur but are shorter than "
                f"`mine.min_length` ({settings.min_length}), so nothing was proposed: {shown}. "
                "A one-call task is a workflow too: set `mine.min_length: 1` in calibration.yaml "
                "to propose them.\n"
            )
        else:
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
    # Pre-fill `never_calls` with what the risk classification calls destructive (plus the
    # user's own `policy.destructive`), except tools this workflow is seen alongside. Only
    # destructive: a tool the classifier could not place is not asserted against, and an
    # irreversible write may be exactly what a task is for.
    override = list(config.policy.destructive) if config is not None else []
    destructive = sorted(
        name
        for name, classification in classify_manifest(list(corpus.manifest.values()), override).items()
        if classification.risk == "destructive"
    )
    never_calls = {
        p.items: [t for t in destructive if t not in supporting_tools(p.items, groups)] for p in fresh
    }
    entries = build_entries(fresh, total_trajectories=total, taken_ids=taken, never_calls=never_calls)

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
        _write(path, new_text)
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
            if entry["assert"]["never_calls"]:
                out.write(
                    "    never_calls (pre-filled from tool risk): "
                    + ", ".join(entry["assert"]["never_calls"])
                    + "\n"
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
    config = load_config(config_path, merge_tasks=False)
    configured = resolve_tasks_file(config, config_path)
    path = file if file is not None else configured
    if not path.exists():
        raise ConfigError(f"{path} not found. Run `drifter tasks mine` first to write candidates.")

    text = _read(path)
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

    _write(path, new_text)
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
