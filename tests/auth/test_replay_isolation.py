"""Import-inspection test: nothing in `replay/` can reach `auth/` (docs/SPEC.md §5.2
"Replay is unchanged"; docs/FEATURES.md F-47; docs/PHASES.md 0.3.0 P1).

Replay is offline: no authorization, no network, no token. The guarantee is locked in at
the layer it lives in -- imports -- the same way F-20 locks out live forwarding
(`tests/replay/test_replay_proxy.py`), because a behavioural test can pass by accident
(a fixture that never happens to exercise a newly-added auth path).

Two checks, because "does not import auth" has two honest readings:
  1. DIRECT: no file under `replay/` mentions `mcp_drifter.auth` anywhere, lazy imports
     included. This is the requirement as written.
  2. TRANSITIVE: nothing `replay/` imports AT MODULE LEVEL can pull in `auth/` either.
     This is the real requirement. `replay_proxy.py` imports `record.proxy`, which is
     exactly where F-44 will put the authenticated connection. If that module imported
     `auth/` at its top, replay would depend on auth through the back door while check 1
     stayed green. A lazy import inside the authenticated branch (or a separate module)
     passes; a top-level one fails here with the path that caused it.

The analysis is tested against synthetic source first, because a guard that was never
shown to catch anything proves nothing.
"""

from __future__ import annotations

import ast
from collections import deque
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
ROOT = "mcp_drifter"
FORBIDDEN = f"{ROOT}.auth"

Sources = dict[str, tuple[str, bool]]  # module name -> (source text, is_package)


def _is_auth(module: str) -> bool:
    # By dotted COMPONENT, not string prefix: `mcp_drifter.replay.authored_responses`
    # starts with "auth" and is not the auth package.
    return module == FORBIDDEN or module.startswith(FORBIDDEN + ".")


def _walk(node: ast.AST, include_nested: bool):
    """Nodes that run when the module is imported. Function bodies run later (on a call),
    so they are skipped unless `include_nested`; class bodies run at import time."""
    for child in ast.iter_child_nodes(node):
        if not include_nested and isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        yield child
        yield from _walk(child, include_nested)


def imported_candidates(module: str, is_package: bool, source: str, include_nested: bool) -> set[str]:
    """Every module name this source could be importing, relative imports resolved.
    `from pkg import name` yields both `pkg` and `pkg.name` (name may be a submodule)."""
    found: set[str] = set()
    package = module if is_package else module.rpartition(".")[0]
    for node in _walk(ast.parse(source), include_nested):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                base_parts = parts[: len(parts) - (node.level - 1)]
                base = ".".join(base_parts + ([node.module] if node.module else []))
            else:
                base = node.module or ""
            found.add(base)
            found.update(f"{base}.{alias.name}" for alias in node.names if alias.name != "*")
    return found


def _with_parents(name: str) -> list[str]:
    parts = name.split(".")
    return [".".join(parts[: i + 1]) for i in range(len(parts))]


def direct_auth_imports(sources: Sources, modules: list[str]) -> list[str]:
    """`module -> auth name` for every mention of auth/ in these modules' OWN source, lazy
    imports included. Does not follow imports into other modules."""
    hits: set[str] = set()
    for module in modules:
        text, is_package = sources[module]
        for candidate in imported_candidates(module, is_package, text, include_nested=True):
            hits.update(f"{module} -> {name}" for name in _with_parents(candidate) if _is_auth(name))
    return sorted(hits)


def find_auth_paths(sources: Sources, roots: list[str]) -> list[list[str]]:
    """Import chains `root -> ... -> mcp_drifter.auth[.x]` through MODULE-LEVEL imports,
    i.e. what actually executes when `root` is imported."""
    paths: list[list[str]] = []
    for root in roots:
        queue = deque([[root]])
        seen = {root}
        while queue:
            chain = queue.popleft()
            module = chain[-1]
            if module not in sources:
                continue
            text, is_package = sources[module]
            for candidate in sorted(imported_candidates(module, is_package, text, include_nested=False)):
                # Importing a.b.c also executes a and a.b.
                for name in _with_parents(candidate):
                    if _is_auth(name):
                        paths.append([*chain, name])
                    elif name not in seen and name in sources:
                        seen.add(name)
                        queue.append([*chain, name])
    unique = {tuple(p) for p in paths}
    return [list(p) for p in sorted(unique)]


def load_sources() -> Sources:
    sources: Sources = {}
    for path in sorted((SRC / ROOT).rglob("*.py")):
        relative = path.relative_to(SRC).with_suffix("")
        is_package = relative.name == "__init__"
        parts = relative.parts[:-1] if is_package else relative.parts
        sources[".".join(parts)] = (path.read_text(encoding="utf-8"), is_package)
    return sources


def _replay_modules(sources: Sources) -> list[str]:
    return [m for m in sources if m == f"{ROOT}.replay" or m.startswith(f"{ROOT}.replay.")]


def _render(paths: list[list[str]]) -> str:
    return "\n".join("  " + " -> ".join(p) for p in paths)


# --- the real tree --------------------------------------------------------------------


def test_no_replay_module_mentions_auth_anywhere_not_even_a_lazy_import():
    sources = load_sources()
    replay = _replay_modules(sources)
    assert len(replay) >= 8, replay  # the walk found replay/, not an empty directory
    hits = direct_auth_imports(sources, replay)
    assert hits == [], "replay/ must never import auth/ (docs/SPEC.md §5.2):\n  " + "\n  ".join(hits)


