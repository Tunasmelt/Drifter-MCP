"""Environment fingerprinting (F-05, docs/SPEC.md §3 principle 9).

Every session hashes agent identity, model name, MCP server names/versions,
and a tool-manifest hash into one `environment.fingerprint` field on the
SessionStart record — docs/SPEC.md §6 lists `environment.fingerprint` among the
fields that "cannot be added retroactively."

Comparisons across sessions must match except for the intended mutation
delta, or the comparison is invalid. docs/SPEC.md is explicit this must "block
comparison with an explicit error, not a silent wrong answer" — so
`require_matching_environments` raises rather than returning a boolean a
caller could silently ignore.
"""

from __future__ import annotations

import hashlib
import json

from record.schema import Environment, SessionStart


def compute_fingerprint(
    agent_identity: str | None,
    model_name: str | None,
    server_versions: dict[str, str],
    tool_manifest_hash: str | None,
) -> str:
    """Hashes the four identity components into one fingerprint.

    Deterministic regardless of server_versions' insertion order — it's
    sorted before hashing. Prefixed like tool_manifest_hash's own
    `sha256:` convention (docs/SPEC.md §6) so a fingerprint reads as "one of
    these hashes," not an opaque blob.
    """
    canonical = json.dumps(
        {
            "agent_identity": agent_identity,
            "model_name": model_name,
            "server_versions": dict(sorted(server_versions.items())),
            "tool_manifest_hash": tool_manifest_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_tool_manifest_hash(tools: list[dict]) -> str:
    """Hashes a served tool manifest, order-independent.

    The *set* of served tools (and their schemas) is what defines a
    manifest — the order a server happens to list them in isn't part of
    its identity, so results are sorted by name before hashing.
    """
    canonical = json.dumps(
        sorted(tools, key=lambda t: t.get("name", "")),
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_environment(
    agent_identity: str | None,
    model_name: str | None,
    server_versions: dict[str, str],
    tool_manifest_hash: str | None,
) -> Environment:
    """Builds a fully-populated Environment, fingerprint included."""
    return Environment(
        agent_identity=agent_identity,
        model_name=model_name,
        server_versions=server_versions,
        tool_manifest_hash=tool_manifest_hash,
        fingerprint=compute_fingerprint(agent_identity, model_name, server_versions, tool_manifest_hash),
    )


class FingerprintMismatchError(ValueError):
    """Two sessions' environments differ — docs/SPEC.md §5: block comparison
    with an explicit error, never proceed silently."""


def diff_environments(a: Environment, b: Environment) -> list[str]:
    """Returns one human-readable line per differing sub-field; empty if
    a and b are equivalent. Pure and non-raising — the building block
    `require_matching_environments` is built on, and independently useful
    wherever a caller wants the detail without the exception.
    """
    diffs: list[str] = []
    if a.agent_identity != b.agent_identity:
        diffs.append(f"agent_identity: {a.agent_identity!r} != {b.agent_identity!r}")
    if a.model_name != b.model_name:
        diffs.append(f"model_name: {a.model_name!r} != {b.model_name!r}")
    if a.tool_manifest_hash != b.tool_manifest_hash:
        diffs.append(f"tool_manifest_hash: {a.tool_manifest_hash!r} != {b.tool_manifest_hash!r}")
    for server in sorted(set(a.server_versions) | set(b.server_versions)):
        va, vb = a.server_versions.get(server), b.server_versions.get(server)
        if va != vb:
            diffs.append(f"server {server!r} version: {va!r} != {vb!r}")

    if not diffs and a.fingerprint != b.fingerprint:
        # No tracked sub-field differs, yet the fingerprints do — schema
        # drift or a hash collision. Surface plainly rather than silently
        # treating unequal fingerprints as a match just because we can't
        # name why they differ.
        diffs.append(f"fingerprint: {a.fingerprint!r} != {b.fingerprint!r} (no tracked sub-field differs)")

    return diffs


def require_matching_environments(a: SessionStart, b: SessionStart) -> None:
    """Raises FingerprintMismatchError if a and b's environments differ,
    naming exactly which sub-field(s) changed. Matching environments
    return silently — docs/SPEC.md's "block comparison with an explicit error,
    not a silent wrong answer" implemented as a raise, not a boolean a
    caller could ignore.

    Every sub-field is fatal, deliberately, not just agent_identity/
    tool_manifest_hash — e.g. a server patch-version bump with an
    unchanged manifest still blocks. docs/SPEC.md §3 principle 9 states this
    as a flat "must match... or the comparison is invalid," and treating
    server_versions as advisory would mean silently comparing across the
    exact blind spot docs/SPEC.md §15 limitation 4 names (a tool's *behavior*
    can change while its schema stays identical) — a version bump with no
    manifest change is precisely when that's most likely to have
    happened. Revisit only with a deliberate docs/SPEC.md amendment, not a
    quiet loosening here.
    """
    diffs = diff_environments(a.environment, b.environment)
    if diffs:
        raise FingerprintMismatchError(
            f"session {a.session_id!r} and {b.session_id!r} have mismatched environments: " + "; ".join(diffs)
        )
