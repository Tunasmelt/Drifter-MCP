"""Minimal MCP server fixture for cli/doctor.py's F-26 classification
tests — tool names deliberately chosen to resolve cleanly via
policy/classify.py's tier-2 name heuristic (get_/delete_ prefixes), unlike
fake_server.py's add/echo/fail (none of which match any known prefix).
"""

from mcp.server.mcpserver import MCPServer

server = MCPServer(name="classifiable-server")


@server.tool()
def get_status() -> str:
    """Read-only by name heuristic."""
    return "ok"


@server.tool()
def delete_record(record_id: str) -> str:
    """Destructive by name heuristic."""
    return f"deleted {record_id}"


if __name__ == "__main__":
    server.run(transport="stdio")
