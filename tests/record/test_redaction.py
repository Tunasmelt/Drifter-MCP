"""Integration test for secret redaction (F-04).

Test-enforced, not documentation-enforced (docs/FEATURES.md): this test is
written and run red — confirming the planted secrets currently leak —
before record/redact.py exists. Once the redaction layer is wired into
record/writer.py, it must go green with zero code changes to this file.

"Done when" per docs/FEATURES.md: a fixture containing planted fake secrets
produces zero leaked values in either output file (the parsed JSONL and the
raw frame mirror) — the raw mirror is explicitly in scope, not exempt as
"just a backup copy" (SECURITY.md).
"""

import sys
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

FIXTURE_SERVER = str(Path(__file__).parent.parent / "fixtures" / "fake_server.py")
DRIFTER_PROXY_COMMAND = [sys.executable, "-m", "record", sys.executable, FIXTURE_SERVER]

# Realistic-shaped planted fakes — not real credentials. The JWT is the
# canonical public example from jwt.io.
PLANTED_OPENAI_KEY = "sk-" + "abcd1234EFGH5678ijkl9012MNOP3456qrst7890UVWX"
PLANTED_BEARER_TOKEN = "Bearer xT9fL2mQ8vC4nR7pW1sD6hK3jY5bE0gA"
PLANTED_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0."
    "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
)
PLANTED_SECRETS = [PLANTED_OPENAI_KEY, PLANTED_BEARER_TOKEN, PLANTED_JWT]


def _proxied_params(runs_dir: Path, raw_dir: Path) -> StdioServerParameters:
    return StdioServerParameters(
        command=DRIFTER_PROXY_COMMAND[0],
        args=DRIFTER_PROXY_COMMAND[1:],
        env={"DRIFTER_RUNS_DIR": str(runs_dir), "DRIFTER_RAW_DIR": str(raw_dir)},
    )


@pytest.mark.anyio
async def test_planted_secrets_never_reach_disk(tmp_path):
    runs_dir, raw_dir = tmp_path / "runs", tmp_path / "raw"
    proxied_params = _proxied_params(runs_dir, raw_dir)

    async with stdio_client(proxied_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            await session.call_tool(
                "echo",
                {
                    "payload": {
                        "api_key": PLANTED_OPENAI_KEY,
                        "authorization": PLANTED_BEARER_TOKEN,
                        "session_token": PLANTED_JWT,
                    }
                },
            )

    jsonl_files = list(runs_dir.glob("*.jsonl"))
    raw_files = list(raw_dir.glob("*.frames"))
    assert len(jsonl_files) == 1
    assert len(raw_files) == 1

    jsonl_bytes = jsonl_files[0].read_bytes()
    raw_bytes = raw_files[0].read_bytes()

    for secret in PLANTED_SECRETS:
        secret_bytes = secret.encode("utf-8")
        assert secret_bytes not in jsonl_bytes, f"leaked in JSONL: {secret!r}"
        assert secret_bytes not in raw_bytes, f"leaked in raw mirror: {secret!r}"


@pytest.fixture
def anyio_backend():
    return "asyncio"
