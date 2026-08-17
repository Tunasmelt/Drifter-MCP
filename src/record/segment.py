"""Trajectory segmentation (F-06 trace-context, F-07 heuristic fallback,
F-08 data-flow references).

A "trajectory" is a sequence of `tools/call`s that belong to one task.
`tools/list` isn't part of any trajectory — it's manifest discovery, not
task activity.

Two independent ways a call joins a trajectory:

1. **Trace context (F-06).** If the request's `_meta.traceparent` carries
   a well-formed W3C trace-context header, every call sharing that trace
   ID is one trajectory, confidence 0.99 — authoritative, not time-based.
2. **Heuristic fallback (F-07).** No trace context: a call continues the
   current heuristic trajectory if it arrived within `idle_gap_seconds`
   of the last one, OR if a data-flow reference connects it to that
   trajectory (a real dependency should stay grouped regardless of a
   long gap — e.g. an agent pausing mid-task). Otherwise it starts a new
   one. Confidence is `heuristic_confidence` from calibration.yaml — a
   guess, not derived, unlike trace context's fixed 0.99.

F-08 (data-flow references) is computed for every call independently of
which path grouped it: does any of this call's argument values match a
value that appeared in a prior call's *result*, within the same
trajectory? Matching is direct equality on scalar leaf values only
(str/int/float/bool) — deliberately literal, not similarity-based
(SPEC.md §3 principle 7's "structural, not free-text" spirit applies here
too). This means small/common values (True, 0, "") can produce a spurious
reference; that's an accepted consequence of the literal design, not a
bug to silently filter around.

Result values are walked from the LIVE, unredacted dict at the moment a
call completes — never persisted themselves (SPEC.md §6's redaction
default), only the fact of a match (source_seq + paths) is.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from record.schema import DataFlowReference

TRACE_CONTEXT_CONFIDENCE = 0.99  # F-06: fixed, not a calibration guess — SPEC.md states it directly.

_TRACEPARENT_RE = re.compile(r"^[0-9a-f]{2}-([0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}$", re.IGNORECASE)


def extract_trace_id(request_meta: dict[str, Any] | None) -> str | None:
    """Returns the trace ID from a request's `_meta.traceparent`, if
    present and well-formed (W3C Trace Context: version-traceid-
    parentid-flags). None otherwise — including on malformed input,
    which falls back to heuristic segmentation rather than erroring.
    """
    if not request_meta:
        return None
    traceparent = request_meta.get("traceparent")
    if not isinstance(traceparent, str):
        return None
    match = _TRACEPARENT_RE.match(traceparent)
    return match.group(1) if match else None


def _try_parse_json_object_or_array(value: str) -> Any | None:
    """Parses value as JSON if it looks like an object/array, else None.

    Needed because a tool result with no declared output schema is
    typically wrapped as `content: [{"type": "text", "text": "<json>"}]`
    (MCPServer's behavior for a plain `dict`/`list` return with no
    structured-output type) rather than surfaced as `structuredContent`.
    Without unwrapping this, F-08 would only ever see one opaque string
    leaf and could never match a value nested inside it.
    """
    stripped = value.strip()
    if not stripped or stripped[0] not in "{[":
        return None
    try:
        parsed = json.loads(stripped)
    except ValueError:
        return None
    return parsed if isinstance(parsed, (dict, list)) else None


def _iter_leaf_paths(value: Any, path: str = "$") -> list[tuple[str, Any]]:
    """Walks a JSON-ish structure, yielding (path, value) for every
    scalar leaf. Dicts use `.key`, lists use `[index]`. A string leaf
    that parses as a JSON object/array is walked too, since that's how
    an unstructured tool result's data actually arrives on the wire."""
    if isinstance(value, dict):
        leaves = []
        for k, v in value.items():
            leaves.extend(_iter_leaf_paths(v, f"{path}.{k}"))
        return leaves
    if isinstance(value, list):
        leaves = []
        for i, v in enumerate(value):
            leaves.extend(_iter_leaf_paths(v, f"{path}[{i}]"))
        return leaves
    if isinstance(value, str):
        parsed = _try_parse_json_object_or_array(value)
        if parsed is not None:
            return _iter_leaf_paths(parsed, path)
        return [(path, value)]
    if isinstance(value, (int, float, bool)):
        return [(path, value)]
    return []  # None or an unrecognized type — nothing to match on


