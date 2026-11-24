"""Drifter's own client identity for servers that support client-ID metadata documents (F-43).

DEC-030 (docs/CHANGELOG.md): the document is hosted on the project website, at ONE canonical origin
that must never change, because the `client_id` an authorization server sees IS that document's URL
and changing it makes Drifter an entirely different client everywhere. The origin (GitHub Pages or
Vercel) has not been chosen yet, so this is deliberately unset and the identity order falls through
to dynamic registration. Setting it is a one-line change once DEC-030 names the origin, together
with publishing the document (docs/PHASES.md 0.3.0 P3).

Do not guess a URL here: a wrong guess is a permanent, public client identity.
"""

from __future__ import annotations

CANONICAL_CLIENT_METADATA_URL: str | None = None
