"""Every package under `src/mcp_drifter/` is listed in `pyproject.toml`'s explicit
`[tool.setuptools].packages`, so it ships in the wheel.

Why this exists before 0.3.0's `auth/` package does (found in the P1 audit): the list is
written by hand, and nothing checked it. A new package left off it works perfectly in an
editable checkout and in every test, then is simply ABSENT from the installed wheel, so a
user's first `import` of it fails, and the first place anyone would notice is the 0.3.0
release smoke test. F-44 imports `auth/` lazily on purpose (tests/auth/test_replay_isolation.py),
which makes it worse: nothing imports it until an authenticated server is used, so even a
broad smoke test of the wheel would not touch it.

Red first, the way F-04 and F-47 were: this fails the moment `src/mcp_drifter/auth/` exists and
is not listed.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def packaging_problems(listed: set[str], dirs_with_modules: set[str], dirs_with_init: set[str]) -> list[str]:
    """What is wrong between the hand-written package list and what is on disk."""
    problems = []
    for name in sorted(dirs_with_modules):
        if name not in dirs_with_init:
            problems.append(f"{name}: has modules but no __init__.py")
        if name not in listed:
            problems.append(f"{name}: not in [tool.setuptools].packages, so the wheel would ship without it")
    for name in sorted(listed - dirs_with_modules):
        problems.append(f"{name}: listed in [tool.setuptools].packages but no such package on disk")
    return problems


def _on_disk(src: Path) -> tuple[set[str], set[str]]:
    with_modules: set[str] = set()
    with_init: set[str] = set()
    for path in (src / "mcp_drifter").rglob("*.py"):
        package = ".".join(path.parent.relative_to(src).parts)
        with_modules.add(package)
        if path.name == "__init__.py":
            with_init.add(package)
    return with_modules, with_init


def test_every_package_on_disk_is_listed_for_the_wheel_and_nothing_listed_is_missing():
    listed = set(tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]["packages"])
    with_modules, with_init = _on_disk(SRC)
    assert len(with_modules) >= 8, with_modules  # it found the real packages, not an empty tree
    assert packaging_problems(listed, with_modules, with_init) == []


# --- the check itself, against synthetic layouts -------------------------------------------


def test_a_new_package_left_off_the_list_is_reported_by_name():
    listed = {"mcp_drifter", "mcp_drifter.record"}
    disk = {"mcp_drifter", "mcp_drifter.record", "mcp_drifter.auth"}
    problems = packaging_problems(listed, disk, disk)
    assert len(problems) == 1 and problems[0].startswith("mcp_drifter.auth:")
    assert "wheel would ship without it" in problems[0]


def test_a_listed_package_that_does_not_exist_is_reported():
    problems = packaging_problems({"mcp_drifter", "mcp_drifter.gone"}, {"mcp_drifter"}, {"mcp_drifter"})
    assert problems == ["mcp_drifter.gone: listed in [tool.setuptools].packages but no such package on disk"]


def test_a_directory_of_modules_without_an_init_is_reported():
    problems = packaging_problems({"mcp_drifter", "mcp_drifter.auth"}, {"mcp_drifter", "mcp_drifter.auth"}, {"mcp_drifter"})
    assert problems == ["mcp_drifter.auth: has modules but no __init__.py"]


def test_a_consistent_layout_has_no_problems():
    layout = {"mcp_drifter", "mcp_drifter.record"}
    assert packaging_problems(layout, layout, layout) == []
