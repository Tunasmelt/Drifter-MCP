"""Builds recorded-session fixtures for the `mine/` tests from the real record
models (never hand-written JSON), so a schema change breaks these loudly.

Each trajectory is written exactly the way `record/writer.py` writes one: its
`ToolCall`s in sequence, then a `TrajectoryEnd` whose `call_seqs` names them.
"""

from __future__ import annotations

from pathlib import Path

from mcp_drifter.record.schema import (
    Environment,
    SessionStart,
    ToolCall,
    ToolDescriptor,
    ToolsList,
    TrajectoryEnd,
)

TS = "2026-09-01T00:00:00Z"


def _name(item) -> str:
    return item if isinstance(item, str) else item[0]


def write_session(
    directory: Path,
    session_id: str,
    trajectories: list[list],
    server: str = "srv",
    extra_tools: tuple[str, ...] = (),
    unsegmented_tail: list | None = None,
) -> Path:
    """One session file. `trajectories` is a list of tool-name sequences.

    An element is a tool name, or `(tool_name, arguments)` when a test needs
    the arguments to differ between otherwise identical trajectories.

    `extra_tools` are advertised in the manifest but never called (what makes
    "tools in no task" observable). `unsegmented_tail` are calls recorded after
    the last trajectory closed, with no `TrajectoryEnd` naming them.
    """
    directory.mkdir(parents=True, exist_ok=True)
    called = [_name(i) for traj in trajectories for i in traj] + [_name(i) for i in (unsegmented_tail or [])]
    served = [
        ToolDescriptor(name=n, description="d", input_schema={})
        for n in sorted(set(called) | set(extra_tools))
    ]
    seq = 0
    lines = [
        SessionStart(
            session_id=session_id, seq=seq, started_at=TS,
            environment=Environment(tool_manifest_hash="h"), raw_frame_offset=0,
        ).model_dump_json()
    ]
    seq += 1
    lines.append(
        ToolsList(
            session_id=session_id, seq=seq, timestamp=TS, server=server,
            tools_raw=served, tools_served=served, raw_frame_offset=seq,
        ).model_dump_json()
    )

    def call(item) -> int:
        nonlocal seq
        name, arguments = (item, {}) if isinstance(item, str) else item
        seq += 1
        lines.append(
            ToolCall(
                session_id=session_id, seq=seq, timestamp=TS, server=server, tool_name=name,
                arguments=arguments, result_shape={"type": "object", "keys": []},
                is_error=False, duration_ms=1.0, fault=False, raw_frame_offset=seq * 10,
            ).model_dump_json()
        )
        return seq

    for index, names in enumerate(trajectories):
        call_seqs = [call(n) for n in names]
        seq += 1
        lines.append(
            TrajectoryEnd(
                session_id=session_id, seq=seq, timestamp=TS,
                trajectory_id=f"{session_id}_t{index}", call_seqs=call_seqs,
                segmentation_method="heuristic", segmentation_confidence=0.6,
                raw_frame_offset=seq * 10,
            ).model_dump_json()
        )
    for name in unsegmented_tail or []:
        call(name)

    path = directory / f"{session_id}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_known_corpus(directory: Path) -> dict[tuple[str, ...], int]:
    """A 300-trajectory corpus with a KNOWN structure, spread over 30 sessions
    (10 trajectories each) so grouping must work across files, not within one.

    Returns the exact `{signature: count}` a correct grouping must produce.
    """
    a = ("search", "get_customer", "create_invoice")
    b = ("search", "get_customer", "update_customer")
    c = ("list_products",)
    d = ("search", "get_customer", "create_invoice", "send_receipt")
    expected = {a: 150, b: 90, c: 50, d: 10}
    pool: list[tuple[str, ...]] = []
    for signature, count in expected.items():
        pool.extend([signature] * count)
    assert len(pool) == 300
    # Interleave deterministically so no session holds a single pattern.
    ordered = [pool[i * 7 % 300] for i in range(300)]
    assert sorted(ordered) == sorted(pool)
    for session_index in range(30):
        chunk = ordered[session_index * 10 : (session_index + 1) * 10]
        write_session(directory, f"s{session_index:02d}", [list(sig) for sig in chunk])
    return expected
