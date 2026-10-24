"""The tools-mode demo server: REST + one MCP tool per route on one app.

Run with ``uv run --extra mcp python -m examples.tools_demo`` (or
``uv run --extra mcp uvicorn examples.tools_demo.server:app --port 8011``).

Endpoints:
- REST (OpenAPI docs):  http://127.0.0.1:8011/docs
- MCP streamable HTTP:  http://127.0.0.1:8011/mcp/   (tools mode: one tool
  per route — get_note / list_notes / create_note / health)
"""

from examples.tools_demo.app import create_app
from fastapi_mcp_neo import FastAPIMCP

app = create_app()

mcp = FastAPIMCP(
    app,
    name="tools-demo",
    # The whole point of this demo: per-route tools instead of the
    # graphql contract. Clients see typed signatures (body model included,
    # enum included), readOnly/destructive annotations per HTTP method,
    # and a `fields` argument for projection.
    mode="tools",
    passthrough_headers=["authorization"],
)
mcp.mount_to(app, "/mcp")
