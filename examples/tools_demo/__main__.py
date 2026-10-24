"""Run the tools demo server: ``uv run --extra mcp python -m examples.tools_demo``."""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    port = int(os.environ.get("PORT", "8011"))
    print(f"\n  MCP tools:   http://127.0.0.1:{port}/mcp/")
    print(f"  REST docs:   http://127.0.0.1:{port}/docs\n")
    uvicorn.run("examples.tools_demo.server:app", host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
