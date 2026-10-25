"""Force a route's read/write classification, overriding the HTTP-verb
default.

The verb default is right for almost every route, but two real-world
shapes break it:

- POST-for-read APIs (payload bodies beat URL-encoded query strings —
  Elasticsearch-style ``POST /search``): mark those routes ``@mcp_query``
  or every search would sit in the Mutation root and carry
  ``destructiveHint``, asking clients to confirm plain reads.
- GET routes with side effects (``GET /export`` firing a job): mark them
  ``@mcp_mutation`` or agents auto-run them as readOnly.

The marker flips the classification at SCAN time, so every consumer
aligns: schema placement (Query/Mutation root), tools-mode annotations
(readOnlyHint/destructiveHint), and the ``allow_mutation`` gate. The
physical HTTP verb is untouched — a ``POST`` route stays a ``POST`` on
the wire.

Usage (either side of the route decorator works — both orders verified;
keep it ADJACENT, any in-between decorator that replaces the function
object would drop the marker):

    from fastapi_mcp_neo import mcp_mutation, mcp_query

    @app.post("/search")
    @mcp_query
    async def search(q: SearchQuery) -> Hits: ...

    @app.get("/admin/flush-cache")
    @mcp_mutation
    async def flush_cache() -> Ok: ...
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

_MARKER = "__mcp_force__"


def _force(kind: str) -> Callable[[Any], Any]:
    def decorate(fn: Any) -> Any:
        setattr(fn, _MARKER, kind)
        return fn

    decorate.__doc__ = f"Mark the endpoint's bridge classification as {kind!r}."
    return decorate


#: This route is a READ regardless of its HTTP verb.
mcp_query = _force("query")

#: This route is a WRITE regardless of its HTTP verb.
mcp_mutation = _force("mutation")


def read_force(endpoint: Any) -> str | None:
    """The forced classification on an endpoint, if any ("query"/"mutation")."""
    return getattr(endpoint, _MARKER, None)
