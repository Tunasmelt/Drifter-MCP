"""`drifter tasks mine` / `drifter tasks approve` (F-28-F-30, docs/SPEC.md §12).

End to end against a real recorded corpus and a real drifter.yaml. The contract worth
proving is the one in FEATURES.md F-30: a mined candidate can be edited and approved
without touching raw recordings, and an approved task is then an ordinary task.
"""

import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "mine"))
from mine_corpus import write_session  # noqa: E402

from mcp_drifter.cli.config import ConfigError, assertions_for, find_task, load_config  # noqa: E402
from mcp_drifter.cli.tasks import run_tasks_approve, run_tasks_mine  # noqa: E402


def _workspace(tmp_path: Path, extra_config: str = "") -> tuple[Path, Path]:
    """drifter.yaml + a corpus with a known embedded workflow. Returns (config, runs)."""
    runs = tmp_path / "runs"
    for i in range(3):
        write_session(runs, f"s{i}", [["search", "get_customer", "create_invoice"]] * 2, extra_tools=("idle_tool",))
    write_session(runs, "s3", [["get_customer", "update_customer"]], extra_tools=("idle_tool",))
    config = tmp_path / "drifter.yaml"
    config.write_text(
        "version: 1\nservers:\n  - name: srv\n    command: ['python']\n"
        f"record:\n  dir: '{runs.as_posix()}'\n" + extra_config,
        encoding="utf-8",
    )
    return config, runs


def _mine(config: Path) -> str:
    out = io.StringIO()
    run_tasks_mine(config_path=config, output_stream=out)
    return out.getvalue()


def _candidates(config: Path) -> Path:
    return config.parent / "task_candidates.yaml"


def test_mine_writes_the_embedded_workflow_as_the_top_candidate_with_exact_evidence(tmp_path):
    config, _ = _workspace(tmp_path)
    output = _mine(config)
    text = _candidates(config).read_text(encoding="utf-8")
    assert "id: search_get_customer_create_invoice" in text
    assert "support: 6" in text
    assert "of_trajectories: 7" in text
    assert "status: candidate" in text
    assert "7 trajectories from 4 session(s) on 'srv'" in output
    assert "search -> get_customer -> create_invoice" in output


def test_mine_lists_the_tools_no_approved_task_covers(tmp_path):
    config, _ = _workspace(tmp_path)
    output = _mine(config)
    # Nothing is approved yet, so every tool is uncovered -- including the one no
    # trajectory ever called.
    assert "TOOLS IN NO APPROVED TASK (5)" in output
    assert "idle_tool" in output


def test_a_second_mine_never_disturbs_what_the_user_edited(tmp_path):
    config, runs = _workspace(tmp_path)
    _mine(config)
    path = _candidates(config)
    edited = "# my note\n" + path.read_text(encoding="utf-8").replace(
        "prompt: ''", "prompt: Invoice acme  # mine"
    )
    path.write_text(edited, encoding="utf-8")
    write_session(runs, "s4", [["list_products", "get_product"]] * 3)
    output = _mine(config)
    after = path.read_text(encoding="utf-8")
    assert after.startswith(edited.rstrip("\n"))
    assert "id: list_products_get_product" in after
    assert "appended" in output


def test_mining_again_with_nothing_new_says_so_and_changes_nothing(tmp_path):
    config, _ = _workspace(tmp_path)
    _mine(config)
    before = _candidates(config).read_text(encoding="utf-8")
    output = _mine(config)
    assert _candidates(config).read_text(encoding="utf-8") == before
    assert "no new candidates" in output


def test_a_corpus_with_no_recurring_workflow_writes_no_file(tmp_path):
    runs = tmp_path / "runs"
    write_session(runs, "only", [["a", "b"]])
    config = tmp_path / "drifter.yaml"
    config.write_text(
        f"version: 1\nservers:\n  - name: srv\n    command: ['python']\nrecord:\n  dir: '{runs.as_posix()}'\n",
        encoding="utf-8",
    )
    output = _mine(config)
    assert not _candidates(config).exists()
    assert "no workflow recurs" in output