def test_no_replay_module_reaches_auth_through_module_level_imports():
    sources = load_sources()
    paths = find_auth_paths(sources, _replay_modules(sources))
    assert paths == [], (
        "replay/ reaches auth/ through a module-level import chain. Replay is offline and must "
        "not depend on auth. Import auth lazily inside the authenticated branch of "
        "`connect_to_server`, or put the authenticated connection in its own module that "
        f"replay/ does not import:\n{_render(paths)}"
    )


def test_the_graph_is_not_vacuous_replay_really_does_reach_record_proxy():
    """If this stopped holding, the transitive check above would be passing because it
    sees nothing. It is also the exact edge F-44 must not turn into an auth dependency."""
    sources = load_sources()
    replay_proxy = f"{ROOT}.replay.replay_proxy"
    assert replay_proxy in sources
    assert f"{ROOT}.record.proxy" in imported_candidates(replay_proxy, False, sources[replay_proxy][0], False)


# --- the detector, against synthetic code ---------------------------------------------

REPLAY_MODULE = f"{ROOT}.replay.example"


@pytest.mark.parametrize(
    "statement",
    [
        "import mcp_drifter.auth",
        "import mcp_drifter.auth.storage as storage",
        "from mcp_drifter.auth import TokenStorage",
        "from mcp_drifter.auth.storage import TokenStorage",
        "from mcp_drifter import auth",
        "from .. import auth",
        "from ..auth import TokenStorage",
        "from ..auth.storage import TokenStorage",
        "from mcp_drifter import record, auth",
    ],
)
def test_every_spelling_of_importing_auth_is_detected(statement):
    sources: Sources = {REPLAY_MODULE: (statement + "\n", False)}
    assert direct_auth_imports(sources, [REPLAY_MODULE]), statement
    assert find_auth_paths(sources, [REPLAY_MODULE]), statement


@pytest.mark.parametrize(
    "statement",
    [
        "from mcp_drifter.replay.authored_responses import AuthoredResponses",  # "auth" prefix, not auth/
        "from .authored_responses import AuthoredResponses",
        "from mcp_drifter import authored",
        "from mcp.client.auth import OAuthClientProvider",  # the SDK's auth, not Drifter's
        "import mcp_drifter.record.reader",
        "from .. import record",
        "import authlib",
    ],
)
def test_lookalikes_are_not_flagged(statement):
    sources: Sources = {REPLAY_MODULE: (statement + "\n", False)}
    assert direct_auth_imports(sources, [REPLAY_MODULE]) == [], statement
    assert find_auth_paths(sources, [REPLAY_MODULE]) == [], statement


def test_a_lazy_import_is_a_direct_mention_but_is_not_executed_when_the_module_is_imported():
    lazy = "def connect():\n    from mcp_drifter.auth import build\n    return build()\n"
    sources: Sources = {f"{ROOT}.record.proxy": (lazy, False)}
    assert direct_auth_imports(sources, [f"{ROOT}.record.proxy"])
    assert find_auth_paths(sources, [f"{ROOT}.record.proxy"]) == []


def test_replay_importing_a_module_whose_auth_import_is_lazy_is_allowed_but_eager_is_not():
    """The F-44 design rule, in miniature: `record.proxy` may use auth only lazily."""
    lazy = "def connect():\n    from mcp_drifter.auth import build\n"
    eager = "from mcp_drifter.auth import build\n"
    replay_side = "from mcp_drifter.record.proxy import Direction\n"
    ok: Sources = {REPLAY_MODULE: (replay_side, False), f"{ROOT}.record.proxy": (lazy, False)}
    bad: Sources = {REPLAY_MODULE: (replay_side, False), f"{ROOT}.record.proxy": (eager, False)}
    assert direct_auth_imports(ok, [REPLAY_MODULE]) == [] and find_auth_paths(ok, [REPLAY_MODULE]) == []
    assert direct_auth_imports(bad, [REPLAY_MODULE]) == [] and find_auth_paths(bad, [REPLAY_MODULE])


def test_a_class_body_import_runs_at_import_time_so_it_counts():
    source = "class Loader:\n    from mcp_drifter.auth import build\n"
    sources: Sources = {REPLAY_MODULE: (source, False)}
    assert find_auth_paths(sources, [REPLAY_MODULE])


def test_the_back_door_is_found_through_an_intermediate_module_with_its_path():
    sources: Sources = {
        REPLAY_MODULE: ("from mcp_drifter.record.proxy import Direction\n", False),
        f"{ROOT}.record.proxy": ("from mcp_drifter.auth.client import authed_http_client\n", False),
        f"{ROOT}.auth": ("", True),
        f"{ROOT}.auth.client": ("", False),
    }
    paths = find_auth_paths(sources, [REPLAY_MODULE])
    assert [REPLAY_MODULE, f"{ROOT}.record.proxy", f"{ROOT}.auth"] in paths
    assert [REPLAY_MODULE, f"{ROOT}.record.proxy", f"{ROOT}.auth.client"] in paths


def test_a_relative_import_inside_a_package_init_resolves_against_that_package():
    sources: Sources = {f"{ROOT}.replay": ("from .. import auth\n", True)}
    assert find_auth_paths(sources, [f"{ROOT}.replay"])
