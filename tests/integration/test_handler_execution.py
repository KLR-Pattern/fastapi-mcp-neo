"""Handler execution semantics: domain grouping, name collisions, the
execute() pipeline over the items app, and failure modes that must fail
fast at construction time.
"""

from typing import Annotated

import pytest
from fastapi import FastAPI, Form, HTTPException
from pydantic import BaseModel

from fastapi_mcp_neo.handler import GQLMCPConfigError, RouterGraphQLHandler
from fastapi_mcp_neo.naming import DuplicateFieldError
from tests.support.apps import items_app
from tests.support.models import ItemCreate, ItemOut


@pytest.fixture
async def handler(make_handler):
    return make_handler(items_app(), allow_mutation=True)


class TestDomainCollisions:
    """A leaf field and a child domain segment landing in the SAME group
    object type compete for one GraphQL field name — this must fail fast
    (same policy as two same-named leaves), never silently drop the leaf."""

    def test_leaf_colliding_with_child_domain_fails_fast(self):
        class Summary(BaseModel):
            total: int

        class Product(BaseModel):
            id: int

        app = FastAPI()

        @app.get("/catalog-summary", response_model=Summary, tags=["shop"])
        async def catalog() -> Summary:
            """Leaf field 'catalog' inside ShopQuery..."""
            return Summary(total=5)

        @app.get("/products", response_model=list[Product], tags=["shop:catalog"])
        async def list_products() -> list[Product]:
            """...and the child domain segment 'catalog' also inside ShopQuery."""
            return [Product(id=1)]

        with pytest.raises(DuplicateFieldError):
            RouterGraphQLHandler(app)

    async def test_leaf_coexisting_with_child_domain_is_kept(self, make_handler):
        """Non-colliding leaf + child group in one domain must both survive."""

        class Summary(BaseModel):
            total: int

        class Product(BaseModel):
            id: int

        app = FastAPI()

        @app.get("/catalog-summary", response_model=Summary, tags=["shop"])
        async def stats() -> Summary:
            return Summary(total=5)

        @app.get("/products", response_model=list[Product], tags=["shop:catalog"])
        async def list_products() -> list[Product]:
            return [Product(id=1)]

        handler = make_handler(app)
        sdl = handler.get_sdl()
        assert "stats: Summary" in sdl
        assert "catalog: ShopCatalogQuery" in sdl
        result = await handler.execute("{ shop { stats { total } } }")
        assert result == {"data": {"shop": {"stats": {"total": 5}}}}


class TestExecution:
    async def test_query_flat(self, handler):
        result = await handler.execute("{ shop { list_items { id name } } }")
        assert result == {
            "data": {"shop": {"list_items": [{"id": 0, "name": "i0"}, {"id": 1, "name": "i1"}]}}
        }

    async def test_field_projection(self, handler):
        result = await handler.execute("{ shop { list_items { name } } }")
        assert result == {"data": {"shop": {"list_items": [{"name": "i0"}, {"name": "i1"}]}}}

    async def test_query_with_args(self, handler):
        result = await handler.execute("{ shop { list_items(limit: 1) { id } } }")
        assert result == {"data": {"shop": {"list_items": [{"id": 0}]}}}

    async def test_path_params(self, handler):
        result = await handler.execute("{ shop { get_item(item_id: 5) { id name } } }")
        assert result == {"data": {"shop": {"get_item": {"id": 5, "name": "single"}}}}

    async def test_variables(self, handler):
        result = await handler.execute(
            "query($id: Int!) { shop { get_item(item_id: $id) { name } } }",
            variables={"id": 3},
        )
        assert result == {"data": {"shop": {"get_item": {"name": "single"}}}}

    async def test_variable_defaults(self, handler):
        result = await handler.execute(
            "query($limit: Int = 1) { shop { list_items(limit: $limit) { id } } }"
        )
        assert result == {"data": {"shop": {"list_items": [{"id": 0}]}}}

    async def test_alias_and_multiple_fields(self, handler):
        result = await handler.execute(
            "{ shop { a: list_items(limit: 1) { id } b: get_item(item_id: 7) { id } } }"
        )
        assert result["data"]["shop"]["a"] == [{"id": 0}]
        assert result["data"]["shop"]["b"] == {"id": 7}

    async def test_mutation_execution(self, handler):
        result = await handler.execute(
            'mutation { items { create_item(payload: {name: "n"}) { id name } } }'
        )
        assert result == {"data": {"items": {"create_item": {"id": 99, "name": "n"}}}}

    async def test_validation_error_format(self, handler):
        result = await handler.execute("{ nope }")
        assert "data" not in result
        assert "Cannot query field 'nope'" in result["errors"][0]["message"]

    async def test_resolver_error_format(self, make_handler):
        app = FastAPI()

        @app.get("/missing/{item_id}", response_model=ItemOut, tags=["misc"])
        async def missing(item_id: int):
            raise HTTPException(status_code=404, detail="not found")

        handler = make_handler(app)
        result = await handler.execute("{ misc { missing(item_id: 1) { id } } }")
        assert result["data"] == {"misc": {"missing": None}}
        error = result["errors"][0]
        assert error["extensions"]["code"] == "HTTP_404"
        assert "404" in error["message"]


