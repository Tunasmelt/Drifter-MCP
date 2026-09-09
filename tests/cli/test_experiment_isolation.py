"""A second `drifter run` must not silently mix into the first's results.

Found by external review (Codex) and confirmed in the code before fixing.
`session_dir` is `runs_dir/run/<task_id>` -- keyed by task id alone -- and
four separate consumers then read that directory as though it held one
experiment:

  * `cli/report.py` globs `baseline/*.jsonl` and `mutated/*.jsonl`, so a
    rebuilt report aggregates every run ever made under that task id.
  * `evaluate_safety_across_arms` scans the whole session_dir, so a safety
    violation from an earlier experiment is inherited by a later one that
    never touched a risky tool.
  * `mutate/audit.py` opens `mutations.jsonl` with mode "w", so the second
    run DESTROYS the first run's paper trail while the first run's sessions
    remain and keep being aggregated -- the audit and the sessions then
    describe different experiments, which is worse than having no audit.
  * `aggregate_baseline_runs` sees the union as one arm, inflating
    `valid_runs` and distorting `natural_variation`/`baseline_spread`.

The eventual fix is an experiment id binding sessions, mutations, config,
assertions and calibration together. This is the guard that makes the
silent version impossible in the meantime: reuse of a non-empty session
directory is refused, in the same "reversible over destructive" spirit as
`drifter init`'s overwrite protection, with `--force` as the explicit
opt-in. Nothing is mixed by accident, and nothing is deleted without being
asked for.
"""

from __future__ import annotations

import pytest

from mcp_drifter.cli.config import ConfigError
from mcp_drifter.cli.run import ensure_clean_session_dir


def test_a_fresh_session_dir_is_accepted(tmp_path):
    ensure_clean_session_dir(tmp_path / "run" / "task", force=False)


def test_an_empty_existing_session_dir_is_accepted(tmp_path):
    session_dir = tmp_path / "run" / "task"
    (session_dir / "baseline").mkdir(parents=True)

    ensure_clean_session_dir(session_dir, force=False)


def test_reusing_a_dir_that_already_holds_sessions_is_refused(tmp_path):
    session_dir = tmp_path / "run" / "task"
    (session_dir / "baseline").mkdir(parents=True)
    (session_dir / "baseline" / "prior.jsonl").write_text("{}\n", encoding="utf-8")

    with pytest.raises(ConfigError) as exc_info:
        ensure_clean_session_dir(session_dir, force=False)

    message = str(exc_info.value)
    assert "--force" in message
    assert "task" in message


def test_force_allows_reuse_and_clears_the_prior_experiment(tmp_path):
    """--force must CLEAR, not merge. Leaving the old sessions in place
    would preserve exactly the contamination this guard exists to stop --
    the user asked to redo the experiment, not to pool two of them.
    """
    session_dir = tmp_path / "run" / "task"
    (session_dir / "baseline").mkdir(parents=True)
    stale = session_dir / "baseline" / "prior.jsonl"
    stale.write_text("{}\n", encoding="utf-8")
    stale_audit = session_dir / "mutations.jsonl"
    stale_audit.write_text("{}\n", encoding="utf-8")

    ensure_clean_session_dir(session_dir, force=True)

    assert not stale.exists()
    assert not stale_audit.exists()


def test_a_mutated_arm_left_over_from_a_prior_run_also_triggers_the_refusal(tmp_path):
    """Both arms count -- a prior run that produced only mutated sessions
    (baseline having been skipped) still contaminates."""
    session_dir = tmp_path / "run" / "task"
    (session_dir / "mutated").mkdir(parents=True)
    (session_dir / "mutated" / "prior.jsonl").write_text("{}\n", encoding="utf-8")

    with pytest.raises(ConfigError):
        ensure_clean_session_dir(session_dir, force=False)
