"""Error taxonomy and response envelopes shared by fastapi-mcp-neo MCP tools."""

from __future__ import annotations

from enum import Enum
from typing import Any


class GQLMCPErrors(str, Enum):
    NO_ROUTES = "no_routes"
    INVALID_QUERY = "invalid_query"
    QUERY_EXECUTION_ERROR = "query_execution_error"
    MUTATION_EXECUTION_ERROR = "mutation_execution_error"
    MUTATION_DISABLED = "mutation_disabled"
    DOMAIN_NOT_FOUND = "domain_not_found"
    INTERNAL_ERROR = "internal_error"


def create_success_response(data: Any, *, hint: str | None = None) -> dict[str, Any]:
    """Success envelope; ``hint`` nudges the agent towards the next step."""
    payload: dict[str, Any] = {"success": True, "data": data}
    if hint:
        payload["hint"] = hint
    return payload


def create_error_response(
    error: str, error_type: GQLMCPErrors | str, *, hint: str | None = None
) -> dict[str, Any]:
    """Error envelope; ``hint`` suggests how to recover."""
    payload: dict[str, Any] = {
        "success": False,
        "error": error,
        "error_type": error_type.value if isinstance(error_type, GQLMCPErrors) else error_type,
    }
    if hint:
        payload["hint"] = hint
    return payload
