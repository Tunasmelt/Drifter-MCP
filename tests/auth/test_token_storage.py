"""F-42: where OAuth tokens live (docs/FEATURES.md F-42, docs/SPEC.md §5.2, DEC-029).

Written before the implementation. What "done" means:
  - one implementation of the SDK's `TokenStorage` protocol, keyed by server name + canonical
    URL, with two backends: the OS keyring (optional extra, usable backend) and a private file in
    the user's config directory, outside the project and `.drifter/`;
  - it refuses, rather than quietly uses, anything it cannot show is private to this user: a
    file or directory with wider permissions, a symlink, a file owned by someone else, a location
    inside the project;
  - it stores tokens and client registration info and NOTHING ELSE: never the authorization code
    or the PKCE verifier;
  - it keeps the ABSOLUTE expiry. The SDK loads tokens from storage with no expiry at all, so a
    token that expired hours ago looks valid to a new process (found while designing this);
    without the stored expiry F-44 could not refresh instead of failing.

POSIX permission checks run on POSIX; the Windows ACL checks have their own file
(`test_windows_acl.py`) and run on Windows CI.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import time
from pathlib import Path

import anyio
import pytest
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

from canary_scan import assert_no_leaks, scan_tree
from mcp_drifter.auth.storage import (
    InsecureStorageError,
    StorageError,
    StorageUnavailableError,
    TokenStore,
    canonical_server_url,
    default_base_dir,
    storage_key,
)
from oauth_server import FixtureConfig, serve_oauth_fixture
from sdk_client import initialized_session, sdk_provider, tool_names

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
URL = "https://mcp.example.com/mcp"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _token(**overrides) -> OAuthToken:
    values = {"access_token": "access-AAAA", "token_type": "Bearer", "expires_in": 3600, "scope": "read", "refresh_token": "refresh-RRRR"}
    return OAuthToken(**{**values, **overrides})


def _client_info() -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id="client-123",
        client_secret="client-secret-SSSS",
        redirect_uris=[AnyUrl("http://127.0.0.1:53682/callback")],
        token_endpoint_auth_method="client_secret_post",
    )


def _file_store(tmp_path: Path, name: str = "remote-crm", url: str = URL, **kwargs) -> TokenStore:
    return TokenStore(name, url, base_dir=tmp_path / "config", backend="file", **kwargs)


# --- keys: server name + canonical URL ----------------------------------------------------------


@pytest.mark.parametrize(
    "raw, canonical",
    [
        ("https://MCP.Example.com/mcp", "https://mcp.example.com/mcp"),
        ("HTTPS://mcp.example.com:443/mcp", "https://mcp.example.com/mcp"),
        ("http://mcp.example.com:80/mcp", "http://mcp.example.com/mcp"),
        ("https://mcp.example.com:8443/mcp", "https://mcp.example.com:8443/mcp"),
        ("https://mcp.example.com/mcp/", "https://mcp.example.com/mcp"),
        ("https://mcp.example.com/mcp#frag", "https://mcp.example.com/mcp"),
        ("https://mcp.example.com", "https://mcp.example.com"),
        ("https://mcp.example.com/", "https://mcp.example.com"),
    ],
)
def test_the_url_is_canonicalized_so_spelling_differences_share_one_login(raw, canonical):
    assert canonical_server_url(raw) == canonical


def test_the_key_depends_on_both_the_name_and_the_canonical_url():
    base = storage_key("crm", "https://a.example.com/mcp")
    assert storage_key("crm", "https://A.example.com:443/mcp/") == base  # same server, spelled differently
    assert storage_key("other", "https://a.example.com/mcp") != base
    assert storage_key("crm", "https://b.example.com/mcp") != base
    assert storage_key("crm", "https://a.example.com/other") != base


@pytest.mark.parametrize("name", ["crm", "my crm", "../../etc/passwd", "a/b", "name\\with\\slashes", "naïve", "x" * 300, "", "."])
def test_a_hostile_or_odd_server_name_cannot_escape_the_token_directory(tmp_path, name):
    key = storage_key(name, URL)
    assert re.fullmatch(r"[A-Za-z0-9._-]{1,120}", key), key
    store = _file_store(tmp_path, name=name)
    assert (tmp_path / "config" / "tokens").resolve() in store.file_path.resolve().parents


# --- the file backend ----------------------------------------------------------------------------


@pytest.mark.anyio
async def test_tokens_and_client_info_round_trip_through_the_file_backend(tmp_path):
    store = _file_store(tmp_path)
    assert await store.get_tokens() is None and await store.get_client_info() is None

    await store.set_tokens(_token())
    await store.set_client_info(_client_info())

    fresh = _file_store(tmp_path)  # a later process
    assert await fresh.get_tokens() == _token()
    assert await fresh.get_client_info() == _client_info()
    assert fresh.backend_name == "file"


@pytest.mark.anyio
async def test_the_file_holds_exactly_tokens_client_info_and_bookkeeping_and_nothing_else(tmp_path):
    """No authorization code, no PKCE verifier, no `state`: the protocol never hands them over,
    and the file format has no place to put them."""
    store = _file_store(tmp_path)
    await store.set_tokens(_token())
    await store.set_client_info(_client_info())
    data = json.loads(store.file_path.read_text(encoding="utf-8"))
    assert set(data) == {"format", "server", "url", "tokens", "client_info", "expires_at", "saved_at"}
    assert data["server"] == "remote-crm" and data["url"] == canonical_server_url(URL)

    def keys(node) -> set[str]:
        if isinstance(node, dict):
            return set(node) | {k for value in node.values() for k in keys(value)}
        if isinstance(node, list):
            return {k for value in node for k in keys(value)}
        return set()

    # Judged by KEY, not substring: "authorization_code" is a legitimate grant-type VALUE in the
    # client registration. What must never exist is a field that could hold the code itself.
    assert not keys(data) & {"code", "code_verifier", "verifier", "state", "authorization_code", "pkce", "code_challenge"}


@pytest.mark.anyio
async def test_the_absolute_expiry_is_stored_so_a_later_process_can_tell_the_token_is_stale(tmp_path):
    store = _file_store(tmp_path)
    before = time.time()
    await store.set_tokens(_token(expires_in=100))
    after = time.time()

    fresh = _file_store(tmp_path)
    expiry = await fresh.get_expiry()
    assert expiry is not None and before + 100 <= expiry <= after + 100
    assert fresh.summary().expires_at == expiry


@pytest.mark.anyio
async def test_a_token_with_no_expires_in_has_no_stored_expiry(tmp_path):
    store = _file_store(tmp_path)
    await store.set_tokens(_token(expires_in=None))
    assert await store.get_expiry() is None


@pytest.mark.anyio
async def test_setting_tokens_keeps_the_client_info_and_vice_versa(tmp_path):
    store = _file_store(tmp_path)
    await store.set_client_info(_client_info())
    await store.set_tokens(_token())
    await store.set_tokens(_token(access_token="access-NEW"))
    fresh = _file_store(tmp_path)
    assert (await fresh.get_tokens()).access_token == "access-NEW"
    assert await fresh.get_client_info() == _client_info()


@pytest.mark.anyio
async def test_a_second_write_replaces_the_first_and_leaves_no_temporary_files(tmp_path):
    store = _file_store(tmp_path)
    for n in range(3):
        await store.set_tokens(_token(access_token=f"access-{n}"))
    assert sorted(p.name for p in store.file_path.parent.iterdir()) == [store.file_path.name]


@pytest.mark.anyio
async def test_the_summary_reports_state_without_any_token_material(tmp_path):
    store = _file_store(tmp_path)
    await store.set_tokens(_token())
    await store.set_client_info(_client_info())
    summary = store.summary()
    assert (summary.backend, summary.has_tokens, summary.has_client_info, summary.refreshable) == ("file", True, True, True)
    assert summary.scope == "read"
    shown = repr(summary) + str(summary)
    for secret in ("access-AAAA", "refresh-RRRR", "client-secret-SSSS"):
        assert secret not in shown


def test_the_summary_of_nothing_stored_says_so(tmp_path):
    summary = _file_store(tmp_path).summary()
    assert (summary.has_tokens, summary.has_client_info, summary.refreshable, summary.expires_at) == (False, False, False, None)


@pytest.mark.anyio
async def test_delete_removes_the_login_and_reports_whether_there_was_one(tmp_path):
    store = _file_store(tmp_path)
    assert store.delete() is False
    await store.set_tokens(_token())
    assert store.delete() is True
    assert not store.file_path.exists()
    assert await _file_store(tmp_path).get_tokens() is None
    assert store.delete() is False


@pytest.mark.anyio
async def test_two_servers_do_not_see_each_others_logins(tmp_path):
    a, b = _file_store(tmp_path, name="a"), _file_store(tmp_path, name="b", url="https://other.example.com/mcp")
    await a.set_tokens(_token(access_token="access-A"))
    assert await b.get_tokens() is None


# --- refusing what it cannot show is private ---------------------------------------------------------


@POSIX_ONLY
@pytest.mark.anyio
@pytest.mark.parametrize("umask", [0o022, 0o000, 0o077])
async def test_the_file_is_0600_and_its_directories_0700_whatever_the_umask(tmp_path, umask):
    previous = os.umask(umask)
    try:
        store = _file_store(tmp_path)
        await store.set_tokens(_token())
    finally:
        os.umask(previous)
    assert stat.S_IMODE(store.file_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.file_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.file_path.parent.parent.stat().st_mode) == 0o700  # the drifter config dir


@POSIX_ONLY
@pytest.mark.anyio
@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o666, 0o660, 0o606])
async def test_a_file_with_wider_permissions_is_refused_not_silently_used(tmp_path, mode):
    store = _file_store(tmp_path)
    await store.set_tokens(_token(access_token="access-SECRET-VALUE"))
    store.file_path.chmod(mode)
    for operation in (store.get_tokens, store.get_client_info, store.get_expiry):
        with pytest.raises(InsecureStorageError) as failure:
            await operation()
        assert str(store.file_path) in str(failure.value) and "chmod 600" in str(failure.value)
        assert "access-SECRET-VALUE" not in str(failure.value)
    with pytest.raises(InsecureStorageError):
        await store.set_tokens(_token())  # nor is it overwritten


@POSIX_ONLY
@pytest.mark.anyio
@pytest.mark.parametrize("mode", [0o755, 0o750, 0o705, 0o770])
async def test_a_token_directory_open_to_others_is_refused(tmp_path, mode):
    store = _file_store(tmp_path)
    await store.set_tokens(_token())
    store.file_path.parent.chmod(mode)
    with pytest.raises(InsecureStorageError, match="chmod 700"):
        await store.get_tokens()
    store.file_path.parent.chmod(0o700)


@POSIX_ONLY
@pytest.mark.anyio
async def test_a_symlinked_token_file_is_refused(tmp_path):
    store = _file_store(tmp_path)
    await store.set_tokens(_token())
    real = tmp_path / "elsewhere.json"
    real.write_bytes(store.file_path.read_bytes())
    real.chmod(0o600)
    store.file_path.unlink()
    store.file_path.symlink_to(real)
    with pytest.raises(InsecureStorageError, match="symbolic link"):
        await store.get_tokens()


@POSIX_ONLY
@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() != 0, reason="needs root to hand a file to another user")
@pytest.mark.anyio
async def test_a_file_owned_by_another_user_is_refused(tmp_path):
    store = _file_store(tmp_path)
    await store.set_tokens(_token())
    os.chown(store.file_path, 12345, 12345)
    with pytest.raises(InsecureStorageError, match="owned by"):
        await store.get_tokens()


@pytest.mark.anyio
async def test_a_corrupt_file_is_a_clear_storage_error_that_does_not_print_its_contents(tmp_path):
    store = _file_store(tmp_path)
    await store.set_tokens(_token())
    store.file_path.write_text('{"tokens": {"access_token": "access-LEAKED"', encoding="utf-8")
    if sys.platform != "win32":
        store.file_path.chmod(0o600)
    with pytest.raises(StorageError) as failure:
        await store.get_tokens()
    assert str(store.file_path) in str(failure.value) and "access-LEAKED" not in str(failure.value)
    assert "drifter auth logout" in str(failure.value)


@pytest.mark.anyio
async def test_a_file_from_a_newer_format_is_refused_not_misread(tmp_path):
    store = _file_store(tmp_path)
    await store.set_tokens(_token())
    data = json.loads(store.file_path.read_text(encoding="utf-8"))
    data["format"] = 99
    store.file_path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(StorageError, match="format"):
        await store.get_tokens()


# --- never inside the project --------------------------------------------------------------------------


def test_the_default_location_is_the_user_config_dir_and_never_inside_a_project(tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / ".drifter" / "runs").mkdir(parents=True)
    monkeypatch.chdir(project)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "home" / "AppData" / "Local"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    base = default_base_dir()
    store = TokenStore("remote-crm", URL, project_root=project, backend="file")
    for path in (base, store.file_path):
        assert project.resolve() not in path.resolve().parents and path.resolve() != project.resolve()
    if sys.platform == "win32":
        assert base == tmp_path / "home" / "AppData" / "Local" / "drifter"
    else:
        assert base == tmp_path / "home" / ".config" / "drifter"


def test_xdg_config_home_is_honored_on_posix(tmp_path, monkeypatch):
    if sys.platform == "win32":
        pytest.skip("POSIX only")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert default_base_dir() == tmp_path / "xdg" / "drifter"


@pytest.mark.parametrize("inside", [".drifter", ".drifter/runs", "", "sub/dir"])
def test_a_token_location_inside_the_project_is_refused(tmp_path, inside):
    project = tmp_path / "project"
    project.mkdir()
    with pytest.raises(InsecureStorageError, match="inside the project"):
        TokenStore("remote-crm", URL, base_dir=project / inside / "drifter", project_root=project, backend="file")


def test_a_location_beside_the_project_is_fine(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    TokenStore("remote-crm", URL, base_dir=tmp_path / "project-tokens", project_root=project, backend="file")


# --- the keyring backend ------------------------------------------------------------------------------------

keyring = pytest.importorskip("keyring", reason="the optional keyring extra")
from keyring.backend import KeyringBackend  # noqa: E402
from keyring.backends.fail import Keyring as FailKeyring  # noqa: E402
from keyring.errors import PasswordSetError  # noqa: E402


class MemoryKeyring(KeyringBackend):
    """A real, working keyring that lives in a dict: what a usable OS backend looks like to the code."""

    priority = 1

    def __init__(self) -> None:
        super().__init__()
        self.items: dict[tuple[str, str], str] = {}

    def set_password(self, service, username, password):
        self.items[(service, username)] = password

    def get_password(self, service, username):
        return self.items.get((service, username))

    def delete_password(self, service, username):
        self.items.pop((service, username), None)


class TooBigKeyring(MemoryKeyring):
    """Windows Credential Manager's behavior for a blob over its limit."""

    def set_password(self, service, username, password):
        if len(password) > 200:
            raise PasswordSetError("credential blob too large")
        super().set_password(service, username, password)


