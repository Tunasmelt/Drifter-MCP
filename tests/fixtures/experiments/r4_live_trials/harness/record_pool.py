"""R4 live trials: record a pool of unmutated runs for one task (see PREREGISTRATION.txt).

Usage: python record_pool.py <orders|econ> [M]
Uses Drifter's own run path: load_corpus + ReplayStore + authored fixture +
make_run_once + run_baseline, serving the ORIGINAL manifest (no mutation).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from mcp_drifter.cli.config import assertions_for, find_task, load_config
from mcp_drifter.cli.run import _template_command
from mcp_drifter.cli.subprocess_adapter import make_run_once
from mcp_drifter.evaluate.baseline import run_baseline
from mcp_drifter.record.calibration import load_calibration
from mcp_drifter.replay.authored_responses import load_authored_responses
from mcp_drifter.replay.corpus import load_corpus
from mcp_drifter.replay.replay_store import ReplayStore

TASKS = {
    "orders": {"workspace": Path(r"ORDERS_WORKSPACE"), "task": "e2", "server": "orders"},
    "econ": {"workspace": Path(r"ECON_WORKSPACE"), "task": "japan-index", "server": "econ"},
    "variance": {"workspace": Path(r"VARIANCE_WORKSPACE"), "task": "t3", "server": "variance"},
}
ROOT = Path(__file__).resolve().parent


def main(name: str, m: int) -> None:
    spec = TASKS[name]
    ws = spec["workspace"]
    config = load_config(ws / "drifter.yaml")
    task = find_task(config.tasks, spec["task"])
    corpus_paths = sorted((ws / ".drifter" / "runs").glob("*.jsonl"))
    corpus = load_corpus(corpus_paths, spec["server"])
    store = ReplayStore()
    store.index_sessions(corpus.session_paths)
    authored = load_authored_responses(ws / "responses.yaml", spec["server"], corpus.session_paths)
    command = _template_command(config.agent.command, task.prompt)

    pool_dir = ROOT / name / "pool"
    raw_dir = ROOT / name / "pool_raw"
    started = time.time()
    run_once = make_run_once(
        command=command, replay_store=store, server_name=spec["server"], tools_served=list(corpus.tools_served),
        session_dir=pool_dir, raw_dir=raw_dir, timeout_s=240.0, agent_mode=config.agent.mode,
        env_var=config.agent.env_var, authored_responses=authored,
    )
    result = run_baseline(f"r4-{name}-pool", run_once, repeats=m, calibration=load_calibration())
    summary = {
        "task": name, "task_id": spec["task"], "server": spec["server"], "repeats": m,
        "corpus": [str(p) for p in corpus.session_paths],
        "assertions": str(assertions_for(config.tasks, spec["task"])),
        "total_runs": result.total_runs, "valid_runs": result.valid_runs,
        "variant_frequencies": {" > ".join(k) or "(none)": v for k, v in result.variant_frequencies.items()},
        "excluded": [e.reason for e in result.excluded_runs],
        "elapsed_s": round(time.time() - started, 1),
    }
    (ROOT / name / "pool_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 60)
