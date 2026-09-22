"""E2 subject: Claude Code against whatever MCP URL Drifter serves (http mode).

Same shape as the limitation-20 wrapper. Its stdout is the session's final
answer, which `answer_matches` reads.
"""
import json
import os
import subprocess
import sys
import tempfile

url = os.environ["DRIFTER_PROXY_URL"]
prompt = sys.argv[1]
cfg = {"mcpServers": {"econ": {"type": "http", "url": url}}}
with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
    json.dump(cfg, fh)
    cfg_path = fh.name
proc = subprocess.run(
    [
        "claude", "-p", prompt, "--mcp-config", cfg_path,
        "--allowedTools", "mcp__econ__econ_index_get_usage_by_country", "mcp__econ__econ_index_list_countries", "mcp__econ__econ_index_get_global_usage", "mcp__econ__econ_index_compare_regions", "mcp__econ__econ_index_get_dataset_overview",
        "--disallowedTools", "Read", "Glob", "Bash", "Grep", "Write", "Edit", "WebFetch", "WebSearch",
    ],
    capture_output=True, text=True, timeout=240,
)
sys.stdout.write(proc.stdout)
sys.stderr.write(proc.stderr)
sys.exit(proc.returncode)
