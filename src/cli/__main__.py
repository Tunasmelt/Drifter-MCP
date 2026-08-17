"""Enables `python -m cli <subcommand> ...` — used by tests to spawn the
CLI as a real subprocess (matching record/__main__.py's `python -m
record` pattern), and equivalent to the installed `drifter` console
script (pyproject.toml's `[project.scripts]`).
"""

from cli.app import main

if __name__ == "__main__":
    main()
