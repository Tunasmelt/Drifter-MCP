"""`drifter auth login | status | logout` (F-43, docs/SPEC.md §5.2).

`login` is a separate command, never part of `observe`: `observe`'s stdout IS the protocol channel
to the agent, so it cannot pause mid-`initialize` for a browser. Run `login` first; `observe` and
`doctor` then use the stored login (F-44).

Nothing here prints a token, a client secret or an authorization code. `status` reports whether a
login exists, when it expires, whether it can refresh, and its scopes: facts, never material.
"""

from __future__ import annotations

import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import anyio

from mcp_drifter.auth.errors import LoginRefused
from mcp_drifter.auth.identity import CANONICAL_CLIENT_METADATA_URL
from mcp_drifter.auth.login import DEFAULT_LOGIN_TIMEOUT_S, default_open_browser, run_login
from mcp_drifter.auth.text import clean_server_text
from mcp_drifter.auth.storage import StorageError, TokenStore, canonical_server_url
from mcp_drifter.cli.config import ConfigError, DrifterConfig, ServerConfig, load_config

EXIT_CANCELLED = 130  # 128 + SIGINT, the shell convention for "ended by Ctrl+C"


def _emit(line: str = "") -> None:
    print(line, flush=True)


def _store_for(server: ServerConfig, config_path: Path) -> TokenStore:
    try:
        return TokenStore(server.name, server.url or "", project_root=config_path.resolve().parent)
    except StorageError as exc:
        raise ConfigError(str(exc)) from None


def _find(config: DrifterConfig, name: str) -> ServerConfig:
    for server in config.servers:
        if server.name == name:
            return server
    raise ConfigError(f"no server named {name!r} in drifter.yaml; available: {', '.join(s.name for s in config.servers)}")


def _login_target(config: DrifterConfig, name: str) -> ServerConfig:
    server = _find(config, name)
    if server.url is None:
        raise ConfigError(f"server {name!r} runs a local command; `auth login` applies to `url` servers (a stdio server uses its own environment)")
    auth = server.auth
    if auth is None:
        raise ConfigError(f"server {name!r} has no `auth` block; add `auth: {{type: oauth}}` to drifter.yaml to log in")
    if auth.type == "none":
        raise ConfigError(f"server {name!r} has `auth: {{type: none}}`; set `type: oauth` to log in to it")
    if auth.type == "bearer_env":
        raise ConfigError(
            f"server {name!r} uses auth type 'bearer_env', so there is no login to perform: its token comes from "
            f"environment variable {auth.token_env}"
        )
    if auth.type == "client_credentials":
        raise ConfigError(f"server {name!r} uses client_credentials, which does not log in interactively and is not built yet")
    return server


