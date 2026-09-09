"""CLI flags must actually reach the code they name.

Written after an external reviewer found that TWO `drifter run` flags were
registered with argparse, accepted without error, and then silently
discarded: `--replay-discovered-values` and `--force`. Both were parsed into
`args` and never passed to `run_run`.

The cost was not theoretical. An A/B experiment for R0 (docs/SPEC.md §15
limitation 17) was run and reported as a real result; because the flag never
reached the code, it was two control runs, and the "treatment" arm proved
nothing. A flag that is silently ignored is worse than a missing one -- a
missing flag errors, an ignored flag produces confident, wrong evidence.

Both breaks shared one cause: a source edit applied with an unasserted
string replacement that silently matched nothing (or matched a DIFFERENT
command's dispatch block). Nothing downstream failed, because every
individual piece existed -- the argparse registration, the parameter, the
implementation. Only the CONNECTION was missing, and no test looked at
connections.

So these tests assert plumbing specifically: that a flag set on the command
line arrives at `run_run`'s parameter of the same name. They deliberately do
not test what the flag DOES -- that belongs with the feature.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_drifter.cli import app as app_module


def _run_cli(monkeypatch, argv: list[str]) -> dict:
    """Invokes the real CLI parser+dispatch, capturing run_run's kwargs."""
    captured: dict = {}

    def _fake_run_run(**kwargs):
        captured.update(kwargs)
        return None  # None means "no comparison ran", so main() exits cleanly

    monkeypatch.setattr("mcp_drifter.cli.run.run_run", _fake_run_run)
    monkeypatch.setattr("sys.argv", ["drifter", *argv])
    try:
        app_module.main()
    except SystemExit as exc:
        assert exc.code in (0, None), f"CLI exited {exc.code}"
    return captured


_BASE = [
    "run",
    "--fixture", "corpus",
    "--server", "srv",
    "--task-id", "t",
    "--prompt", "p",
]


def test_replay_discovered_values_reaches_run_run(monkeypatch):
    captured = _run_cli(monkeypatch, [*_BASE, "--replay-discovered-values"])

    assert captured["replay_discovered_values"] is True


def test_replay_discovered_values_defaults_off(monkeypatch):
    captured = _run_cli(monkeypatch, _BASE)

    assert captured["replay_discovered_values"] is False


def test_force_reaches_run_run(monkeypatch):
    captured = _run_cli(monkeypatch, [*_BASE, "--force"])

    assert captured["force"] is True


def test_force_defaults_off(monkeypatch):
    captured = _run_cli(monkeypatch, _BASE)

    assert captured["force"] is False


def test_every_registered_run_flag_is_forwarded(monkeypatch):
    """The general guard, so the next flag added cannot repeat this.

    Compares what the `run` subparser accepts against what `run_run`
    actually receives. A flag registered and then dropped fails here
    without anyone remembering to write a test for it specifically.
    """
    import argparse
    import inspect

    from mcp_drifter.cli.run import run_run

    parser_holder: dict = {}
    real_add_parser = argparse._SubParsersAction.add_parser

    def _spy(self, name, **kwargs):
        sub = real_add_parser(self, name, **kwargs)
        if name == "run":
            parser_holder["run"] = sub
        return sub

    monkeypatch.setattr(argparse._SubParsersAction, "add_parser", _spy)
    captured = _run_cli(monkeypatch, _BASE)

    run_parser = parser_holder["run"]
    dests = {
        a.dest
        for a in run_parser._actions
        if a.dest not in ("help",) and not isinstance(a, argparse._HelpAction)
    }
    accepted = set(inspect.signature(run_run).parameters)

    # Every flag the parser accepts must correspond to a run_run parameter
    # AND actually be passed. `dest` names that run_run spells differently
    # are listed explicitly rather than fuzzily matched, so a rename shows
    # up here as a decision instead of silently passing.
    aliases = {
        "config": "config_path",
        "server": "server_name",
        "timeout": "timeout_s",
        "assume_yes": "assume_yes",
        "max_wall_time_s": "max_wall_time_s",
        "no_adaptive": "adaptive",
    }
    missing = []
    for dest in sorted(dests):
        param = aliases.get(dest, dest)
        if param not in accepted:
            missing.append(f"{dest} -> no run_run parameter {param!r}")
        elif param not in captured:
            missing.append(f"{dest} -> parsed but never forwarded as {param!r}")

    assert not missing, "flags registered but not plumbed through:\n  " + "\n  ".join(missing)
