"""CLI entrypoint for the recording proxy: `python -m record <command> [args...]`.

This is F-01/F-02/F-03's invocation shape only — the way Drifter stands in
for the real server in an MCP client's config. `drifter observe` (F-09) is
the user-facing command that wraps this in later gates, including reading
the server's name from drifter.yaml instead of using the raw command
string; this module is intentionally usable standalone until then.

Recording paths default to SPEC.md §11's `record: {dir: .drifter/runs}`
default, overridable via DRIFTER_RUNS_DIR / DRIFTER_RAW_DIR — enough to
keep tests hermetic without building the full config loader early.

DRIFTER_MODEL_NAME is the same kind of stopgap for F-05's environment
fingerprint: MCP traffic never reveals which model the agent is running
(SPEC.md §15's limitation 2), so it can only ever be supplied out-of-band
— an env var now, `drifter.yaml` once the config loader exists.

DRIFTER_CALIBRATION_PATH overrides where calibration.yaml (SPEC.md §9) is
read from — a path override, not a per-field one, so tests can point at a
throwaway file with e.g. a short idle_gap_seconds without a config loader
having to exist yet.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import anyio
from mcp.client.stdio import StdioServerParameters

from record.calibration import load_calibration
from record.proxy import run_passthrough_proxy
from record.writer import SessionRecorder


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: python -m record <command> [args...]")

    command, args = sys.argv[1], sys.argv[2:]
    server = StdioServerParameters(command=command, args=args)

    runs_dir = Path(os.environ.get("DRIFTER_RUNS_DIR", ".drifter/runs"))
    raw_dir = Path(os.environ.get("DRIFTER_RAW_DIR", ".drifter/raw"))
    model_name = os.environ.get("DRIFTER_MODEL_NAME")
    calibration_path = os.environ.get("DRIFTER_CALIBRATION_PATH")
    calibration = load_calibration(Path(calibration_path) if calibration_path else None)
    recorder = SessionRecorder(
        session_dir=runs_dir, raw_dir=raw_dir, server_name=command, model_name=model_name, calibration=calibration
    )

    try:
        anyio.run(run_passthrough_proxy, server, recorder.observe)
    finally:
        recorder.close()


if __name__ == "__main__":
    main()
