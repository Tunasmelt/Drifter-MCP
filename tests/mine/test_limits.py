"""Audit findings: mining thresholds must be sane, and the search must be bounded.

Found by probing, not by the original tests: `mine.min_support: 0` and `-3` were accepted
(printing "support >= -3"), `max_length: 0` silently mined nothing and then blamed the
corpus ("no workflow recurs"), and nothing bounded how many patterns the search may
produce -- 300 random trajectories of 20 calls generated 1.4 million and ran for 10s.
"""

import io
import random
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from mcp_drifter.mine.prefixspan import PatternLimitError, mine_patterns
from mcp_drifter.mine.signature import SignatureGroup
from mcp_drifter.record.calibration import Calibration, Mine

sys.path.insert(0, str(Path(__file__).parent))


@pytest.mark.parametrize(
    "field, value",
    [("min_support", 0), ("min_support", -3), ("min_length", 0), ("max_length", 0), ("max_candidates", 0), ("max_patterns", 0)],
)
def test_a_nonsensical_threshold_is_refused_at_load(field, value):
    with pytest.raises(ValidationError, match=field):
        Mine(**{field: value})


def test_a_minimum_length_above_the_maximum_is_refused():
    with pytest.raises(ValidationError, match="min_length"):
        Mine(min_length=5, max_length=3)


def test_the_shipped_defaults_are_valid_and_reach_calibration():
    assert Calibration().mine == Mine(min_support=2, min_length=2, max_length=6, max_candidates=10, max_patterns=100_000)


def _wide_corpus(n=100, length=12):
    rng = random.Random(1)
    tools = [f"t{i}" for i in range(14)]
    return [
        SignatureGroup(tuple(rng.choice(tools) for _ in range(length)), 1, (f"s{i}",)) for i in range(n)
    ]


def test_the_search_stops_with_an_explanation_when_it_would_produce_too_many_patterns():
    with pytest.raises(PatternLimitError, match="min_support"):
        mine_patterns(_wide_corpus(), min_support=2, min_length=2, max_length=6, max_patterns=1000)


def test_a_search_under_the_limit_is_unaffected():
    limited = mine_patterns(_wide_corpus(50, 10), min_support=2, min_length=2, max_length=6, max_patterns=1_000_000)
    unlimited = mine_patterns(_wide_corpus(50, 10), min_support=2, min_length=2, max_length=6)
    assert limited == unlimited


def test_the_cli_turns_the_limit_into_an_actionable_config_error(tmp_path):
    from mine_corpus import write_session
    from mcp_drifter.cli.config import ConfigError
    from mcp_drifter.cli.tasks import run_tasks_mine

    runs = tmp_path / "runs"
    rng = random.Random(3)
    tools = [f"t{i}" for i in range(10)]
    for i in range(30):
        write_session(runs, f"s{i}", [[rng.choice(tools) for _ in range(10)]])
    config = tmp_path / "drifter.yaml"
    config.write_text(
        f"version: 1\nservers:\n  - name: srv\n    command: ['python']\nrecord:\n  dir: '{runs.as_posix()}'\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="mine.min_support"):
        run_tasks_mine(
            config_path=config,
            calibration=Calibration(mine=Mine(max_patterns=50)),
            output_stream=io.StringIO(),
        )
    assert not (tmp_path / "task_candidates.yaml").exists()