class TestConfigErrors:
    def test_no_routable_routes(self):
        app = FastAPI()

        @app.post("/upload")
        async def upload(name: Annotated[str, Form()]) -> dict:
            return {"name": name}

        with pytest.raises(GQLMCPConfigError, match="no routable endpoints"):
            RouterGraphQLHandler(app)


class TestDuplicateEndpointNames:
    def test_same_name_same_domain_fails_fast(self):
        app = FastAPI()

        @app.get("/users/{user_id}", response_model=ItemOut, tags=["iam"])
        async def get_user(user_id: int):
            return ItemOut(id=user_id, name="a")

        @app.get("/staff/users/{user_id}", response_model=ItemOut, tags=["iam"])
        async def get_user(user_id: int):  # noqa: F811 — the collision under test
            return ItemOut(id=user_id, name="b")

        with pytest.raises(DuplicateFieldError, match="domain group 'iam'"):
            RouterGraphQLHandler(app)

    async def test_same_name_different_domains_builds_and_executes(self, make_handler):
        """D1: names must only clash WITHIN a domain group (one object type).

        iam.get_user and admin.iam.get_user are different GraphQL object
        types' fields — both must build and both must route to their own
        endpoint.
        """
        app = FastAPI()

        @app.get("/users/{user_id}", response_model=ItemOut, tags=["iam"])
        async def get_user(user_id: int):
            return ItemOut(id=user_id, name="public")

        @app.get("/admin/users/{user_id}", response_model=ItemOut, tags=["admin:iam"])
        async def get_user(user_id: int):  # noqa: F811 — same name, other domain
            return ItemOut(id=user_id, name="admin")

        handler = make_handler(app)
        result = await handler.execute(
            "{ iam { get_user(user_id: 1) { name } }"
            " admin { iam { get_user(user_id: 2) { name } } } }"
        )
        assert result["data"]["iam"]["get_user"] == {"name": "public"}
        assert result["data"]["admin"]["iam"]["get_user"] == {"name": "admin"}

    def test_same_name_across_namespaces_is_fine(self):
        app = FastAPI()

        @app.get("/things/{thing_id}", response_model=ItemOut)
        async def thing(thing_id: int):
            return ItemOut(id=thing_id, name="q")

        @app.post("/things", response_model=ItemOut)
        async def thing(payload: ItemCreate):  # noqa: F811
            return ItemOut(id=1, name=payload.name)

        handler = RouterGraphQLHandler(app, allow_mutation=True)
        assert "thing" in handler.get_sdl()


class TestMutationOnlyApp:
    def test_mutation_only_schema_fails_fast(self):
        app = FastAPI()

        @app.post("/things", response_model=ItemOut)
        async def create_thing(payload: ItemCreate):
            return ItemOut(id=1, name=payload.name)

        with pytest.raises(GQLMCPConfigError, match="mutation-only"):
            RouterGraphQLHandler(app, allow_mutation=True)