def test_mining_with_no_recorded_sessions_is_an_actionable_error(tmp_path):
    config = tmp_path / "drifter.yaml"
    config.write_text(
        f"version: 1\nservers:\n  - name: srv\n    command: ['python']\nrecord:\n  dir: '{(tmp_path / 'none').as_posix()}'\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="drifter observe"):
        _mine(config)


def test_approving_needs_a_prompt_because_mining_cannot_know_the_intent(tmp_path):
    config, _ = _workspace(tmp_path)
    _mine(config)
    with pytest.raises(ConfigError, match="prompt"):
        run_tasks_approve("search_get_customer_create_invoice", config_path=config, output_stream=io.StringIO())
    assert "status: approved" not in _candidates(config).read_text(encoding="utf-8")


def _approve_with_prompt(config: Path, task_id: str = "search_get_customer_create_invoice") -> None:
    path = _candidates(config)
    path.write_text(path.read_text(encoding="utf-8").replace("prompt: ''", "prompt: Invoice acme", 1), encoding="utf-8")
    run_tasks_approve(task_id, config_path=config, output_stream=io.StringIO())


def test_an_approved_candidate_is_then_an_ordinary_task(tmp_path):
    config, _ = _workspace(tmp_path)
    _mine(config)
    _approve_with_prompt(config)
    loaded = load_config(config)
    task = find_task(loaded.tasks, "search_get_customer_create_invoice")
    assert task is not None
    assert task.prompt == "Invoice acme"
    oracle = assertions_for(loaded.tasks, "search_get_customer_create_invoice")
    assert oracle.calls == ("search", "get_customer", "create_invoice")
    assert oracle.calls_before == (("search", "get_customer"), ("get_customer", "create_invoice"))


def test_an_unapproved_candidate_is_not_a_task(tmp_path):
    config, _ = _workspace(tmp_path)
    _mine(config)
    assert load_config(config).tasks == []


def test_approving_leaves_the_recordings_untouched(tmp_path):
    config, runs = _workspace(tmp_path)
    before = {p.name: p.read_bytes() for p in runs.glob("*.jsonl")}
    _mine(config)
    _approve_with_prompt(config)
    assert {p.name: p.read_bytes() for p in runs.glob("*.jsonl")} == before


def test_approval_covers_the_tools_the_task_calls(tmp_path):
    config, _ = _workspace(tmp_path)
    _mine(config)
    _approve_with_prompt(config)
    output = _mine(config)
    assert "TOOLS IN NO APPROVED TASK (2)" in output
    assert "idle_tool" in output and "update_customer" in output


def test_an_invalid_edited_assertion_is_refused_at_approval_not_at_run_time(tmp_path):
    config, _ = _workspace(tmp_path)
    _mine(config)
    path = _candidates(config)
    text = path.read_text(encoding="utf-8").replace("prompt: ''", "prompt: p", 1)
    text = text.replace("no_errors: false", "no_errors: false\n    result_contains: [x]", 1)
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="result_contains"):
        run_tasks_approve("search_get_customer_create_invoice", config_path=config, output_stream=io.StringIO())
    assert "status: approved" not in path.read_text(encoding="utf-8")


def test_approving_over_an_inline_task_with_the_same_id_is_refused(tmp_path):
    config, _ = _workspace(
        tmp_path, extra_config="tasks:\n  - id: search_get_customer_create_invoice\n    prompt: hand written\n"
    )
    _mine(config)
    path = _candidates(config)
    path.write_text(path.read_text(encoding="utf-8").replace("prompt: ''", "prompt: p", 1), encoding="utf-8")
    with pytest.raises(ConfigError, match="already defined"):
        run_tasks_approve("search_get_customer_create_invoice", config_path=config, output_stream=io.StringIO())


