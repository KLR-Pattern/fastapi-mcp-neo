"""OAuth end-to-end: the caller's token from a (mock) authorization server
travels the MCP bridge and is verified by the app's own security scheme.

Locks in the id:46 workflow against regressions:
- verification logic (OAuth2PasswordBearer + JWT signature) passes through
  untouched — the bridge verifies nothing itself;
- protocol endpoints (/oauth/token) stay OUT of the schema via exclude;
- the token's lifecycle (issue / expire / tamper) is entirely the caller's
  business: an expired or forged token is a clean field-level 401.

The authorization server is mocked with a hand-rolled HS256 JWT (stdlib
hmac/base64 only — no new dependency); swapping it for a real IdP changes
nothing on the bridge side.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import httpx
from asgi_lifespan import LifespanManager
from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from pydantic import BaseModel

from fastapi_mcp_neo import FastAPIMCP
from tests.support.mcp import asgi_client_factory

SECRET = b"test-oauth-secret"
USERS_DB = {"alice": "wonderland", "bob": "builder"}
ORDERS = {"alice": ["coffee grinder"], "bob": ["pour-over kettle"]}


# ------------------------------------------------------- mock authorization server


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_jwt(sub: str, expires_in: int = 600, secret: bytes = SECRET) -> str:
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps({"sub": sub, "exp": int(time.time()) + expires_in}).encode())
    signing_input = f"{header}.{payload}"
    signature = _b64url(hmac.new(secret, signing_input.encode(), hashlib.sha256).digest())
    return f"{signing_input}.{signature}"


def decode_jwt(token: str) -> dict:
    header, payload, signature = token.split(".")
    signing_input = f"{header}.{payload}".encode()
    expected = hmac.new(SECRET, signing_input, hashlib.sha256).digest()
    if not hmac.compare_digest(base64.urlsafe_b64decode(signature + "=="), expected):
        raise ValueError("bad signature")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
    if claims.get("exp", 0) < time.time():
        raise ValueError("expired")
    return claims


# ------------------------------------------------------------------- the app


class TokenOut(BaseModel):
    access_token: str
    token_type: str


class MeOut(BaseModel):
    user: str


class OrdersOut(BaseModel):
    orders: list[str]


oauth2 = OAuth2PasswordBearer(tokenUrl="/oauth/token")


def current_user(token: str = Depends(oauth2)) -> str:
    try:
        return decode_jwt(token)["sub"]
    except ValueError as exc:
        raise HTTPException(401, f"invalid token: {exc}") from exc


def build_app() -> FastAPI:
    app = FastAPI()

    @app.post("/oauth/token", response_model=TokenOut, tags=["auth"])
    async def issue_token(form: OAuth2PasswordRequestForm = Depends()) -> TokenOut:
        """The (mock) authorization server: password grant, issues a JWT."""
        if USERS_DB.get(form.username) != form.password:
            raise HTTPException(401, "bad credentials")
        return TokenOut(access_token=make_jwt(form.username), token_type="bearer")

    @app.get("/me", response_model=MeOut, tags=["account"])
    async def me(user: str = Depends(current_user)) -> MeOut:
        return MeOut(user=user)

    @app.get("/orders", response_model=OrdersOut, tags=["account"])
    async def list_orders(user: str = Depends(current_user)) -> OrdersOut:
        return OrdersOut(orders=ORDERS.get(user, []))

    return app


def build_mcp(app: FastAPI, *, exclude_oauth: bool = True) -> FastAPIMCP:
    # Tag-based scoping (path globs are deprecated): the bridge serves the
    # notes domain; OAuth login routes stay HTTP-only either way.
    mcp = FastAPIMCP(
        app,
        name="oauth-e2e",
        include_tags=["account"] if exclude_oauth else None,
    )
    mcp.mount_to(app, "/mcp")
    return mcp


async def fetch_token(app: FastAPI, username: str, password: str) -> str:
    """Step 1 of the flow: the user's side of OAuth — get a token over HTTP,
    entirely outside the MCP bridge."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        r = await client.post("/oauth/token", data={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def graphql_as(app: FastAPI, token: str) -> dict:
    """Step 2: connect to MCP with that token; the bridge forwards it."""
    transport = StreamableHttpTransport(
        url="http://t/mcp/",
        headers={"authorization": f"Bearer {token}"},
        httpx_client_factory=asgi_client_factory(app),
    )
    async with LifespanManager(app):
        async with Client(transport) as client:
            r = await client.call_tool(
                "graphql_query",
                {"query": "{ account { me { user } list_orders { orders } } }"},
            )
    import json as _json

    return _json.loads(r.content[0].text)["data"]


