"""Drifter — proxy-based regression-testing harness for MCP tool interfaces.

Every module lives under this single top-level package deliberately. An
earlier layout installed `record`, `replay`, `mutate`, `evaluate`, `mine`,
`policy` and `cli` as SEPARATE top-level packages in site-packages — six of
those seven names are real, existing PyPI distributions (`evaluate` is
HuggingFace's, installed in a large fraction of ML environments), so
installing Drifter alongside any of them meant whichever landed second
silently shadowed the other. A wrong-import, not an error. Caught during
the pre-publish audit, before the first real release rather than after.

`mcp_drifter` rather than `drifter`: the bare name is already taken on PyPI
(a VirtualBox control tool), and nesting under a name someone else can
install would recreate the same collision class this layout exists to fix.
Matching the distribution name exactly means nothing else can claim it.

The module dependency order CLAUDE.md fixes is unchanged by the move:
`record/` -> `replay/` -> `mutate/` -> `evaluate/` -> `mine/` -> `policy/`
-> `cli/`, now as `mcp_drifter.record` and so on.
"""
