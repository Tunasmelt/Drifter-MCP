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
    server: str
    tool_name: str
    arguments: dict = {}
    # Type/keys/length only — never the payload itself (F-02, F-04).
    result_shape: dict | None = None
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
    trajectory_id: str
    call_seqs: list[int] = []
    segmentation_method: SegmentationMethod | None = None
    segmentation_confidence: float | None = None
    # F-21/F-22. Populated only for trajectories that are baseline-arm
    # runs, once evaluate/ exists; None otherwise.
    baseline_fidelity: float | None = None
    raw_frame_offset: int


Record = SessionStart | ToolsList | ToolCall | TrajectoryEnd
