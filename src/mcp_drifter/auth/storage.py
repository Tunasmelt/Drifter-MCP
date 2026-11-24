"""Where OAuth tokens live (F-42, docs/SPEC.md §5.2, DEC-029).

One implementation of the MCP SDK's `TokenStorage` protocol, keyed by server name + canonical URL:

  - the OS keyring, when the optional `mcp-drifter[keyring]` extra is installed and a usable
    backend exists (macOS Keychain, Windows Credential Manager, a running Secret Service);
  - otherwise a private JSON file in the user's config directory (`~/.config/drifter/tokens/`,
    or `$XDG_CONFIG_HOME/drifter/tokens/`; `%LOCALAPPDATA%\\drifter\\tokens\\` on Windows).

It never stores anything inside the project or `.drifter/`, and it REFUSES, rather than quietly
uses, anything it cannot show is private to this user: a file or directory with wider
permissions, a symbolic link, a file owned by someone else, or on Windows an ACL that grants
anyone else access (verified by reading the ACL back, see `winacl.py`).

What it stores: the tokens and the client registration, plus bookkeeping. Never the
authorization code or the PKCE verifier (the SDK never hands those to storage, and the file
format has no field for them), and no token ever appears in an error message or a log line.

It also stores the ABSOLUTE expiry (`expires_at`). The SDK loads tokens from storage with no
expiry at all, so a token that expired hours ago looks valid to a fresh process and is sent
as-is; without this, F-44 could not refresh a stale token instead of failing.

Known limits, stated rather than hidden: two processes updating the same login at the same moment
can lose one update (writes are atomic, so the file is never torn, but this is not a locked
read-modify-write; logins are manual and rare). A value too large for the keyring (Windows
Credential Manager caps a credential at a few KB, below some JWTs) is written to the private
file instead, and the stale keyring entry removed so there is one source of truth.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, TypeVar
from urllib.parse import urlsplit

import anyio
import anyio.from_thread
import anyio.lowlevel
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from mcp_drifter.auth import winacl

logger = logging.getLogger(__name__)

KEYRING_SERVICE = "mcp-drifter"
FORMAT_VERSION = 1
_DEFAULT_PORTS = {"http": 80, "https": 443}
_T = TypeVar("_T")


class StorageError(Exception):
    """Token storage failed. Messages name paths and servers, never token material."""


class InsecureStorageError(StorageError):
    """The storage location or file is not provably private to this user, so it is refused."""


class StorageUnavailableError(StorageError):
    """A requested backend cannot be used (no keyring), or protections cannot be established."""


# --- keys and locations ---------------------------------------------------------------------------


def canonical_server_url(url: str) -> str:
    """Spelling-independent form of a server URL: lowercase scheme and host, default port and
    trailing slash dropped, fragment and any user:password@ dropped (credentials must not
    reach a token file or a key)."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    port = parts.port
    netloc = host if port is None or port == _DEFAULT_PORTS.get(scheme) else f"{host}:{port}"
    path = parts.path.rstrip("/")
    query = f"?{parts.query}" if parts.query else ""
    return f"{scheme}://{netloc}{path}{query}"


def storage_key(server_name: str, server_url: str) -> str:
    """A safe file/keyring name for one server's login: a readable prefix plus a hash of the
    exact (name, canonical URL) pair, so odd or hostile names cannot escape the token directory
    and two servers cannot share a login by accident."""
    readable = re.sub(r"[^A-Za-z0-9._-]", "_", server_name)[:40].lstrip(".") or "server"
    digest = hashlib.sha256(f"{server_name}\0{canonical_server_url(server_url)}".encode()).hexdigest()[:16]
    return f"{readable}-{digest}"


def default_base_dir() -> Path:
    """The user's Drifter config directory, outside any project."""
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        return (Path(local) if local else Path.home() / "AppData" / "Local") / "drifter"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg else Path.home() / ".config") / "drifter"


def _is_inside(path: Path, root: Path) -> bool:
    real, real_root = Path(os.path.realpath(path)), Path(os.path.realpath(root))
    return real == real_root or real_root in real.parents


