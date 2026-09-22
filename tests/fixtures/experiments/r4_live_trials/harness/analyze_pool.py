"""R4 live trials: score resampled A/A splits of a recorded pool (see PREREGISTRATION.txt).

Usage: python analyze_pool.py <orders|econ>
Reads only files on disk; zero agent execution.
"""
from __future__ import annotations

import json
import random
import sys
from collections import Counter
from pathlib import Path

from mcp_drifter.cli.config import assertions_for, load_config
from mcp_drifter.evaluate.assertions import evaluate_task
from mcp_drifter.evaluate.baseline import BaselineResult, aggregate_baseline_runs
from mcp_drifter.evaluate.effect_size import PATH_SOURCE_CORPUS, compute_behavior_effect_size, path_of_interest_from_sessions
from mcp_drifter.record.calibration import load_calibration
from mcp_drifter.record.reader import read_session
from mcp_drifter.record.schema import ToolCall

sys.path.insert(0, str(Path(__file__).resolve().parent))
from record_pool import TASKS  # noqa: E402

ROOT = Path(__file__).resolve().parent
SPLITS, ARM, SEED, BAR = 2000, 20, 20260915, 0.05


def _arm(paths: list[tuple[str, ...]]) -> BaselineResult:
    counts = Counter(paths)
    return BaselineResult(
        task_id="split", total_runs=len(paths), valid_runs=len(paths),
        dominant_path=max(counts, key=lambda p: counts[p]), variant_frequencies=dict(counts),
        natural_variation=0.0, baseline_spread=0.0, baseline_fidelity=1.0, excluded_runs=[],
    )


def main(name: str) -> None:
    spec = TASKS[name]
    ws = spec["workspace"]
    calibration = load_calibration()
    pool = sorted((ROOT / name / "pool").glob("*.jsonl"))
    aggregate = aggregate_baseline_runs(name, pool, calibration=calibration)
    valid = list(aggregate.valid_session_paths)
    corpus = sorted((ws / ".drifter" / "runs").glob("*.jsonl"))
    path_of_interest = path_of_interest_from_sessions(corpus, spec["server"])
    per_run = [tuple(c.tool_name for c in read_session(s) if isinstance(c, ToolCall)) for s in valid]
    config = load_config(ws / "drifter.yaml")
    oracle = evaluate_task(valid, assertions_for(config.tasks, spec["task"]))

    report = {
        "task": name,
        "pool_runs": len(pool),
        "valid_runs": len(valid),
        "exclusions": Counter(e.reason.split(":")[0] for e in aggregate.excluded_runs),
        "path_of_interest": " > ".join(path_of_interest or ()),
        "path_distribution": {" > ".join(p) or "(none)": n for p, n in Counter(per_run).most_common()},
        "measured_p_on_path": (sum(1 for p in per_run if p == path_of_interest) / len(per_run)) if per_run else None,
        "task_oracle": f"{oracle.verdict} {oracle.runs_passed}/{oracle.runs_evaluated}",
    }
    if len(valid) < 2 * ARM:
        report["splits"] = f"not measurable: {len(valid)} valid runs < {2 * ARM}"
    else:
        rng = random.Random(SEED)
        verdicts = Counter()
        for _ in range(SPLITS):
            order = rng.sample(range(len(per_run)), 2 * ARM)
            base = _arm([per_run[i] for i in order[:ARM]])
            mut = _arm([per_run[i] for i in order[ARM:]])
            verdicts[compute_behavior_effect_size(
                base, mut, calibration=calibration, path_of_interest=path_of_interest, path_source=PATH_SOURCE_CORPUS,
            ).verdict] += 1
        rates = {k: verdicts[k] / SPLITS for k in ("REGRESSION", "INCONCLUSIVE", "NO_REGRESSION", "UNKNOWN")}
        report["split_rates"] = rates
        report["acceptance_false_regression_le_5pct"] = rates["REGRESSION"] <= BAR
    (ROOT / name / "analysis.json").write_text(json.dumps(report, indent=2, default=dict) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, default=dict))


if __name__ == "__main__":
    main(sys.argv[1])
