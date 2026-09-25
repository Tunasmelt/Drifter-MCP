"""Task candidate generation and approval (F-30, docs/FEATURES.md).

Turns mined workflows into editable YAML task candidates (`status: candidate`) that
the user edits and promotes (`status: approved`). Nothing becomes a task without that
promotion: mining proposes, it never decides. (It also cannot recover INTENT -- a
frequent sequence says what the agent did, not what it was asked -- which is why a
candidate's `prompt` is written empty for the user to fill, never guessed.)

THE FILE BELONGS TO THE USER. They will add comments, write prompts, tighten
assertions. So nothing here ever re-serializes the whole file: `append_entries` adds
text at the end and `approve_in_text` changes one word on one line, and both leave every
other byte exactly as the user left it. When a safe targeted edit is impossible (the
user reformatted an entry beyond what can be located), the operation refuses and says
what to change by hand -- it never falls back to rewriting the file around them.

This module works on TEXT and plain dicts; reading and writing files, and turning an
approved entry into a `TaskConfig`, belong to `cli/` (which is downstream and owns the
task schema). Imports only its own sibling.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

import yaml

from mcp_drifter.mine.prefixspan import Pattern

CANDIDATES_VERSION = 1
STATUSES = ("candidate", "approved")
_ID_MAX = 60

_HEADER = """\
# Task candidates mined by `drifter tasks mine` from your recorded sessions.
# Nothing here is a task until you approve it. For each candidate you want:
#   1. write `prompt` -- what the agent should be asked (mining cannot know your intent),
#   2. review `assert` -- calls, calls_before, never_calls, no_errors (add whatever is
#      right for this task). `never_calls` is pre-filled with the tools your risk
#      classification calls destructive (and your `policy.destructive` list), minus any tool
#      this workflow was seen alongside; a tool it could not classify is NOT listed.
#   3. run `drifter tasks approve <id>`.
# Re-running `drifter tasks mine` only appends patterns not already listed here; it never
# rewrites anything you edited. `pattern`, `support`, `of_trajectories` and `sessions`
# are evidence for you to read -- nothing uses them.
"""


class CandidateFileError(ValueError):
    """The candidates file is malformed, or an operation on it cannot be done safely."""


@dataclass
class CandidateDoc:
    server: str
    entries: list[dict]


def candidate_id(items: Sequence[str], taken: Iterable[str]) -> str:
    taken = set(taken)
    base = re.sub(r"[^A-Za-z0-9_]+", "_", "_".join(items)).strip("_")[:_ID_MAX] or "task"
    if base not in taken:
        return base
    n = 2
    while f"{base}_{n}" in taken:
        n += 1
    return f"{base}_{n}"


def build_entries(
    patterns: Sequence[Pattern],
    total_trajectories: int,
    taken_ids: Iterable[str],
    never_calls: Mapping[tuple[str, ...], Sequence[str]] | None = None,
) -> list[dict]:
    taken = set(taken_ids)
    entries: list[dict] = []
    for p in patterns:
        cid = candidate_id(p.items, taken)
        taken.add(cid)
        calls = list(dict.fromkeys(p.items))
        # Adjacent ordering constraints, skipping a tool "before itself" (repeated
        # calls) and repeats of the same pair: what is asserted is the order in which
        # DIFFERENT tools were reached.
        pairs: list[list[str]] = []
        for earlier, later in zip(p.items, p.items[1:]):
            pair = [earlier, later]
            if earlier != later and pair not in pairs:
                pairs.append(pair)
        entries.append(
            {
                "id": cid,
                "status": "candidate",
                "support": p.support,
                "of_trajectories": total_trajectories,
                "sessions": p.session_count,
                "pattern": list(p.items),
                "prompt": "",
                "assert": {
                    "calls": calls,
                    "calls_before": pairs,
                    # Supplied by the caller (cli/, which owns the risk classification).
                    # Empty unless it says otherwise; a proposal to review, like the rest.
                    "never_calls": list((never_calls or {}).get(p.items, ())),
                    "no_errors": False,
                },
            }
        )
    return entries


def _entry_text(entry: dict) -> str:
    body = yaml.safe_dump(entry, sort_keys=False, default_flow_style=None, width=100).rstrip("\n")
    lines = body.split("\n")
    return "\n".join(["- " + lines[0], *("  " + line for line in lines[1:])]) + "\n"


def render_file(server: str, entries: Sequence[dict]) -> str:
    head = yaml.safe_dump({"version": CANDIDATES_VERSION, "server": server}, sort_keys=False)
    if not entries:
        return _HEADER + head + "candidates: []\n"
    return _HEADER + head + "candidates:\n" + "".join(_entry_text(e) for e in entries)


def read_candidates(text: str) -> CandidateDoc:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise CandidateFileError(f"task candidates file is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise CandidateFileError("task candidates file must be a mapping with version, server and candidates")
    if data.get("version") != CANDIDATES_VERSION:
        raise CandidateFileError(
            f"task candidates file declares version {data.get('version')!r}; "
            f"this build reads version {CANDIDATES_VERSION}"
        )
    server = data.get("server")
    if not isinstance(server, str) or not server:
        raise CandidateFileError("task candidates file needs a `server:` naming the server it was mined from")
    # No `or []`: a missing key, or a null/false/0/{} value, is a file that lost its list,
    # and reading it as "no candidates" would silently drop every approved task.
    raw = data.get("candidates")
    if not isinstance(raw, list):
        raise CandidateFileError(
            "task candidates file needs a `candidates:` list (write `candidates: []` for none)"
        )
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise CandidateFileError(f"candidate #{index + 1} must be a mapping")
        cid = entry.get("id")
        if not isinstance(cid, str) or not cid:
            raise CandidateFileError(f"candidate #{index + 1} has no `id`")
        if entry.get("status") not in STATUSES:
            raise CandidateFileError(
                f"candidate {cid!r} has status {entry.get('status')!r}; expected one of {', '.join(STATUSES)}"
            )
        if cid in seen:
            raise CandidateFileError(f"duplicate candidate id {cid!r}: ids must be unique")
        seen.add(cid)
    return CandidateDoc(server=server, entries=raw)


def append_entries(text: str, server: str, new_entries: Sequence[dict]) -> str:
    """Adds candidates whose pattern the file does not already hold, leaving every
    existing byte alone. Returns `text` unchanged when there is nothing new."""
    doc = read_candidates(text)
    if doc.server != server:
        raise CandidateFileError(
            f"this file was mined from server {doc.server!r}, not {server!r}; "
            "use a separate file per server (--output)"
        )
    existing = {tuple(e.get("pattern") or ()) for e in doc.entries}
    fresh = [e for e in new_entries if tuple(e["pattern"]) not in existing]
    if not fresh:
        return text
    # Match the file's own line-ending convention: appending bare LFs to a CRLF file would
    # leave it half one and half the other, and editors then rewrite every line.
    newline = "\r\n" if "\r\n" in text else "\n"
    block = "".join(_entry_text(e) for e in fresh).replace("\n", newline)
    # `\r?` because with CRLF, `$` matches before the `\n` and so leaves the `\r` behind.
    empty = re.search(r"^candidates:[ \t]*\[\][ \t]*(\r?\n|\Z)", text, flags=re.MULTILINE)
    if empty:
        # Replace only `candidates: []`; its own line ending and everything after it (blank
        # lines, trailing comments) stay, with the new entries slotted in between.
        eol = empty.group(1) or newline
        result = text[: empty.start()] + "candidates:" + eol + block + text[empty.end() :]
    else:
        # Append after the file as it is, trailing blank lines included: only supply the
        # final newline if the last line lacks one.
        result = text + ("" if text.endswith("\n") else newline) + block
    # Appending is only safe when `candidates:` is the file's last top-level key and
    # the user kept the list's indentation. Prove it rather than assume it.
    try:
        after = read_candidates(result)
    except CandidateFileError as exc:
        raise CandidateFileError(f"could not append to the candidates file safely: {exc}") from exc
    if [e["id"] for e in after.entries] != [e["id"] for e in doc.entries] + [e["id"] for e in fresh]:
        raise CandidateFileError(
            "could not append to the candidates file safely (is `candidates:` still the last "
            "key, with its list at column 0?). Move the new entries in by hand or use --output."
        )
    return result


def approve_in_text(text: str, candidate: str) -> str:
    doc = read_candidates(text)
    entry = next((e for e in doc.entries if e["id"] == candidate), None)
    if entry is None:
        known = ", ".join(e["id"] for e in doc.entries) or "(none)"
        raise CandidateFileError(f"no candidate {candidate!r} in this file; candidates: {known}")
    if entry["status"] == "approved":
        raise CandidateFileError(f"candidate {candidate!r} is already approved")

    manual = f"set `status: approved` on candidate {candidate!r} by hand"
    lines = text.splitlines(keepends=True)
    start = next(
        (
            i
            for i, line in enumerate(lines)
            if re.fullmatch(rf"- id:\s*['\"]?{re.escape(candidate)}['\"]?\s*", line.rstrip("\r\n"))
        ),
        None,
    )
    if start is None:
        raise CandidateFileError(f"could not locate candidate {candidate!r} to edit safely; {manual}")
    for i in range(start + 1, len(lines)):
        content = lines[i].rstrip("\r\n")
        if content.startswith("- "):
            break
        if re.match(r"^  status:\s*candidate\b", content):
            ending = lines[i][len(content) :]
            lines[i] = re.sub(r"candidate\b", "approved", content, count=1) + ending
            result = "".join(lines)
            check = next(e for e in read_candidates(result).entries if e["id"] == candidate)
            if check["status"] != "approved":  # pragma: no cover - belt and braces
                raise CandidateFileError(f"edit did not take; {manual}")
            return result
    raise CandidateFileError(f"could not locate candidate {candidate!r}'s status line to edit safely; {manual}")


def approved_entries(doc: CandidateDoc) -> list[dict]:
    return [e for e in doc.entries if e["status"] == "approved"]


def uncovered_tools(all_tools: Sequence[str], entries: Iterable[dict]) -> list[str]:
    """Tools no given (approved) task asserts a call to -- what is still untested."""
    covered = {
        tool for e in entries for tool in ((e.get("assert") or {}).get("calls") or [])
    }
    return [t for t in all_tools if t not in covered]