async def _run_in_daemon_thread(function: Callable[[], _T]) -> _T:
    """Run blocking storage work off the event loop, so that cancelling it is prompt AND the
    abandoned thread cannot keep the process alive.

    A keyring call can block indefinitely (an unlock prompt nobody answers). `anyio.to_thread.
    run_sync(abandon_on_cancel=True)` frees the event loop but its worker threads are NOT
    daemons (they inherit the main thread's flag), so the interpreter would wait on the stuck
    call forever at exit: the same shutdown-hang shape CLAUDE.md records for stdin reads.
    A daemon thread of our own is simply dropped at exit. Proven with a real subprocess in
    tests/auth/test_token_storage.py, not assumed. No timeout constant on purpose: the callers
    already bound their waits (doctor's `fail_after`, the agent giving up on `observe`).
    """
    sender, receiver = anyio.create_memory_object_stream[tuple[bool, Any]](1)
    token = anyio.lowlevel.current_token()

    def work() -> None:
        try:
            outcome: tuple[bool, Any] = (True, function())
        except BaseException as exc:  # relayed to the awaiting task, not lost with the thread
            outcome = (False, exc)
        try:
            anyio.from_thread.run_sync(sender.send_nowait, outcome, token=token)
        except Exception:  # the event loop is gone or the awaiter was cancelled: nobody is waiting
            pass

    threading.Thread(target=work, name="drifter-keyring", daemon=True).start()
    with receiver:
        ok, value = await receiver.receive()
    if ok:
        return value
    raise value


# --- what a summary may show ------------------------------------------------------------------------


@dataclass(frozen=True)
class StoredSummary:
    """What `drifter auth status` may print. No token, secret or code, by construction."""

    backend: str
    has_tokens: bool
    has_client_info: bool
    refreshable: bool
    expires_at: float | None
    scope: str | None


# --- the store ----------------------------------------------------------------------------------------


Backend = Literal["auto", "keyring", "file"]