class TestSkips:
    """R2: the scanner's skip report is caller-visible, not log-only."""

    def test_untyped_route_bridges_with_note(self):
        """Untyped endpoints bridge as raw JSON with an advisory note."""
        app = FastAPI()

        @app.get("/now")
        async def now():  # untyped on purpose: no response_model, no annotation
            return {"ts": 1}

        h = RouterGraphQLHandler(app)
        assert h.skips == []
        sdl = h.get_sdl()
        assert "now: JSON" in sdl
        assert "declares no response type" in sdl

    def test_no_skips_on_clean_app(self):
        h = RouterGraphQLHandler(items_app(), allow_mutation=True)
        assert h.skips == []


class TestRecursiveResponseModels:
    """Self-referencing Pydantic models, end to end: schema build, SDL and
    actual nested execution (the regression scenario from
    tadata-org/fastapi_mcp#155 — RecursionError in their OpenAPI $ref
    resolution; here recursion is a native GraphQL shape)."""

    async def test_recursive_model_builds_sdl_and_executes_nested(self, make_handler):
        class Node(BaseModel):
            name: str
            children: list["Node"] = []

        app = FastAPI()

        @app.get("/tree", response_model=Node, tags=["tree"])
        async def tree():
            return Node(
                name="root",
                children=[
                    Node(name="a"),
                    Node(name="b", children=[Node(name="c")]),
                ],
            )

        handler = make_handler(app)
        sdl = handler.get_sdl()
        assert "children" in sdl  # the cycle resolved, not refused

        result = await handler.execute(
            "{ tree { tree { name children { name children { name } } } } }"
        )
        assert "errors" not in result, result
        nested = result["data"]["tree"]["tree"]
        assert nested["children"][1]["children"][0]["name"] == "c"


class TestGroupNameCollisions:
    async def test_same_pascal_name_qualifies(self, make_handler, caplog):
        """Two domain paths that title-case to the same group type (nested
        'shop:catalog' vs flat 'shop_catalog') must both survive: the second
        gets a numbered name and a warning, never a silent clash."""
        import logging

        app = FastAPI()

        @app.get("/nested", response_model=ItemOut, tags=["shop:catalog"])
        async def nested() -> ItemOut:
            return ItemOut(id=1, name="n")

        @app.get("/flat", response_model=ItemOut, tags=["shop_catalog"])
        async def flat() -> ItemOut:
            return ItemOut(id=2, name="f")

        with caplog.at_level(logging.WARNING, logger="fastapi_mcp_neo.schema_builder"):
            handler = make_handler(app)
        sdl = handler.get_sdl()
        assert "type ShopCatalogQuery {" in sdl
        assert "type ShopCatalogQuery2 {" in sdl
        assert "already owned by" in caplog.text

        result = await handler.execute(
            "{ shop { catalog { nested { id } } } shop_catalog { flat { id } } }"
        )
        assert result == {
            "data": {
                "shop": {"catalog": {"nested": {"id": 1}}},
                "shop_catalog": {"flat": {"id": 2}},
            }
        }, result


class TestUnhandledExceptions:
    """Migrated from fastapi_mcp's test_call_tool_raise_error, hardened:
    unhandled route exceptions must match REAL network behavior — a 500
    with the generic body — and never leak the raw exception text (the
    default ASGITransport would propagate it into the GraphQL message)."""

    @pytest.fixture
    def boom_app(self):
        from fastapi import FastAPI
        from pydantic import BaseModel

        class Out(BaseModel):
            ok: bool

        app = FastAPI()

        @app.get("/boom", tags=["t"])
        async def boom() -> Out:
            raise RuntimeError("secret-internal-detail")

        @app.get("/fine", tags=["t"])
        async def fine() -> Out:
            return Out(ok=True)

        return app

    async def test_exception_becomes_http_500_not_a_leak(self, boom_app):
        handler = RouterGraphQLHandler(boom_app, allow_mutation=False)
        try:
            result = await handler.execute("{ t { boom { ok } } }")
        finally:
            await handler.aclose()
        (error,) = result["errors"]
        assert error["message"] == "GET /boom -> 500: Internal Server Error"
        assert error["extensions"]["http_status"] == 500
        assert "secret-internal-detail" not in error["message"]
        assert result["data"] == {"t": {"boom": None}}

    async def test_siblings_survive_an_exploding_field(self, boom_app):
        handler = RouterGraphQLHandler(boom_app, allow_mutation=False)
        try:
            result = await handler.execute("{ t { fine { ok } boom { ok } } }")
        finally:
            await handler.aclose()
        assert result["data"]["t"]["fine"] == {"ok": True}
        assert result["data"]["t"]["boom"] is None


