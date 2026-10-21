"""depth_guard at the handler choke point: enforcement end-to-end.

Every test here drives RouterGraphQLHandler.execute — the guard's contract
with real documents (nested selections, fragment-hidden depth, cycles) and
the introspection exemption against a live schema.
"""

import pytest
from fastapi import FastAPI
from graphql.utilities import get_introspection_query
from pydantic import BaseModel

from fastapi_mcp_neo.handler import RouterGraphQLHandler


class Node(BaseModel):
    value: int
    children: list["Node"] = []


Node.model_rebuild()


def node_app() -> FastAPI:
    app = FastAPI()

    @app.get("/node/{node_id}", response_model=Node, tags=["tree"])
    async def get_node(node_id: int) -> Node:
        return Node(value=node_id)

    return app


def nested_query(levels: int) -> str:
    """``{ tree { get_node(node_id: 1) { children {…} value } } }`` — depth = levels + 3."""
    inner = "value"
    for _ in range(levels):
        inner = f"children {{ {inner} }}"
    return f"{{ tree {{ get_node(node_id: 1) {{ {inner} }} }} }}"


def fragment_query(body_levels: int) -> str:
    """Shallow on paper (depth 3); the fragment carries the real nesting."""
    body = "value"
    for _ in range(body_levels):
        body = f"children {{ {body} }}"
    return (
        "query { tree { get_node(node_id: 1) { ...Deep } } } "
        f"fragment Deep on Node {{ {body} }}"
    )


class TestHandlerIntegration:
    async def test_deep_query_rejected_before_execution(self, make_handler):
        handler = make_handler(node_app())  # default max_depth=10
        result = await handler.execute(nested_query(20))  # depth 23
        assert "data" not in result
        assert "exceeds max_depth=10" in result["errors"][0]["message"]

    async def test_reasonable_depth_passes(self, make_handler):
        handler = make_handler(node_app())  # depth 8 < 10
        result = await handler.execute(nested_query(5))
        assert result["data"]["tree"]["get_node"]["children"] == []

    async def test_fragment_hidden_depth_rejected(self, make_handler):
        handler = make_handler(node_app())
        result = await handler.execute(fragment_query(12))  # real depth 15 > 10
        assert "data" not in result
        assert "exceeds max_depth=10" in result["errors"][0]["message"]

    async def test_cyclic_fragments_rejected(self, make_handler):
        handler = make_handler(node_app())
        query = "query { tree { get_node(node_id: 1) { ...F } } } " \
                "fragment F on Node { children { ...F } }"
        result = await handler.execute(query)
        assert "cyclic fragment" in result["errors"][0]["message"]

    async def test_max_depth_none_disables_guard(self, make_handler):
        handler = make_handler(node_app(), max_depth=None)
        result = await handler.execute(nested_query(20))
        assert result["data"]["tree"]["get_node"]["children"] == []

    async def test_custom_max_depth(self, make_handler):
        handler = make_handler(node_app(), max_depth=3)
        result = await handler.execute(nested_query(5))  # depth 8 > 3
        assert "exceeds max_depth=3" in result["errors"][0]["message"]

    def test_invalid_max_depth_rejected(self):
        with pytest.raises(ValueError, match="max_depth"):
            RouterGraphQLHandler(node_app(), max_depth=0)

    async def test_graphiql_schema_fetch_against_handler(self, make_handler):
        handler = make_handler(node_app())  # default max_depth=10
        result = await handler.execute(get_introspection_query())
        assert "errors" not in result, result.get("errors")
        # The shallow introspection shape resolves through the same pipeline
        # (merged from the former test_introspection_works).
        shallow = await handler.execute("{ __schema { queryType { name } } }")
        assert shallow == {"data": {"__schema": {"queryType": {"name": "Query"}}}}

    async def test_syntax_error_with_guard_disabled(self, make_handler):
        """max_depth=None takes the unguarded parse path — a malformed
        document still surfaces as a clean error envelope, never a crash."""
        handler = make_handler(node_app(), max_depth=None)
        result = await handler.execute("{ a {")
        assert "data" not in result
        assert "Syntax Error" in result["errors"][0]["message"]
