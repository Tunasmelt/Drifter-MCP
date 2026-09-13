"""HTTP test agent whose second call depends on the first response payload."""

from __future__ import annotations

import anyio
import os

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main() -> None:
    url = os.environ["DRIFTER_PROXY_URL"]
    async with streamable_http_client(url) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            listing = await session.call_tool("list_directory", {"path": "/project"})
            path = listing.content[0].text.strip()
            document = await session.call_tool("read_text_file", {"path": path})
            lines = [line for line in document.content[0].text.splitlines() if line.strip()]
    print(f"{max(0, len(lines) - 1)} data rows", flush=True)


if __name__ == "__main__":
    anyio.run(main)
