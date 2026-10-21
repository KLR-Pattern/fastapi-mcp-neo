"""Run the demo: ``uv run python -m app`` (reads .env, serves on :8020).

Optional tracing: set ``OTEL_OTLP_ENDPOINT=http://localhost:4317`` (with
``uv sync --group otel``) to export spans to Jaeger — see
app/observability.py.
"""

from __future__ import annotations

import uvicorn

from app.config import BASE_URL, mcp_oauth_configured, oauth_configured
from app.observability import install_otel


def main() -> None:
    host = BASE_URL.split("//", 1)[-1]
    port = int(host.split(":")[1]) if ":" in host else 8020
    otel_service = install_otel()
    print(
        "\n  fastapi-mcp-neo demo\n"
        "  ─────────────────────────────────────────────────────\n"
        f"  Home / login:           http://localhost:{port}/auth/\n"
        f"  REST docs:              http://localhost:{port}/docs\n"
        f"  GraphiQL:               http://localhost:{port}/graphiql\n"
        f"  MCP (streamable HTTP):  http://localhost:{port}/mcp\n"
        "  ─────────────────────────────────────────────────────\n"
        f"  GitHub OAuth configured:  {oauth_configured()}\n"
        f"  MCP OAuth login enabled:  {mcp_oauth_configured()}\n"
        f"  Tracing:                 {otel_service or 'off (set OTEL_OTLP_ENDPOINT)'}\n"
        "  (Ctrl-C to stop)\n",
    )
    uvicorn.run("app.main:app", host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
