"""Authors responses.yaml from a REAL convert_time response for the exact recorded request."""
import glob, json, anyio, yaml
from pathlib import Path
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp_drifter.replay.authored_responses import load_authored_responses

ARGS = {"source_timezone": "Asia/Tokyo", "time": "14:00", "target_timezone": "America/New_York"}

async def main():
    async with stdio_client(StdioServerParameters(command="uvx", args=["mcp-server-time"])) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool("convert_time", ARGS)
    result = res.model_dump(mode="json", by_alias=True, exclude_none=True)
    doc = {"version": 1, "server": "time", "responses": [{"tool_name": "convert_time", "arguments": ARGS, "result": result}]}
    Path("responses.yaml").write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    loaded = load_authored_responses(Path("responses.yaml"), "time", [Path(p) for p in glob.glob(".drifter/runs/*.jsonl")])
    print("fixture entries:", len(loaded.by_key)); print(json.dumps(result)[:300])
anyio.run(main)
