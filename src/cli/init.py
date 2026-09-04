"""`drifter init` (F-33), docs/SPEC.md §11.

Found missing, not planned as absent: sanity-checking Gate 4's own
handoff checklist (docs/PHASES.md — "They run `drifter init` ->
`drifter observe` -> ...") against the real CLI showed this subcommand
didn't exist at all. A second user's literal first command would have
failed with argparse's "invalid choice" error before ever reaching
`drifter observe` (docs/CHANGELOG.md's corresponding entry).

Deliberately NARROWER than docs/FEATURES.md's F-33 text ("runs initial
tool classification (F-26)"): `policy/` (F-26) is empty — not built in
Gate 3 despite PHASES.md's own checklist naming it — and
`cli.config.DrifterConfig` has no risk-classification field to
populate even if it were. F-33's own "Done when" bar ("produces a
working drifter.yaml with zero manual edits required to run `drifter
observe`") doesn't require classification output, so this narrower
scope still satisfies it — same precedent as F-34's documented
narrower-than-spec Gate 2 scope (cli/subprocess_adapter.py's
docstring).

Scans known MCP client config file locations for STDIO-transport
server definitions only — the sole transport Drifter's v0 architecture
supports (docs/SPEC.md's stdio-only note; `record/proxy.py` and
`cli/subprocess_adapter.py` both assume a spawnable argv command, not a
URL). Checked in this order; a name found in an earlier file wins over
a same-named later one — deterministic, not "whichever the filesystem
iterates last":

1. `<search_root>/.mcp.json`                    (Claude Code, project-scoped)
2. `<search_root>/.cursor/mcp.json`              (Cursor)
3. the platform's Claude Desktop config path     (Claude Desktop)

An entry that isn't stdio-shaped (`type` in {"http","sse","ws"}, or a
`url` with no `command`) is never silently dropped or silently
mis-parsed as if it were a stdio command — it's reported as skipped,
with a reason, since Drifter's proxy genuinely cannot drive it yet.
Same for a malformed entry (no usable `command` at all).

Overwrite protection: `run_init` refuses to touch an existing
`drifter.yaml` unless `force=True` — this project's own operating
discipline treats overwriting a file the user may have hand-edited as
exactly the kind of hard-to-reverse action that needs an explicit,
deliberate override, not a default.
"""

from __future__ import annotations

import json
import os
import platform
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

import yaml

from cli.config import ConfigError, ServerConfig


@dataclass(frozen=True)
class SkippedServer:
    """One `mcpServers` entry this scan found but could not turn into a
    Drifter `ServerConfig` — always reported, never silently dropped."""

    name: str
    source: Path
    reason: str


def claude_desktop_config_path() -> Path | None:
    """Returns the platform-specific Claude Desktop config path, or
    `None` on a platform it doesn't ship on (e.g. Linux) — `None` is a
    real, honest answer here, not a missing case; `scan_mcp_configs`
    simply has one fewer location to check.
    """
    system = platform.system()
    if system == "Windows":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            return None
        return Path(appdata) / "Claude" / "claude_desktop_config.json"
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    return None


def _known_config_locations(search_root: Path) -> list[Path]:
    locations = [search_root / ".mcp.json", search_root / ".cursor" / "mcp.json"]
    desktop_path = claude_desktop_config_path()
    if desktop_path is not None:
        locations.append(desktop_path)
    return locations


def _extract_stdio_servers(raw: Any, source: Path) -> tuple[list[ServerConfig], list[SkippedServer]]:
    servers: list[ServerConfig] = []
    skipped: list[SkippedServer] = []

    if not isinstance(raw, dict):
        return servers, skipped
    mcp_servers = raw.get("mcpServers")
    if not isinstance(mcp_servers, dict):
        return servers, skipped

    for name, entry in mcp_servers.items():
        if not isinstance(entry, dict):
            skipped.append(SkippedServer(name=name, source=source, reason="entry is not an object"))
            continue

        entry_type = entry.get("type")
        command = entry.get("command")

        if entry_type in ("http", "sse", "ws") or ("url" in entry and not isinstance(command, str)):
            skipped.append(
                SkippedServer(
                    name=name,
                    source=source,
                    reason=(
                        f"non-stdio transport ({entry_type or 'url-based'}) — Drifter's v0 proxy "
                        "only supports stdio servers"
                    ),
                )
            )
            continue

        if not isinstance(command, str) or not command:
            skipped.append(SkippedServer(name=name, source=source, reason="missing a usable 'command' field"))
            continue

        args = entry.get("args", [])
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            skipped.append(SkippedServer(name=name, source=source, reason="'args' is not a list of strings"))
            continue

        servers.append(ServerConfig(name=name, command=[command, *args]))

    return servers, skipped


