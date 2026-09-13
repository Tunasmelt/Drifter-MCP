"""Controlled MCP server for the release gate (docs/PHASES.md R0.5, blocker 1).

The filesystem server gives `parameter_rename` nothing to rename: no property
has an underscore. This server is built so the operator has exactly one real
target on the task's path, and the task cannot pass under content-empty replay:

- `find_order(customer)` -- no underscore, never renamed. Returns an order id
  the caller cannot guess.
- `get_order(order_id)` -- `order_id` is required and snake_case, so
  `parameter_rename` renames it to `orderId`. Needs the id `find_order`
  returned, and returns the total the task asks for.

Standalone script (not a Drifter module), spawned as a real subprocess, like
tests/fixtures/fake_server.py. Data is fixed and synthetic.
"""

from mcp.server.mcpserver import MCPServer

ORDERS = {"acme": ("ord-7f3a91", 1240)}
TOTALS = {order_id: total for order_id, total in ORDERS.values()}

server = MCPServer(name="orders-server")


@server.tool()
def find_order(customer: str) -> str:
    """Find the most recent order for a customer. Returns its order id."""
    found = ORDERS.get(customer.lower())
    if found is None:
        raise ValueError(f"no order for customer {customer!r}")
    return found[0]


@server.tool()
def get_order(order_id: str) -> str:
    """Get an order's total, in whole dollars, by its order id."""
    if order_id not in TOTALS:
        raise ValueError(f"unknown order id {order_id!r}")
    return f"order {order_id} total: {TOTALS[order_id]} dollars"


if __name__ == "__main__":
    server.run(transport="stdio")