@pytest.fixture
def memory_keyring():
    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(previous)


@pytest.fixture
def no_keyring():
    previous = keyring.get_keyring()
    keyring.set_keyring(FailKeyring())
    yield
    keyring.set_keyring(previous)


def _auto_store(tmp_path: Path, **kwargs) -> TokenStore:
    return TokenStore("remote-crm", URL, base_dir=tmp_path / "config", backend="auto", **kwargs)


@pytest.mark.anyio
async def test_auto_uses_a_usable_keyring_and_writes_no_file(tmp_path, memory_keyring):
    store = _auto_store(tmp_path)
    assert store.backend_name == "keyring"
    await store.set_tokens(_token())
    await store.set_client_info(_client_info())

    fresh = _auto_store(tmp_path)
    assert await fresh.get_tokens() == _token()
    assert await fresh.get_client_info() == _client_info()
    assert not (tmp_path / "config").exists()  # nothing touched the disk
    (service, username), blob = next(iter(memory_keyring.items.items()))
    assert service == "mcp-drifter" and username == storage_key("remote-crm", URL)
    assert json.loads(blob)["tokens"]["access_token"] == "access-AAAA"


@pytest.mark.anyio
async def test_the_keyring_keeps_the_expiry_too(tmp_path, memory_keyring):
    await _auto_store(tmp_path).set_tokens(_token(expires_in=50))
    expiry = await _auto_store(tmp_path).get_expiry()
    assert expiry is not None and time.time() + 40 < expiry < time.time() + 51


