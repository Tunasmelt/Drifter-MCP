"""A stand-in for a person's browser, and a helper that logs in against the OAuth fixture.

Used by the F-44 tests, which need a stored login to exist before the thing under test (a
non-interactive connection) runs. `tests/auth/test_login_flow.py` keeps its own copy: it tests
the login itself and varies the browser's behavior, which these tests do not.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx2

from mcp_drifter.auth.login import run_login
from mcp_drifter.auth.storage import TokenStore
from mcp_drifter.cli.config import ServerConfig


class ApprovingBrowser:
    async def __call__(self, url: str) -> None:
        query = parse_qs(urlparse(url).query)
        assert query["redirect_uri"][0] and query["state"][0]
        async with httpx2.AsyncClient(follow_redirects=False) as client:
            response = await client.get(url)
            await client.get(response.headers["location"])


def oauth_server(handle, **auth) -> ServerConfig:
    return ServerConfig.model_validate({"name": "remote-crm", "url": handle.mcp_url, "auth": {"type": "oauth", **auth}})


def store_for(handle, base_dir) -> TokenStore:
    return TokenStore("remote-crm", handle.mcp_url, base_dir=base_dir, backend="file")


async def log_in(handle, base_dir, **auth) -> TokenStore:
    """A completed login, stored in `base_dir`, exactly as `drifter auth login` would leave it."""
    store = store_for(handle, base_dir)
    await run_login(
        oauth_server(handle, **auth),
        store,
        client_metadata_url=None,
        open_browser=ApprovingBrowser(),
        announce=lambda *_: None,
        timeout=20,
    )
    return store
