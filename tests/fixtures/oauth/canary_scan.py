"""F-47's detector: finds a planted canary secret wherever Drifter might write or
print one (docs/FEATURES.md F-47, docs/SPEC.md §5.2).

The canary is a fake token (`drifter-canary-<uuid>`) minted by the OAuth fixture
(`oauth_server.py`); every access token, refresh token, authorization code and client
secret it issues starts with it, so ONE prefix scan covers all of them. This module
only DETECTS. Whether Drifter leaks is decided by tests that run Drifter and hand the
outputs here -- the same split F-04 used (a redaction test that existed before the
recorder did).

Channels scanned, because each one is a place a token has leaked in real clients:
  - files under a directory (`.drifter/`), including file NAMES and binary content
  - log records (message, args, formatted exception text)
  - exception text, with chained causes and ExceptionGroup members
  - plain text (captured stdout/stderr, `auth status` output)

A secret is searched for in the forms a leak usually takes, not just verbatim: raw,
URL-encoded, and base64 -- at all three byte alignments, since `Authorization: Basic
base64(client_id:secret)` puts the secret at an arbitrary offset inside the encoding.

Callers should pass the BARE canary (`state.canary`) along with the full tokens
(`FixtureState.all_secrets()` already does): every secret starts with it, so a token that
was truncated or masked AFTER the prefix still contains it. Known limit, by construction:
a mask that cuts INSIDE the 47-character prefix (say, the first 12 characters of a token)
reveals too little to distinguish from any other text, and no scan can find it.
"""

from __future__ import annotations

import base64
import logging
import os
import traceback
import urllib.parse
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

CANARY_PREFIX = "drifter-canary-"


def new_canary() -> str:
    """A fresh canary prefix. Each fixture instance gets its own, so a leak from one
    test cannot be mistaken for (or masked by) another's."""
    return f"{CANARY_PREFIX}{uuid.uuid4().hex}"


@dataclass(frozen=True)
class Leak:
    channel: str  # "file" | "filename" | "log" | "exception" | "text"
    where: str  # path, logger name, or caller-supplied label
    form: str  # "raw" | "url-encoded" | "base64@0" | "base64@1" | "base64@2"

    def __str__(self) -> str:
        return f"{self.channel}:{self.where} ({self.form})"


def needles(secret: str) -> dict[str, bytes]:
    """The byte strings that mean `secret` leaked, keyed by the form they take.

    For base64 at offset k (k leading bytes before the secret), the last 4-character
    group is dropped (it depends on whatever follows the secret, and on padding), and
    for k > 0 the first group too (it mixes in the k leading bytes). What remains is a
    function of the secret alone, so it appears in any larger encoded string.
    """
    raw = secret.encode()
    forms: dict[str, bytes] = {
        "raw": raw,
        "url-encoded": urllib.parse.quote(secret, safe="").encode(),
    }
    for k in range(3):
        encoded = base64.b64encode(b"\0" * k + raw).decode()
        middle = encoded[:-4] if k == 0 else encoded[4:-4]
        forms[f"base64@{k}"] = middle.encode()
        # URL-safe alphabet (JWT-style segments): same bytes, different 2 characters.
        urlsafe = middle.replace("+", "-").replace("/", "_")
        if urlsafe != middle:
            forms[f"base64url@{k}"] = urlsafe.encode()
    return forms


def _scan_bytes(channel: str, where: str, data: bytes, secrets: Iterable[str]) -> list[Leak]:
    leaks: list[Leak] = []
    for secret in secrets:
        seen: set[bytes] = set()  # a plain token is its own URL-encoding: report it once, as "raw"
        for form, needle in needles(secret).items():
            if needle and needle not in seen and needle in data:
                leaks.append(Leak(channel, where, form))
            seen.add(needle)
    # The bare canary and a full token both match the same text; report each place once.
    return list(dict.fromkeys(leaks))


def scan_text(label: str, text: str, secrets: Iterable[str], channel: str = "text") -> list[Leak]:
    return _scan_bytes(channel, label, text.encode(errors="replace"), secrets)


def scan_tree(root: Path, secrets: Iterable[str]) -> list[Leak]:
    """Every file under `root` (hidden files and binary content included) and every
    file or directory NAME, since a token in a path is still a token on disk."""
    secrets = list(secrets)
    leaks: list[Leak] = []
    if not root.exists():
        return leaks
    for dirpath, dirnames, filenames in os.walk(root):
        for name in [*dirnames, *filenames]:
            path = Path(dirpath) / name
            leaks += _scan_bytes("filename", str(path.relative_to(root)), str(path.relative_to(root)).encode(), secrets)
        for name in filenames:
            path = Path(dirpath) / name
            try:
                data = path.read_bytes()
            except OSError:
                continue
            leaks += _scan_bytes("file", str(path.relative_to(root)), data, secrets)
    return leaks


def scan_log_records(records: Iterable[logging.LogRecord], secrets: Iterable[str]) -> list[Leak]:
    secrets = list(secrets)
    leaks: list[Leak] = []
    for record in records:
        parts = [record.getMessage(), repr(record.args), record.exc_text or ""]
        if record.exc_info and record.exc_info[1] is not None:
            parts.append("".join(traceback.format_exception(*record.exc_info)))
        leaks += scan_text(record.name, "\n".join(parts), secrets, channel="log")
    return leaks


def scan_exception(exc: BaseException, secrets: Iterable[str]) -> list[Leak]:
    """Text of the exception, its args, notes, and everything chained to it.
    `traceback.format_exception` already walks `__cause__`, `__context__` and
    ExceptionGroup members."""
    text = "".join(traceback.format_exception(exc))
    text += repr(getattr(exc, "args", ())) + "\n".join(getattr(exc, "__notes__", []))
    return scan_text(type(exc).__name__, text, secrets, channel="exception")


def assert_no_leaks(leaks: list[Leak]) -> None:
    """Fails naming each channel and place, never the value (the canary is fake, but
    the habit of not echoing a token into test output is the one being tested)."""
    assert not leaks, "canary token leaked: " + "; ".join(str(leak) for leak in leaks)
