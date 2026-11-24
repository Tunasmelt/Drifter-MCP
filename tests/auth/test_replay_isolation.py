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

Dynamic imports are covered too, because `importlib.import_module("mcp_drifter.auth")` is
the obvious way around an import-statement check (found in the P1 audit):
  - a literal name is treated exactly like an import statement;
  - in `replay/`, a dynamic import whose name is NOT a literal is itself a failure (it cannot
    be shown to avoid auth/), and so is any string that is exactly an auth module name;
  - `replay/` currently has no dynamic imports at all.
Known limit: this is a static check. `getattr`, `eval`/`exec` or building a module name from
pieces and importing it through something other than `importlib`/`__import__` is invisible to
it. Nothing in `replay/` does any of that, and review would notice if it started to.

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


DYNAMIC_IMPORTERS = frozenset({"import_module", "__import__", "run_module"})


def _dynamic_names(tree: ast.AST) -> frozenset[str]:
    """Names that call a dynamic importer in this source, including `from importlib import
    import_module as im` style aliases."""
    names = set(DYNAMIC_IMPORTERS)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in ("importlib", "runpy", "builtins"):
            names.update(alias.asname for alias in node.names if alias.name in DYNAMIC_IMPORTERS and alias.asname)
    return frozenset(names)


def _is_dynamic_import(node: ast.AST, names: frozenset[str]) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (isinstance(func, ast.Name) and func.id in names) or (isinstance(func, ast.Attribute) and func.attr in names)


def _literal(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def non_literal_dynamic_imports(source: str) -> list[int]:
    """Line numbers of dynamic imports whose module name is not a string literal."""
    tree = ast.parse(source)
    names = _dynamic_names(tree)
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if _is_dynamic_import(node, names) and not (node.args and _literal(node.args[0]) is not None)
    )


def imported_candidates(
    module: str, is_package: bool, source: str, include_nested: bool, include_strings: bool = False
) -> set[str]:
    """Every module name this source could be importing, relative imports resolved.
    `from pkg import name` yields both `pkg` and `pkg.name` (name may be a submodule). A dynamic
    import with a literal name counts like an import statement; with `include_strings`, so does
    any string that is exactly a module name (`MODULE = "mcp_drifter.auth.client"`)."""
    found: set[str] = set()
    package = module if is_package else module.rpartition(".")[0]
    tree = ast.parse(source)
    dynamic = _dynamic_names(tree)
    for node in _walk(tree, include_nested):
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
        elif _is_dynamic_import(node, dynamic):
            name = _literal(node.args[0]) if node.args else None
            if name:
                found.add(name)
        elif include_strings:
            value = _literal(node)
            if value and _is_auth(value):
                found.add(value)
    return found


def _with_parents(name: str) -> list[str]:
    parts = name.split(".")
    return [".".join(parts[: i + 1]) for i in range(len(parts))]


def direct_auth_imports(sources: Sources, modules: list[str]) -> list[str]:
    """`module -> auth name` for every mention of auth/ in these modules' OWN source: lazy
    imports, literal dynamic imports and exact module-name strings included, and any dynamic
    import whose name cannot be read. Does not follow imports into other modules."""
    hits: set[str] = set()
    for module in modules:
        text, is_package = sources[module]
        for candidate in imported_candidates(module, is_package, text, include_nested=True, include_strings=True):
            hits.update(f"{module} -> {name}" for name in _with_parents(candidate) if _is_auth(name))
        hits.update(
            f"{module} -> <dynamic import with a non-literal name, line {line}>"
            for line in non_literal_dynamic_imports(text)
        )
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


@pytest.mark.parametrize(
    "source",
    [
        'import importlib\nimportlib.import_module("mcp_drifter.auth")\n',
        'import importlib\nimportlib.import_module("mcp_drifter.auth.client")\n',
        '__import__("mcp_drifter.auth.client")\n',
        'from importlib import import_module as load\nload("mcp_drifter.auth")\n',
        'import runpy\nrunpy.run_module("mcp_drifter.auth")\n',
    ],
)
def test_a_dynamic_import_with_a_literal_auth_name_is_caught_by_both_checks(source):
    sources: Sources = {REPLAY_MODULE: (source, False)}
    assert direct_auth_imports(sources, [REPLAY_MODULE]), source
    assert find_auth_paths(sources, [REPLAY_MODULE]), source


@pytest.mark.parametrize(
    "source",
    [
        'import importlib\nname = "mcp_drifter." + "auth"\nimportlib.import_module(name)\n',
        "import importlib\nimportlib.import_module(some_config_value)\n",
        "__import__(chosen)\n",
        "from importlib import import_module as load\nload(f'mcp_drifter.{x}')\n",
        "import importlib\nimportlib.import_module()\n",
    ],
)
def test_a_dynamic_import_whose_name_cannot_be_read_fails_the_direct_check(source):
    """It cannot be shown to avoid auth/, so replay/ may not contain one at all."""
    sources: Sources = {REPLAY_MODULE: (source, False)}
    hits = direct_auth_imports(sources, [REPLAY_MODULE])
    assert hits and "non-literal" in hits[0], hits


def test_an_exact_auth_module_name_as_a_string_fails_the_direct_check_but_prose_does_not():
    flagged: Sources = {REPLAY_MODULE: ('MODULE = "mcp_drifter.auth.client"\n', False)}
    assert direct_auth_imports(flagged, [REPLAY_MODULE])
    prose: Sources = {
        REPLAY_MODULE: ('"""Replay never touches mcp_drifter.auth: see SPEC 5.2."""\nNOTE = "run `drifter auth login`"\n', False)
    }
    assert direct_auth_imports(prose, [REPLAY_MODULE]) == []


def test_dynamic_imports_of_unrelated_modules_with_literal_names_are_not_flagged_transitively():
    sources: Sources = {REPLAY_MODULE: ('import importlib\nimportlib.import_module("json")\n', False)}
    assert find_auth_paths(sources, [REPLAY_MODULE]) == []
    assert direct_auth_imports(sources, [REPLAY_MODULE]) == []


def test_the_real_replay_package_contains_no_dynamic_imports_at_all():
    sources = load_sources()
    offenders = {m: non_literal_dynamic_imports(sources[m][0]) for m in _replay_modules(sources)}
    assert {m: lines for m, lines in offenders.items() if lines} == {}
    for module in _replay_modules(sources):
        tree = ast.parse(sources[module][0])
        assert not [n for n in ast.walk(tree) if _is_dynamic_import(n, _dynamic_names(tree))], module


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
