"""A second minimal MCP server fixture, for Gate 4 pre-handoff dry-run
tests (tests/cli/gate4_dry_run/) that need tool descriptions containing
real `description_update` synonym-table words (mutate/description_update.py's
`_SYNONYMS`) -- not added to fake_server.py itself to avoid changing that
fixture's tool count/shape out from under the several existing tests
already built against it.

Standalone script, same reasoning as fake_server.py's own docstring: it's
spawned as a real subprocess, both directly and as the real server behind
the Drifter proxy.
"""

from mcp.server.mcpserver import MCPServer

server = MCPServer(name="fake-annotated-server")


@server.tool()
def get_status() -> dict:
    """Returns detailed information about server status."""
    return {"status": "ok"}


@server.tool()
def list_items() -> list:
    """Lists every item currently tracked by the server."""
    return []


if __name__ == "__main__":
    server.run(transport="stdio")
