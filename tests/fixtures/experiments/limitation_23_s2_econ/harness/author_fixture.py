"""Authors responses.yaml from a REAL econ_index_get_usage_by_country response for the exact recorded request,
and derives the answer oracle from that response."""
import glob, json, re, anyio, yaml
from pathlib import Path
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp_drifter.replay.authored_responses import load_authored_responses

ARGS = {"country_code": "JPN"}

async def main():
    async with streamable_http_client("https://econ-index.mcp.claude.com/mcp") as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool("econ_index_get_usage_by_country", ARGS)
    result = res.model_dump(mode="json", by_alias=True, exclude_none=True)
    doc = {"version": 1, "server": "econ", "responses": [{"tool_name": "econ_index_get_usage_by_country", "arguments": ARGS, "result": result}]}
    Path("responses.yaml").write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")
    loaded = load_authored_responses(Path("responses.yaml"), "econ", [Path(p) for p in glob.glob(".drifter/runs/*.jsonl")])
    text = json.dumps(result)
    m = re.search(r'anthropic_usage_index\?"?\s*[:=]\s*\?"?([0-9]+\.[0-9]+)', text)
    print("fixture entries:", len(loaded.by_key), "| is_error:", result.get("isError"), "| index:", m.group(1) if m else None)
    print(text[:500])
anyio.run(main)
