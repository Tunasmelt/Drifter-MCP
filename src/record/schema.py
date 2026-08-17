"""Record schema for Drifter session logs (SPEC.md §6).

One JSONL file per session; every line is one of the record models below,
disambiguated by `record_type`. `schema_version` is stamped on every record
so `record/reader.py` can validate compatibility as the schema evolves.

Field-inclusion rule (SPEC.md §6): a field is recorded only if it cannot be
derived later from what *is* recorded. The fields below marked "cannot be
added retroactively" are the exception list from SPEC.md §6 — they exist in
full now even though the logic that populates most of them doesn't land
until later gates (F-05 environment fingerprinting, F-08 data-flow
references, F-26 tool risk classification, F-21/F-22 baseline fidelity).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

SCHEMA_VERSION = "0.1"

RiskLevel = Literal[
    "unknown",
    "read_only_local",
    "read_only_external",
    "reversible_write",
    "irreversible_write",
    "destructive",
]

ClassificationSource = Literal[
    "mcp_annotation",
    "heuristic",
    "observed_behavior",
    "user_override",
]

ResultProvenance = Literal["real", "synthetic"]

SegmentationMethod = Literal["trace_context", "heuristic"]


class Environment(BaseModel):
    """Agent/model/server identity for one session (F-05)."""

    model_config = ConfigDict(extra="allow")

    agent_identity: str | None = None
    model_name: str | None = None
    server_versions: dict[str, str] = {}
    tool_manifest_hash: str | None = None
    # Hash of the fields above. Cannot be added retroactively — recomputing
    # it later would require re-deriving identity from data we chose not to
    # keep once shape-only redaction has already applied.
    fingerprint: str | None = None


class ToolDescriptor(BaseModel):
    """One tool as it appeared in a `tools/list` response."""

    model_config = ConfigDict(extra="allow")

    name: str
    description: str
    input_schema: dict = {}
    # F-26. Populated once policy/ exists; None at Gate 1.
    risk: RiskLevel | None = None
    classification_source: ClassificationSource | None = None


class DataFlowReference(BaseModel):
    """One argument value traced back to a prior call's result (F-08)."""

    model_config = ConfigDict(extra="allow")

    source_seq: int
    source_path: str
    target_path: str


class SessionStart(BaseModel):
    """First line of every session JSONL file."""

    model_config = ConfigDict(extra="allow")

    schema_version: Literal["0.1"] = SCHEMA_VERSION
    record_type: Literal["session_start"] = "session_start"
    session_id: str
    seq: int
    started_at: str
    environment: Environment
    raw_frame_offset: int


class ToolsList(BaseModel):
    """One `tools/list` exchange.

    `tools_raw` (as declared by the real server) and `tools_served` (what
    the agent actually received) are both always recorded — at Gate 1,
    before mutate/ exists, they are identical, but the schema carries both
    from commit one since a mutated manifest can't be reconstructed later
    from the served version alone.
    """

    model_config = ConfigDict(extra="allow")

    schema_version: Literal["0.1"] = SCHEMA_VERSION
    record_type: Literal["tools_list"] = "tools_list"
    session_id: str
    seq: int
    # ISO 8601 UTC, captured when the response was observed. Cannot be
    # added retroactively — see the note on ToolCall.timestamp below.
    timestamp: str
    server: str
    tools_raw: list[ToolDescriptor]
    tools_served: list[ToolDescriptor]
    raw_frame_offset: int


class ToolCall(BaseModel):
    """One `tools/call` request/response pair."""

    model_config = ConfigDict(extra="allow")

    schema_version: Literal["0.1"] = SCHEMA_VERSION
    record_type: Literal["tool_call"] = "tool_call"
    session_id: str
    seq: int
    # ISO 8601 UTC, captured when the response was observed. Added in
    # Prompt 6 (CHANGELOG.md) — F-07's idle-gap segmentation and F-28's
    # signature normalization both presuppose a per-call timestamp exists,
    # but SPEC.md §6's original field list never named one. Genuinely
    # transient data: cannot be added retroactively to an already-recorded
    # call, so it belongs on this same list going forward.
    timestamp: str
    server: str
    tool_name: str
    arguments: dict = {}
    # Type/keys/length only — never the payload itself (F-02, F-04).
    result_shape: dict | None = None
    # Added in Prompt 8 (CHANGELOG.md), for the same reason as `timestamp`:
    # F-10's error rate needs to know whether this specific call failed,
    # and `result_shape` deliberately never stores values — only type/keys/
    # length — so a result's `isError: true` (MCP's CallToolResult field;
    # SHOULD be how tool-execution failures are reported, per the SDK's own
    # docstring, rather than a protocol-level JSON-RPC error) would
    # otherwise be indistinguishable from `isError: false` once written.
    # Cannot be added retroactively — the boolean is gone the moment
    # result_shape is computed and the raw result is discarded.
    #
    # `| None`, deliberately, unlike `timestamp`'s plain `str`: `timestamp`
    # was added (Prompt 6) when no real recorded data existed anywhere to
    # protect. `is_error`/`duration_ms` are being added with a real weekly
    # trial imminent (PHASES.md Gate 1's exit test), so a record written
    # before this field existed must still parse — `record/reader.py`
    # raising a raw pydantic ValidationError on old data, or a required
    # field silently coercing to a default, are exactly the two failure
    # modes CLAUDE.md's testing-discipline note warns about (a field
    # populated, or in this case *unpopulated*, with a value that reads as
    # legitimate). `None` here means "unknown — recorded before this field
    # existed," which `cli/stats.py` must report as such (excluded from
    # error_rate's denominator), never coerced to `False`/0.
    is_error: bool | None = None
    # Milliseconds between this call's request being observed and its
    # response being observed, measured with a monotonic clock in
    # record/writer.py. Added in Prompt 8 alongside `is_error`, for F-10's
    # latency percentiles — `timestamp` alone can't serve this: it's an
    # ISO 8601 string with one-second resolution, far too coarse for a
    # typical tool-call round trip, and only one is recorded per call
    # rather than a request/response pair. Cannot be added retroactively:
    # the monotonic-clock delta only exists at the instant the response
    # arrives, mid-request. `| None` for the same backward-compatibility
    # reason as `is_error` above.
    duration_ms: float | None = None
    result_provenance: ResultProvenance = "real"
    references: list[DataFlowReference] = []
    # Inverse mapping consumed by replay's F-12 key resolution when this
    # call was made under an active mutation. None at Gate 1 (mutate/
    # doesn't exist yet) but the field must exist now — the inverse can
    # only be captured at the moment the mutation was applied.
    mutation_inverse: dict | None = None
    raw_frame_offset: int


class TrajectoryEnd(BaseModel):
    """Closes one segmented trajectory (F-06/F-07)."""

    model_config = ConfigDict(extra="allow")

    schema_version: Literal["0.1"] = SCHEMA_VERSION
    record_type: Literal["trajectory_end"] = "trajectory_end"
    session_id: str
    seq: int
    # ISO 8601 UTC, when this trajectory was closed (see ToolCall.timestamp).
    timestamp: str
    trajectory_id: str
    call_seqs: list[int] = []
    segmentation_method: SegmentationMethod | None = None
    segmentation_confidence: float | None = None
    # F-21/F-22. Populated only for trajectories that are baseline-arm
    # runs, once evaluate/ exists; None otherwise.
    baseline_fidelity: float | None = None
    raw_frame_offset: int


Record = SessionStart | ToolsList | ToolCall | TrajectoryEnd
