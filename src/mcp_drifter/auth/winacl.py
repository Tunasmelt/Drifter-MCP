"""Windows file privacy for the token store: a VERIFIED user-private ACL (F-42, DEC-029).

POSIX mode bits mean nothing on Windows, and "I called chmod" is not evidence. So this module
makes a path private and then reads the ACL back and judges it, failing closed: an ACL that
cannot be read, parsed or understood is a problem, never a pass.

Reading uses `icacls <path> /save <file>`, whose SDDL output is locale-independent. Plain
`icacls <path>` prints account NAMES, which Windows translates ("Everyone" is not "Todos"), so
parsing it would silently pass on a non-English machine. The judge (`acl_problems`) is a pure
function over an SDDL string, so its logic is tested on every platform; only the two `icacls`
and one `whoami` calls need Windows, and those run in Windows CI.

What counts as private: every access-allow entry grants only the current user, SYSTEM or
Administrators (who can override any ACL anyway), or the owner/creator pseudo-accounts. Anyone
else, any unknown account alias, any object or conditional allow entry, or a NULL DACL (which
grants everyone everything) is a problem. Deny entries are ignored: they only narrow access.
"""

from __future__ import annotations

import csv
import io
import re
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

Runner = Callable[[list[str]], Any]  # returns an object with .returncode, .stdout, .stderr (bytes)


class AclError(Exception):
    """An ACL could not be read or changed. Never contains file contents."""


def _run(args: list[str]) -> subprocess.CompletedProcess:
    # An argument list, never a shell string: the path is data, not syntax.
    return subprocess.run(args, capture_output=True, check=False)


# SDDL aliases and SIDs that may hold access without making the file shared with other people.
_SAFE_ALIASES = frozenset({"SY", "BA", "OW", "CO"})  # SYSTEM, Administrators, Owner Rights, Creator Owner
_SAFE_SIDS = frozenset({"S-1-5-18", "S-1-5-32-544", "S-1-3-4", "S-1-3-0"})
_ALLOW_TYPES = frozenset({"A", "OA", "XA", "ZA"})
_DENY_TYPES = frozenset({"D", "OD", "XD"})
_SID_RE = re.compile(r"^S-\d-\d+(-\d+)+$")
# SDDL prints a few account SIDs by alias. These two name USER accounts (by their well-known RID), so they
# are "us" only when we are that account. Found by Windows CI: the runner's user is the built-in
# Administrator, so `icacls` printed the user's own entry as `LA` and it was judged a stranger.
_ALIAS_BY_USER_RID = {"500": "LA", "501": "LG"}


def _own_alias(user_sid: str) -> str | None:
    return _ALIAS_BY_USER_RID.get(user_sid.rsplit("-", 1)[-1])


def _split_aces(body: str) -> list[str] | None:
    """The top-level `(...)` groups of a DACL body, or None if the parentheses do not balance
    or there is anything between them. Conditional ACEs nest parentheses, so a regex will not do."""
    aces: list[str] = []
    depth, start = 0, 0
    for index, char in enumerate(body):
        if char == "(":
            if depth == 0:
                start = index + 1
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return None
            if depth == 0:
                aces.append(body[start:index])
        elif depth == 0 and not char.isspace():
            return None
    return aces if depth == 0 else None


def acl_problems(dacl: str, user_sid: str) -> list[str]:
    """Why this DACL (an SDDL string starting `D:`) is not private to `user_sid`; empty if it is."""
    dacl = dacl.strip()
    if not dacl.startswith("D:"):
        return [f"not a DACL: {dacl[:30]!r}"]
    body = dacl[2:]
    flags = re.match(r"^(?:P|AR|AI)*", body)
    body = body[flags.end() if flags else 0 :]
    if body.startswith("NO_ACCESS_CONTROL"):
        return ["the DACL is NULL, which grants everyone full access"]
    aces = _split_aces(body)
    if aces is None:
        return ["the DACL is malformed (unbalanced parentheses or stray text)"]

    own_alias = _own_alias(user_sid)
    problems: list[str] = []
    for ace in aces:
        fields = ace.split(";")
        if len(fields) < 6:
            problems.append(f"malformed ACE {ace[:40]!r}")
            continue
        ace_type, trustee = fields[0], fields[5].strip()
        if ace_type in _DENY_TYPES:
            continue
        if ace_type not in _ALLOW_TYPES:
            problems.append(f"unrecognized ACE type {ace_type!r}")
            continue
        if not trustee:
            problems.append(f"{ace_type} entry with no trustee")
        elif trustee == user_sid or trustee == own_alias or trustee in _SAFE_ALIASES or trustee in _SAFE_SIDS:
            continue
        else:
            problems.append(f"{ace_type} entry grants access to {trustee}")
    return problems


def extract_dacl(saved: bytes) -> str:
    """The `D:...` SDDL out of what `icacls /save` wrote (UTF-16, a name line, then the SDDL)."""
    text = saved.decode("utf-16" if saved[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-16-le", errors="replace")
    for line in text.splitlines():
        match = re.search(r"D:(?:P|AR|AI)*(?:NO_ACCESS_CONTROL)?(?:\(.*)?$", line.strip())
        if match:
            dacl = match.group(0)
            return re.split(r"(?<=\))S:", dacl, maxsplit=1)[0]  # drop a trailing SACL, if any
    raise AclError("icacls output contained no DACL")


def current_user_sid(run: Runner = _run) -> str:
    """The SID of the user running Drifter, from `whoami /user /fo csv /nh` (locale-independent)."""
    result = run(["whoami", "/user", "/fo", "csv", "/nh"])
    if result.returncode != 0:
        raise AclError(f"whoami failed (exit {result.returncode})")
    rows = list(csv.reader(io.StringIO(result.stdout.decode(errors="replace"))))
    sid = rows[0][1].strip() if rows and len(rows[0]) >= 2 else ""
    if not _SID_RE.match(sid):
        raise AclError("could not read the current user's SID from whoami")
    return sid


def make_private(path: Path, user_sid: str, *, is_dir: bool, run: Runner = _run) -> None:
    """Remove inherited permissions and leave exactly one grant: full control for `user_sid`
    (inheritable, for a directory, so files created in it start out private too)."""
    grant = f"*{user_sid}:(OI)(CI)F" if is_dir else f"*{user_sid}:F"
    result = run(["icacls", str(path), "/inheritance:r", "/grant:r", grant])
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or b"").decode(errors="replace").strip()[:200]
        raise AclError(f"icacls could not restrict {path} (exit {result.returncode}): {detail}")


def verify_private(path: Path, user_sid: str, *, run: Runner = _run) -> list[str]:
    """Problems with `path`'s ACL; empty means verified private. Fails closed: if the ACL cannot
    be read, that is itself reported as a problem."""
    try:
        with tempfile.TemporaryDirectory() as scratch:
            saved = Path(scratch) / "acl.txt"
            result = run(["icacls", str(path), "/save", str(saved)])
            if result.returncode != 0:
                return [f"could not read the ACL of {path} (icacls exit {result.returncode})"]
            return acl_problems(extract_dacl(saved.read_bytes()), user_sid)
    except (OSError, AclError) as exc:
        return [f"could not verify the ACL of {path}: {exc}"]