def _when(timestamp: float | None) -> str:
    if timestamp is None:
        return "unknown (the server did not say)"
    try:
        moment = datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (ValueError, OverflowError, OSError):
        # `expires_in` is the server's number and is stored as-is, so an absurd one must not crash every `status`.
        return "unknown (the server sent an expiry that is out of range)"
    delta = timestamp - time.time()
    minutes = int(abs(delta) // 60)
    span = f"{minutes} min" if minutes < 120 else f"{minutes // 60} h"
    return f"{moment} ({'in ' + span if delta >= 0 else 'expired ' + span + ' ago'})"


def _scopes(scope: str | None) -> str:
    """Scopes as the server stored them, reduced to ordinary token characters (they reach a terminal)."""
    return clean_server_text(scope, 300) or "none reported"


def _where(backend: str) -> str:
    return "the OS keyring" if backend == "keyring" else "a private file in your user config directory"


def run_auth_login(config_path: Path, server_name: str, *, no_browser: bool, timeout: float) -> None:
    if not math.isfinite(timeout) or timeout <= 0:
        raise ConfigError("--timeout must be a positive number of seconds")
    config = load_config(config_path, merge_tasks=False)
    server = _login_target(config, server_name)
    store = _store_for(server, config_path)

    async def open_browser(url: str) -> None:
        if no_browser:
            return
        await default_open_browser(url)

    try:
        result = anyio.run(
            lambda: run_login(
                server,
                store,
                client_metadata_url=CANONICAL_CLIENT_METADATA_URL,
                open_browser=open_browser,
                announce=_emit,
                timeout=timeout,
            )
        )
    except LoginRefused as exc:
        raise ConfigError(str(exc)) from None
    except StorageError as exc:
        raise ConfigError(str(exc)) from None
    _emit()
    _emit(f"Logged in to {server.name!r}.")
    _emit(f"  scopes    : {_scopes(result.scope)}")
    _emit(f"  expires   : {_when(result.expires_at)}")
    _emit(f"  stored in : {_where(result.backend)}")


def run_auth_status(config_path: Path, server_name: str | None) -> int:
    config = load_config(config_path, merge_tasks=False)
    servers = [_find(config, server_name)] if server_name else [s for s in config.servers if s.url is not None]
    exit_code = 0
    for server in servers:
        # The canonical form: it drops any user:password@ that was typed into the URL, which must not reach a terminal.
        _emit(f"{server.name}  {canonical_server_url(server.url)}")
        auth = server.auth
        auth_type = auth.type if auth is not None else "none"
        if auth is not None and auth.type == "bearer_env":
            state = "set" if os.environ.get(auth.token_env or "") else "not set"
            _emit(f"  auth        : bearer_env (token from environment variable {auth.token_env}, currently {state})")
        else:
            _emit(f"  auth        : {auth_type}")
        if auth_type == "oauth":
            try:
                summary = _store_for(server, config_path).summary()
            except StorageError as exc:
                _emit(f"  logged in   : unknown ({exc})")
                exit_code = 4
                continue
            if not summary.has_tokens:
                _emit("  logged in   : no")
                _emit(f"  next step   : drifter auth login {server.name}")
            else:
                _emit("  logged in   : yes")
                _emit(f"  expires     : {_when(summary.expires_at)}")
                _emit(f"  refreshable : {'yes' if summary.refreshable else 'no'}")
                _emit(f"  scopes      : {_scopes(summary.scope)}")
                _emit(f"  stored in   : {_where(summary.backend)}")
        _emit()
    return exit_code


def run_auth_logout(config_path: Path, server_name: str) -> None:
    config = load_config(config_path, merge_tasks=False)
    server = _find(config, server_name)
    if server.url is None:
        raise ConfigError(f"server {server_name!r} runs a local command; there is no stored login for it")
    try:
        removed = _store_for(server, config_path).delete()
    except StorageError as exc:
        raise ConfigError(str(exc)) from None
    if removed:
        _emit(f"Logged out of {server.name!r}: the stored login was removed from this machine (it was not revoked at the server).")
    else:
        _emit(f"No stored login for {server.name!r}; nothing to remove.")


def main_auth(args) -> None:
    """Dispatches a parsed `drifter auth ...` command and translates failures to exit codes
    (docs/SPEC.md §12: 4 = config/connectivity; 130 = ended by Ctrl+C)."""
    try:
        if args.auth_command == "login":
            timeout = args.timeout if args.timeout is not None else DEFAULT_LOGIN_TIMEOUT_S
            run_auth_login(args.config, args.server, no_browser=args.no_browser, timeout=timeout)
        elif args.auth_command == "status":
            code = run_auth_status(args.config, args.server)
            if code:
                raise SystemExit(code)
        elif args.auth_command == "logout":
            run_auth_logout(args.config, args.server)
    except ConfigError as exc:
        print(f"drifter auth {args.auth_command}: {exc}", file=sys.stderr)
        raise SystemExit(4) from None
    except KeyboardInterrupt:
        print(f"drifter auth {args.auth_command}: cancelled", file=sys.stderr)
        raise SystemExit(EXIT_CANCELLED) from None
