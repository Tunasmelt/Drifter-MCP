"""Minimal MCP server fixture for F-01's integration test.

Standalone script (not a Drifter module) so it can be spawned as a real
subprocess — both directly, by the test, and indirectly, as the "real
server" behind the Drifter proxy. Responds to `tools/list` and one
`tools/call` (`add`), nothing else.
"""

from mcp.server.mcpserver import MCPServer

server = MCPServer(name="fake-server")


@server.tool()
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


if __name__ == "__main__":
    server.run(transport="stdio")
