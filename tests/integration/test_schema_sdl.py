"""Schema surface through the handler: SDL shape, descriptions, degradation
notes, deprecation directives — everything an agent reads before composing.

The JSON-fallback classes pin degradation WITH field-level granularity:
only the unusable part degrades, the note names the cause and the way out.
"""

from typing import Annotated

import pytest
from fastapi import APIRouter, FastAPI
from pydantic import BaseModel, Field

from fastapi_mcp_neo.handler import RouterGraphQLHandler
from tests.support.apps import items_app
from tests.support.models import Err, ItemOut


@pytest.fixture
def handler():
    h = RouterGraphQLHandler(items_app(), allow_mutation=True)
    # SDL-only fixture: the invoker's lifespan may never start, and aclose
    # is idempotent-safe, so no teardown is needed here.
    return h


class TestSDL:
    def test_sdl_contains_types_and_fields(self, handler):
        sdl = handler.get_sdl()
        assert "type Query {" in sdl
        assert "shop: ShopQuery!" in sdl
        assert "list_items(limit: Int = 2): [ItemOut!]" in sdl
        assert "get_item(item_id: Int!): ItemOut" in sdl
        assert "type Mutation {" in sdl
        assert "items: ItemsMutation!" in sdl
        assert "create_item(payload: ItemCreateInput!): ItemOut" in sdl
        assert "type ItemOut {" in sdl
        assert "input ItemCreateInput {" in sdl

    def test_mutation_gated_off(self):
        h = RouterGraphQLHandler(items_app(), allow_mutation=False)
        assert "Mutation" not in h.get_sdl()
        assert "create_item" not in h.get_sdl()


class TestDescriptions:
    @staticmethod
    def _sdl():
        from fastapi import Body, Query

        class Documented(BaseModel):
            """A documented response model."""

            id: int
            note: str = Field(description="field-level description")

        app = FastAPI()

        @app.get("/search", response_model=list[Documented], tags=["docs"])
        async def search(
            q: Annotated[str, Query(description="full-text search terms")] = "",
            limit: Annotated[int, Query(ge=1)] = 5,
        ) -> list[Documented]:
            """Search documented things.

            Docstring body with details.
            """
            return [Documented(id=1, note="x")]

        @app.post("/things", response_model=Documented, tags=["docs"])
        async def create(
            payload: Annotated[Documented, Body(description="the thing to create")],
        ) -> Documented:
            return payload

        return RouterGraphQLHandler(app, allow_mutation=True).get_sdl()

    def test_model_docstring_becomes_type_description(self):
        sdl = self._sdl()
        assert '"""\nA documented response model.\n"""' in sdl or (
            '"""A documented response model."""' in sdl
        )

    def test_field_description_present(self):
        assert '"""field-level description"""' in self._sdl()

    def test_endpoint_docstring_becomes_field_description(self):
        sdl = self._sdl()
        assert "Search documented things." in sdl
        assert "Docstring body with details." in sdl

    def test_param_description_becomes_argument_description(self):
        sdl = self._sdl()
        assert '"""full-text search terms"""' in sdl
        assert '"""the thing to create"""' in sdl

    def test_undocumented_params_have_no_description(self):
        sdl = self._sdl()
        # limit has no Query(description=...): its SDL line is bare
        assert "limit: Int = 5" in sdl


class TestResponseFilterPassthrough:
    """Routes whose serialization kwargs reshape the response JSON
    (exclude_unset / exclude_defaults / include / exclude /
    by_alias=False) stay in the schema as JSON-scalar fields instead of
    being skipped: filtering happens after validation, so per-field
    non-null promises cannot hold — the JSON blob carries whatever
    arrives, and the field description tells agents why.
    exclude_none is exempt: it only drops Optional-valued keys, which the
    bridge maps to nullable fields anyway."""

    class Filtered(BaseModel):
        id: int
        tag: str = "t"

    @staticmethod
    def _app(**route_kwargs) -> FastAPI:
        app = FastAPI()

        @app.get("/x", response_model=TestResponseFilterPassthrough.Filtered,
                 tags=["g"], **route_kwargs)
        async def x() -> TestResponseFilterPassthrough.Filtered:
            return TestResponseFilterPassthrough.Filtered(id=1)

        return app

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"response_model_exclude_unset": True},
            {"response_model_exclude_defaults": True},
            {"response_model_include": {"id"}},
            {"response_model_exclude": {"tag"}},
            {"response_model_by_alias": False},
        ],
        ids=["unset", "defaults", "include", "exclude", "by-alias"],
    )
    def test_filtered_response_bridges_as_json(self, kwargs):
        handler = RouterGraphQLHandler(self._app(**kwargs))
        sdl = handler.get_sdl()
        assert handler.skips == []
        assert "x: JSON" in sdl
        assert "raw JSON blob" in sdl  # agent-facing note in the field description

    def test_exclude_none_keeps_structured_type(self):
        handler = RouterGraphQLHandler(self._app(response_model_exclude_none=True))
        assert handler.skips == []
        assert "x: Filtered" in handler.get_sdl()

    async def test_filtered_route_executes_without_null_violation(self, make_handler):
        """The G1 bug: exclude_unset dropped defaulted keys and the non-null
        field nulled the whole object. As JSON the same query just returns
        the filtered blob."""
        handler = make_handler(self._app(response_model_exclude_unset=True))
        result = await handler.execute("{ g { x } }")
        assert result == {"data": {"g": {"x": {"id": 1}}}}  # tag dropped by the app

    async def test_filter_detected_through_include_router(self, make_handler):
        inner = APIRouter()

        @inner.get("/sub", response_model=TestResponseFilterPassthrough.Filtered,
                   response_model_exclude_unset=True, tags=["h"])
        async def sub() -> TestResponseFilterPassthrough.Filtered:
            return TestResponseFilterPassthrough.Filtered(id=2)

        app = FastAPI()
        app.include_router(inner, prefix="/api")
        handler = make_handler(app)
        assert "sub: JSON" in handler.get_sdl()


