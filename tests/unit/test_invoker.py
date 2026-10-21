"""invoker: request planning + in-process ASGI execution (unit altitude).

No RouterGraphQLHandler here — plan building, invoker lifecycle, direct
invokes, and the header-passthrough filter. Runtime behavior that goes
through handler.execute lives in integration/test_handler_runtime.py.
"""

from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from graphql import GraphQLError

from fastapi_mcp_neo.invoker import (
    GQLMCPRuntimeError,
    RouteInvoker,
    _render_param,
    build_request,
    filter_passthrough_headers,
)
from fastapi_mcp_neo.scanner import RouterScanner
from tests.support.models import ItemCreate, ItemOut


def build_app() -> tuple[FastAPI, list[str]]:
    events: list[str] = []

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        events.append("startup")
        yield
        events.append("shutdown")

    app = FastAPI(lifespan=lifespan)

    @app.get("/items/{item_id}", response_model=ItemOut)
    async def get_item(item_id: int):
        return ItemOut(id=item_id, name="x")

    @app.get("/search", response_model=list[ItemOut])
    async def search(q: str | None = None, limit: int = 5, active: bool = True):
        return [ItemOut(id=i, name=q or "-") for i in range(limit if active else 0)]

    @app.post("/items", response_model=ItemOut)
    async def create_item(payload: ItemCreate):
        return ItemOut(id=7, name=payload.name)

    @app.post("/bulk", response_model=list[ItemOut])
    async def create_bulk(a: ItemCreate, b: ItemCreate):
        return [ItemOut(id=1, name=a.name), ItemOut(id=2, name=b.name)]

    @app.get("/boom")
    async def boom():
        raise RuntimeError("kapow")

    return app, events


def route_for(app, method, path):
    routes, _ = RouterScanner(app, allow_mutation=True).scan()
    return next(r for r in routes if r.method == method and r.path == path)


class TestBuildRequest:
    def test_path_fill_and_query(self):
        app, _ = build_app()
        r = route_for(app, "GET", "/items/{item_id}")
        plan = build_request(r, {"item_id": 42, "unused": 1})
        assert plan.path == "/items/42"
        assert plan.params == ()
        assert plan.json_body is None

    def test_query_none_dropped_bool_rendered(self):
        app, _ = build_app()
        r = route_for(app, "GET", "/search")
        plan = build_request(r, {"q": None, "limit": 3, "active": False})
        assert plan.params == (("limit", "3"), ("active", "false"))

    def test_query_default_used_when_absent(self):
        app, _ = build_app()
        r = route_for(app, "GET", "/search")
        plan = build_request(r, {})
        assert dict(plan.params) == {"limit": "5", "active": "true"}

    def test_single_body_not_embedded(self):
        app, _ = build_app()
        r = route_for(app, "POST", "/items")
        plan = build_request(r, {"payload": {"name": "n", "price": 1.0}})
        assert plan.json_body == {"name": "n", "price": 1.0}

    def test_multiple_bodies_embedded(self):
        app, _ = build_app()
        r = route_for(app, "POST", "/bulk")
        plan = build_request(r, {"a": {"name": "a"}, "b": {"name": "b"}})
        assert plan.json_body == {"a": {"name": "a"}, "b": {"name": "b"}}

    def test_extra_headers_merged(self):
        app, _ = build_app()
        r = route_for(app, "GET", "/items/{item_id}")
        plan = build_request(r, {"item_id": 1}, {"authorization": "Bearer t"})
        assert plan.headers["authorization"] == "Bearer t"
        assert plan.headers["accept"] == "application/json"

    def test_missing_path_param_raises_graphql_error(self):
        app, _ = build_app()
        r = route_for(app, "GET", "/items/{item_id}")
        with pytest.raises(GraphQLError, match="path parameter 'item_id'"):
            build_request(r, {})


class TestLifecycle:
    async def test_lifespan_lazy_and_idempotent(self):
        app, events = build_app()
        invoker = RouteInvoker(app)
        assert events == []  # not started yet
        r = route_for(app, "GET", "/items/{item_id}")
        await invoker.invoke(r, {"item_id": 1})
        assert events == ["startup"]
        await invoker.invoke(r, {"item_id": 2})
        assert events == ["startup"]  # still exactly one startup
        await invoker.aclose()
        assert events == ["startup", "shutdown"]

    async def test_closed_invoker_rejects_use(self):
        app, _ = build_app()
        invoker = RouteInvoker(app)
        await invoker.aclose()
        with pytest.raises(GQLMCPRuntimeError):
            await invoker.start()

    async def test_no_managed_lifespan(self):
        app, events = build_app()
        invoker = RouteInvoker(app, manage_lifespan=False)
        r = route_for(app, "GET", "/items/{item_id}")
        result = await invoker.invoke(r, {"item_id": 1})
        assert result == {"id": 1, "name": "x"}
        assert events == []


class TestInvoke:
    async def test_invoke_returns_json(self):
        app, _ = build_app()
        r = route_for(app, "GET", "/items/{item_id}")
        invoker = RouteInvoker(app, manage_lifespan=False)
        assert await invoker.invoke(r, {"item_id": 9}) == {"id": 9, "name": "x"}
        await invoker.aclose()

    async def test_invoke_body_route(self):
        app, _ = build_app()
        r = route_for(app, "POST", "/items")
        invoker = RouteInvoker(app, manage_lifespan=False)
        result = await invoker.invoke(r, {"payload": {"name": "n"}})
        assert result == {"id": 7, "name": "n"}
        await invoker.aclose()

    async def test_422_maps_to_http_error(self):
        app, _ = build_app()
        r = route_for(app, "GET", "/items/{item_id}")
        invoker = RouteInvoker(app, manage_lifespan=False)
        # int coercion happens at the GraphQL layer; str id -> FastAPI 422
        with pytest.raises(GraphQLError) as exc:
            await invoker.invoke(r, {"item_id": "not-an-int"})
        assert exc.value.extensions["code"] == "HTTP_422"
        await invoker.aclose()


