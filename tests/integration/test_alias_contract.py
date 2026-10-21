"""The alias wire contract: GraphQL names are legal identifiers, wire keys
are whatever FastAPI validates/serializes by — the bridge translates at
both boundaries instead of diverging.

Before this contract, three failure modes: an illegal wire name crashed
schema construction outright; a sanitized name accepted calls that FastAPI
then rejected (input) or could not resolve (output); AliasChoices fell
back to the Python field name that FastAPI never recognizes. Every test
here executes end to end through the real ASGI app.
"""

from typing import Annotated

from fastapi import FastAPI, Query
from pydantic import AliasChoices, BaseModel, Field
from typing_extensions import TypedDict

from fastapi_mcp_neo.handler import RouterGraphQLHandler
from tests.support.apps import add_ping


class TestTopLevelParams:
    async def test_hyphenated_query_alias_constructs_and_calls(self):
        """Query(alias="order-id") is legal FastAPI; the GraphQL argument
        is the sanitized order_id and the resolver translates back to the
        wire name — previously this crashed the whole handler build."""
        app = FastAPI()

        @app.get("/orders", response_model=str, tags=["t"])
        async def orders(order_id: str = Query(alias="order-id")) -> str:
            return f"got:{order_id}"

        handler = RouterGraphQLHandler(app)
        assert "orders(order_id: String!): String" in handler.get_sdl()
        result = await handler.execute('{ t { orders(order_id: "abc") } }')
        assert result == {"data": {"t": {"orders": "got:abc"}}}, result

    async def test_query_model_field_alias_translates(self):
        class Filters(BaseModel):
            in_stock: bool = Field(default=False, alias="in-stock")

        app = FastAPI()

        @app.get("/list", response_model=int, tags=["t"])
        async def list_items(filters: Annotated[Filters, Query()]) -> int:
            return int(filters.in_stock)

        handler = RouterGraphQLHandler(app)
        sdl = handler.get_sdl()
        assert "in_stock: Boolean" in sdl  # sanitized GraphQL name
        result = await handler.execute("{ t { list_items(in_stock: true) } }")
        assert result == {"data": {"t": {"list_items": 1}}}, result


class TestModelFields:
    async def test_input_alias_sanitized_translates(self):
        """Field(alias="item-sku"): the input object's out_type rewrites
        the coerced dict to the wire key before FastAPI sees it —
        previously every call 422'd on 'Field required'."""

        class CreateBody(BaseModel):
            sku: str = Field(alias="item-sku")

        app = FastAPI()
        add_ping(app)

        @app.post("/create", tags=["t"])
        async def create(body: CreateBody) -> str:
            return body.sku

        handler = RouterGraphQLHandler(app, allow_mutation=True)
        assert "item_sku: String!" in handler.get_sdl()
        result = await handler.execute(
            'mutation { t { create(body: { item_sku: "x" }) } }'
        )
        assert result == {"data": {"t": {"create": "x"}}}, result

    async def test_output_serialization_alias_resolves_wire_key(self):
        """serialization_alias="item-sku": the field resolves from the
        wire key instead of nulling — previously 'Cannot return null for
        non-nullable field'."""

        class OutM(BaseModel):
            name: str = Field(serialization_alias="item-sku")

        app = FastAPI()

        @app.get("/out", response_model=OutM, tags=["t"])
        async def out() -> OutM:
            return OutM(name="real-value")

        handler = RouterGraphQLHandler(app)
        result = await handler.execute("{ t { out { item_sku } } }")
        assert result == {"data": {"t": {"out": {"item_sku": "real-value"}}}}, result

    async def test_alias_choices_names_the_first_choice(self):
        """AliasChoices("m1","m2"): the wire name is the first choice —
        previously the Python field name was used, which FastAPI rejects."""

        class ACBody(BaseModel):
            mode: str = Field(validation_alias=AliasChoices("m1", "m2"))

        app = FastAPI()
        add_ping(app)

        @app.post("/set", tags=["t"])
        async def set_mode(body: ACBody) -> str:
            return body.mode

        handler = RouterGraphQLHandler(app, allow_mutation=True)
        assert "m1: String!" in handler.get_sdl()
        result = await handler.execute('mutation { t { set_mode(body: { m1: "fast" }) } }')
        assert result == {"data": {"t": {"set_mode": "fast"}}}, result

    async def test_nested_input_alias_translates_through_levels(self):
        """The out_type translation composes: an aliased field inside a
        nested body model reaches FastAPI under its wire key."""

        class Inner(BaseModel):
            sku: str = Field(alias="item-sku")

        class Outer(BaseModel):
            label: str
            inner: Inner

        app = FastAPI()
        add_ping(app)

        @app.post("/order", tags=["t"])
        async def order(body: Outer) -> str:
            return f"{body.label}:{body.inner.sku}"

        handler = RouterGraphQLHandler(app, allow_mutation=True)
        result = await handler.execute(
            'mutation { t { order(body: { label: "L", inner: { item_sku: "S" } }) } }'
        )
        assert result == {"data": {"t": {"order": "L:S"}}}, result

    async def test_nothing_degrades_nothing_lies(self):
        """Translation preserves field selection and types — the audit
        staying green is now honest (nothing is degraded)."""

        class OutM(BaseModel):
            name: str = Field(serialization_alias="item-sku")

        app = FastAPI()

        @app.get("/out", response_model=OutM, tags=["t"])
        async def out() -> OutM:
            return OutM(name="v")

        handler = RouterGraphQLHandler(app)
        report = handler.readiness()
        assert report.degraded_fields == ()
        assert report.ready is True


class TestTypedDictKeys:
    async def test_hyphenated_typeddict_key_both_sides(self):
        # A TypedDict whose KEY string itself carries a hyphen (functional
        # form): the GraphQL name is sanitized, the resolver reads the wire
        # key on output.
        Hyphened = TypedDict("Hyphened", {"item-sku": str})  # noqa: N806

        app = FastAPI()

        @app.get("/hyph", tags=["t"])
        async def hyph() -> Hyphened:
            return {"item-sku": "wire-value"}

        handler = RouterGraphQLHandler(app)
        sdl = handler.get_sdl()
        assert "item_sku: String!" in sdl
        result = await handler.execute("{ t { hyph { item_sku } } }")
        assert result == {"data": {"t": {"hyph": {"item_sku": "wire-value"}}}}, result

    async def test_hyphenated_typeddict_input_key_translates(self, make_handler):
        """Input side of the hyphenated-key contract: the GraphQL argument
        is the sanitized item_sku and the resolver translates back to the
        wire key the TypedDict validates by."""
        from typing_extensions import TypedDict

        Hyphened = TypedDict("Hyphened", {"item-sku": str})  # noqa: N806

        app = FastAPI()
        received: dict = {}

        from tests.support.apps import add_ping

        add_ping(app)

        @app.post("/hyph", tags=["t"])
        async def hyph(payload: Hyphened) -> str:
            received.update(payload)
            return "ok"

        handler = make_handler(app, allow_mutation=True)
        result = await handler.execute(
            'mutation { t { hyph(payload: {item_sku: "abc"}) } }'
        )
        assert result == {"data": {"t": {"hyph": "ok"}}}, result
        assert received == {"item-sku": "abc"}