@pytest.mark.anyio
async def test_an_unusable_keyring_falls_back_to_the_private_file(tmp_path, no_keyring):
    store = _auto_store(tmp_path)
    assert store.backend_name == "file"
    await store.set_tokens(_token())
    assert store.file_path.exists()
    assert await _auto_store(tmp_path).get_tokens() == _token()


def test_asking_for_the_keyring_when_there_is_none_is_an_error_not_a_quiet_fallback(tmp_path, no_keyring):
    with pytest.raises(StorageUnavailableError, match="keyring"):
        TokenStore("remote-crm", URL, base_dir=tmp_path / "config", backend="keyring").backend_name


def test_a_missing_keyring_package_means_auto_uses_the_file(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "keyring", None)  # `import keyring` now raises ImportError
    assert _auto_store(tmp_path).backend_name == "file"
    with pytest.raises(StorageUnavailableError, match="mcp-drifter\\[keyring\\]"):
        TokenStore("remote-crm", URL, base_dir=tmp_path / "config", backend="keyring").backend_name


@pytest.mark.anyio
async def test_a_value_the_keyring_will_not_hold_falls_back_to_the_file_and_is_still_found(tmp_path):
    previous = keyring.get_keyring()
    keyring.set_keyring(TooBigKeyring())
    try:
        store = _auto_store(tmp_path)
        assert store.backend_name == "keyring"
        await store.set_tokens(_token(access_token="a" * 400))  # a large JWT, over the backend's limit
        assert store.file_path.exists()
        fresh = _auto_store(tmp_path)
        assert (await fresh.get_tokens()).access_token == "a" * 400
    finally:
        keyring.set_keyring(previous)


