"""`drifter fixture capture` / `drifter fixture check` — authoring and
maintaining response fixtures (docs/PHASES.md R0.5, docs/SPEC.md §15
limitation 20's open item (c)).

Every experiment through limitation 23 used response fixtures written by
hand or by a one-off script. That left two gaps the external review named:
there was no supported way for a user to author fixtures for their own server,
and nothing could say whether an authored body was still what the server
returns (request-match coverage only says the REQUEST was recorded).

`capture` replays each distinct recorded request against the LIVE server and
writes the real response, with provenance. `check` re-calls the live server
for every fixture entry and reports whether the stored body still matches.

Safety boundary, stated because this module deliberately makes live calls:
- A tool is called only if the live server's own manifest classifies it
  read-only (`policy.classify`). `--allow-tool NAME` may promote a tool
  classified `unknown`; it never promotes a write or destructive tool, which
  are refused outright (docs/SPEC.md §10: unknown is "never live-invoked" by
  default; writes are never replayed live by Drifter).
- Recorded arguments are stored redacted. A request whose arguments contain a
  redaction marker cannot be replayed faithfully, so it is skipped and
  reported — author that entry by hand.
- The fixture file holds REAL response payloads. That is its purpose, and it
  is the opposite of the observation corpus's shape-only invariant; the file
  header says so, and it lives wherever the user puts it, never under the
  recording directory.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

import anyio
import yaml
from mcp import ClientSession

from mcp_drifter.cli.config import ConfigError, ServerConfig, load_config, server_target
from mcp_drifter.cli.observe import select_server
from mcp_drifter.policy.classify import classify_manifest
from mcp_drifter.record.proxy import connect_to_server
from mcp_drifter.record.reader import read_session
from mcp_drifter.record.redact import is_redaction_marker
from mcp_drifter.record.schema import ToolCall, ToolDescriptor
from mcp_drifter.replay.corpus import resolve_session_paths
from mcp_drifter.replay.replay_store import replay_key

CAPTURABLE_RISKS = frozenset({"read_only_local", "read_only_external"})
PROMOTABLE_RISKS = frozenset({"unknown"})
DEFAULT_TIMEOUT_S = 120.0

_HEADER = (
    "# Drifter response fixture, written by `drifter fixture capture`.\n"
    "# Contains REAL response payloads from the live server. Review it before\n"
    "# committing or sharing it: unlike recorded sessions, nothing here is redacted.\n"
    "# Re-verify against the live server with `drifter fixture check`.\n"
)


@dataclass(frozen=True)
class Request:
    tool_name: str
    arguments: dict
    key: str


@dataclass(frozen=True)
class EntryStatus:
    tool_name: str
    arguments: dict
    status: str  # CAPTURED | FRESH | STALE | UNBOUND | SKIPPED | ERROR
    detail: str = ""


def contains_redaction(value: object) -> bool:
    if is_redaction_marker(value):
        return True
    if isinstance(value, dict):
        return any(contains_redaction(v) for v in value.values())
    if isinstance(value, list):
        return any(contains_redaction(v) for v in value)
    return False


def plan_requests(corpus_inputs: Sequence[Path], server: str) -> list[Request]:
    """Distinct REAL recorded requests for `server`, first-seen order. Synthetic,
    authored and faulted calls are not requests the server actually answered."""
    seen: dict[str, Request] = {}
    for path in resolve_session_paths(list(corpus_inputs)):
        for record in read_session(path):
            if not isinstance(record, ToolCall) or record.server != server:
                continue
            if record.result_provenance != "real" or record.fault is True:
                continue
            key = replay_key(server, record.tool_name, record.arguments)
            seen.setdefault(key, Request(record.tool_name, record.arguments, key))
    return list(seen.values())


def content_digest(result: dict) -> str:
    """Hash of what an agent reads: `_meta` is transport metadata, not content."""
    body = {k: v for k, v in result.items() if k != "_meta"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def gate(tool_name: str, risk: str | None, allow_tools: frozenset[str]) -> str | None:
    """Why `tool_name` must not be called live, or None if it may be."""
    if risk is None:
        return "not in the live server's manifest"
    if risk in CAPTURABLE_RISKS:
        return None
    if risk in PROMOTABLE_RISKS and tool_name in allow_tools:
        return None
    if risk in PROMOTABLE_RISKS:
        return f"classified {risk}; pass --allow-tool {tool_name} after confirming it is read-only"
    return f"classified {risk}; fixture capture never calls write or destructive tools live"


async def _live_results(
    server: ServerConfig,
    requests: Sequence[tuple[str, dict]],
    allow_tools: frozenset[str],
    timeout_s: float,
) -> tuple[dict[str, str], list[tuple[str, dict | str]]]:
    """One live session: manifest, classification, then each allowed call.
    Returns (server_info, [(kind, payload)]) where kind is "ok" (payload is the
    result dict), "skip" or "error" (payload is the reason)."""
    outcomes: list[tuple[str, dict | str]] = []
    server_info: dict[str, str] = {}
    with anyio.fail_after(timeout_s):
        async with connect_to_server(server_target(server)) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                info = getattr(init, "server_info", None) or getattr(init, "serverInfo", None)
                if info is not None:
                    server_info = {"name": info.name, "version": info.version or ""}
                listed = (await session.list_tools()).tools
                tools = [
                    ToolDescriptor(
                        name=t.name,
                        description=t.description or "",
                        input_schema=t.input_schema or {},
                        annotations=t.annotations.model_dump(mode="json", by_alias=True, exclude_none=True)
                        if t.annotations is not None
                        else None,
                    )
                    for t in listed
                ]
                risks = {name: c.risk for name, c in classify_manifest(tools).items()}
                for tool_name, arguments in requests:
                    reason = gate(tool_name, risks.get(tool_name), allow_tools)
                    if reason is not None:
                        outcomes.append(("skip", reason))
                        continue
                    if contains_redaction(arguments):
                        outcomes.append(("skip", "recorded arguments are redacted; author this entry by hand"))
                        continue
                    try:
                        result = await session.call_tool(tool_name, arguments)
                    except Exception as exc:  # a protocol error for this one call
                        outcomes.append(("error", f"{type(exc).__name__}: {exc}"))
                        continue
                    outcomes.append(("ok", result.model_dump(mode="json", by_alias=True, exclude_none=True)))
    return server_info, outcomes


def _run_live(server, requests, allow_tools, timeout_s):
    try:
        return anyio.run(_live_results, server, requests, allow_tools, timeout_s)
    except TimeoutError:
        return {}, [("error", f"live server did not finish within {timeout_s:.0f}s")] * len(requests)
    except Exception as exc:
        detail = f"could not talk to the live server: {type(exc).__name__}: {exc}"
        return {}, [("error", detail)] * len(requests)


def _load_server(config_path: Path | None, server_name: str | None) -> ServerConfig:
    return select_server(load_config(config_path, merge_tasks=False), server_name)


def _print(statuses: list[EntryStatus], verb: str, output_stream: TextIO) -> None:
    for s in statuses:
        args = json.dumps(s.arguments, sort_keys=True)
        line = f"[{s.status:>8}] {s.tool_name} {args}"
        output_stream.write(line + (f" — {s.detail}" if s.detail else "") + "\n")
    counts: dict[str, int] = {}
    for s in statuses:
        counts[s.status] = counts.get(s.status, 0) + 1
    summary = ", ".join(f"{n} {k.lower()}" for k, n in sorted(counts.items())) or "nothing to do"
    output_stream.write(f"drifter fixture {verb}: {summary}\n")


def run_fixture_capture(
    fixture: Sequence[Path],
    output: Path,
    config_path: Path | None = None,
    server_name: str | None = None,
    allow_tools: Sequence[str] = (),
    force: bool = False,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    output_stream: TextIO = sys.stdout,
) -> list[EntryStatus]:
    if output.exists() and not force:
        raise ConfigError(f"{output} already exists; pass --force to overwrite it")
    server = _load_server(config_path, server_name)
    requests = plan_requests(fixture, server.name)
    if not requests:
        raise ConfigError(f"no recorded real calls for server {server.name!r} in {', '.join(map(str, fixture))}")

    server_info, outcomes = _run_live(server, [(r.tool_name, r.arguments) for r in requests], frozenset(allow_tools), timeout_s)
    captured_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entries: list[dict] = []
    statuses: list[EntryStatus] = []
    for request, (kind, payload) in zip(requests, outcomes):
        if kind != "ok":
            statuses.append(EntryStatus(request.tool_name, request.arguments, "SKIPPED" if kind == "skip" else "ERROR", str(payload)))
            continue
        assert isinstance(payload, dict)
        entries.append({
            "tool_name": request.tool_name,
            "arguments": request.arguments,
            "result": payload,
            "provenance": {
                "source": "live_capture",
                "captured_at": captured_at,
                "server": server_info,
                "content_sha256": content_digest(payload),
            },
        })
        detail = "tool returned isError: true" if payload.get("isError") else ""
        statuses.append(EntryStatus(request.tool_name, request.arguments, "CAPTURED", detail))

    if entries:
        output.parent.mkdir(parents=True, exist_ok=True)
        document = {"version": 1, "server": server.name, "responses": entries}
        output.write_text(_HEADER + yaml.safe_dump(document, sort_keys=False, allow_unicode=True), encoding="utf-8")
    _print(statuses, "capture", output_stream)
    if entries:
        output_stream.write(f"wrote {len(entries)} entr{'y' if len(entries) == 1 else 'ies'} to {output}\n")
    return statuses


def run_fixture_check(
    fixture: Sequence[Path],
    responses: Path,
    config_path: Path | None = None,
    server_name: str | None = None,
    allow_tools: Sequence[str] = (),
    timeout_s: float = DEFAULT_TIMEOUT_S,
    output_stream: TextIO = sys.stdout,
) -> list[EntryStatus]:
    server = _load_server(config_path, server_name)
    try:
        document = yaml.safe_load(responses.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read response fixture {responses}: {exc}") from exc
    if not isinstance(document, dict) or document.get("version") != 1 or not isinstance(document.get("responses"), list):
        raise ConfigError(f"{responses} is not a version 1 response fixture")
    if document.get("server") != server.name:
        raise ConfigError(f"{responses} is for server {document.get('server')!r}, not {server.name!r}")

    corpus_keys = {r.key for r in plan_requests(fixture, server.name)}
    entries = document["responses"]
    statuses: list[EntryStatus | None] = [None] * len(entries)
    live_indexes: list[int] = []
    for i, entry in enumerate(entries):
        key = replay_key(server.name, entry["tool_name"], entry["arguments"])
        if key not in corpus_keys:
            statuses[i] = EntryStatus(entry["tool_name"], entry["arguments"], "UNBOUND", "request is not in the recorded corpus; replay will never serve it")
        else:
            live_indexes.append(i)

    _, outcomes = _run_live(server, [(entries[i]["tool_name"], entries[i]["arguments"]) for i in live_indexes], frozenset(allow_tools), timeout_s)
    for i, (kind, payload) in zip(live_indexes, outcomes):
        entry = entries[i]
        if kind != "ok":
            statuses[i] = EntryStatus(entry["tool_name"], entry["arguments"], "SKIPPED" if kind == "skip" else "ERROR", str(payload))
            continue
        assert isinstance(payload, dict)
        stored = content_digest(entry.get("result") or {})
        notes = []
        recorded = (entry.get("provenance") or {}).get("content_sha256")
        if recorded is None:
            notes.append("no capture provenance (hand-authored)")
        elif recorded != stored:
            notes.append("stored body was edited after capture")
        if content_digest(payload) == stored:
            statuses[i] = EntryStatus(entry["tool_name"], entry["arguments"], "FRESH", "; ".join(notes))
        else:
            notes.insert(0, "live response differs from the stored body")
            statuses[i] = EntryStatus(entry["tool_name"], entry["arguments"], "STALE", "; ".join(notes))

    final = [s for s in statuses if s is not None]
    _print(final, "check", output_stream)
    return final


def check_failed(statuses: Sequence[EntryStatus]) -> bool:
    """SKIPPED is reported but does not fail: it means "not verified", and the
    output says why. STALE, UNBOUND and ERROR mean the fixture cannot be trusted."""
    return any(s.status in ("STALE", "UNBOUND", "ERROR") for s in statuses)
