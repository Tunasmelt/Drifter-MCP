"""Round-trip test for record/schema.py.

Per SPEC.md §6 / the Gate 1 prompt pack: construct one of each model with
realistic values, round-trip through model_dump_json() -> model_validate_json(),
and assert equality. This is the seed of the golden fixture test — it must
never break as the schema evolves.
"""

from record.schema import (
    DataFlowReference,
    Environment,
    SessionStart,
    ToolCall,
    ToolDescriptor,
    ToolsList,
    TrajectoryEnd,
)

MODELS = [
    SessionStart(
        session_id="sess_abc123",
        seq=0,
        started_at="2026-08-16T19:40:00Z",
        environment=Environment(
            agent_identity="claude-code",
            model_name="claude-sonnet-5",
            server_versions={"crm": "1.4.2"},
            tool_manifest_hash="sha256:deadbeef",
            fingerprint="sha256:cafef00d",
        ),
        raw_frame_offset=0,
    ),
    ToolsList(
        session_id="sess_abc123",
        seq=1,
        timestamp="2026-08-16T19:40:01Z",
        server="crm",
        tools_raw=[
            ToolDescriptor(
                name="get_customer",
                description="Look up a customer by ID.",
                input_schema={"type": "object", "properties": {"customer_id": {"type": "string"}}},
                risk="read_only_external",
                classification_source="heuristic",
            )
        ],
        tools_served=[
            ToolDescriptor(
                name="get_customer",
                description="Look up a customer by ID.",
                input_schema={"type": "object", "properties": {"customer_id": {"type": "string"}}},
                risk="read_only_external",
                classification_source="heuristic",
            )
        ],
        raw_frame_offset=512,
    ),
    ToolCall(
        session_id="sess_abc123",
        seq=2,
        timestamp="2026-08-16T19:40:02Z",
        server="crm",
        tool_name="get_customer",
        arguments={"customer_id": "cust_42"},
        result_shape={"type": "object", "keys": ["id", "name", "email"], "length": 3},
        is_error=False,
        duration_ms=12.5,
        result_provenance="real",
        references=[
            DataFlowReference(source_seq=1, source_path="$.result.id", target_path="$.customer_id")
        ],
        mutation_inverse=None,
        raw_frame_offset=1024,
    ),
    TrajectoryEnd(
        session_id="sess_abc123",
        seq=3,
        timestamp="2026-08-16T19:40:03Z",
        trajectory_id="traj_001",
        call_seqs=[2],
        segmentation_method="heuristic",
        segmentation_confidence=0.87,
        baseline_fidelity=None,
        raw_frame_offset=2048,
    ),
]

# `mutation_inverse` and `baseline_fidelity` are None above because nothing
# in Gate 1 populates them yet (mutate/ and evaluate/ don't exist). That's
# not the same claim as "the field survives serialization with a real
# value" — so these two instances deliberately populate every SPEC.md §6
# "cannot be added retroactively" field with a non-default value, including
# the two above. If either field were dropped by (de)serialization, only
# these instances would catch it.
FULLY_POPULATED_MODELS = [
    ToolCall(
        session_id="sess_abc123",
        seq=4,
        timestamp="2026-08-16T19:40:04Z",
        server="crm",
        tool_name="get_customer",
        arguments={"customerId": "cust_42"},
        result_shape={"type": "object", "keys": ["id", "name", "email"], "length": 3},
        is_error=True,
        duration_ms=843.219,
        result_provenance="real",
        references=[
            DataFlowReference(source_seq=1, source_path="$.result.id", target_path="$.customerId")
        ],
        # F-12 inverse-mutation key resolution: this call arrived under an
        # active parameter_rename mutation (customer_id -> customerId), so
        # the inverse mapping needed to recover the pre-mutation key is
        # captured here at record time.
        mutation_inverse={"customerId": "customer_id"},
        raw_frame_offset=3072,
    ),
    TrajectoryEnd(
        session_id="sess_abc123",
        seq=5,
        timestamp="2026-08-16T19:40:05Z",
        trajectory_id="traj_002",
        call_seqs=[4],
        segmentation_method="trace_context",
        segmentation_confidence=0.99,
        # F-22: this trajectory is a baseline-arm run; its own replay
        # fidelity was computed and must survive round-tripping as a real
        # float, not silently collapse to None.
        baseline_fidelity=0.94,
        raw_frame_offset=3584,
    ),
]


def test_round_trip_zero_data_loss():
    for model in MODELS + FULLY_POPULATED_MODELS:
        cls = type(model)
        restored = cls.model_validate_json(model.model_dump_json())
        assert restored == model
        assert restored.model_dump() == model.model_dump()


def test_schema_version_stamped_on_every_record():
    for model in MODELS + FULLY_POPULATED_MODELS:
        assert model.schema_version == "0.1"


def test_retroactive_fields_survive_with_real_values_not_just_defaults():
    """SPEC.md §6: these fields can't be added after the fact, so it isn't
    enough that they exist on the model — a populated, non-default value
    must actually survive serialization now, while Gate 1 can still verify it."""
    call, trajectory = FULLY_POPULATED_MODELS
    restored_call = ToolCall.model_validate_json(call.model_dump_json())
    restored_trajectory = TrajectoryEnd.model_validate_json(trajectory.model_dump_json())

    assert restored_call.references != []
    assert restored_call.references[0].source_path == "$.result.id"
    assert restored_call.mutation_inverse == {"customerId": "customer_id"}
    assert restored_call.result_provenance == "real"
    assert restored_call.timestamp == "2026-08-16T19:40:04Z"  # exact value, not just non-None
    assert restored_call.is_error is True
    assert restored_call.duration_ms == 843.219

    assert restored_trajectory.baseline_fidelity == 0.94
    assert restored_trajectory.timestamp == "2026-08-16T19:40:05Z"

    # And on the ToolsList/ToolDescriptor instance already in MODELS:
    tools_list = next(m for m in MODELS if type(m).__name__ == "ToolsList")
    restored_tools_list = ToolsList.model_validate_json(tools_list.model_dump_json())
    assert restored_tools_list.tools_raw[0].classification_source == "heuristic"
    assert restored_tools_list.tools_raw[0].risk == "read_only_external"


def test_extra_fields_allowed_for_forward_compatibility():
    # A future schema version may add fields; old readers must not choke.
    restored = SessionStart.model_validate(
        {**MODELS[0].model_dump(), "future_field": "unseen-by-this-version"}
    )
    assert restored.model_dump()["future_field"] == "unseen-by-this-version"
