"""docs/PHASES.md R2: experiment identity and reproducibility.

Replaces the `ensure_clean_session_dir` guard, which only refused to mix two
experiments (and deleted the first on `--force`). Found by external review:
`session_dir` was keyed by task id alone, so the report, the safety scan, the
mutation audit and the aggregation each treated every run ever made under one
task id as a single experiment.

Now every `drifter run` gets its own experiment directory,
`run/<task_id>/<experiment_id>/`, and writes `experiment.json` (corpus hashes,
assertions, policy, calibration, operator, seed, repeats) before any agent runs.
Reruns never overwrite or merge; `--dry-run` touches nothing; `drifter report`
rebuilds from the persisted settings, not from whatever the config says today.

Written before implementation and confirmed failing first.
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
from pathlib import Path

import pytest

from mcp_drifter.cli.config import ConfigError
from mcp_drifter.cli.report import build_report_result, run_report
from mcp_drifter.cli.run import run_mutation_comparison, run_run
from mcp_drifter.record.reader import read_session
from mcp_drifter.record.schema import ToolCall

FIXTURES = Path(__file__).parent.parent / "fixtures"
GOLDEN = FIXTURES / "golden_v0.1.jsonl"
AGENT = FIXTURES / "scripted_agent.py"
SERVER = "filesystem"
TASK = "iso_task"


def _config(tmp_path: Path, *, destructive: list[str] | None = None) -> Path:
    calls = [r for r in read_session(GOLDEN) if isinstance(r, ToolCall)][:2]
    command = json.dumps([sys.executable, str(AGENT), *(f"{c.tool_name}|{json.dumps(c.arguments)}" for c in calls)])
    text = (
        "version: 1\nservers:\n  - name: filesystem\n    command: ['echo', 'unused-in-replay-mode']\n"
        f"agent:\n  command: {command}\n"
        f"tasks:\n  - id: {TASK}\n    prompt: list and search\n    assert:\n      calls: [{calls[0].tool_name}]\n"
    )
    if destructive:
        text += "policy:\n  destructive: [" + ", ".join(destructive) + "]\n"
    path = tmp_path / "drifter.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _run(tmp_path: Path, config: Path, **kwargs):
    return run_run(
        config_path=config, fixture=GOLDEN, server_name=SERVER, task_id=TASK, runs_dir=tmp_path / "runs",
        repeats=1, timeout_s=30.0, assume_yes=True, output_stream=io.StringIO(), **kwargs,
    )


def _tree(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*")) if p.is_file()
    } if root.exists() else {}


# --- reruns ------------------------------------------------------------------------


def test_two_runs_of_the_same_task_get_separate_experiments_and_the_first_is_untouched(tmp_path):
    config = _config(tmp_path)
    first = _run(tmp_path, config)
    first_dir = tmp_path / "runs" / "run" / TASK / first.experiment_id
    first_tree = _tree(first_dir)

    second = _run(tmp_path, config)

    assert first.experiment_id != second.experiment_id
    assert _tree(first_dir) == first_tree
    for result in (first, second):
        experiment_dir = tmp_path / "runs" / "run" / TASK / result.experiment_id
        assert len(list((experiment_dir / "baseline").glob("*.jsonl"))) == 1
        assert (experiment_dir / "experiment.json").exists()


def test_force_is_a_no_op_that_never_deletes(tmp_path):
    config = _config(tmp_path)
    first = _run(tmp_path, config)
    first_tree = _tree(tmp_path / "runs" / "run" / TASK / first.experiment_id)

    second = _run(tmp_path, config, force=True)

    assert second.experiment_id != first.experiment_id
    assert _tree(tmp_path / "runs" / "run" / TASK / first.experiment_id) == first_tree


# --- persisted settings ---------------------------------------------------------------


def test_experiment_settings_are_persisted(tmp_path):
    config = _config(tmp_path, destructive=["get_file_info"])
    result = _run(tmp_path, config, seed=7)

    settings = json.loads((tmp_path / "runs" / "run" / TASK / result.experiment_id / "experiment.json").read_text(encoding="utf-8"))

    assert settings["experiment_id"] == result.experiment_id
    assert settings["task_id"] == TASK
    assert (settings["operator"], settings["seed"], settings["repeats"]) == ("description_update", 7, 1)
    assert settings["server"] == SERVER
    assert settings["corpus"] == [{"path": str(GOLDEN), "sha256": hashlib.sha256(GOLDEN.read_bytes()).hexdigest()}]
    assert settings["assertions"]["calls"] == [next(r for r in read_session(GOLDEN) if isinstance(r, ToolCall)).tool_name]
    assert settings["policy"]["destructive"] == ["get_file_info"]
    assert settings["calibration"]["behavior"]["margin"] == 0.3
    assert settings["agent_command"][0] == sys.executable


# --- dry run -----------------------------------------------------------------------------


def test_dry_run_is_side_effect_free_even_with_force(tmp_path):
    config = _config(tmp_path)
    _run(tmp_path, config)
    before = _tree(tmp_path / "runs")

    assert _run(tmp_path, config, dry_run=True, force=True) is None

    assert _tree(tmp_path / "runs") == before


# --- report reconstruction -------------------------------------------------------------------


def test_report_rebuilds_assertions_and_policy_from_the_experiment_not_the_config(tmp_path):
    """The path that used to lose policy/assertions: --runs-dir given, no config."""
    config = _config(tmp_path, destructive=["list_directory"])
    live = _run(tmp_path, config)
    assert live.baseline_task.verdict == "PASS" and live.safety.verdict == "VIOLATION"

    rebuilt = run_report(config_path=tmp_path / "does-not-exist.yaml", runs_dir=tmp_path / "runs", task_id=TASK,
                         output_stream=io.StringIO())

    assert rebuilt.experiment_id == live.experiment_id
    assert rebuilt.baseline_task.verdict == "PASS"
    assert rebuilt.safety.verdict == "VIOLATION"


def test_report_defaults_to_the_latest_experiment_and_can_select_one(tmp_path):
    config = _config(tmp_path)
    first = _run(tmp_path, config)
    second = _run(tmp_path, config)

    assert build_report_result(TASK, tmp_path / "runs").experiment_id == second.experiment_id
    assert build_report_result(TASK, tmp_path / "runs", experiment=first.experiment_id).experiment_id == first.experiment_id


def test_an_unknown_experiment_id_names_the_available_ones(tmp_path):
    config = _config(tmp_path)
    first = _run(tmp_path, config)
    with pytest.raises(ConfigError) as exc:
        build_report_result(TASK, tmp_path / "runs", experiment="no-such-experiment")
    assert first.experiment_id in str(exc.value)


def test_a_pre_r2_run_directory_still_rebuilds(tmp_path):
    """Legacy layout: sessions directly under run/<task_id>/, no experiment.json."""
    calls = [r for r in read_session(GOLDEN) if isinstance(r, ToolCall)][:2]
    command = [sys.executable, str(AGENT), *(f"{c.tool_name}|{json.dumps(c.arguments)}" for c in calls)]
    legacy_dir = tmp_path / "runs" / "run" / "legacy"
    run_mutation_comparison(task_id="legacy", prompt="", fixture=GOLDEN, server_name=SERVER, agent_command=command,
                            operator="description_update", session_dir=legacy_dir, raw_dir=tmp_path / "raw",
                            repeats=1, timeout_s=30.0)
    (legacy_dir / "experiment.json").unlink()

    rebuilt = build_report_result("legacy", tmp_path / "runs")

    assert rebuilt.baseline.total_runs == 1
    assert rebuilt.experiment_id is None