@pytest.mark.anyio
async def test_delete_clears_both_places(tmp_path, memory_keyring):
    store = _auto_store(tmp_path)
    await store.set_tokens(_token())
    # and a stale file from an earlier run on a machine that had no keyring
    file_store = _file_store(tmp_path)
    await file_store.set_tokens(_token(access_token="access-OLD"))
    assert store.delete() is True
    assert memory_keyring.items == {} and not file_store.file_path.exists()


# --- it is a real SDK TokenStorage: a login persists across "processes" -------------------------------------------


@pytest.mark.anyio
async def test_a_login_made_through_the_store_is_reused_by_a_later_one_without_logging_in_again(tmp_path):
    async with serve_oauth_fixture(FixtureConfig(registration="dcr")) as handle:
        first = TokenStore("remote-crm", handle.mcp_url, base_dir=tmp_path / "config", backend="file")
        async with initialized_session(handle, sdk_provider(handle, first)) as session:
            assert await tool_names(session) == ["get_item", "list_items"]
        assert handle.state.authorize_requests == 1

        later = TokenStore("remote-crm", handle.mcp_url, base_dir=tmp_path / "config", backend="file")
        async with initialized_session(handle, sdk_provider(handle, later)) as session:
            assert await tool_names(session) == ["get_item", "list_items"]
        assert handle.state.authorize_requests == 1  # no second login
        assert handle.state.registrations == 1  # and no second registration

        # The tokens really are in the user's config dir (this IS where they belong) and nowhere a
        # project would pick them up.
        in_config = scan_tree(tmp_path / "config", handle.state.all_secrets())
        assert in_config, "the token store should hold the real tokens"
        project = tmp_path / "project"
        project.mkdir()
        assert_no_leaks(scan_tree(project, handle.state.all_secrets()))


