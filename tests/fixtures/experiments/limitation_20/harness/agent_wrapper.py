"""Launches Claude Code against whatever MCP URL Drifter is serving.

`mode: http`: Drifter stands up the replay proxy on loopback and puts its URL
in DRIFTER_PROXY_URL. Claude Code spawns MCP servers from its own
--mcp-config, so this wrapper builds a config pointing at that URL and runs
`claude -p`. Its stdout becomes the session's final answer (<session>.stdout.txt),
which is what the `answer_matches` oracle reads.
"""
import json
import os
import subprocess
import sys
import tempfile

url = os.environ["DRIFTER_PROXY_URL"]
prompt = sys.argv[1] if len(sys.argv) > 1 else "Do the task."
cfg = {"mcpServers": {"filesystem": {"type": "http", "url": url}}}
with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
    json.dump(cfg, fh)
    cfg_path = fh.name
proc = subprocess.run(
    [
        "claude", "-p", prompt, "--mcp-config", cfg_path,
        "--allowedTools",
        "mcp__filesystem__list_directory",
        "mcp__filesystem__read_text_file",
        "mcp__filesystem__list_allowed_directories",
        "--disallowedTools", "Read", "Glob", "Bash", "Grep",
    ],
    capture_output=True, text=True, timeout=240,
)
sys.stdout.write(proc.stdout)
sys.stderr.write(proc.stderr)
sys.exit(proc.returncode)
