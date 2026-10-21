"""MCP tool registration: executor tools + the simple-mode discovery tool.

Tool surface mirrors the "GraphQL as the MCP contract" pattern: a small,
constant number of tools; agents compose freely against the schema.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from graphql import parse
from graphql.language import OperationDefinitionNode, OperationType
from mcp.types import ToolAnnotations

from fastapi_mcp_neo.invoker import filter_passthrough_headers
from fastapi_mcp_neo.mcp.errors import (
    GQLMCPErrors,
    create_error_response,
    create_success_response,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from fastapi_mcp_neo.handler import RouterGraphQLHandler

_HINT_AFTER_SCHEMA = (
    "Write a GraphQL query against this schema and run it with graphql_query."
)


# MCP spec tool annotations: agents use these hints to decide when a call
# is safe (e.g. auto-run read-only tools, confirm destructive ones).
READ_ONLY = ToolAnnotations(read_only_hint=True)
DESTRUCTIVE = ToolAnnotations(destructive_hint=True)


def register_executor_tools(
    mcp: FastMCP, handler: RouterGraphQLHandler, *, allow_mutation: bool
) -> None:
    """Register graphql_query (+ graphql_mutation) on a FastMCP."""

    @mcp.tool(annotations=READ_ONLY)
    async def graphql_query(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute a GraphQL query against the app's routes.

        Args:
            query: A GraphQL query document, e.g.
                ``"{ list_items(limit: 5) { id name } }"`` — field names
                are the endpoint function names.
            variables: Optional variables for the query document, e.g.
                ``{"id": 3}`` used as ``$id``.

        Each top-level field calls the matching FastAPI route in-process —
        authentication and dependencies apply exactly as over HTTP.

        Returns:
            dict with:
            - success: True and data: the GraphQL result (``{"data": ...}``
              plus ``errors`` when fields failed), or
            - success: False and error/error_type for invalid queries.
        """
        return await _execute(handler, query, variables, mutation=False)

    if allow_mutation:

        @mcp.tool(annotations=DESTRUCTIVE)
        async def graphql_mutation(
            mutation: str, variables: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            """Execute a GraphQL mutation against the app's write routes.

            Args:
                mutation: A GraphQL mutation document, e.g.
                    ``'mutation { create_items(payload: {name: "n"}) { id } }'``.
                variables: Optional variables for the mutation document.

            Returns:
                Same envelope as graphql_query.
            """
            return await _execute(handler, mutation, variables, mutation=True)


def register_simple_tools(
    mcp: FastMCP, handler: RouterGraphQLHandler, *, allow_mutation: bool
) -> None:
    """Register get_schema + executor tools (small apps, one-shot discovery)."""

    @mcp.tool(annotations=READ_ONLY)
    def get_schema() -> dict[str, Any]:
        """Get the complete GraphQL schema of this FastAPI app, in SDL format.

        This is the single discovery entry point. Every Query field maps to a
        GET route and every Mutation field (if present) to a write route:
        an endpoint ``async def get_item(item_id: int)`` on ``GET /items/{id}``
        yields ``get_item(item_id: Int!): ItemOut``. Read this before writing
        any query — the field signatures here are authoritative.

        Responses are nullable per field: a route error nulls only its own
        field, so several routes can be combined in one query safely.

        Returns:
            dict with:
            - success: True
            - data: {"sdl": "<GraphQL SDL string>"}
            - hint: how to proceed next
        """
        try:
            return create_success_response({"sdl": handler.get_sdl()}, hint=_HINT_AFTER_SCHEMA)
        except Exception as e:  # pragma: no cover - defensive
            return create_error_response(str(e), GQLMCPErrors.INTERNAL_ERROR)

    register_executor_tools(mcp, handler, allow_mutation=allow_mutation)


def _document_matches(document: str, *, mutation: bool) -> bool:
    """True if every operation in the document is of the requested kind."""
    try:
        ast = parse(document)
    except Exception:
        return True  # let graphql() produce the parse error
    operations = [
        d.operation for d in ast.definitions if isinstance(d, OperationDefinitionNode)
    ]
    if not operations:
        return True  # fragments only / anonymous — graphql() will complain
    expected = OperationType.MUTATION if mutation else OperationType.QUERY
    return all(op == expected for op in operations)


def _per_call_passthrough_headers(allowed: tuple[str, ...]) -> dict[str, Any]:
    """Whitelisted headers off the MCP caller's own HTTP request, if any.

    fastmcp's accessor never raises: with no live HTTP request (in-memory
    Client transport, background-task workers) it returns {}, so per-call
    identity is simply absent — protected routes answer 401 rather than the
    call crashing. ``include`` re-admits credential headers fastmcp strips by
    default (authorization, cookie); the whitelist filter applied afterwards
    is the actual security boundary.
    """
    if not allowed:
        return {}
    from fastmcp.server.dependencies import get_http_headers

    return filter_passthrough_headers(get_http_headers(include=set(allowed)), allowed)


async def _execute(
    handler: RouterGraphQLHandler,
    document: str,
    variables: dict[str, Any] | None,
    *,
    mutation: bool,
) -> dict[str, Any]:
    if not _document_matches(document, mutation=mutation):
        return create_error_response(
            f"graphql_query only accepts query documents; "
            f"{'queries' if mutation else 'mutations'} must go through "
            f"{'graphql_query' if mutation else 'graphql_mutation'}",
            GQLMCPErrors.MUTATION_DISABLED if not mutation
            else GQLMCPErrors.INVALID_QUERY,
            hint="Send the document to the matching tool.",
        )
    try:
        per_call = _per_call_passthrough_headers(handler.passthrough_headers)
        result = await handler.execute(document, variables=variables, headers=per_call)
    except Exception as e:  # pragma: no cover - defensive
        return create_error_response(str(e), GQLMCPErrors.INTERNAL_ERROR)

    errors = result.get("errors")
    if errors and "data" not in result:
        # Whole-document failure: parse/validation level.
        kind = (
            GQLMCPErrors.MUTATION_EXECUTION_ERROR
            if mutation
            else GQLMCPErrors.QUERY_EXECUTION_ERROR
        )
        return create_error_response(
            "; ".join(e.get("message", "") for e in errors),
            kind,
            hint="Call get_schema (or list_domains) and check the field names, "
            "argument types and operation type.",
        )
    # Partial or full success (field-level errors stay inside data envelope).
    return create_success_response(result)
