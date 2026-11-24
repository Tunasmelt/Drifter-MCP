"""F-42, Windows half: "a verified user-private ACL, not a chmod approximation" (docs/FEATURES.md
F-42, docs/SPEC.md §5.2, DEC-029). POSIX mode bits mean nothing on Windows.

The ACL is read back with `icacls <path> /save <file>`, whose SDDL output is locale-independent
(`icacls <path>` prints account NAMES, which are translated), and judged by a pure parser so the
logic is tested on every platform. The real `icacls` calls are tested only on Windows, in CI.

Fail closed throughout: an ACL that cannot be read, parsed or understood is a problem, never "fine".
"""

from __future__ import annotations

import subprocess
import sys
from collections import namedtuple
from pathlib import Path

import pytest

from mcp_drifter.auth.winacl import (
    AclError,
    acl_problems,
    current_user_sid,
    extract_dacl,
    make_private,
    verify_private,
)

USER = "S-1-5-21-1111111111-2222222222-3333333333-1001"
OTHER = "S-1-5-21-1111111111-2222222222-3333333333-1002"
Result = namedtuple("Result", "returncode stdout stderr")


# --- the DACL judge -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "dacl",
    [
        pytest.param(f"D:P(A;;FA;;;{USER})", id="only-the-user"),
        pytest.param(f"D:PAI(A;;FA;;;{USER})(A;;FA;;;SY)(A;;FA;;;BA)", id="user-system-admins"),
        pytest.param(f"D:AI(A;OICIID;FA;;;{USER})(A;OICIID;FA;;;S-1-5-18)(A;OICIID;FA;;;S-1-5-32-544)", id="inherited-by-sid"),
        pytest.param(f"D:P(A;;FA;;;OW)(A;;FA;;;{USER})", id="owner-rights"),
        pytest.param(f"D:P(A;;FA;;;{USER})(A;OICIIO;FA;;;CO)", id="creator-owner-inherit-only"),
        pytest.param(f"D:P(D;;FA;;;WD)(A;;FA;;;{USER})", id="a-deny-for-everyone-is-harmless"),
        pytest.param("D:P", id="nobody-at-all"),
    ],
)
def test_a_dacl_granting_only_this_user_and_the_system_accounts_is_private(dacl):
    assert acl_problems(dacl, USER) == []


@pytest.mark.parametrize(
    "dacl, who",
    [
        pytest.param(f"D:P(A;;FA;;;{USER})(A;;FR;;;WD)", "WD", id="everyone"),
        pytest.param(f"D:P(A;;FA;;;{USER})(A;;FR;;;BU)", "BU", id="builtin-users"),
        pytest.param(f"D:P(A;;FA;;;{USER})(A;;FR;;;AU)", "AU", id="authenticated-users"),
        pytest.param(f"D:P(A;;FA;;;{USER})(A;;FR;;;IU)", "IU", id="interactive"),
        pytest.param(f"D:P(A;;FA;;;{USER})(A;;FR;;;NU)", "NU", id="network"),
        pytest.param(f"D:P(A;;FA;;;{USER})(A;;FR;;;S-1-5-32-545)", "S-1-5-32-545", id="users-by-sid"),
        pytest.param(f"D:P(A;;FA;;;{USER})(A;;FR;;;{OTHER})", OTHER, id="another-user"),
        pytest.param(f"D:P(A;;FA;;;{USER})(A;;FR;;;S-1-1-0)", "S-1-1-0", id="everyone-by-sid"),
        pytest.param(f"D:P(A;;FA;;;{USER})(A;;FR;;;LS)", "LS", id="an-alias-not-on-the-allowlist"),
        pytest.param(f"D:P(A;;FA;;;{USER})(OA;;CC;4c164200-20c0-11d0-a768-00aa006e0529;;WD)", "WD", id="object-allow-ace"),
        pytest.param(f"D:P(A;;FA;;;{USER})(XA;;FA;;;WD;(Member_of{{SID(WD)}}))", "WD", id="conditional-allow-ace"),
    ],
)
def test_a_dacl_that_grants_anyone_else_anything_is_refused_naming_who(dacl, who):
    problems = acl_problems(dacl, USER)
    assert problems and any(who in problem for problem in problems), problems


@pytest.mark.parametrize(
    "dacl",
    ["", "garbage", "D:P(A;;FA;;;)", "D:P(A;;FA;;;WD", "D:P(A;;FA)", "O:BAG:SY", "D:NO_ACCESS_CONTROL", "D:P(Z;;FA;;;WD)"],
)
def test_anything_that_cannot_be_understood_is_a_problem_not_a_pass(dacl):
    assert acl_problems(dacl, USER), dacl


# SDDL prints some accounts by alias instead of by SID. `LA` is the built-in Administrator account
# (RID 500) and `LG` the Guest account (RID 501). Found by Windows CI: the GitHub runner's user IS the
# built-in Administrator, so `icacls` showed the user's own entry as `LA` and the check called it a stranger.
ADMIN_ACCOUNT = "S-1-5-21-1111111111-2222222222-3333333333-500"
GUEST_ACCOUNT = "S-1-5-21-1111111111-2222222222-3333333333-501"


@pytest.mark.parametrize("dacl", ["D:P(A;;FA;;;LA)", "D:PAI(A;;FA;;;LA)(A;;FA;;;SY)", "D:AI(A;ID;FA;;;LA)(A;OICIID;FA;;;LA)"])
def test_the_built_in_administrator_account_is_recognized_as_itself_when_that_is_who_we_are(dacl):
    assert acl_problems(dacl, ADMIN_ACCOUNT) == []