def test_an_approved_id_that_collides_with_an_inline_task_fails_loading(tmp_path):
    config, _ = _workspace(tmp_path)
    _mine(config)
    _approve_with_prompt(config)
    config.write_text(
        config.read_text(encoding="utf-8") + "tasks:\n  - id: search_get_customer_create_invoice\n    prompt: x\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="search_get_customer_create_invoice"):
        load_config(config)


def test_a_malformed_candidates_file_names_itself_when_config_loads(tmp_path):
    config, _ = _workspace(tmp_path)
    _candidates(config).write_text("version: 9\nserver: srv\ncandidates: []\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="task_candidates.yaml"):
        load_config(config)


def test_approving_without_a_candidates_file_points_at_mine(tmp_path):
    config, _ = _workspace(tmp_path)
    with pytest.raises(ConfigError, match="drifter tasks mine"):
        run_tasks_approve("anything", config_path=config, output_stream=io.StringIO())


def test_the_command_group_is_wired_and_reports_config_errors_with_exit_4(tmp_path, monkeypatch, capsys):
    from mcp_drifter.cli.app import main

    config, _ = _workspace(tmp_path)
    monkeypatch.setattr(sys, "argv", ["drifter", "tasks", "approve", "nope", "--config", str(config)])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 4
    assert "drifter tasks approve" in capsys.readouterr().err

    monkeypatch.setattr(sys, "argv", ["drifter", "tasks", "mine", "--config", str(config)])
    main()  # success returns normally, like every other command
    assert _candidates(config).exists()


# --- audit finding: a broken tasks file must not take unrelated commands down -------
#
# Found by audit: merging approved tasks inside `load_config` meant a YAML typo in
# task_candidates.yaml made `drifter observe` exit 4 -- the recording proxy an agent's
# MCP config launches, refusing to start over a file that has nothing to do with
# recording. Only `run`, `report` and `tasks approve` use `config.tasks`.

FAKE_SERVER = str(Path(__file__).parent.parent / "fixtures" / "fake_server.py")


def _broken_tasks_file(config: Path) -> Path:
    path = _candidates(config)
    path.write_text("version: 1\nserver: srv\ncandidates:\n  - id: x\n   status: candidate\n", encoding="utf-8")
    return path


def test_load_config_can_skip_the_tasks_file_entirely(tmp_path):
    config, _ = _workspace(tmp_path)
    _broken_tasks_file(config)
    assert load_config(config, merge_tasks=False).servers[0].name == "srv"
    with pytest.raises(ConfigError, match="task_candidates.yaml"):
        load_config(config)


def test_observe_starts_despite_a_broken_tasks_file(tmp_path, monkeypatch):
    from mcp_drifter.cli import observe

    config, _ = _workspace(tmp_path)
    _broken_tasks_file(config)
    ran = []
    monkeypatch.setattr(observe.anyio, "run", lambda *a, **k: ran.append(True))
    observe.run_observe(config_path=config, server_name="srv", status_stream=io.StringIO())
    assert ran == [True]


def test_stats_score_and_coverage_ignore_a_broken_tasks_file(tmp_path):
    from mcp_drifter.cli.coverage_cmd import run_coverage
    from mcp_drifter.cli.score import run_score
    from mcp_drifter.cli.stats import run_stats

    config, _ = _workspace(tmp_path)
    _broken_tasks_file(config)
    for command in (run_stats, run_score, run_coverage):
        out = io.StringIO()
        command(config_path=config, output_stream=out, **({"server": "srv"} if command is run_coverage else {}))
        assert out.getvalue()


def test_run_and_report_still_fail_loudly_because_they_need_the_tasks(tmp_path):
    from mcp_drifter.cli.report import run_report

    config, _ = _workspace(tmp_path)
    _broken_tasks_file(config)
    with pytest.raises(ConfigError, match="task_candidates.yaml"):
        run_report(config_path=config, task_id="t", output_stream=io.StringIO())


def test_doctor_surfaces_a_broken_tasks_file_as_a_warning_not_a_failure(tmp_path):
    from mcp_drifter.cli.doctor import run_doctor

    runs = tmp_path / "runs"
    config = tmp_path / "drifter.yaml"
    config.write_text(
        f"version: 1\nservers:\n  - name: srv\n    command: ['{sys.executable}', '{FAKE_SERVER}']\n"
        f"record:\n  dir: '{runs.as_posix()}'\n",
        encoding="utf-8",
    )
    _broken_tasks_file(config)
    out = io.StringIO()
    ok = run_doctor(config_path=config, output_stream=out)
    text = out.getvalue()
    assert "[WARN] tasks_file" in text and "task_candidates.yaml" in text
    assert ok is True


def test_mine_skips_sessions_replay_recorded_and_says_how_many(tmp_path):
    """Audit finding: replay-serve writes into the same directory observe does, and mining
    its sessions proposed the agent's own replays back to it as a workflow."""
    config, runs = _workspace(tmp_path)
    for i in range(3):
        write_session(runs, f"replay{i}", [["c", "d"]] * 2, replayed=True)
    output = _mine(config)
    text = _candidates(config).read_text(encoding="utf-8")
    assert "7 trajectories from 4 session(s)" in output  # unchanged: the replays are not counted
    assert "3 session(s) recorded by `drifter replay-serve` were skipped" in output
    assert "pattern:\n  - c\n" not in text and "c_d" not in text


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"], ids=["LF", "CRLF"])
def test_approve_changes_one_word_and_no_line_ending_whatever_the_files_convention(tmp_path, newline):
    """Audit finding: reading and writing through text mode translated the whole file's
    line endings (an LF file became CRLF on Windows and the reverse elsewhere), so
    "your edits survive byte for byte" was false for every line. Compared as BYTES,
    because a test that reads the file back through the same translating API cannot see
    the difference -- which is how it slipped through."""
    config, _ = _workspace(tmp_path)
    _mine(config)
    path = _candidates(config)
    raw = path.read_bytes().replace(b"\r\n", b"\n").replace(b"prompt: ''", b"prompt: Look up acme")
    before = raw.replace(b"\n", newline)
    path.write_bytes(before)
    run_tasks_approve("search_get_customer_create_invoice", config_path=config, output_stream=io.StringIO())
    after = path.read_bytes()
    assert after.replace(b"status: approved", b"status: candidate") == before


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"], ids=["LF", "CRLF"])
def test_a_second_mine_appends_without_changing_any_existing_byte_or_line_ending(tmp_path, newline):
    config, runs = _workspace(tmp_path)
    _mine(config)
    path = _candidates(config)
    before = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", newline)
    path.write_bytes(before)
    write_session(runs, "s9", [["list_products", "get_product"]] * 3)
    _mine(config)
    after = path.read_bytes()
    assert after.startswith(before.rstrip(b"\r\n"))
    assert b"list_products_get_product" in after
    if newline == b"\r\n":
        assert b"\n" not in after.replace(b"\r\n", b"")
    else:
        assert b"\r" not in after


# --- second-reviewer findings: provenance ------------------------------------------------


def test_candidates_mined_from_another_server_are_not_loaded_as_tasks(tmp_path):
    """A candidate file records which server it was mined from; loading it under a project
    that only configures a different server used to succeed silently."""
    config, _ = _workspace(tmp_path)
    _mine(config)
    _approve_with_prompt(config)
    text = _candidates(config).read_text(encoding="utf-8")
    _candidates(config).write_text(text.replace("server: srv", "server: other", 1), encoding="utf-8")
    with pytest.raises(ConfigError, match="other.*srv|srv.*other"):
        load_config(config)


def test_a_candidates_file_with_nothing_approved_is_not_checked_against_the_server(tmp_path):
    config, _ = _workspace(tmp_path)
    _mine(config)
    text = _candidates(config).read_text(encoding="utf-8")
    _candidates(config).write_text(text.replace("server: srv", "server: other", 1), encoding="utf-8")
    assert load_config(config).tasks == []


@pytest.mark.parametrize("newline", ["\r\n", "\n"])
def test_mine_fills_an_empty_candidates_list_in_place_keeping_trailing_lines(tmp_path, newline):
    config, _ = _workspace(tmp_path)
    original = f"version: 1{newline}server: srv{newline}candidates: []{newline}{newline}# mine{newline}"
    with _candidates(config).open("w", encoding="utf-8", newline="") as handle:
        handle.write(original)
    _mine(config)
    with _candidates(config).open("r", encoding="utf-8", newline="") as handle:
        result = handle.read()
    assert result.startswith(f"version: 1{newline}server: srv{newline}candidates:{newline}- id: ")
    assert result.endswith(f"{newline}{newline}# mine{newline}")
    if newline == "\r\n":
        assert "\n" not in result.replace("\r\n", "")
    assert "search_get_customer_create_invoice" in result


def test_mine_only_reads_the_named_server_when_several_are_recorded(tmp_path):
    config, runs = _workspace(tmp_path)
    for i in range(3):
        write_session(runs, f"o{i}", [["x", "y"]] * 2, server="other")
    out = io.StringIO()
    run_tasks_mine(config_path=config, server="srv", output_stream=out)
    text = _candidates(config).read_text(encoding="utf-8")
    assert "x_y" not in text
    assert "search_get_customer_create_invoice" in text


# --- mining pre-fill: never_calls from the risk classification ---------------------------------


def _prefill_workspace(tmp_path: Path, *, extra_config: str = "", risky_in_pattern: bool = False) -> Path:
    """Sessions whose manifest advertises destructive-, irreversible- and unclassifiable-
    looking tools. With `risky_in_pattern`, some trajectories that support the mined pattern
    also call `delete_customer`."""
    runs = tmp_path / "runs"
    extras = ("idle_tool", "delete_customer", "remove_item", "send_receipt")
    core = ["search", "get_customer", "create_invoice"]
    for i in range(3):
        trajectories = [core, core]
        if risky_in_pattern:
            trajectories = [core, ["search", "get_customer", "delete_customer", "create_invoice"]]
        write_session(runs, f"p{i}", trajectories, extra_tools=extras)
    config = tmp_path / "drifter.yaml"
    config.write_text(
        "version: 1\nservers:\n  - name: srv\n    command: ['python']\n"
        f"record:\n  dir: '{runs.as_posix()}'\n" + extra_config,
        encoding="utf-8",
    )
    return config


def _never_calls(config: Path, candidate: str = "search_get_customer_create_invoice") -> list[str]:
    import yaml

    doc = yaml.safe_load(_candidates(config).read_text(encoding="utf-8"))
    return next(e for e in doc["candidates"] if e["id"] == candidate)["assert"]["never_calls"]


def test_mine_prefills_never_calls_with_destructive_tools_the_pattern_does_not_use(tmp_path):
    config = _prefill_workspace(tmp_path)
    output = _mine(config)
    # Exactly the destructive-classified ones, sorted. `send_receipt` is an irreversible write
    # and `idle_tool` is unclassifiable: neither is asserted against.
    assert _never_calls(config) == ["delete_customer", "remove_item"]
    assert "never_calls (pre-filled from tool risk): delete_customer, remove_item" in output


def test_a_destructive_tool_called_in_a_supporting_trajectory_is_not_prefilled(tmp_path):
    """Otherwise the candidate would fail its own baseline: the recordings it was mined from
    call `delete_customer` inside this very workflow."""
    config = _prefill_workspace(tmp_path, risky_in_pattern=True)
    _mine(config)
    assert _never_calls(config) == ["remove_item"]


def test_the_users_destructive_override_is_prefilled_too(tmp_path):
    config = _prefill_workspace(tmp_path, extra_config="policy:\n  destructive: [idle_tool]\n")
    _mine(config)
    assert _never_calls(config) == ["delete_customer", "idle_tool", "remove_item"]


def test_a_prefilled_candidate_approves_into_a_task_that_enforces_it(tmp_path):
    config = _prefill_workspace(tmp_path)
    _mine(config)
    _approve_with_prompt(config)
    oracle = assertions_for(load_config(config).tasks, "search_get_customer_create_invoice")
    assert oracle.never_calls == ("delete_customer", "remove_item")


def test_a_corpus_with_no_destructive_tools_prefills_nothing_and_says_nothing(tmp_path):
    config, _ = _workspace(tmp_path)
    output = _mine(config)
    assert _never_calls(config) == []
    assert "pre-filled" not in output
