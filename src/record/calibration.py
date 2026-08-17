"""Calibration register loader (SPEC.md §9).

Every constant here is an engineering default, not a research finding —
CLAUDE.md's non-negotiable invariant: don't treat these as authoritative,
and don't add new unlabeled constants elsewhere. `calibration.yaml` at the
repo root is the single place they live; this module's field defaults
exist only as a fallback for callers with no file at their cwd (e.g. a
package installed and run from an arbitrary directory before `drifter
init` has written a project-local copy), not as a second source of truth.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict


class EffectSize(BaseModel):
    model_config = ConfigDict(extra="allow")
    inconclusive: float = 1.0
    regression: float = 2.0


class Segmentation(BaseModel):
    model_config = ConfigDict(extra="allow")
    idle_gap_seconds: float = 30
    heuristic_confidence: float = 0.6


class Baseline(BaseModel):
    model_config = ConfigDict(extra="allow")
    repeats: int = 10


class MutationRepeats(BaseModel):
    model_config = ConfigDict(extra="allow")
    screen: int = 1
    confirm: int = 5
    resolve: int = 20


class Mutation(BaseModel):
    model_config = ConfigDict(extra="allow")
    repeats: MutationRepeats = MutationRepeats()


class Doctor(BaseModel):
    model_config = ConfigDict(extra="allow")
    # Not in SPEC.md §9's original table — added here per CLAUDE.md's
    # invariant that new invented constants belong in this file, not
    # hardcoded (same reasoning as segmentation.heuristic_confidence
    # above). How long `drifter doctor` waits for a configured server to
    # complete the MCP initialize handshake before reporting it
    # unreachable; a guess, not derived from anything.
    connectivity_timeout_seconds: float = 10


class Calibration(BaseModel):
    model_config = ConfigDict(extra="allow")
    semantic_weight: float = 0.8
    fidelity_floor: float = 0.70
    fidelity_flag_threshold: float = 0.90
    effect_size: EffectSize = EffectSize()
    segmentation: Segmentation = Segmentation()
    baseline: Baseline = Baseline()
    mutation: Mutation = Mutation()
    doctor: Doctor = Doctor()


def load_calibration(path: Path | None = None) -> Calibration:
    """Loads calibration.yaml, or this module's field defaults if absent.

    `path` defaults to `calibration.yaml` relative to the current working
    directory — the same convention `drifter.yaml` will follow once the
    config loader exists (F-33).
    """
    path = path or Path("calibration.yaml")
    if not path.exists():
        return Calibration()
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return Calibration.model_validate(data)
