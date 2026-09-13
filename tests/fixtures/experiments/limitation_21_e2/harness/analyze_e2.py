"""Checks E2's pre-registered acceptance (PREREGISTRATION.txt) from the records.

Per run: validity is Drifter's own verdict (aggregate_baseline_runs); the
get_order argument names, fault, tier and provenance come from the session
records; the answer comes from the captured stdout.
"""
import json
import re
from pathlib import Path

from mcp_drifter.evaluate.assertions import TaskAssertions, evaluate_task
from mcp_drifter.evaluate.baseline import aggregate_baseline_runs
from mcp_drifter.record.reader import read_session
from mcp_drifter.record.schema import ToolCall

ROOT = Path(__file__).resolve().parent
RUN = ROOT / ".drifter" / "runs" / "run" / "e2"
ORACLE = TaskAssertions(calls=("find_order", "get_order"), answer_matches=r"(?i)\b1,?240\b")

for arm in ("baseline", "mutated"):
    sessions = sorted((RUN / arm).glob("*.jsonl"))
    agg = aggregate_baseline_runs(arm, sessions)
    valid = set(agg.valid_session_paths)
    print(f"\n=== {arm}: {len(sessions)} run(s), {agg.valid_runs} valid, coverage {agg.baseline_fidelity}")
    print(f"    provenance {agg.provenance_breakdown}")
    for ex in agg.excluded_runs:
        print(f"    excluded {ex.session_id[:8] if ex.session_id else '?'}: {ex.reason}")
    for s in sessions:
        calls = [c for c in read_session(s) if isinstance(c, ToolCall)]
        trail = " > ".join(
            f"{c.tool_name}({','.join(c.arguments)}){'!' if c.fault else ''}[{c.match_tier or '-'}/{c.result_provenance}]"
            for c in calls
        )
        answer = s.with_suffix(".stdout.txt")
        text = " ".join(answer.read_text(encoding="utf-8").split()) if answer.exists() else "(no answer)"
        print(f"  {s.stem[:8]} valid={s in valid}")
        print(f"     {trail}")
        print(f"     answer: {text[-160:]}")
    task = evaluate_task(list(valid), ORACLE)
    print(f"    TASK {task.verdict}: {task.runs_passed}/{task.runs_evaluated} passed")
    if arm == "mutated":
        get_orders = [c for s in sessions for c in read_session(s) if isinstance(c, ToolCall) and c.tool_name == "get_order"]
        uses_new = all("orderId" in c.arguments and "order_id" not in c.arguments for c in get_orders)
        print(f"    get_order calls: {len(get_orders)}; all use orderId: {uses_new}")
        print(
            "    ACCEPTANCE: "
            f"valid>=3: {agg.valid_runs >= 3}; "
            f"every valid run PASS: {task.runs_evaluated == agg.valid_runs and task.runs_passed == agg.valid_runs and agg.valid_runs > 0}; "
            f"orderId on every get_order: {uses_new and bool(get_orders)}"
        )

audit = RUN / "mutations.jsonl"
if audit.exists():
    renamed = [json.loads(l) for l in audit.read_text(encoding="utf-8").splitlines() if json.loads(l).get("inverse")]
    print("\nmutation audit (renames):", [(r.get("target"), r.get("before"), r.get("after")) for r in renamed])