class TestReviewRegressionPins:
    """Pins for the architecture-review findings: streaming-mid-exception
    context, debug=True sanitization, server-side 500 diagnostics, and
    tuple-degrade auditability."""

    async def test_streaming_mid_exception_is_contextual(self):
        from fastapi.responses import StreamingResponse

        class Out(BaseModel):
            items: list[int]

        app = FastAPI()

        @app.get("/liar", tags=["t"])
        async def liar() -> Out:
            async def gen():
                yield b'{"items": [1, '
                raise RuntimeError("mid-stream secret")

            return StreamingResponse(gen(), media_type="application/json")

        @app.get("/fine", tags=["t"])
        async def fine() -> Out:
            return Out(items=[7])

        handler = RouterGraphQLHandler(app, allow_mutation=False)
        try:
            result = await handler.execute("{ t { fine { items } liar { items } } }")
        finally:
            await handler.aclose()
        (error,) = result["errors"]
        assert "truncated response body" in error["message"]
        assert "GET /liar" in error["message"]
        assert error["extensions"]["http_status"] == 502
        assert "mid-stream secret" not in error["message"]
        assert result["data"]["t"]["fine"] == {"items": [7]}

    async def test_debug_traceback_never_reaches_the_agent(self):
        class Out(BaseModel):
            ok: bool

        app = FastAPI(debug=True)

        @app.get("/boom", tags=["t"])
        async def boom() -> Out:
            raise RuntimeError("secret-internal-detail")

        handler = RouterGraphQLHandler(app, allow_mutation=False)
        try:
            result = await handler.execute("{ t { boom { ok } } }")
        finally:
            await handler.aclose()
        (error,) = result["errors"]
        assert "Traceback" not in error["message"]
        assert "secret-internal-detail" not in error["message"]
        assert error["extensions"]["http_status"] == 500

    async def test_swallowed_500_leaves_a_server_trace(self, caplog):
        class Out(BaseModel):
            ok: bool

        app = FastAPI()

        @app.get("/boom", tags=["t"])
        async def boom() -> Out:
            raise RuntimeError("deadlock-in-pool-7")

        handler = RouterGraphQLHandler(app, allow_mutation=False)
        try:
            with caplog.at_level("ERROR", logger="fastapi_mcp_neo.invoker"):
                await handler.execute("{ t { boom { ok } } }")
        finally:
            await handler.aclose()
        assert "unhandled route exception" in caplog.text
        assert "/boom" in caplog.text

    async def test_heterogeneous_tuple_degrade_is_audited(self):
        class Item(BaseModel):
            name: str

        app = FastAPI()

        @app.get("/pair", tags=["t"])
        async def pair() -> tuple[Item, int]:
            return (Item(name="x"), 2)

        handler = RouterGraphQLHandler(app, allow_mutation=False)
        try:
            degraded = dict(handler.readiness().degraded_fields)
            assert any(
                "heterogeneous tuple" in reason for reason in degraded.values()
            )
        finally:
            await handler.aclose()

    async def test_bare_lowercase_tuple_degrades_like_typing_tuple(self):
        app = FastAPI()

        @app.get("/bare", tags=["t"])
        async def bare() -> tuple:  # noqa: UP006 — deliberate legacy spelling
            return (1, "x")

        handler = RouterGraphQLHandler(app, allow_mutation=False)
        try:
            sdl = handler.get_sdl()
            assert "bare: JSON" in sdl  # same verdict as typing.Tuple
        finally:
            await handler.aclose()
