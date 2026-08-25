"""Structured recording (F-02), raw frame mirroring (F-03), and
environment fingerprinting (F-05).

Deliberately kept separate from reader.py (CLAUDE.md, PHASES.md Gate 1): a
shared read/write module invites silent format drift between what's written
and what's read.

`SessionRecorder.observe` is built to be `record/proxy.py`'s `on_message`
hook: a pure observer that never alters what crosses the wire, only what
gets written to disk. It correlates each `initialize`/`tools/list`/
`tools/call` request with its matching response (by JSON-RPC id) to build
one `SessionStart` / `ToolsList` / `ToolCall` record per exchange.

SessionStart's environment fingerprint needs data that only exists once
the `initialize` handshake and the first `tools/list` have been observed
(agent identity, server info, and the tool manifest respectively) — so
unlike Prompt 1's placeholder, SessionStart is no longer written the
instant the recorder is constructed. It's flushed lazily, the first time
there's something to write after it, using whatever identity info has been
gathered so far; `close()` flushes it regardless as a safety net for a
session that ends before any tool call happens. It still always ends up
first in the file (seq=0), since nothing else is written before it.

Only `result_shape` (type, keys, array lengths) is ever computed from a
response body — never the payload itself. Argument values and the raw
frame mirror's payload fields are passed through F-04's redaction layer
(record/redact.py) before anything touches disk — see
tests/record/test_redaction.py for the enforced guarantee.

Trajectory segmentation (F-06/F-07/F-08, record/segment.py) runs on every
`tools/call`: trace-context grouping when `_meta.traceparent` is present,
idle-gap-plus-data-flow heuristic fallback otherwise. A heuristic
trajectory closing mid-session emits its TrajectoryEnd before the call
that closed it is written; any trajectories still open at `close()` are
flushed then.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from mcp_types import JSONRPCError, JSONRPCRequest, JSONRPCResponse

from record.calibration import Calibration, load_calibration
from record.fingerprint import build_environment, compute_tool_manifest_hash
from record.proxy import Direction
from record.redact import redact_rpc_payload, redact_secrets
from record.schema import (
    SYNTHETIC_RESULT_MARKER_KEY,
    ResultProvenance,
    SessionStart,
    ToolCall,
    ToolDescriptor,
    ToolsList,
    TrajectoryEnd,
)
from record.segment import Trajectory, TrajectoryTracker, extract_trace_id

_TRACKED_METHODS = ("initialize", "tools/list", "tools/call")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


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

    def __init__(
        self,
        session_dir: Path,
        raw_dir: Path,
        server_name: str,
        session_id: str | None = None,
        model_name: str | None = None,
        calibration: Calibration | None = None,
    ) -> None:
        self.session_id = session_id or uuid.uuid4().hex
        self.server_name = server_name
        # MCP traffic has no concept of "which model" — the protocol only
        # ever exposes agent/server identity (SPEC.md §15's limitation 2).
        # Sourced out-of-band by the caller (env var, later drifter.yaml).
        self.model_name = model_name

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

        self._started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._first_raw_offset: int | None = None
        self._last_raw_offset = 0
        self._session_start_written = False
        self._closed = False
        self._agent_identity: str | None = None
        self._server_versions: dict[str, str] = {}
        self._tool_manifest_hash: str | None = None

        calibration = calibration or load_calibration()
        self._tracker = TrajectoryTracker(
            idle_gap_seconds=calibration.segmentation.idle_gap_seconds,
            heuristic_confidence=calibration.segmentation.heuristic_confidence,
        )

        # F-09: live counters for drifter observe's terminal feedback.
        # Public — cli/observe.py reads these directly, no separate
        # counting logic duplicated there.
        self.call_count = 0
        self.error_count = 0

    @property
    def trajectory_count(self) -> int:
        return self._tracker.trajectories_started

    @property
    def closed(self) -> bool:
        """True once close() has been invoked (started or finished) —
        cli/observe.py's handle_sigint uses this as its own idempotency
        guard against a rapid double Ctrl+C. Set at the very start of
        close(), not the end, so it also covers a caller re-entering
        close() while an earlier call is still running (Python signal
        handlers are not automatically re-entrancy-safe)."""
        return self._closed

    def _next_seq(self) -> int:
        seq = self._seq
        self._seq += 1
        return seq

    def _write_raw_frame(self, message) -> int:
        """Appends the message's wire content to the raw mirror, redacted.

        Starts from the SDK's own `model_dump(by_alias=True,
        exclude_unset=True)` — the same shape `stdio_client`/`stdio_server`
        put on the wire, verified byte-for-byte faithful to actual wire
        content by record/proxy.py's F-01 fidelity test — then redacts
        payload fields (params/result/error.data) before writing. The raw
        mirror gets the SAME redaction as the JSONL, not a weaker pass
        (SECURITY.md, F-04): this is the one place a full response payload
        is otherwise recorded, so it's exactly where a gap would matter
        most. Returns the byte offset the frame was written at.
        """
        offset = self._raw_file.tell()
        if self._first_raw_offset is None:
            self._first_raw_offset = offset
        self._last_raw_offset = offset
        raw = message.model_dump(mode="json", by_alias=True, exclude_unset=True)
        redacted = redact_rpc_payload(raw)
        text = json.dumps(redacted, separators=(",", ":"))
        self._raw_file.write(text.encode("utf-8") + b"\n")
        self._raw_file.flush()
        return offset

    def _write_record(self, record) -> None:
        self._jsonl_file.write(record.model_dump_json() + "\n")
        self._jsonl_file.flush()

    def _ensure_session_start_written(self) -> None:
        """Flushes SessionStart using whatever identity info is known so
        far. Idempotent, and always the first record written (seq=0),
        since nothing else is written before this is called.
        """
        if self._session_start_written:
            return
        self._session_start_written = True
        self._write_record(
            SessionStart(
                session_id=self.session_id,
                seq=self._next_seq(),
                started_at=self._started_at,
                environment=build_environment(
                    agent_identity=self._agent_identity,
                    model_name=self.model_name,
                    server_versions=self._server_versions,
                    tool_manifest_hash=self._tool_manifest_hash,
                ),
                raw_frame_offset=self._first_raw_offset or 0,
            )
        )

    def observe(self, direction: Direction, message) -> None:
        """The `on_message` hook passed to `record.proxy.run_passthrough_proxy`."""
        if isinstance(message, Exception):
            self.error_count += 1  # F-09: a frame that failed to parse
            return

        offset = self._write_raw_frame(message.message)
        rpc = message.message

        if isinstance(rpc, JSONRPCRequest) and rpc.method in _TRACKED_METHODS:
            # F-10: duration_ms needs a start point. Monotonic, not wall-clock
            # (time.monotonic() can't be affected by clock adjustments
            # mid-call, and has far finer resolution than _now()'s
            # one-second-granularity ISO string).
            self._pending[rpc.id] = {"method": rpc.method, "params": rpc.params or {}, "requested_at": time.monotonic()}
            if rpc.method == "initialize":
                client_info = (rpc.params or {}).get("clientInfo") or {}
                name, version = client_info.get("name"), client_info.get("version")
                if name:
                    self._agent_identity = f"{name}/{version}" if version else name
        elif isinstance(rpc, JSONRPCResponse):
            pending = self._pending.pop(rpc.id, None)
            if pending is None:
                return  # not a request this recorder tracks
            if pending["method"] == "initialize":
                server_info = (rpc.result or {}).get("serverInfo") or {}
                name, version = server_info.get("name"), server_info.get("version")
                if name:
                    self._server_versions = {name: version or ""}
            elif pending["method"] == "tools/call":
                self._ensure_session_start_written()
                duration_ms = (time.monotonic() - pending["requested_at"]) * 1000
                # A synthetic-response producer (tool_addition's F-14-
                # scoped support) marks its result dict with this private
                # key before handing it to on_message -- never present on
                # a real recorded response, and never sent to the actual
                # agent (that's a separate, clean dict on the wire; see
                # replay_proxy.py's on_call_tool). Stripped here so it
                # never leaks into result_shape as if it were a real key.
                result = rpc.result
                provenance: ResultProvenance = "real"
                if isinstance(result, dict) and SYNTHETIC_RESULT_MARKER_KEY in result:
                    result = dict(result)
                    provenance = result.pop(SYNTHETIC_RESULT_MARKER_KEY)
                self._write_tool_call(pending["params"], result, offset, duration_ms, result_provenance=provenance)
            elif pending["method"] == "tools/list":
                # Must run before _ensure_session_start_written(): the
                # manifest hash has to be known *before* SessionStart is
                # flushed, or it's flushed with tool_manifest_hash still
                # None and there's no going back — SessionStart is only
                # ever written once.
                self._note_tool_manifest(rpc.result or {})
                self._ensure_session_start_written()
                self._write_tools_list(rpc.result, offset)
        elif isinstance(rpc, JSONRPCError):
            pending = self._pending.pop(rpc.id, None)
            self.error_count += 1
            # `initialize`/`tools/list` protocol faults have no ToolCall-
            # shaped home (they're not per-call attempts) and stay
            # unmodeled, same as before. A `tools/call` protocol fault
            # (this response is a JSON-RPC error, not a CallToolResult —
            # the SDK's own docstring: this SHOULD only happen for "errors
            # in finding the tool," not a tool-reported failure, which
            # goes through isError instead) now gets a real record — see
            # `fault` on ToolCall (CHANGELOG.md, this prompt). Previously
            # dropped silently, with no per-tool attribution possible at
            # all — closing the exact gap the is_error investigation
            # surfaced, not a new unrelated feature.
            if pending is not None and pending["method"] == "tools/call":
                self._ensure_session_start_written()
                duration_ms = (time.monotonic() - pending["requested_at"]) * 1000
                self._write_tool_call_fault(pending["params"], offset, duration_ms)

    def _write_tool_call(
        self,
        params: dict,
        result: dict,
        raw_frame_offset: int,
        duration_ms: float,
        result_provenance: ResultProvenance = "real",
    ) -> None:
        seq = self._next_seq()
        self.call_count += 1
        arguments = params.get("arguments") or {}
        # MCP's CallToolResult.isError (SHOULD be how tool-execution
        # failures are reported, per the SDK's own docstring, rather than a
        # protocol-level JSON-RPC error) — the wire key, since `result` is
        # the raw dict as received, not a re-serialized model.
        is_error = bool((result or {}).get("isError", False))
        if is_error:
            self.error_count += 1

        # F-06/F-07/F-08: segment before writing, so a closing heuristic
        # trajectory's TrajectoryEnd lands before the call that closed it.
        trace_id = extract_trace_id(params.get("_meta"))
        outcome = self._tracker.record_call(seq, trace_id, arguments, result)
        if outcome.closed is not None:
            self._write_trajectory_end(outcome.closed)

        self._write_record(
            ToolCall(
                session_id=self.session_id,
                seq=seq,
                timestamp=_now(),
                server=self.server_name,
                tool_name=params.get("name", ""),
                # F-04: secret-shaped values redacted before this ever
                # reaches disk. result isn't redacted here — result_shape
                # never carries payload values in the first place.
                arguments=redact_secrets(arguments),
                result_shape=compute_result_shape(result),
                is_error=is_error,
                duration_ms=duration_ms,
                # This call reached a real CallToolResult — known, not a
                # protocol fault. Explicit False (not left at the schema's
                # `None` default), so fault_rate can read "definitely zero
                # faults" rather than "unknown" for a corpus with none.
                fault=False,
                result_provenance=result_provenance,
                references=outcome.references,
                raw_frame_offset=raw_frame_offset,
            )
        )

    def _write_tool_call_fault(self, params: dict, raw_frame_offset: int, duration_ms: float) -> None:
        """Writes a ToolCall record for a `tools/call` that failed at the
        protocol level (JSONRPCError) instead of producing a
        CallToolResult. Distinct from `_write_tool_call`'s `is_error`
        path: there is no result here at all, so `result_shape` is None
        and `is_error` is left at its own `None` default — not
        `False` — since "did the tool report a semantic error" genuinely
        doesn't apply when the tool was never actually invoked in a way
        that could answer that. Trajectory segmentation still runs: the
        attempt genuinely happened and belongs in whatever trajectory it
        arrived in, even though it produced no result to index for future
        data-flow matches (record.segment.TrajectoryTracker tolerates a
        None result — see its docstring/tests).
        """
        seq = self._next_seq()
        self.call_count += 1
        arguments = params.get("arguments") or {}

        trace_id = extract_trace_id(params.get("_meta"))
        outcome = self._tracker.record_call(seq, trace_id, arguments, None)
        if outcome.closed is not None:
            self._write_trajectory_end(outcome.closed)

        self._write_record(
            ToolCall(
                session_id=self.session_id,
                seq=seq,
                timestamp=_now(),
                server=self.server_name,
                tool_name=params.get("name", ""),
                arguments=redact_secrets(arguments),
                result_shape=None,
                duration_ms=duration_ms,
                fault=True,
                references=outcome.references,
                raw_frame_offset=raw_frame_offset,
            )
        )

    def _write_trajectory_end(self, trajectory: Trajectory) -> None:
        self._write_record(
            TrajectoryEnd(
                session_id=self.session_id,
                seq=self._next_seq(),
                timestamp=_now(),
                trajectory_id=trajectory.trajectory_id,
                call_seqs=trajectory.call_seqs,
                segmentation_method=trajectory.method,
                segmentation_confidence=trajectory.confidence,
                # No single wire frame corresponds to a trajectory
                # boundary (it's derived from several calls); the most
                # recent frame observed is the closest meaningful anchor.
                raw_frame_offset=self._last_raw_offset,
            )
        )

    def _note_tool_manifest(self, result: dict) -> None:
        """Records the manifest hash — must run before the first
        SessionStart flush (see the call site in observe()).

        Captured the first time any tools/list completes. Gate 1 doesn't
        re-list mid-session, so "first" and "only" coincide for now; a
        later gate that does needs to decide whether a manifest change
        mid-session should re-fingerprint.
        """
        if self._tool_manifest_hash is None:
            self._tool_manifest_hash = compute_tool_manifest_hash(result.get("tools", []))

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
                timestamp=_now(),
                server=self.server_name,
                # No mutate/ yet (Gate 3+) — raw and served are identical.
                tools_raw=tools,
                tools_served=tools,
                raw_frame_offset=raw_frame_offset,
            )
        )

    def close(self) -> None:
        # Guard set first, before any work — protects against a second,
        # re-entrant call landing mid-execution (e.g. a signal handler
        # firing twice in quick succession), not just a call after this
        # one has already finished. See the `closed` property's docstring.
        if self._closed:
            return
        self._closed = True
        # Safety net: a session that ends before any tools/list or
        # tools/call still gets a SessionStart record, using whatever
        # identity info (e.g. just the initialize handshake) was seen.
        self._ensure_session_start_written()
        for trajectory in self._tracker.close_all():
            self._write_trajectory_end(trajectory)
        self._jsonl_file.close()
        self._raw_file.close()