# --- CLAUDE.md async-shutdown rule: a stuck keyring must not hang the process, timed ------------------------


class _BlockingKeyring(MemoryKeyring):
    """A keyring whose read never returns, like one waiting on an unlock prompt nobody answers."""

    release = None  # set per test

    def get_password(self, service, username):
        type(self).release.wait(timeout=30)
        return None


@pytest.mark.anyio
async def test_cancelling_a_stuck_keyring_read_returns_promptly_and_leaves_a_daemon_thread():
    import threading

    previous = keyring.get_keyring()
    _BlockingKeyring.release = threading.Event()
    keyring.set_keyring(_BlockingKeyring())
    try:
        store = TokenStore("remote-crm", URL, base_dir=Path("unused"), backend="auto")
        started = time.monotonic()
        with anyio.move_on_after(0.3):
            await store.get_tokens()
        elapsed = time.monotonic() - started
        assert elapsed < 1.5, f"cancelling a stuck keyring call took {elapsed:.2f}s"

        stuck = [t for t in threading.enumerate() if t.name == "drifter-keyring"]
        assert stuck and all(t.daemon for t in stuck), "the abandoned thread must not be able to block interpreter exit"
    finally:
        _BlockingKeyring.release.set()
        keyring.set_keyring(previous)
        for thread in [t for t in threading.enumerate() if t.name == "drifter-keyring"]:
            thread.join(timeout=5)


def test_a_process_that_abandoned_a_stuck_keyring_call_still_exits(tmp_path):
    """The real property, in a real process: anyio's own worker threads are NOT daemons, so
    `to_thread.run_sync(abandon_on_cancel=True)` would free the event loop but leave the
    interpreter waiting forever on the stuck thread at exit."""
    import subprocess

    script = """
import threading, anyio, keyring
from keyring.backend import KeyringBackend

class Stuck(KeyringBackend):
    priority = 1
    def get_password(self, service, username):
        threading.Event().wait()          # forever
    def set_password(self, *args): pass
    def delete_password(self, *args): pass

keyring.set_keyring(Stuck())
from mcp_drifter.auth.storage import TokenStore

async def main():
    store = TokenStore("remote-crm", "https://mcp.example.com/mcp", base_dir=%r, backend="auto")
    with anyio.move_on_after(0.3):
        await store.get_tokens()

anyio.run(main)
print("main finished")
""" % str(tmp_path / "config")
    started = time.monotonic()
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0 and "main finished" in result.stdout, (result.stdout, result.stderr)
    assert time.monotonic() - started < 15
