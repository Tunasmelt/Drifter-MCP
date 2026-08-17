"""`drifter stats` (F-10): summarizes the JSONL corpus `drifter observe`
produces — per-(server, tool) call frequency, unused tools, retry rate,
error rate, and latency percentiles.

Reads every `*.jsonl` file under the corpus directory independently (one
file per `drifter observe` session — SPEC.md's architecture, F-09) and
aggregates across all of them. Must work against whatever corpus exists at
the time it's run, including zero sessions or a single call: this is meant
to be run mid-trial, not only once a full week of data exists (FEATURES.md
F-10's "Done when" explicitly ties this to the author checking it against
their own real usage).

Retry rate's definition (not specified anywhere in SPEC.md/FEATURES.md —
a genuine design decision made here, not a hidden assumption): a tool_call
is a retry if it repeats the immediately preceding call to the same
(server, tool_name) *within the same session* with identical (already-
redacted) arguments. Reset per session, since two independent `drifter
observe` connections aren't the same interaction. No time window — SPEC.md
§13's own example (`search → get_customer → retry → create_invoice`) is
about adjacency in the call sequence, not elapsed time, and no calibration
precedent exists for inventing one here.

Error rate uses `is_error` (added Prompt 8, CHANGELOG v1.0.7) — never
`result_shape`'s keys, which never carried the boolean value.

`is_error`/`duration_ms` are `None` on any `ToolCall` recorded before the
schema version that added them (record/schema.py's `| None`, backward-
compat, CHANGELOG v1.0.8). Verified directly against an authentic
reconstructed pre-migration corpus (a real fixture recording under the
actual old code via `git stash`, not a hand-built fixture): a naive
"missing == False/0.0" read silently under-reports error_rate as 0% for
calls whose outcome is genuinely unknown — precisely the "recurring bug
pattern" CLAUDE.md's testing-discipline note warns about, just surfaced
through old data instead of new code. `error_rate`/`percentiles()` below
exclude unknown calls from their denominator and return `None` (rendered
"N/A", never "0.0%") when nothing is known at all.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from cli.config import DrifterConfig, load_config
from record.reader import read_session
from record.schema import ToolCall, ToolsList

_PERCENTILES = (0.5, 0.9, 0.99)


def _percentile(sorted_values: list[float], p: float) -> float:
    """Linear-interpolation percentile (matches numpy's default "linear"
    method) — chosen specifically because it stays well-defined for a
    single-element list (returns that element for any p), which a
    partway-through-the-trial corpus will routinely have.
    """
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (len(sorted_values) - 1) * p
    f, c = int(k), min(int(k) + 1, len(sorted_values) - 1)
    if f == c:
        return sorted_values[f]
    return sorted_values[f] + (sorted_values[c] - sorted_values[f]) * (k - f)


@dataclass
class ToolStats:
    server: str
    tool_name: str
    calls: int = 0
    errors: int = 0
    # Calls whose is_error is None — recorded before that field existed.
    # Excluded from error_rate's denominator, never coerced into "not an
    # error." See this module's docstring for why that distinction matters.
    error_unknown: int = 0
    retries: int = 0
    # Only calls with a known duration_ms ever land here — a call recorded
    # before that field existed contributes nothing, not a 0ms sample.
    durations_ms: list[float] = field(default_factory=list)

    @property
    def known_error_calls(self) -> int:
        return self.calls - self.error_unknown

    @property
    def error_rate(self) -> float | None:
        known = self.known_error_calls
        return (self.errors / known) if known else None

    @property
    def retry_rate(self) -> float:
        return self.retries / self.calls if self.calls else 0.0

    def percentiles(self) -> dict[float, float] | None:
        if not self.durations_ms:
            return None
        values = sorted(self.durations_ms)
        return {p: _percentile(values, p) for p in _PERCENTILES}


@dataclass
class CorpusStats:
    sessions: int = 0
    per_tool: dict[tuple[str, str], ToolStats] = field(default_factory=dict)
    # Every (server, tool) seen in a tools_served manifest, whether or not
    # it was ever called — the universe unused_tools() is computed against.
    known_tools: set[tuple[str, str]] = field(default_factory=set)

    def _tool(self, server: str, tool_name: str) -> ToolStats:
        key = (server, tool_name)
        if key not in self.per_tool:
            self.per_tool[key] = ToolStats(server=server, tool_name=tool_name)
        return self.per_tool[key]

    def unused_tools(self) -> set[tuple[str, str]]:
        return self.known_tools - set(self.per_tool.keys())

    @property
    def total_calls(self) -> int:
        return sum(t.calls for t in self.per_tool.values())

    @property
    def total_errors(self) -> int:
        return sum(t.errors for t in self.per_tool.values())

    @property
    def total_error_unknown(self) -> int:
        return sum(t.error_unknown for t in self.per_tool.values())

    @property
    def total_known_error_calls(self) -> int:
        return sum(t.known_error_calls for t in self.per_tool.values())

    @property
    def total_retries(self) -> int:
        return sum(t.retries for t in self.per_tool.values())


def collect_stats(runs_dir: Path, server_filter: str | None = None) -> CorpusStats:
    stats = CorpusStats()
    for jsonl_path in sorted(runs_dir.glob("*.jsonl")):
        stats.sessions += 1
        # Reset per session — a retry is "called again in this same
        # conversation," not across independent observe connections.
        last_call_args: dict[tuple[str, str], dict] = {}
        for record in read_session(jsonl_path):
            if isinstance(record, ToolsList):
                if server_filter is not None and record.server != server_filter:
                    continue
                for tool in record.tools_served:
                    stats.known_tools.add((record.server, tool.name))
            elif isinstance(record, ToolCall):
                if server_filter is not None and record.server != server_filter:
                    continue
                key = (record.server, record.tool_name)
                tool_stats = stats._tool(*key)
                tool_stats.calls += 1
                # is_error is None on any ToolCall recorded before this
                # field existed — must stay excluded from error_rate, not
                # fall through to "not True" and read as a known non-error.
                if record.is_error is None:
                    tool_stats.error_unknown += 1
                elif record.is_error:
                    tool_stats.errors += 1
                if record.duration_ms is not None:
                    tool_stats.durations_ms.append(record.duration_ms)
                if last_call_args.get(key) == record.arguments:
                    tool_stats.retries += 1
                last_call_args[key] = record.arguments
    return stats


def render_stats(stats: CorpusStats) -> str:
    lines: list[str] = []
    if stats.sessions == 0:
        lines.append("drifter stats — no sessions found in the corpus.")
        return "\n".join(lines) + "\n"

    lines.append(f"drifter stats — {stats.sessions} session(s), {stats.total_calls} tool call(s)")
    lines.append("")

    if not stats.per_tool:
        lines.append("No tool calls recorded yet.")
    else:
        header = f"{'TOOL':<32} {'CALLS':>6} {'ERR%':>7} {'RETRY%':>7} {'P50ms':>8} {'P90ms':>8} {'P99ms':>8}"
        lines.append(header)
        lines.append("-" * len(header))
        ordered = sorted(stats.per_tool.values(), key=lambda t: t.calls, reverse=True)
        any_unknown = False
        for tool_stats in ordered:
            label = f"{tool_stats.server}.{tool_stats.tool_name}"
            percentiles = tool_stats.percentiles()
            p50 = f"{percentiles[0.5]:.1f}" if percentiles else "-"
            p90 = f"{percentiles[0.9]:.1f}" if percentiles else "-"
            p99 = f"{percentiles[0.99]:.1f}" if percentiles else "-"

            err_display = "N/A" if tool_stats.error_rate is None else f"{tool_stats.error_rate * 100:.1f}%"
            if tool_stats.error_unknown:
                any_unknown = True
                err_display += "*"

            lines.append(
                f"{label:<32} {tool_stats.calls:>6} "
                f"{err_display:>7} {tool_stats.retry_rate * 100:>6.1f}% "
                f"{p50:>8} {p90:>8} {p99:>8}"
            )
        if any_unknown:
            lines.append(
                "* some calls recorded before error tracking existed (schema pre-v1.0.7); "
                "excluded from ERR%, not counted as non-errors"
            )

    unused = sorted(stats.unused_tools())
    lines.append("")
    if unused:
        lines.append("UNUSED TOOLS (in manifest, never called):")
        for server, tool_name in unused:
            lines.append(f"  {server}.{tool_name}")
    else:
        lines.append("UNUSED TOOLS: none")

    lines.append("")
    total = stats.total_calls
    known_error_calls = stats.total_known_error_calls
    err_summary = (
        f"{stats.total_errors} ({stats.total_errors / known_error_calls * 100:.1f}% of {known_error_calls} known)"
        if known_error_calls
        else "N/A (no calls with known error status)"
    )
    retry_pct = (stats.total_retries / total * 100) if total else 0.0
    lines.append(f"TOTALS  calls={total}  errors={err_summary}  retries={stats.total_retries} ({retry_pct:.1f}%)")
    if stats.total_error_unknown:
        lines.append(
            f"        {stats.total_error_unknown} call(s) recorded before error tracking existed "
            "(schema pre-v1.0.7) — excluded from error totals above, not counted as non-errors"
        )
    return "\n".join(lines) + "\n"


def resolve_runs_dir(config: DrifterConfig | None) -> Path:
    """Same DRIFTER_RUNS_DIR precedence as cli/observe.py's run_observe:
    env var wins over drifter.yaml's record.dir when set."""
    default = config.record.dir if config is not None else ".drifter/runs"
    return Path(os.environ.get("DRIFTER_RUNS_DIR", default))


def run_stats(
    config_path: Path | None = None,
    runs_dir: Path | None = None,
    server_name: str | None = None,
    output_stream: TextIO = sys.stdout,
) -> None:
    if runs_dir is None:
        config = load_config(config_path)
        runs_dir = resolve_runs_dir(config)
    stats = collect_stats(runs_dir, server_filter=server_name)
    output_stream.write(render_stats(stats))
