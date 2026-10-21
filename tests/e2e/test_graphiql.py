"""GraphiQL + GraphQL-over-HTTP endpoints."""

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from pydantic import BaseModel

from fastapi_mcp_neo import RouterGraphQLHandler


class Out(BaseModel):
    id: int
    name: str


def build_app() -> FastAPI:
    app = FastAPI()

    @app.get("/things", response_model=list[Out], tags=["demo"])
    async def things():
        return [Out(id=1, name="a")]

    return app


@pytest.fixture
def mounted():
    app = build_app()
    handler = RouterGraphQLHandler(app)
    handler.mount_graphql(app)
    return app, handler


class TestGraphiQLPage:
    async def test_html_served_and_wired(self, mounted):
        app, _ = mounted
        async with LifespanManager(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                response = await c.get("/graphiql")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "createGraphiQLFetcher({ url: '/graphql' })" in response.text

    async def test_custom_paths(self):
        app = build_app()
        handler = RouterGraphQLHandler(app)
        handler.mount_graphql(app, graphql_path="/gql", graphiql_path="/play")
        async with LifespanManager(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                html = await c.get("/play")
                result = await c.post("/gql", json={"query": "{ demo { things { id } } }"})
        assert "url: '/gql'" in html.text
        assert result.json() == {"data": {"demo": {"things": [{"id": 1}]}}}


class TestGraphqlHttp:
    async def test_post_executes_query(self, mounted):
        app, _ = mounted
        async with LifespanManager(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                response = await c.post(
                    "/graphql", json={"query": "{ demo { things { id name } } }"}
                )
        assert response.status_code == 200
        assert response.json() == {"data": {"demo": {"things": [{"id": 1, "name": "a"}]}}}

    async def test_post_with_variables(self, mounted):
        app, _ = mounted
        new = FastAPI()

        @new.get("/items/{item_id}", response_model=Out, tags=["demo"])
        async def item(item_id: int):
            return Out(id=item_id, name="x")

        handler = RouterGraphQLHandler(new)
        handler.mount_graphql(new)
        async with LifespanManager(new):
            transport = httpx.ASGITransport(app=new)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                response = await c.post(
                    "/graphql",
                    json={
                        "query": "query($id: Int!) { demo { item(item_id: $id)"
                        " { name } } }",
                        "variables": {"id": 7},
                    },
                )
        assert response.json() == {"data": {"demo": {"item": {"name": "x"}}}}

    async def test_validation_error_is_400(self, mounted):
        app, _ = mounted
        async with LifespanManager(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                response = await c.post("/graphql", json={"query": "{ nope }"})
        assert response.status_code == 400
        assert "errors" in response.json()

    async def test_missing_query_is_400(self, mounted):
        app, _ = mounted
        async with LifespanManager(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                response = await c.post("/graphql", json={})
                bad_json = await c.post(
                    "/graphql", content=b"not-json", headers={"content-type": "application/json"}
                )
        assert response.status_code == 400
        assert bad_json.status_code == 400

    async def test_get_usage_hint(self, mounted):
        app, _ = mounted
        async with LifespanManager(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                response = await c.get("/graphql")
        assert response.status_code == 405
        assert response.json()["usage"]["playground"] == "/graphiql"


class TestCredentialForwarding:
    async def test_graphql_forwards_cookie_to_protected_route(self):
        """Browser sessions through GraphiQL: add cookie to the whitelist."""
        from fastapi import Depends, HTTPException, Request

        app = FastAPI()

        def session(request: Request) -> str:
            token = request.cookies.get("session")
            if token != "s3cret":
                raise HTTPException(status_code=401, detail="no session")
            return token

        @app.get("/private", response_model=Out, tags=["demo"])
        async def private(user: str = Depends(session)):
            return Out(id=1, name=f"user:{user}")

        handler = RouterGraphQLHandler(app, passthrough_headers=["authorization", "cookie"])
        handler.mount_graphql(app)
        async with LifespanManager(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                no_cookie = await c.post(
                    "/graphql", json={"query": "{ demo { private { name } } }"}
                )
                with_cookie = await c.post(
                    "/graphql",
                    json={"query": "{ demo { private { name } } }"},
                    headers={"cookie": "session=s3cret"},
                )
        # Without credentials the field fails (401); with the cookie the
        # caller's own session reaches the route.
        assert no_cookie.json()["data"]["demo"]["private"] is None
        assert no_cookie.json()["errors"][0]["extensions"]["code"] == "HTTP_401"
        assert with_cookie.json() == {
            "data": {"demo": {"private": {"name": "user:s3cret"}}}
        }
        await handler.aclose()

    async def test_forwarding_can_be_disabled(self):
        app = FastAPI()

        @app.get("/who", response_model=Out, tags=["demo"])
        async def who():
            return Out(id=1, name="pub")

        handler = RouterGraphQLHandler(app, passthrough_headers=[])
        handler.mount_graphql(app, graphql_path="/g", graphiql_path="/p")
        async with LifespanManager(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                r = await c.post(
                    "/g",
                    json={"query": "{ demo { who { name } } }"},
                    headers={"cookie": "x=1"},
                )
        assert r.json() == {"data": {"demo": {"who": {"name": "pub"}}}}
        await handler.aclose()
