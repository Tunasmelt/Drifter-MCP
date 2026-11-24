"""Upstream authorization: how Drifter authenticates TO the real MCP server (0.3.0,
docs/SPEC.md §5.2, F-41–F-47).

Only the Drifter <-> real server axis needs this. The agent always reaches `drifter observe`
over stdio, and every replay path is offline, so `replay/` must never import this package:
`tests/auth/test_replay_isolation.py` enforces it, directly and through module-level import
chains. Keep this `__init__` free of imports for the same reason.

Drifter is its own client. It never forwards an agent's token, and no token, client secret or
authorization code may reach `.drifter/`, a log line or an error message (F-47).
"""