def test_the_guest_account_is_recognized_as_itself_when_that_is_who_we_are():
    assert acl_problems("D:P(A;;FA;;;LG)", GUEST_ACCOUNT) == []


@pytest.mark.parametrize("alias, user", [("LA", USER), ("LG", USER), ("LA", GUEST_ACCOUNT), ("LG", ADMIN_ACCOUNT)])
def test_those_aliases_are_a_problem_when_they_name_someone_else(alias, user):
    problems = acl_problems(f"D:P(A;;FA;;;{user})(A;;FR;;;{alias})", user)
    assert problems and any(alias in problem for problem in problems), problems


# --- reading the icacls /save output -------------------------------------------------------------------------


def test_the_dacl_is_found_in_icacls_save_output():
    saved = f"\ufefftokens\\key.json\nD:PAI(A;;FA;;;{USER})\n\n".encode("utf-16-le")
    assert extract_dacl(saved) == f"D:PAI(A;;FA;;;{USER})"


def test_the_dacl_is_found_with_or_without_a_byte_order_mark_and_with_an_owner_prefix():
    plain = f"key.json\r\nO:BAG:SYD:PAI(A;;FA;;;{USER})\r\n".encode("utf-16-le")
    assert extract_dacl(plain) == f"D:PAI(A;;FA;;;{USER})"


@pytest.mark.parametrize("saved", [b"", "just a name\n".encode("utf-16"), b"\xff\xfe\x00"])
def test_output_with_no_dacl_is_an_error(saved):
    with pytest.raises(AclError):
        extract_dacl(saved)


# --- the current user's SID ---------------------------------------------------------------------------------


def test_the_current_users_sid_is_read_from_locale_independent_csv():
    def run(args):
        assert args[:2] == ["whoami", "/user"]
        return Result(0, f'"DESKTOP-1\\harris","{USER}"\r\n'.encode(), b"")

    assert current_user_sid(run) == USER


@pytest.mark.parametrize("result", [Result(1, b"", b"boom"), Result(0, b"nonsense", b""), Result(0, b'"a","not-a-sid"\n', b"")])
def test_an_unreadable_sid_is_an_error(result):
    with pytest.raises(AclError):
        current_user_sid(lambda args: result)


# --- making a path private and checking it ----------------------------------------------------------------------


def test_making_a_file_private_removes_inheritance_and_grants_only_the_user():
    calls: list[list[str]] = []

    def run(args):
        calls.append(args)
        return Result(0, b"", b"")

    make_private(Path("C:/cfg/tokens/key.json"), USER, is_dir=False, run=run)
    assert calls == [["icacls", str(Path("C:/cfg/tokens/key.json")), "/inheritance:r", "/grant:r", f"*{USER}:F"]]


def test_making_a_directory_private_grants_the_user_inheritable_full_control():
    calls: list[list[str]] = []
    make_private(Path("C:/cfg/tokens"), USER, is_dir=True, run=lambda args: calls.append(args) or Result(0, b"", b""))
    assert calls[0][-1] == f"*{USER}:(OI)(CI)F" and "/inheritance:r" in calls[0]


def test_a_failing_icacls_is_an_error_not_a_silent_success():
    with pytest.raises(AclError, match="icacls"):
        make_private(Path("x"), USER, is_dir=False, run=lambda args: Result(5, b"", b"Access is denied."))


def _runner_saving(dacl: str):
    """A fake `icacls <path> /save <file>`: writes `dacl` where the real tool would."""

    def run(args):
        assert args[1:2] and args[2] == "/save"
        Path(args[3]).write_bytes(f"\ufeffsome\\path\n{dacl}\n\n".encode("utf-16-le"))
        return Result(0, b"", b"")

    return run


def test_verify_private_reports_nothing_for_a_private_path_and_the_culprit_otherwise():
    assert verify_private(Path("p"), USER, run=_runner_saving(f"D:P(A;;FA;;;{USER})")) == []
    problems = verify_private(Path("p"), USER, run=_runner_saving(f"D:P(A;;FA;;;{USER})(A;;FR;;;WD)"))
    assert problems and "WD" in problems[0]


def test_verify_private_fails_closed_when_icacls_cannot_be_run():
    def run(args):
        raise FileNotFoundError("icacls")

    assert verify_private(Path("p"), USER, run=run)


# --- the real thing, on Windows CI ----------------------------------------------------------------------------------

WINDOWS_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="real icacls")


@WINDOWS_ONLY
def test_the_real_current_user_sid_looks_like_a_sid():
    assert current_user_sid().startswith("S-1-5-")


@WINDOWS_ONLY
def test_a_file_made_private_verifies_and_one_opened_to_everyone_does_not(tmp_path):
    target = tmp_path / "secret.json"
    target.write_text("{}", encoding="utf-8")
    sid = current_user_sid()
    make_private(target, sid, is_dir=False)
    assert verify_private(target, sid) == []

    subprocess.run(["icacls", str(target), "/grant", "*S-1-1-0:R"], check=True, capture_output=True)
    problems = verify_private(target, sid)
    assert problems and any("S-1-1-0" in p or "WD" in p for p in problems), problems


@WINDOWS_ONLY
def test_a_directory_made_private_verifies(tmp_path):
    directory = tmp_path / "tokens"
    directory.mkdir()
    sid = current_user_sid()
    make_private(directory, sid, is_dir=True)
    assert verify_private(directory, sid) == []
