"""Structured recording (F-02) and raw frame mirroring (F-03).

Deliberately kept separate from reader.py (CLAUDE.md, PHASES.md Gate 1): a
shared read/write module invites silent format drift between what's written
and what's read.

`SessionRecorder.observe` is built to be `record/proxy.py`'s `on_message`
hook: a pure observer that never alters what crosses the wire, only what
gets written to disk. It correlates each `tools/list`/`tools/call` request
with its matching response (by JSON-RPC id) to build one `ToolsList` /
`ToolCall` record per exchange.

Only `result_shape` (type, keys, array lengths) is ever computed from a
response body — never the payload itself. That guarantee is enforced by
F-04's secret-redaction fixture test, not by this module alone.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

from mcp_types import JSONRPCError, JSONRPCRequest, JSONRPCResponse

from record.proxy import Direction
from record.schema import Environment, SessionStart, ToolCall, ToolDescriptor, ToolsList


def compute_result_shape(result: Any) -> dict:
    """Type/keys/array-lengths only (F-02) — the payload itself is never stored."""
    if isinstance(result, dict):
        return {
            "type": "object",
            "keys": sorted(result.keys()),
            "array_lengths": {k: len(v) for k, v in result.items() if isinstance(v, list)},
        }
    if isinstance(result, list):
        return {"type": "array", "length": len(result)}
    return {"type": type(result).__name__}


class SessionRecorder:
    """Writes one session's JSONL + raw frame mirror as messages arrive."""

    def __init__(self, session_dir: Path, raw_dir: Path, server_name: str, session_id: str | None = None) -> None:
        self.session_id = session_id or uuid.uuid4().hex
        self.server_name = server_name

        session_dir.mkdir(parents=True, exist_ok=True)
        raw_dir.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = session_dir / f"{self.session_id}.jsonl"
        self.raw_path = raw_dir / f"{self.session_id}.frames"
        self._jsonl_file = self.jsonl_path.open("a", encoding="utf-8")
        # Binary, deliberately: raw_frame_offset must be a byte offset a
        # later reader can seek to directly. A text-mode file on Windows
        # translates "\n" to "\r\n" on write, which shifts tell()'s offsets
        # out of sync with the character positions a text-mode read sees.
        self._raw_file = self.raw_path.open("ab")

        self._seq = 0
        # JSON-RPC request id -> what we'll need once its response arrives.
        self._pending: dict[Any, dict[str, Any]] = {}

        self._write_record(
            SessionStart(
                session_id=self.session_id,
                seq=self._next_seq(),
                started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                # F-05 (environment fingerprinting) isn't built yet; the
                # field exists per SPEC.md §6 but stays empty until then.
                environment=Environment(),
                raw_frame_offset=0,
            )
        )

    def _next_seq(self) -> int:
        seq = self._seq
        self._seq += 1
        return seq

    def _write_raw_frame(self, message) -> int:
        """Appends the message's wire-serialized text to the raw mirror.

        Reuses the SDK's own `model_dump_json(by_alias=True,
        exclude_unset=True)` — the same call `stdio_client`/`stdio_server`
        use to put bytes on the wire, and verified byte-for-byte faithful
        to the actual wire content by record/proxy.py's F-01 fidelity test.
        Returns the byte offset the frame was written at.
        """
        offset = self._raw_file.tell()
        text = message.model_dump_json(by_alias=True, exclude_unset=True)
        self._raw_file.write(text.encode("utf-8") + b"\n")
        self._raw_file.flush()
        return offset

    def _write_record(self, record) -> None:
        self._jsonl_file.write(record.model_dump_json() + "\n")
        self._jsonl_file.flush()

    def observe(self, direction: Direction, message) -> None:
        """The `on_message` hook passed to `record.proxy.run_passthrough_proxy`."""
        if isinstance(message, Exception):
            return

        offset = self._write_raw_frame(message.message)
        rpc = message.message

        if isinstance(rpc, JSONRPCRequest) and rpc.method in ("tools/list", "tools/call"):
            self._pending[rpc.id] = {"method": rpc.method, "params": rpc.params or {}}
        elif isinstance(rpc, JSONRPCResponse):
            pending = self._pending.pop(rpc.id, None)
            if pending is None:
                return  # not a request this recorder tracks (e.g. initialize)
            if pending["method"] == "tools/call":
                self._write_tool_call(pending["params"], rpc.result, offset)
            elif pending["method"] == "tools/list":
                self._write_tools_list(rpc.result, offset)
        elif isinstance(rpc, JSONRPCError):
            # Gate 1 doesn't model call failures yet; drop the pending
            # entry so it can't leak, but write nothing for it.
            self._pending.pop(rpc.id, None)

    def _write_tool_call(self, params: dict, result: dict, raw_frame_offset: int) -> None:
        self._write_record(
            ToolCall(
                session_id=self.session_id,
                seq=self._next_seq(),
                server=self.server_name,
                tool_name=params.get("name", ""),
                arguments=params.get("arguments") or {},
                result_shape=compute_result_shape(result),
                raw_frame_offset=raw_frame_offset,
            )
        )

    def _write_tools_list(self, result: dict, raw_frame_offset: int) -> None:
        tools = [
            ToolDescriptor(
                name=t.get("name", ""),
                description=t.get("description") or "",
                input_schema=t.get("inputSchema") or {},
            )
            for t in result.get("tools", [])
        ]
        self._write_record(
            ToolsList(
                session_id=self.session_id,
                seq=self._next_seq(),
                server=self.server_name,
                # No mutate/ yet (Gate 3+) — raw and served are identical.
                tools_raw=tools,
                tools_served=tools,
                raw_frame_offset=raw_frame_offset,
            )
        )

    def close(self) -> None:
        self._jsonl_file.close()
        self._raw_file.close()