@dataclass
class Trajectory:
    trajectory_id: str
    method: str  # "trace_context" | "heuristic"
    confidence: float
    call_seqs: list[int] = field(default_factory=list)
    _value_index: dict[Any, tuple[int, str]] = field(default_factory=dict)
    _last_activity: float = field(default_factory=time.monotonic)

    def index_result(self, seq: int, result: Any) -> None:
        for path, value in _iter_leaf_paths(result):
            # Last writer wins on a repeated value — a reference should
            # point at the most recent prior call that produced it.
            self._value_index[value] = (seq, path)

    def find_references(self, arguments: dict) -> list[DataFlowReference]:
        refs = []
        for target_path, value in _iter_leaf_paths(arguments):
            hit = self._value_index.get(value)
            if hit is not None:
                source_seq, source_path = hit
                refs.append(DataFlowReference(source_seq=source_seq, source_path=source_path, target_path=target_path))
        return refs

    def has_reference_to(self, arguments: dict) -> bool:
        return any(value in self._value_index for _, value in _iter_leaf_paths(arguments))

    def touch(self, seq: int) -> None:
        self.call_seqs.append(seq)
        self._last_activity = time.monotonic()

    def idle_seconds(self) -> float:
        return time.monotonic() - self._last_activity


@dataclass
class CallResult:
    trajectory_id: str
    references: list[DataFlowReference]
    closed: Trajectory | None  # a heuristic trajectory this call's arrival just closed, if any


class TrajectoryTracker:
    """Stateful, per-session. Call `record_call` as each `tools/call`
    completes, `close_all` when the session ends."""

    def __init__(self, idle_gap_seconds: float, heuristic_confidence: float) -> None:
        self._idle_gap_seconds = idle_gap_seconds
        self._heuristic_confidence = heuristic_confidence
        self._trace_trajectories: dict[str, Trajectory] = {}
        self._current_heuristic: Trajectory | None = None
        # F-09: live count of distinct trajectories seen so far (started,
        # not necessarily closed yet) — for drifter observe's terminal
        # feedback. Never decremented.
        self.trajectories_started = 0

    def record_call(self, seq: int, trace_id: str | None, arguments: dict, result: Any) -> CallResult:
        closed: Trajectory | None = None

        if trace_id is not None:
            trajectory = self._trace_trajectories.get(trace_id)
            if trajectory is None:
                trajectory = Trajectory(trajectory_id=f"traj_{uuid.uuid4().hex[:12]}", method="trace_context", confidence=TRACE_CONTEXT_CONFIDENCE)
                self._trace_trajectories[trace_id] = trajectory
                self.trajectories_started += 1
        else:
            current = self._current_heuristic
            continues = current is not None and (
                current.idle_seconds() <= self._idle_gap_seconds or current.has_reference_to(arguments)
            )
            if continues:
                trajectory = current
            else:
                if current is not None:
                    closed = current
                trajectory = Trajectory(
                    trajectory_id=f"traj_{uuid.uuid4().hex[:12]}", method="heuristic", confidence=self._heuristic_confidence
                )
                self._current_heuristic = trajectory
                self.trajectories_started += 1

        references = trajectory.find_references(arguments)
        trajectory.touch(seq)
        trajectory.index_result(seq, result)

        return CallResult(trajectory_id=trajectory.trajectory_id, references=references, closed=closed)

    def close_all(self) -> list[Trajectory]:
        """Returns every still-open trajectory, for TrajectoryEnd
        emission at session close. Clears internal state — idempotent
        only in the sense that a second call returns nothing new."""
        closed = list(self._trace_trajectories.values())
        if self._current_heuristic is not None:
            closed.append(self._current_heuristic)
        self._trace_trajectories = {}
        self._current_heuristic = None
        return closed
