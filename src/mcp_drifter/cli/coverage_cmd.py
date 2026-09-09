"""`drifter coverage` — the analysis half of the limitation-16 experiment.

Answers two questions from recordings alone, with zero execution and zero
API cost (same discipline as `cli/score.py`):

  1. What fraction of a fresh run's calls will resolve from this corpus?
     (`replay/coverage.py`, DEC-027(c) — leave-one-out, so a generalization
     estimate rather than self-congratulation.)

  2. Is that number going anywhere as the corpus grows? (`--curve`,
     `replay/coverage_curve.py`.)

Question 2 is the one docs/SPEC.md §15 limitation 16 turns on. A curve that
climbs to `calibration.fidelity_floor` means corpus-based replay works and
the remaining cost is recording effort. A curve that flattens below it means
no amount of recording fixes exact/semantic-tier replay for an exploratory
agent, and the honest response is to change the architecture or narrow the
claim.

Exposed as a real command rather than kept as an experiment script because
the question generalizes: any user pointing Drifter at their own agent needs
to know whether their corpus is adequate BEFORE spending real agent runs
finding out, which is precisely the mistake limitation 16 records.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TextIO

from mcp_drifter.cli.config import ConfigError, DrifterConfig, load_config
from mcp_drifter.cli.stats import resolve_runs_dir
from mcp_drifter.record.calibration import Calibration, load_calibration
from mcp_drifter.replay.coverage import estimate_coverage, render_coverage
from mcp_drifter.replay.coverage_curve import coverage_curve, render_curve


def run_coverage(
    config_path: Path | None = None,
    runs_dir: Path | None = None,
    server: str | None = None,
    curve: bool = False,
    samples_per_size: int = 12,
    seed: int = 0,
    calibration: Calibration | None = None,
    output_stream: TextIO = sys.stdout,
) -> None:
    """Renders the point estimate, and the curve when `curve` is set.

    `server` is resolved from `drifter.yaml` when not given, and is
    required rather than guessed when the config defines more than one --
    coverage is per-server (a key includes the server name), so silently
    picking one would produce a number about a corpus the user did not ask
    about.
    """
    config: DrifterConfig | None = None
    if runs_dir is None or server is None:
        config = load_config(config_path)

    if runs_dir is None:
        runs_dir = resolve_runs_dir(config)

    if server is None:
        if config is None or not config.servers:
            raise ConfigError("No server given and none found in config — pass --server.")
        if len(config.servers) > 1:
            names = ", ".join(s.name for s in config.servers)
            raise ConfigError(f"More than one server is configured ({names}) — pass --server to choose one.")
        server = config.servers[0].name

    calibration = calibration or load_calibration()
    session_paths = sorted(runs_dir.glob("*.jsonl")) if runs_dir.exists() else []
    if not session_paths:
        raise ConfigError(f"No recorded sessions found in {runs_dir}. Run `drifter observe` first.")

    estimate = estimate_coverage(session_paths, server)
    output_stream.write(render_coverage(estimate, fidelity_floor=calibration.fidelity_floor))

    if curve:
        output_stream.write("\n")
        output_stream.write(
            render_curve(
                coverage_curve(
                    session_paths,
                    server,
                    samples_per_size=samples_per_size,
                    seed=seed,
                    plateau=calibration.plateau,
                ),
                floor=calibration.fidelity_floor,
            )
        )
