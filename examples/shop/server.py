"""The all-mounted demo server: REST + MCP + GraphiQL + GraphQL on one app.

Run with ``uv run --extra mcp python -m examples.shop`` (or
``uv run --extra mcp uvicorn examples.shop.server:app --port 8010``).

Endpoints:
- REST (OpenAPI docs):  http://127.0.0.1:8010/docs
- MCP streamable HTTP:  http://127.0.0.1:8010/mcp/
- GraphiQL playground:  http://127.0.0.1:8010/graphiql
- GraphQL HTTP:         POST http://127.0.0.1:8010/graphql
"""

from examples.shop.app import create_app
from fastapi_mcp_neo import FastAPIMCP

app = create_app()

mcp = FastAPIMCP(
    app,
    name="fastapi-mcp-neo demo",
    allow_mutation=True,
    # The demo app stays under the 25-route threshold, so "auto" would pick
    # simple mode (get_schema + graphql_query). Force progressive to
    # expose the tag-based disclosure tools:
    # list_domains -> list_queries/list_mutations -> get_query_schema -> graphql_query
    mode="progressive",
    # Callers bring the demo token themselves (single identity source):
    passthrough_headers=["x-token", "authorization"],
)
mcp.mount_to(app, "/mcp")
mcp.handler.mount_graphql(app)