# ---------------------------------------------------------------------- tests


class TestOAuthEndToEnd:
    async def test_token_from_oauth_server_travels_the_bridge(self):
        """alice's OAuth token -> MCP -> routes verify alice, see her orders."""
        app = build_app()
        build_mcp(app)
        token = await fetch_token(app, "alice", "wonderland")
        result = await graphql_as(app, token)
        assert result["data"]["account"]["me"] == {"user": "alice"}
        assert result["data"]["account"]["list_orders"] == {"orders": ["coffee grinder"]}

    async def test_each_user_gets_their_own_identity(self):
        app = build_app()
        build_mcp(app)
        token = await fetch_token(app, "bob", "builder")
        result = await graphql_as(app, token)
        assert result["data"]["account"]["me"] == {"user": "bob"}
        assert result["data"]["account"]["list_orders"] == {"orders": ["pour-over kettle"]}

    async def test_no_token_is_clean_401(self):
        """No OAuth token, no identity: nothing to fall back on."""
        app = build_app()
        build_mcp(app)
        result = await graphql_as(app, token="")
        assert result["data"]["account"]["me"] is None
        assert result["errors"][0]["extensions"]["code"] == "HTTP_401"

    async def test_expired_token_is_field_level_401(self):
        """Token lifecycle is the caller's business: expiry -> clean 401."""
        app = build_app()
        build_mcp(app)
        result = await graphql_as(app, make_jwt("alice", expires_in=-10))
        assert result["data"]["account"]["me"] is None
        assert result["errors"][0]["extensions"]["code"] == "HTTP_401"

    async def test_tampered_token_rejected_by_signature(self):
        app = build_app()
        build_mcp(app)
        forged = make_jwt("alice", secret=b"wrong-key")
        result = await graphql_as(app, forged)
        assert result["data"]["account"]["me"] is None
        assert result["errors"][0]["extensions"]["code"] == "HTTP_401"


class TestProtocolEndpointsExcluded:
    def test_oauth_token_endpoint_stays_out_of_schema(self):
        """/oauth/token is protocol, not business — excluded per id:46."""
        app = build_app()
        mcp = build_mcp(app)
        sdl = mcp.handler.get_sdl()
        assert "issue_token" not in sdl
        assert "Mutation" not in sdl  # nothing else is a write route

    def test_without_exclude_the_token_endpoint_cannot_leak(self):
        """Before form/file routes were skipped at scan time, the token
        endpoint (OAuth2PasswordRequestForm = form-encoded credentials)
        DID leak into the schema without an exclude — as a mutation that
        always 422ed (the invoker speaks JSON). The Form skip now prevents
        that leak structurally; the exclude glob remains defense-in-depth
        for future non-form protocol routes."""
        app = build_app()
        mcp = FastAPIMCP(app, name="leaky", allow_mutation=True)
        assert "issue_token" not in mcp.handler.get_sdl()
        assert any(
            s.path == "/oauth/token" and "form/file parameter" in s.reason
            for s in mcp.handler.skips
        )


class TestBadCredentialsAtAuthorizationServer:
    async def test_wrong_password_no_token(self):
        """The mock AS rejects bad credentials — nothing to forward later."""
        app = build_app()
        build_mcp(app)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:
            r = await client.post(
                "/oauth/token", data={"username": "alice", "password": "nope"}
            )
        assert r.status_code == 401