class TokenStore:
    """The SDK's `TokenStorage` protocol (`get_tokens`, `set_tokens`, `get_client_info`,
    `set_client_info`), plus `get_expiry`, `summary` and `delete` for Drifter's own commands."""

    def __init__(
        self,
        server_name: str,
        server_url: str,
        *,
        base_dir: Path | None = None,
        project_root: Path | None = None,
        backend: Backend = "auto",
    ) -> None:
        self.server_name = server_name
        self.server_url = canonical_server_url(server_url)
        self.key = storage_key(server_name, server_url)
        self._base_dir = Path(base_dir) if base_dir is not None else default_base_dir()
        self._tokens_dir = self._base_dir / "tokens"
        self.file_path = self._tokens_dir / f"{self.key}.json"
        self._requested: Backend = backend
        self._resolved: str | None = None
        self._sid: str | None = None
        self._last_source = "file"
        if project_root is not None and _is_inside(self._base_dir, project_root):
            raise InsecureStorageError(
                f"token storage at {self._base_dir} would be inside the project {Path(project_root)}; tokens "
                "must never be stored where they could be committed or shared. Use the default "
                "location (your user config directory)"
            )

    # -- backend choice ------------------------------------------------------------------------------

    @property
    def backend_name(self) -> str:
        """`"keyring"` or `"file"`: where new writes go."""
        if self._resolved is None:
            self._resolved = self._choose_backend()
        return self._resolved

    def _choose_backend(self) -> str:
        if self._requested == "file":
            return "file"
        reason = _keyring_unusable_reason()
        if reason is None:
            return "keyring"
        if self._requested == "keyring":
            raise StorageUnavailableError(f"the OS keyring was requested but cannot be used: {reason}")
        logger.debug("keyring unusable (%s); using the private file", reason)
        return "file"

    # -- the SDK protocol ----------------------------------------------------------------------------

    async def get_tokens(self) -> OAuthToken | None:
        record = await self._run(self._load)
        return OAuthToken.model_validate(record["tokens"]) if record and record.get("tokens") else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        def update(record: dict[str, Any]) -> None:
            record["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
            record["expires_at"] = time.time() + tokens.expires_in if tokens.expires_in is not None else None

        await self._run(lambda: self._update(update))

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        record = await self._run(self._load)
        return OAuthClientInformationFull.model_validate(record["client_info"]) if record and record.get("client_info") else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        def update(record: dict[str, Any]) -> None:
            record["client_info"] = client_info.model_dump(mode="json", exclude_none=True)

        await self._run(lambda: self._update(update))

    async def get_expiry(self) -> float | None:
        """When the stored access token expires (epoch seconds), or None if it never says."""
        record = await self._run(self._load)
        return record.get("expires_at") if record else None

    # -- Drifter's own uses ----------------------------------------------------------------------------

    def summary(self) -> StoredSummary:
        record = self._load()
        tokens = (record or {}).get("tokens") or {}
        return StoredSummary(
            backend=self._last_source if record else self.backend_name,
            has_tokens=bool(tokens),
            has_client_info=bool((record or {}).get("client_info")),
            refreshable=bool(tokens.get("refresh_token")),
            expires_at=(record or {}).get("expires_at"),
            scope=tokens.get("scope"),
        )

    def delete(self) -> bool:
        """Forget this server's login everywhere it could be. True if there was one."""
        removed = _keyring_delete(self.key)
        try:
            self.file_path.unlink()  # deleting needs no privacy check: removing a file leaks nothing
            removed = True
        except FileNotFoundError:
            pass
        return removed

    # -- loading and saving -------------------------------------------------------------------------------

    @staticmethod
    async def _run(function: Callable[[], _T]) -> _T:
        return await _run_in_daemon_thread(function)

    def _load(self) -> dict[str, Any] | None:
        if self._requested != "file" and _keyring_unusable_reason() is None:
            blob = _keyring_get(self.key)
            if blob is not None:
                self._last_source = "keyring"
                return self._parse(blob, f"the keyring entry for {self.server_name!r}")
        if not self.file_path.exists() and not self.file_path.is_symlink():
            return None
        self._check_private(self.file_path, is_dir=False)
        self._check_directories()
        try:
            text = self.file_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise StorageError(f"could not read {self.file_path}: {exc.strerror or type(exc).__name__}") from None
        self._last_source = "file"
        return self._parse(text, str(self.file_path))

    def _parse(self, text: str, where: str) -> dict[str, Any]:
        try:
            record = json.loads(text)
            if not isinstance(record, dict):
                raise ValueError("not an object")
        except ValueError:
            raise StorageError(
                f"{where} is not a valid token record; run `drifter auth logout {self.server_name}` to remove "
                "it, then log in again"
            ) from None
        if record.get("format") != FORMAT_VERSION:
            raise StorageError(f"{where} has format {record.get('format')!r}, but this build reads format {FORMAT_VERSION}")
        return record

    def _update(self, change: Callable[[dict[str, Any]], None]) -> None:
        record = self._load() or {"format": FORMAT_VERSION, "server": self.server_name, "url": self.server_url,
                                  "tokens": None, "client_info": None, "expires_at": None}
        change(record)
        record["saved_at"] = time.time()
        self._save(record)

    def _save(self, record: dict[str, Any]) -> None:
        payload = json.dumps(record, separators=(",", ":"))
        if self.backend_name == "keyring":
            try:
                _keyring_set(self.key, payload)
            except Exception as exc:  # a backend may refuse (size limit, locked); never lose the login
                logger.warning(
                    "the OS keyring refused the write (%s); storing the login in the private file instead",
                    type(exc).__name__,
                )
                _keyring_delete(self.key)  # one source of truth: a stale entry must not shadow the file
            else:
                self._remove_stale_file()
                return
        self._write_file(payload)

    def _remove_stale_file(self) -> None:
        try:
            self.file_path.unlink()
        except FileNotFoundError:
            pass

    # -- the private file ---------------------------------------------------------------------------------

    def _write_file(self, payload: str) -> None:
        self._ensure_private_dir(self._base_dir)
        self._ensure_private_dir(self._tokens_dir)
        if self.file_path.exists() or self.file_path.is_symlink():
            self._check_private(self.file_path, is_dir=False)  # never overwrite what we would refuse to read
        handle, temporary = tempfile.mkstemp(dir=self._tokens_dir, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            if sys.platform != "win32":
                os.chmod(temporary, 0o600)
            os.replace(temporary, self.file_path)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise
        if sys.platform == "win32":
            self._check_private(self.file_path, is_dir=False)

    def _check_directories(self) -> None:
        for directory in (self._base_dir, self._tokens_dir):
            if directory.exists():
                self._check_private(directory, is_dir=True)

    def _ensure_private_dir(self, directory: Path) -> None:
        if directory.exists() or directory.is_symlink():
            self._check_private(directory, is_dir=True)
            return
        directory.parent.mkdir(parents=True, exist_ok=True)
        directory.mkdir(mode=0o700)
        if sys.platform == "win32":
            try:
                winacl.make_private(directory, self._user_sid(), is_dir=True)
            except winacl.AclError as exc:
                raise StorageUnavailableError(f"cannot make {directory} private on this machine: {exc}") from None
        else:
            os.chmod(directory, 0o700)  # mkdir's mode is masked by the umask
        self._check_private(directory, is_dir=True)

    def _user_sid(self) -> str:
        if self._sid is None:
            try:
                self._sid = winacl.current_user_sid()
            except winacl.AclError as exc:
                raise StorageUnavailableError(f"cannot establish who you are to protect the token file: {exc}") from None
        return self._sid

    def _check_private(self, path: Path, *, is_dir: bool) -> None:
        """Raise `InsecureStorageError` unless `path` is provably private to this user."""
        if path.is_symlink():
            raise InsecureStorageError(f"{path} is a symbolic link; refusing to read or write tokens through it")
        if sys.platform == "win32":
            problems = winacl.verify_private(path, self._user_sid())
            if problems:
                raise InsecureStorageError(
                    f"{path} is not private to your Windows account: {'; '.join(problems)}. "
                    f"Remove it with `drifter auth logout {self.server_name}` and log in again"
                )
            return
        info = os.lstat(path)
        if info.st_uid != os.geteuid():
            raise InsecureStorageError(f"{path} is owned by uid {info.st_uid}, not by you (uid {os.geteuid()}); refusing to use it")
        mode = stat.S_IMODE(info.st_mode)
        if mode & 0o077:
            fix = "chmod 700" if is_dir else "chmod 600"
            raise InsecureStorageError(
                f"{path} is accessible to other users (mode {mode:04o}); refusing to use it. Fix it with `{fix} {path}`"
            )


# --- the keyring, isolated so it can be absent ---------------------------------------------------------------


def _keyring_unusable_reason() -> str | None:
    """None if a real keyring backend is available, else why not."""
    try:
        import keyring
        from keyring.backends import fail
    except ImportError:
        return "the keyring package is not installed (install it with `pip install mcp-drifter[keyring]`)"
    backend = keyring.get_keyring()
    if isinstance(backend, fail.Keyring):
        return "no usable keyring backend on this machine (headless Linux, or no Secret Service running)"
    chained = getattr(backend, "backends", None)
    if chained is not None and not any(not isinstance(b, fail.Keyring) for b in chained):
        return "no usable keyring backend on this machine"
    return None


def _keyring_get(key: str) -> str | None:
    import keyring

    try:
        return keyring.get_password(KEYRING_SERVICE, key)
    except Exception as exc:  # locked, no session bus: treat as "not in the keyring", fall through to the file
        logger.warning("could not read the OS keyring (%s)", type(exc).__name__)
        return None


def _keyring_set(key: str, payload: str) -> None:
    import keyring

    keyring.set_password(KEYRING_SERVICE, key, payload)


def _keyring_delete(key: str) -> bool:
    if _keyring_unusable_reason() is not None:
        return False
    import keyring

    try:
        if keyring.get_password(KEYRING_SERVICE, key) is None:
            return False
        keyring.delete_password(KEYRING_SERVICE, key)
        return True
    except Exception as exc:
        logger.warning("could not remove the OS keyring entry (%s)", type(exc).__name__)
        return False