class TestPathConvertorRoutes:
    """B1 regression: URLs must be rendered by Starlette (url_path_for), not
    by string surgery on route.path — convertor syntax {id:int} / {p:path}
    made hand-rolled replacement miss and 404 on the literal template."""

    @staticmethod
    def build_app() -> FastAPI:
        from pydantic import BaseModel

        class Got(BaseModel):
            got: str

        app = FastAPI()

        @app.get("/conv/{item_id:int}", response_model=Got)
        async def get_conv(item_id: int) -> Got:
            return Got(got=f"id={item_id}")

        @app.get("/files/{p:path}", response_model=Got)
        async def get_file(p: str) -> Got:
            return Got(got=f"p={p}")

        @app.get("/城市/{name}", response_model=Got)
        async def get_city(name: str) -> Got:
            return Got(got=f"city={name}")

        return app

    async def _invoke(self, path: str, kwargs: dict) -> dict:
        app = self.build_app()
        routes, _ = RouterScanner(app).scan()
        route = next(r for r in routes if r.path == path)
        invoker = RouteInvoker(app, manage_lifespan=False)
        try:
            return await invoker.invoke(route, kwargs)
        finally:
            await invoker.aclose()

    async def test_int_convertor_b1_regression(self):
        assert await self._invoke("/conv/{item_id:int}", {"item_id": 5}) == {"got": "id=5"}

    async def test_path_convertor_with_spaces(self):
        assert await self._invoke("/files/{p:path}", {"p": "docs/read me.pdf"}) == {
            "got": "p=docs/read me.pdf"
        }

    async def test_unicode_path_segment(self):
        assert await self._invoke("/城市/{name}", {"name": "上海"}) == {"got": "city=上海"}

    async def test_negative_int_is_field_error_not_crash(self):
        # IntegerConvertor asserts on negatives; surfaced as BAD_REQUEST.
        with pytest.raises(GraphQLError) as exc:
            await self._invoke("/conv/{item_id:int}", {"item_id": -1})
        assert exc.value.extensions["code"] == "BAD_REQUEST"


class TestFilterPassthroughHeaders:
    """The untrusted-boundary filter (merged: R4 protocol protection +
    whitelist semantics). Content-type/accept are refused even when
    whitelisted — a forwarded value would retype the JSON body request
    into a 422."""

    def test_keeps_only_whitelisted(self):
        raw = {"Authorization": "Bearer u", "x-internal-token": "s", "accept": "*/*"}
        assert filter_passthrough_headers(raw, ("authorization",)) == {
            "authorization": "Bearer u"
        }

    def test_case_insensitive_both_sides(self):
        raw = {"AUTHORIZATION": "Bearer u"}
        assert filter_passthrough_headers(raw, ("Authorization",)) == {
            "authorization": "Bearer u"
        }

    def test_empty_allowed_drops_everything(self):
        assert filter_passthrough_headers({"authorization": "Bearer u"}, ()) == {}

    def test_protocol_headers_never_forwarded(self):
        filtered = filter_passthrough_headers(
            {"Content-Type": "text/plain", "Accept": "*/*", "Authorization": "Bearer x"},
            ["content-type", "accept", "authorization"],
        )
        assert filtered == {"authorization": "Bearer x"}

    def test_custom_headers_still_forwarded(self):
        assert filter_passthrough_headers({"X-Custom": "1"}, ["x-custom"]) == {
            "x-custom": "1"
        }


class TestRenderParamContract:
    """Internal contract pin (M1 wave): ``_render_param`` is private, but
    mixed lists drop None items instead of stringifying them into the
    literal query value "None"."""

    def test_mixed_list_drops_none_items(self):
        assert _render_param([1, None, 3]) == [1, 3]
        assert _render_param([None, None]) is None  # all-None → not sent
        assert _render_param(None) is None


class TestResponseBodyShapes:
    async def test_non_json_2xx_body_wrapped_as_raw(self):
        """A typed route that returns a bare Response (FastAPI skips
        serialization for Response returns) serves text/plain with 200 —
        the invoker wraps the body instead of failing to parse it."""
        from fastapi.responses import PlainTextResponse

        app = FastAPI()

        @app.get("/text", response_model=str)
        async def text() -> str:
            return PlainTextResponse("hello")

        r = route_for(app, "GET", "/text")
        invoker = RouteInvoker(app, manage_lifespan=False)
        try:
            assert await invoker.invoke(r, {}) == {"_raw": "hello"}
        finally:
            await invoker.aclose()

    async def test_long_error_body_truncated(self):
        from fastapi import HTTPException

        app = FastAPI()

        @app.get("/boom", response_model=ItemOut)
        async def boom() -> ItemOut:
            raise HTTPException(status_code=400, detail="x" * 600)

        r = route_for(app, "GET", "/boom")
        invoker = RouteInvoker(app, manage_lifespan=False)
        try:
            with pytest.raises(GraphQLError) as exc:
                await invoker.invoke(r, {})
        finally:
            await invoker.aclose()
        message = exc.value.message
        assert message.endswith("…"), "the overflow must be visibly truncated"
        assert "x" * 600 not in message
