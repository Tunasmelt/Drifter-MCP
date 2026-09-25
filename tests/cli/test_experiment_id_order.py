"""`drifter report` picks the latest experiment by sorting directory names, so the ids
must sort in creation order -- including experiments created inside the same second.

Found by CI on Python 3.12: the id was `<second-resolution timestamp>-<random hex>`, so
two experiments started in the same second sorted by the random suffix, and the report
could show the OLDER one. It failed roughly one run in a few, which is why it survived.
"""

from datetime import datetime, timedelta, timezone

import pytest

from mcp_drifter.cli.config import ConfigError
from mcp_drifter.cli.experiment import new_experiment_id, resolve_experiment_dir

BASE = datetime(2026, 9, 25, 18, 50, 43, tzinfo=timezone.utc)


def test_ids_created_within_one_second_sort_in_creation_order():
    # Many draws: with a random suffix deciding the order, this fails almost surely.
    moments = [BASE + timedelta(microseconds=500 * i) for i in range(200)]
    ids = [new_experiment_id(m) for m in moments]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)


def test_ids_created_in_different_seconds_still_sort_by_time():
    earlier = new_experiment_id(BASE)
    later = new_experiment_id(BASE + timedelta(seconds=1, microseconds=1))
    assert earlier < later


def test_the_id_still_starts_with_the_second_resolution_timestamp():
    """Ids on disk from earlier versions look like this; the prefix must not change."""
    assert new_experiment_id(BASE).startswith("20260925T185043Z-")


def test_ids_from_before_the_fix_still_sort_before_later_seconds():
    old_style = "20260925T185043Z-a3e555"
    assert old_style < new_experiment_id(BASE + timedelta(seconds=1))


@pytest.mark.parametrize("attempt", range(20))  # the random suffix differs each time
def test_the_report_picks_the_latest_of_experiments_created_in_the_same_second(tmp_path, attempt):
    task_dir = tmp_path / "run" / "t"
    moments = [BASE + timedelta(microseconds=1000 * i) for i in range(4)]
    for m in moments:
        experiment = task_dir / new_experiment_id(m)
        experiment.mkdir(parents=True)
        (experiment / "experiment.json").write_text("{}", encoding="utf-8")
    newest = max(p.name for p in task_dir.iterdir() if p.is_dir())
    assert newest.split("-")[1] == f"{moments[-1].microsecond:06d}"
    assert resolve_experiment_dir(tmp_path, "t").name == newest


def test_unknown_task_is_still_a_config_error(tmp_path):
    with pytest.raises(ConfigError):
        resolve_experiment_dir(tmp_path, "nope")
