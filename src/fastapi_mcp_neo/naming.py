"""GraphQL field names come from the FastAPI endpoint function's name.

The function that implements a route carries the developer's own vocabulary —
``async def list_active_users(...)`` is a better field name than anything
reconstructed from ``/users?active=true``. Python identifiers are always legal
GraphQL names, so no sanitization is needed; the path/query/body parameters
still become the field's arguments (see ``scanner``).

Uniqueness is scoped to one DOMAIN GROUP (one GraphQL object type), matching
GraphQL's own rule — ``iam.get_user`` and ``admin.get_user`` coexist fine;
two same-named routes inside the same tag group fail fast with
``DuplicateFieldError`` (raised by the schema builder while assembling the
group).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi.routing import APIRoute


class DuplicateFieldError(ValueError):
    """Two routes map onto the same field name within one domain group."""


def field_name_for(route: APIRoute) -> str:
    """Derive the GraphQL field name for one route: the endpoint function name."""
    name = route.name
    if not name or not name.isidentifier():  # pragma: no cover - defensive
        raise ValueError(
            f"Route {route.path} has no usable endpoint function name ({name!r})"
        )
    return name
