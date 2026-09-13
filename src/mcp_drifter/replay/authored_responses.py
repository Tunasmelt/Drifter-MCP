"""Explicit response fixtures for content-dependent offline replay.

These values are authored test data. They are never inferred from recorded
payloads and never written into the shape-only observation format.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml
from mcp import types
from pydantic import TypeAdapter, ValidationError

from mcp_drifter.record.reader import read_session
from mcp_drifter.record.schema import ToolCall
from mcp_drifter.replay.replay_store import replay_key


class AuthoredResponseError(ValueError):
    pass


@dataclass(frozen=True)
class AuthoredResponses:
    server: str
    by_key: dict[str, types.CallToolResult]

    def lookup(
        self,
        tool_name: str,
        arguments: dict,
        inverse_param_map: dict[str, str] | None = None,
    ) -> types.CallToolResult | None:
        result = self.by_key.get(replay_key(self.server, tool_name, arguments))
        if result is None and inverse_param_map:
            original = {inverse_param_map.get(k, k): v for k, v in arguments.items()}
            result = self.by_key.get(replay_key(self.server, tool_name, original))
        return result.model_copy(deep=True) if result is not None else None


def load_authored_responses(
    path: Path,
    server: str,
    session_paths: Sequence[Path],
) -> AuthoredResponses:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise AuthoredResponseError(f"cannot read authored response fixture {path}: {exc}") from exc
    if not isinstance(document, dict) or document.get("version") != 1:
        raise AuthoredResponseError("authored response fixture must be a mapping with version: 1")
    if document.get("server") != server:
        raise AuthoredResponseError(
            f"authored response fixture server {document.get('server')!r} does not match {server!r}"
        )
    responses = document.get("responses")
    if not isinstance(responses, list) or not responses:
        raise AuthoredResponseError("authored response fixture needs a non-empty responses list")

    corpus_keys = {
        replay_key(record.server, record.tool_name, record.arguments)
        for session_path in session_paths
        for record in read_session(session_path)
        if isinstance(record, ToolCall) and record.server == server and record.result_provenance == "real"
    }
    adapter = TypeAdapter(types.CallToolResult)
    indexed: dict[str, types.CallToolResult] = {}
    for number, entry in enumerate(responses, 1):
        if not isinstance(entry, dict) or not isinstance(entry.get("tool_name"), str) or not isinstance(entry.get("arguments"), dict):
            raise AuthoredResponseError(f"response {number} needs tool_name, arguments, and result")
        key = replay_key(server, entry["tool_name"], entry["arguments"])
        if key not in corpus_keys:
            raise AuthoredResponseError(
                f"response {number} ({entry['tool_name']}) is not present in the recorded corpus"
            )
        if key in indexed:
            raise AuthoredResponseError(f"response {number} duplicates an earlier exact request")
        try:
            indexed[key] = adapter.validate_python(entry.get("result"))
        except ValidationError as exc:
            raise AuthoredResponseError(f"response {number} has an invalid CallToolResult: {exc}") from exc
    return AuthoredResponses(server=server, by_key=indexed)
