"""Authors responses.yaml for the controlled dogfood workspace.

Every entry is keyed on an EXACT request already present in the recorded
corpus (the loader rejects anything else). Response bodies are authored from
the controlled files in this workspace -- known test data, never inferred
from recorded payloads, which are shape-only and hold none.

Listing text mirrors @modelcontextprotocol/server-filesystem's own format
("[DIR] name" / "[FILE] name") one level at a time, so nothing downstream is
disclosed early -- the defect that disqualified R0 (docs/SPEC.md §15
limitation 19).

Also adds two authored tasks to drifter.yaml, `fx-off` and `fx-on`, with the
same prompt and the same answer oracle, so each A/B arm gets a fresh
experiment directory and an identical TASK check.
"""
from __future__ import annotations

import glob
import json
from pathlib import Path

import yaml

from mcp_drifter.replay.authored_responses import load_authored_responses

ROOT = Path(__file__).resolve().parent
SERVER = "filesystem"
PROMPT = (
    "Using ONLY the filesystem MCP tools (do not use your own Read/Glob/Bash tools), "
    "list the files in the allowed directory and then read readings.csv. "
    "Report how many data rows it has."
)


def _text(body: str) -> dict:
    return {"content": [{"type": "text", "text": body}], "isError": False}


def _listing(directory: Path) -> str:
    entries = sorted(directory.iterdir(), key=lambda p: p.name.lower())
    return "\n".join(f"[{'DIR' if e.is_dir() else 'FILE'}] {e.name}" for e in entries)


def _response_for(tool: str, args: dict, allowed_root: str) -> dict | None:
    if tool == "list_allowed_directories":
        return _text(f"Allowed directories:\n{allowed_root}")
    path = args.get("path")
    if not isinstance(path, str):
        return None
    target = Path(path)
    if tool == "list_directory" and target.is_dir():
        return _text(_listing(target))
    if tool == "read_text_file" and target.is_file():
        lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
        head, tail = args.get("head"), args.get("tail")
        if isinstance(head, int):
            lines = lines[:head]
        elif isinstance(tail, int):
            lines = lines[-tail:]
        return _text("".join(lines))
    # A request the controlled workspace cannot answer (an invented path, an
    # unsupported tool) gets no authored entry and falls back to ordinary
    # shape-only replay -- never a guessed body.
    return None


def main() -> None:
    corpus = [Path(p) for p in sorted(glob.glob(str(ROOT / ".drifter" / "runs" / "*.jsonl")))]
    if not corpus:
        raise SystemExit("no recorded sessions found -- run the recording loop first")

    seen: dict[str, tuple[str, dict]] = {}
    for session in corpus:
        for line in session.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if record.get("record_type") != "tool_call" or record.get("server") != SERVER:
                continue
            if record.get("result_provenance", "real") != "real":
                continue
            key = json.dumps([record["tool_name"], record["arguments"]], sort_keys=True)
            seen.setdefault(key, (record["tool_name"], record["arguments"]))

    allowed_root = str(ROOT)
    responses, skipped = [], []
    for tool, args in seen.values():
        body = _response_for(tool, args, allowed_root)
        if body is None:
            skipped.append((tool, args))
        else:
            responses.append({"tool_name": tool, "arguments": args, "result": body})

    out = ROOT / "responses.yaml"
    out.write_text(
        yaml.safe_dump({"version": 1, "server": SERVER, "responses": responses}, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    loaded = load_authored_responses(out, SERVER, corpus)
    print(f"distinct recorded requests: {len(seen)}")
    print(f"authored and corpus-validated: {len(loaded.by_key)}")
    for tool, args in skipped:
        print(f"  no authored body (falls back to shape replay): {tool} {json.dumps(args)[:90]}")

    config_path = ROOT / "drifter.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    oracle = {
        "calls": ["read_text_file"],
        "answer_matches": r"(?i)\b(?:2|two)\s+data\s+rows?\b",
    }
    config["tasks"] = [
        {"id": "fx-off", "prompt": PROMPT, "assert": oracle},
        {"id": "fx-on", "prompt": PROMPT, "assert": oracle},
    ]
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    print("drifter.yaml: tasks fx-off / fx-on added with the tightened answer oracle")


if __name__ == "__main__":
    main()
