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
cfg = {"mcpServers": {"orders": {"type": "http", "url": url}}}
with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
    json.dump(cfg, fh)
    cfg_path = fh.name
proc = subprocess.run(
    [
        "claude", "-p", prompt, "--mcp-config", cfg_path,
        "--allowedTools", "mcp__orders__find_order", "mcp__orders__get_order",
        "--disallowedTools", "Read", "Glob", "Bash", "Grep", "Write", "Edit", "WebFetch", "WebSearch",
    ],
    capture_output=True, text=True, timeout=240,
)
sys.stdout.write(proc.stdout)
sys.stderr.write(proc.stderr)
sys.exit(proc.returncode)
