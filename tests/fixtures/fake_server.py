"""Minimal MCP server fixture for record/'s integration tests.

Standalone script (not a Drifter module) so it can be spawned as a real
subprocess — both directly, by tests, and indirectly, as the "real server"
behind the Drifter proxy.
"""

from mcp.server.mcpserver import MCPServer

server = MCPServer(name="fake-server")


@server.tool()
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@server.tool()
def echo(payload: dict) -> dict:
    """Echo the input payload back unchanged.

    Used by the F-04 redaction test: a tool that reflects its arguments
    back in its result exercises redaction on both the request side (the
    recorded arguments) and the response side (the recorded result) of the
    write path in one call.
    """
    return payload


if __name__ == "__main__":
    server.run(transport="stdio")
