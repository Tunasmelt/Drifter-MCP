"""Controlled MCP server for R4's stochastic-path trial (docs/PHASES.md R4).

Deliberately offers TWO equally valid routes to the same answer, so a real
agent's choice between them is a genuine behavioral variable, not an artifact
of there being only one sane path (as in orders/econ, where p=1.0 both times).

- list_widgets() -> the 7 widget names, shape only. An agent can answer the
  count by counting this list itself.
- count_widgets() -> the integer 7 directly.

Task: "How many widgets are currently in stock?" Both tools are read-only,
equally discoverable, and equally sufficient; nothing in either description
favors one over the other. Standalone script, spawned as a real subprocess,
data fixed and synthetic (matches tests/fixtures/orders_server.py's pattern).
"""

from mcp.server.mcpserver import MCPServer

WIDGETS = ["bolt", "washer", "nut", "screw", "rivet", "clip", "pin"]

server = MCPServer(name="variance-server")


@server.tool()
def list_widgets() -> list:
    """List every widget currently in stock, by name."""
    return WIDGETS


@server.tool()
def count_widgets() -> int:
    """Return the number of widgets currently in stock."""
    return len(WIDGETS)


if __name__ == "__main__":
    server.run(transport="stdio")
