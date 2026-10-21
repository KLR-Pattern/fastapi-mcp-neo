"""Per-caller JWT passthrough demo — each MCP client acts as its own user.

    uv run --extra mcp python -m examples.shop.jwt_passthrough

Scenario: an orders API where ``/me`` and ``/orders`` identify the caller by
their Bearer JWT. The MCP server is configured with
``passthrough_headers=["authorization"]`` (also the default), so each MCP
client's own token — the single identity source — reaches the routes:

1. alice's client  -> queries run as alice (caller identity travels)
2. bob's client    -> same query, bob's data
3. bare client     -> 401: single identity source, nothing to fall back on
4. smuggled header -> x-internal-token never reaches a route (whitelist)
5. in-memory client-> no HTTP request context -> no identity -> 401

Tokens are plain strings ("jwt-alice") to keep the demo dependency-free; a
real deployment swaps the ``verify_token`` body for jwt/jose verification.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from pydantic import BaseModel

from fastapi_mcp_neo import FastAPIMCP

PORT = 8011
USERS = {
    "jwt-alice": "alice",
    "jwt-bob": "bob",
}

security = HTTPBearer(auto_error=True)


def current_user(creds: HTTPAuthorizationCredentials = Depends(security)) -> str:
    if creds.credentials not in USERS:
        raise HTTPException(401, f"unknown token {creds.credentials!r}")
    return USERS[creds.credentials]


class Me(BaseModel):
    user: str


class Order(BaseModel):
    order_id: int
    owner: str
    item: str


class HeaderProbe(BaseModel):
    user: str
    x_internal_token_seen: bool


ORDERS: list[Order] = [
    Order(order_id=1, owner="alice", item="coffee grinder"),
    Order(order_id=2, owner="alice", item="espresso beans"),
    Order(order_id=3, owner="bob", item="pour-over kettle"),
]

app = FastAPI()


@app.get("/me", response_model=Me, tags=["account"])
async def me(user: str = Depends(current_user)) -> Me:
    """Who am I? — resolved from the CALLER's Bearer token."""
    return Me(user=user)


@app.get("/orders", response_model=list[Order], tags=["account"])
async def list_orders(user: str = Depends(current_user)) -> list[Order]:
    """My orders only — per-caller identity scopes the result."""
    return [o for o in ORDERS if o.owner == user]


@app.get("/debug/header-probe", response_model=HeaderProbe, tags=["account"])
async def header_probe(request: Request, user: str = Depends(current_user)) -> HeaderProbe:
    """Echo whether an internal header reached the route (it must not)."""
    return HeaderProbe(user=user, x_internal_token_seen="x-internal-token" in request.headers)


mcp = FastAPIMCP(
    app,
    name="jwt-passthrough demo",
    passthrough_headers=["authorization"],  # also the default; spelled out here
)
mcp.mount_to(app, "/mcp")


def show(title: str, payload: object) -> None:
    print(f"\n== {title} ==")
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    print(rendered if len(rendered) < 1200 else rendered[:1200] + "\n  …")


async def call(client: Client, document: str) -> dict:
    result = await client.call_tool("graphql_query", {"query": document})
    return json.loads(result.content[0].text)


QUERY = "{ account { me { user } list_orders { order_id item } } }"


async def main() -> None:
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")
    )
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(80):
        try:
            socket.create_connection(("127.0.0.1", PORT), 0.2).close()
            break
        except OSError:
            time.sleep(0.1)

    url = f"http://127.0.0.1:{PORT}/mcp"

    async with Client(url, auth="jwt-alice") as alice:
        show("1. alice's client — per-call JWT wins", (await call(alice, QUERY))["data"])
    async with Client(url, auth="jwt-bob") as bob:
        show("2. bob's client — same query, bob's data", (await call(bob, QUERY))["data"])
    async with Client(url) as bare:
        result = await call(bare, QUERY)
        show(
            "3. bare client — no identity, clean 401 (single identity source)",
            {"field_errors": [e["extensions"]["code"] for e in result["data"].get("errors", [])]},
        )

    # 4. A non-whitelisted header must never reach a route: the client sends
    #    x-internal-token alongside its valid auth, the probe route reports
    #    whether the smuggled header survived the whitelist boundary.
    evil = StreamableHttpTransport(
        url=url, headers={"x-internal-token": "evil"}, auth="jwt-alice"
    )
    async with Client(evil) as smuggler:
        probe = await call(
            smuggler,
            "{ account { header_probe { user x_internal_token_seen } } }",
        )
    show("4. smuggled x-internal-token — dropped at the whitelist boundary", probe["data"])

    # 5. In-memory client: no HTTP request context means no identity at all —
    #    protected routes answer 401 (no server-side credential to borrow).
    async with Client(mcp.mcp) as memory:
        result = await call(memory, QUERY)
        show(
            "5. in-memory client — no HTTP context, no identity -> 401",
            {"field_errors": [e["extensions"]["code"] for e in result["data"].get("errors", [])]},
        )

    await mcp.handler.aclose()
    print("\njwt passthrough demo complete.")


if __name__ == "__main__":
    asyncio.run(main())
