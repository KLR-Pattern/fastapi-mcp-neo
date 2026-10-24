"""An agent's-eye walkthrough of the demo app over MCP — no client needed.

    uv run --extra mcp python -m examples.shop.mcp_walkthrough

Drives the MCP server with an in-memory client through every layer:
domains -> operations -> schema fragments -> composed queries -> mutation.
"""

from __future__ import annotations

import asyncio
import json

import httpx
from asgi_lifespan import LifespanManager
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from examples.shop.app import DEMO_TOKEN, create_app
from fastapi_mcp_neo import FastAPIMCP


def show(title: str, payload: object) -> None:
    print(f"\n== {title} ==")
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    print(rendered if len(rendered) < 1600 else rendered[:1600] + "\n  …")


async def main() -> None:
    app = create_app()
    mcp = FastAPIMCP(
        app,
        name="fastapi-mcp-neo demo",
        allow_mutation=True,
        # 13 routes < threshold, so force progressive to show the 4 layers;
        # graphql mode registers get_schema + graphql_query.
        mode="progressive",
        # The caller brings the demo token itself (single identity source):
        passthrough_headers=["x-token", "authorization"],
    )
    mcp.mount_to(app, "/mcp")

    def asgi_factory(**kwargs) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), **kwargs)

    transport = StreamableHttpTransport(
        url="http://demo.local/mcp/",
        headers={"x-token": DEMO_TOKEN},
        httpx_client_factory=asgi_factory,
    )

    async with LifespanManager(app):
        async with Client(transport) as client:

            async def tool(name: str, args: dict | None = None) -> dict:
                result = await client.call_tool(name, args or {})
                return json.loads(result.content[0].text)

            show(
                "L1  list_domains",
                (await tool("list_domains"))["data"]["domains"],
            )
            show(
                "L2  list_queries('shop:catalog')",
                (await tool("list_queries", {"domain": "shop:catalog"}))["data"]["queries"],
            )
            fragment = (await tool("get_query_schema", {"domain": "shop:orders"}))["data"]["sdl"]
            show("L3  get_query_schema('shop:orders')", fragment)
            result = await tool(
                "graphql_query",
                {
                    "query": (
                        "{ shop { catalog { list_products(in_stock: true)"
                        " { name price_cents } }"
                        " orders { list_orders { status quantity } } }"
                        " analytics { shop_stats { revenue_cents } } }"
                    )
                },
            )
            show("L4  graphql_query — one query, three domains composed", result["data"])
            result = await tool(
                "graphql_query",
                {"query": "{ shop { orders { get_order(order_id: 999) { id } } } }"},
            )
            show("error shape — 404 nulls only its field", result["data"])
            result = await tool(
                "graphql_mutation",
                {
                    "mutation": (
                        'mutation($p: OrderCreateInput!)'
                        " { shop { orders { create_order(payload: $p)"
                        " { id status quantity } } } }"
                    ),
                    "variables": {"p": {"product_id": 4, "quantity": 3}},
                },
            )
            show("graphql_mutation — create_order", result["data"])

    await mcp.handler.aclose()
    print("\nwalkthrough complete.")


if __name__ == "__main__":
    asyncio.run(main())
