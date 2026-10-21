"""Run the full demo server: ``uv run --extra mcp python -m examples.shop``."""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    port = int(os.environ.get("PORT", "8010"))
    print(
        "\n  fastapi-mcp-neo demo\n"
        "  ─────────────────────────────────────────────────────\n"
        f"  REST docs (swagger):   http://127.0.0.1:{port}/docs\n"
        f"  MCP streamable HTTP:   http://127.0.0.1:{port}/mcp/\n"
        f"  GraphiQL playground:   http://127.0.0.1:{port}/graphiql\n"
        f"  GraphQL HTTP:          POST http://127.0.0.1:{port}/graphql\n"
        "  ─────────────────────────────────────────────────────\n"
        "  auth: orders & writes need header x-token: demo-secret\n"
        "  (Ctrl-C to stop)\n",
    )
    uvicorn.run("examples.shop.server:app", host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
