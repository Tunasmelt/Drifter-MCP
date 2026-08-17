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


@server.tool()
def fail(message: str = "boom") -> str:
    """Always raises. MCPServer turns this into CallToolResult(is_error=True)
    rather than a protocol-level JSON-RPC error (see mcp.server.mcpserver's
    _handle_call_tool) — used to exercise F-10's error-rate/is_error path.
    """
    raise RuntimeError(message)


if __name__ == "__main__":
    server.run(transport="stdio")