def scan_mcp_configs(search_root: Path) -> tuple[list[ServerConfig], list[SkippedServer], list[Path]]:
    """Scans every known config location under `search_root`. Returns
    (servers found, entries skipped with reasons, config files that
    actually existed on disk) — the third element lets a caller report
    honestly on "found nothing" vs. "no config files exist at all",
    which are different situations for a user to act on.
    """
    servers_by_name: dict[str, ServerConfig] = {}
    all_skipped: list[SkippedServer] = []
    found_files: list[Path] = []

    for location in _known_config_locations(search_root):
        if not location.exists():
            continue
        found_files.append(location)
        try:
            raw = json.loads(location.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # Reported via found_files (the caller sees the file existed
            # but contributed zero servers) rather than raising — a
            # foreign, unrelated JSON file that happens to fail to parse
            # shouldn't halt the whole scan.
            continue

        found_servers, skipped = _extract_stdio_servers(raw, location)
        all_skipped.extend(skipped)
        for server in found_servers:
            servers_by_name.setdefault(server.name, server)

    return list(servers_by_name.values()), all_skipped, found_files


def _render_drifter_yaml(servers: list[ServerConfig]) -> str:
    header = (
        "# Drifter project config (docs/SPEC.md §11), generated by `drifter init`.\n"
        "#\n"
        "# Only `servers:` and `record:` are read by `drifter observe` today.\n"
        "# `drifter init` does not yet run tool risk classification (F-26,\n"
        "# `policy/`, not built as of this generation) -- see docs/SPEC.md §15 for\n"
        "# this project's other stated limitations. Review the server list below\n"
        "# before running `drifter observe` against it.\n\n"
    )
    body = {
        "version": 1,
        "servers": [{"name": s.name, "command": s.command} for s in servers],
        "record": {"dir": ".drifter/runs", "redact": "shape"},
    }
    # default_flow_style=False keeps the block style the hand-written
    # drifter.yaml uses; safe_dump quotes any backslash-bearing value
    # (a real Windows path in a scanned command) correctly on its own --
    # no manual quoting logic needed here (see this module's own
    # docstring / tests/cli/test_observe.py's own precedent for the bug
    # this would otherwise reintroduce).
    return header + yaml.safe_dump(body, default_flow_style=False, sort_keys=False)


def run_init(
    output_path: Path = Path("drifter.yaml"),
    search_root: Path = Path("."),
    force: bool = False,
    status_stream: TextIO = sys.stdout,
) -> None:
    """Scans known MCP client configs under `search_root` and writes a
    starter `drifter.yaml` to `output_path`. Raises `ConfigError` (same
    exception every other subcommand already surfaces as an actionable,
    non-traceback message — see `cli/app.py`) if `output_path` already
    exists and `force` is not set, or if no usable stdio server was
    found anywhere.
    """
    if output_path.exists() and not force:
        raise ConfigError(f"{output_path} already exists. Re-run with --force to overwrite it.")

    servers, skipped, found_files = scan_mcp_configs(search_root)

    if not servers:
        if not found_files:
            raise ConfigError(
                "No known MCP client config files found "
                f"({', '.join(str(p) for p in _known_config_locations(search_root))}). "
                "Write a drifter.yaml by hand instead — see docs/SPEC.md §11 for the expected shape."
            )
        raise ConfigError(
            f"Found {len(found_files)} config file(s) but no usable stdio server definitions "
            f"({len(skipped)} entry(ies) skipped — see below). Write a drifter.yaml by hand instead."
            + "".join(f"\n  - {s.name} ({s.source}): {s.reason}" for s in skipped)
        )

    output_path.write_text(_render_drifter_yaml(servers), encoding="utf-8")

    print(f"Wrote {output_path} with {len(servers)} server(s):", file=status_stream)
    for s in servers:
        print(f"  - {s.name}: {' '.join(s.command)}", file=status_stream)
    if skipped:
        print(f"Skipped {len(skipped)} non-stdio or malformed entry(ies):", file=status_stream)
        for s in skipped:
            print(f"  - {s.name} ({s.source}): {s.reason}", file=status_stream)
