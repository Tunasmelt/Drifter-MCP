"""`drifter score` (F-36), docs/SPEC.md §12/§13. Gate 2's actual exit test
(docs/PHASES.md): re-analyzes already-recorded session JSONL with zero new
agent execution and zero network/subprocess calls, producing output in
seconds against a corpus of any age.

Module: `cli/` — same `## Module: cli/ and adapters` heading F-34
resolved to earlier this gate; F-36 is filed there in docs/FEATURES.md's own
list, unambiguous this time (no stale/typo'd module name to correct).

Zero execution, structurally, not just by what this module happens to
do today: this file imports only `argparse`/`os`/`sys`/`pathlib`, this
project's own `cli.config`/`cli.stats`/`evaluate.baseline`/
`record.calibration`/`record.reader` modules, and nothing from
`cli.subprocess_adapter`, `replay.replay_proxy`, or any MCP client/
server package. There is no code path here that could spawn a process
or open a connection, because the code to do so does not exist in this
file — same standard as `replay_proxy.py`'s and `subprocess_adapter.py`'s
own no-live-connection guarantees, and `test_score.py` asserts this
holds by inspecting the module's actual imports, not by trusting this
paragraph.

Task grouping — checked before writing anything, not assumed: does
anything today tie a recorded session to the task that produced it?
No. `task_id` is a caller-supplied string `run_baseline`/
`aggregate_baseline_runs` take as a parameter; it is never written into
`SessionStart` or anywhere else in the recorded schema
(`record/schema.py`). A directory of session JSONL files carries no
on-disk signal distinguishing "these three sessions are repeats of the
same task" from "these three sessions are three unrelated tasks that
happened to land in the same runs directory." Per this prompt's own
instruction not to invent a grouping scheme as a side effect: this
command's scope is deliberately minimal — every session under the
resolved runs directory is aggregated as ONE baseline group. This is a
real, stated limitation (surfaced in both this docstring and the
command's own output, not just here), not a hidden assumption; a real
task-grouping key (recorded at write time, e.g. into `SessionStart` or
a sibling manifest) is a separate, later feature.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TextIO

from cli.config import DrifterConfig, load_config
from cli.stats import resolve_runs_dir
from evaluate.baseline import BaselineResult, aggregate_baseline_runs
from record.calibration import Calibration, load_calibration

_NO_GROUPING_TASK_ID = "(all sessions — no task-grouping key exists yet, see cli/score.py)"


def render_score(result: BaselineResult, runs_dir: Path) -> str:
    lines: list[str] = []
    lines.append(f"drifter score — {runs_dir}")
    lines.append(
        "NOTE: no task-grouping key exists in the recorded schema yet — every session "
        "under this directory is aggregated as one baseline group, not split by task."
    )
    lines.append("")

    if result.total_runs == 0:
        lines.append("No sessions found.")
        return "\n".join(lines) + "\n"

    lines.append(f"sessions: {result.total_runs}  valid: {result.valid_runs}  excluded: {len(result.excluded_runs)}")
    lines.append("")

    if not result.has_data:
        # N/A, not 0/None rendered as if they were real answers — same
        # discipline as cli/stats.py's error_rate/fault_rate "N/A" when
        # nothing known, not "0.0%".
        lines.append("dominant_path:      N/A (no valid sessions)")
        lines.append("natural_variation:  N/A")
        lines.append("baseline_spread:    N/A")
        lines.append("baseline_fidelity:  N/A")
    else:
        path_display = " → ".join(result.dominant_path) if result.dominant_path else "(no tool calls)"
        lines.append(f"dominant_path:      {path_display}")
        lines.append(f"natural_variation:  {result.natural_variation:.3f}")
        lines.append(f"baseline_spread:    {result.baseline_spread:.3f}")
        lines.append(f"baseline_fidelity:  {result.baseline_fidelity:.3f}")
        if len(result.variant_frequencies) > 1:
            lines.append("")
            lines.append("variants:")
            for path, count in sorted(result.variant_frequencies.items(), key=lambda kv: kv[1], reverse=True):
                path_str = " → ".join(path) if path else "(no tool calls)"
                lines.append(f"  {count:>3}x  {path_str}")

    if result.excluded_runs:
        lines.append("")
        lines.append("EXCLUDED RUNS:")
        for run in result.excluded_runs:
            label = run.session_id or (str(run.path) if run.path is not None else "<no session>")
            lines.append(f"  {label}: {run.reason}")

    return "\n".join(lines) + "\n"


def run_score(
    config_path: Path | None = None,
    runs_dir: Path | None = None,
    calibration: Calibration | None = None,
    output_stream: TextIO = sys.stdout,
) -> None:
    """Reads every `*.jsonl` session under the resolved runs directory
    and aggregates them as one baseline group (see this module's
    docstring for why "one group" is this prompt's deliberate, stated
    scope). Zero execution: `aggregate_baseline_runs` never calls
    anything that spawns a process or opens a connection — see this
    module's own docstring and `test_score.py`'s structural import
    check.
    """
    if runs_dir is None:
        config: DrifterConfig | None = load_config(config_path)
        runs_dir = resolve_runs_dir(config)

    calibration = calibration or load_calibration()
    session_paths = sorted(runs_dir.glob("*.jsonl")) if runs_dir.exists() else []
    result = aggregate_baseline_runs(_NO_GROUPING_TASK_ID, session_paths, calibration=calibration)
    output_stream.write(render_score(result, runs_dir))
