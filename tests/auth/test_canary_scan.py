"""F-47, first half (docs/FEATURES.md F-47, docs/PHASES.md 0.3.0 P1): the canary
detector exists, and it FAILS against a deliberately leaky stub.

This is the same discipline as F-04, whose redaction test existed before the recorder
did. Nothing here runs Drifter's auth code (none exists). It proves the detector the
later end-to-end F-47 test will rely on really catches a token on every channel a
token has leaked through in real clients -- because a leak detector that was never
shown to detect anything passes for the wrong reason, and "it passed" is the one
result this codebase has learned not to trust.

Once F-41-F-46 exist, F-47's own "Done when" is: this detector, pointed at the output
of `auth login`, `observe`, a forced refresh, a forced 401 and a forced 403, finds
nothing.
"""

from __future__ import annotations

import base64
import logging
import urllib.parse
from pathlib import Path

import pytest

from canary_scan import (
    Leak,
    assert_no_leaks,
    needles,
    new_canary,
    scan_exception,
    scan_log_records,
    scan_text,
    scan_tree,
)
from oauth_server import serve_oauth_fixture
from sdk_client import MemoryStorage, initialized_session, sdk_provider


@pytest.fixture
def anyio_backend():
    return "asyncio"


SECRET = new_canary() + "-access-1"


class LeakyStub:
    """A deliberately bad stand-in for Drifter: it writes the token everywhere the real
    code must never write it. Each method is one channel."""

    def __init__(self, token: str) -> None:
        self.token = token

    def write_session(self, drifter_dir: Path) -> None:
        runs = drifter_dir / "runs"
        runs.mkdir(parents=True)
        (runs / "session.jsonl").write_text(f'{{"auth": {{"type": "oauth", "authorization": "Bearer {self.token}"}}}}\n')

    def write_token_into_a_filename(self, drifter_dir: Path) -> None:
        drifter_dir.mkdir(parents=True, exist_ok=True)
        (drifter_dir / f"tokens-{self.token}.json").write_text("{}")

    def log_it(self, logger: logging.Logger) -> None:
        logger.warning("refreshing with %s", self.token)

    def log_it_via_a_traceback(self, logger: logging.Logger) -> None:
        try:
            raise RuntimeError(f"upstream said 401 for {self.token}")
        except RuntimeError:
            logger.exception("auth failed")

    def fail(self) -> RuntimeError:
        return RuntimeError(f"token {self.token} was rejected")

    def fail_with_the_token_only_in_the_cause(self) -> RuntimeError:
        try:
            try:
                raise ValueError(f"bad token {self.token}")
            except ValueError as inner:
                raise RuntimeError("login failed") from inner
        except RuntimeError as outer:
            return outer

    def fail_inside_an_exception_group(self) -> BaseException:
        return ExceptionGroup("unhandled errors in a TaskGroup", [OSError(f"connect failed for {self.token}")])

    def status_text(self) -> str:
        return f"server: remote-crm\ntoken: {self.token}\nexpires: soon\n"