class TestUnionFallback:
    """Non-Optional unions (``Item | Error``) pick their member at runtime,
    so the bridge falls back to the JSON scalar instead of skipping the
    route — with FIELD-level granularity: only the union part degrades,
    the surrounding model stays selectable. A real GraphQLUnionType with
    resolve_type remains a future upgrade; JSON never blocks it (issue #3)."""

    @staticmethod
    def _app() -> FastAPI:
        union = ItemOut | Err

        class Wrapped(BaseModel):
            result: union  # type: ignore[valid-type]

        app = FastAPI()

        @app.get("/risky", tags=["u"])
        async def risky(ok: bool = True) -> union:  # type: ignore[valid-type]
            return (
                ItemOut(id=1, name="n")
                if ok
                else Err(code=400, message="bad")
            )

        @app.get("/wrapped", response_model=Wrapped, tags=["u"])
        async def wrapped() -> Wrapped:
            return Wrapped(result=ItemOut(id=2, name="w"))

        @app.get("/mixed", tags=["u"])
        async def mixed() -> list[union]:  # type: ignore[valid-type]
            return [ItemOut(id=3, name="m"), Err(code=404, message="x")]

        @app.get("/scalar-union", tags=["u"])
        async def scalar_union() -> int | str:
            return 7

        return app

    def test_three_shapes_bridged_not_skipped(self):
        handler = RouterGraphQLHandler(self._app())
        assert handler.skips == []
        sdl = handler.get_sdl()
        assert "risky(ok: Boolean = true): JSON" in sdl
        assert "result: JSON!" in sdl       # field-level: only the union degrades
        assert "mixed: [JSON!]" in sdl
        assert "scalar_union: JSON" in sdl  # scalar unions ride the same fallback

    def test_descriptions_name_the_members(self):
        sdl = RouterGraphQLHandler(self._app()).get_sdl()
        assert "shape is one of: ItemOut, Err" in sdl           # route-level note
        assert "shape is one of: ItemOut, Err (union field" in sdl  # nested-field note

    async def test_union_route_executes_both_members(self, make_handler):
        handler = make_handler(self._app())
        result = await handler.execute(
            "{ u { risky(ok: false) wrapped { result } mixed } }"
        )
        assert result == {
            "data": {
                "u": {
                    "risky": {"code": 400, "message": "bad"},
                    "wrapped": {"result": {"id": 2, "name": "w"}},
                    "mixed": [
                        {"id": 3, "name": "m"},
                        {"code": 404, "message": "x"},
                    ],
                }
            }
        }


class TestDeprecation:
    """OpenAPI deprecated=True maps onto GraphQL-native deprecation: SDL
    directive, introspection visibility (hidden unless includeDeprecated),
    and executability are all spec behavior worth pinning."""

    @staticmethod
    def _app() -> FastAPI:
        app = FastAPI()

        @app.get("/old", response_model=ItemOut, deprecated=True, tags=["misc"])
        async def old() -> ItemOut:
            return ItemOut(id=1, name="old")

        @app.get("/new", response_model=ItemOut, tags=["misc"])
        async def new() -> ItemOut:
            return ItemOut(id=2, name="new")

        return app

    def test_sdl_carries_deprecation_directive(self):
        sdl = RouterGraphQLHandler(self._app()).get_sdl()
        assert 'old: ItemOut @deprecated(reason: "This endpoint is deprecated.")' in sdl
        assert "new: ItemOut" in sdl  # undeprecated fields stay bare

    async def test_introspection_hides_unless_requested(self, make_handler):
        handler = make_handler(self._app())
        result = await handler.execute(
            '{ __type(name: "MiscQuery") { fields { name } '
            'all: fields(includeDeprecated: true) { name isDeprecated deprecationReason } } }'
        )
        visible = [f["name"] for f in result["data"]["__type"]["fields"]]
        assert visible == ["new"]  # spec default: deprecated fields are hidden
        by_name = {f["name"]: f for f in result["data"]["__type"]["all"]}
        assert by_name["old"]["isDeprecated"] is True
        assert by_name["old"]["deprecationReason"] == "This endpoint is deprecated."
        assert by_name["new"]["isDeprecated"] is False

    async def test_deprecated_field_still_executes(self, make_handler):
        handler = make_handler(self._app())
        result = await handler.execute("{ misc { old { id } new { id } } }")
        assert result == {"data": {"misc": {"old": {"id": 1}, "new": {"id": 2}}}}