def _leak_channels(stub: LeakyStub, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> dict[str, list[Leak]]:
    secrets = [stub.token]
    drifter_dir = tmp_path / ".drifter"
    stub.write_session(drifter_dir)
    with caplog.at_level(logging.DEBUG):
        stub.log_it(logging.getLogger("leaky"))
    return {
        "file": scan_tree(drifter_dir, secrets),
        "log": scan_log_records(caplog.records, secrets),
        "exception": scan_exception(stub.fail(), secrets),
        "text": scan_text("auth status", stub.status_text(), secrets),
    }


def test_the_detector_finds_the_token_on_every_channel_a_leaky_stub_writes(tmp_path, caplog):
    for channel, leaks in _leak_channels(LeakyStub(SECRET), tmp_path, caplog).items():
        assert leaks, f"detector missed a token leaked through the {channel} channel"
        assert all(leak.channel in (channel, "filename") for leak in leaks)


def test_the_f47_assertion_fails_against_the_leaky_stub(tmp_path, caplog):
    """The literal F-47 'red' state: the assertion the real test will make, run against a
    stub that leaks, fails -- and names where, without echoing the token."""
    leaks = [leak for found in _leak_channels(LeakyStub(SECRET), tmp_path, caplog).values() for leak in found]
    with pytest.raises(AssertionError, match="canary token leaked") as failure:
        assert_no_leaks(leaks)
    assert SECRET not in str(failure.value)
    assert "session.jsonl" in str(failure.value)


def test_a_token_in_a_file_or_directory_NAME_is_a_leak(tmp_path):
    LeakyStub(SECRET).write_token_into_a_filename(tmp_path / ".drifter")
    leaks = scan_tree(tmp_path / ".drifter", [SECRET])
    assert [leak.channel for leak in leaks] == ["filename"]


def test_a_traceback_logged_through_logger_exception_is_scanned(caplog):
    with caplog.at_level(logging.DEBUG):
        LeakyStub(SECRET).log_it_via_a_traceback(logging.getLogger("leaky"))
    # The token is in the formatted traceback and in exc_info, not in the log message itself.
    assert "auth failed" == caplog.records[0].getMessage()
    assert scan_log_records(caplog.records, [SECRET])


def test_a_token_only_in_a_chained_cause_or_an_exception_group_is_found():
    stub = LeakyStub(SECRET)
    assert scan_exception(stub.fail_with_the_token_only_in_the_cause(), [SECRET])
    assert scan_exception(stub.fail_inside_an_exception_group(), [SECRET])


def test_binary_files_are_scanned_too(tmp_path):
    (tmp_path / "blob.bin").write_bytes(b"\x00\xff\x00" + SECRET.encode() + b"\xfe\x00")
    assert [leak.where for leak in scan_tree(tmp_path, [SECRET])] == ["blob.bin"]


# --- encoded forms: the leaks a plain substring search would miss ----------------------


def _b64(data: str) -> str:
    return base64.b64encode(data.encode()).decode()


@pytest.mark.parametrize(
    "rendering",
    [
        pytest.param(lambda s: _b64(s), id="base64-of-the-secret-alone"),
        pytest.param(lambda s: _b64("x" + s), id="base64-offset-1"),
        pytest.param(lambda s: _b64("xy" + s), id="base64-offset-2"),
        pytest.param(lambda s: "Authorization: Basic " + _b64("drifter-client:" + s), id="basic-auth-header"),
        pytest.param(lambda s: "Authorization: Basic " + _b64("c:" + s + ":and-more-trailing-bytes"), id="basic-auth-with-suffix"),
        pytest.param(lambda s: base64.urlsafe_b64encode(("a" + s).encode()).decode(), id="base64url-segment"),
    ],
)
def test_base64_wrapped_tokens_are_found_at_every_alignment(rendering):
    assert scan_text("header log", rendering(SECRET), [SECRET]), rendering(SECRET)


def test_a_url_encoded_token_is_found():
    odd = new_canary() + "-tok/en+with space"
    encoded = urllib.parse.quote(odd, safe="")
    assert encoded != odd
    assert scan_text("redirect url", f"http://127.0.0.1/cb?access_token={encoded}", [odd])


# --- no false positives -------------------------------------------------------------------


def test_clean_output_passes_and_so_does_another_instances_canary(tmp_path, caplog):
    other = new_canary() + "-access-1"
    drifter_dir = tmp_path / ".drifter" / "runs"
    drifter_dir.mkdir(parents=True)
    (drifter_dir / "session.jsonl").write_text('{"auth": {"type": "oauth", "granted_scopes": ["read"]}}\n')
    (drifter_dir / "note.txt").write_text(f"token: [REDACTED]  {other}\n")
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("clean").warning("login ok for %s", "remote-crm")

    leaks = [
        *scan_tree(tmp_path, [SECRET]),
        *scan_log_records(caplog.records, [SECRET]),
        *scan_exception(RuntimeError("login required: run `drifter auth login remote-crm`"), [SECRET]),
        *scan_text("auth status", "token present: yes\nrefreshable: yes\n", [SECRET]),
    ]
    assert_no_leaks(leaks)


def test_a_secret_that_merely_shares_the_canary_prefix_is_not_confused_with_it():
    assert scan_text("x", new_canary() + "-access-1", [SECRET]) == []


def test_needles_are_long_enough_not_to_match_by_accident():
    for form, needle in needles(SECRET).items():
        assert len(needle) >= 20, form


# --- tied to real fixture-minted secrets ---------------------------------------------------


@pytest.mark.anyio
async def test_a_real_fixture_token_written_to_disk_is_caught_and_a_clean_dir_is_not(tmp_path):
    async with serve_oauth_fixture() as handle:
        storage = MemoryStorage()
        async with initialized_session(handle, sdk_provider(handle, storage)):
            pass
        assert storage.tokens is not None and storage.tokens.refresh_token is not None
        secrets = handle.state.all_secrets()

        clean = tmp_path / "clean"
        clean.mkdir()
        (clean / "session.jsonl").write_text('{"auth": {"type": "oauth"}}\n')
        assert_no_leaks(scan_tree(clean, secrets))

        leaky = tmp_path / "leaky"
        leaky.mkdir()
        (leaky / "tokens.json").write_text(storage.tokens.model_dump_json())
        found = scan_tree(leaky, secrets)
        assert found and {leak.where for leak in found} == {"tokens.json"}
