"""Dataclass support: stdlib @dataclass maps like a model, both sides.

FastAPI treats dataclasses as first-class citizens (pydantic wraps them
for validation and serialization), and the invoker rides the real ASGI
app — so the runtime path is native. These tests pin the SCHEMA layer:
field extraction, nullability, defaults, nesting, recursion, and the
degradation audit.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar, Optional

import pytest
from fastapi import FastAPI
from pydantic import BaseModel

from fastapi_mcp_neo.handler import RouterGraphQLHandler
from fastapi_mcp_neo.type_builder import UnsupportedFieldTypeError


class Mode(Enum):
    FAST = "fast"
    SLOW = "slow"


@dataclass
class Link:
    name: str
    next: Optional["Link"] = None


@dataclass
class Item:
    name: str
    price: int = 10
    note: str | None = None
    tags: list[str] = field(default_factory=list)
    mode: Mode = Mode.FAST
    legacy: ClassVar[str] = "excluded"  # ClassVar never becomes a field


class TestOutput:
    async def test_fields_nullability_and_execution(self):
        app = FastAPI()

        @app.get("/item", response_model=Item, tags=["t"])
        async def item() -> Item:
            return Item(name="widget")

        handler = RouterGraphQLHandler(app)
        sdl = handler.get_sdl()
        # output nullability follows the ANNOTATION only: a defaulted plain
        # field is always materialized (non-null), Optional is nullable
        assert "name: String!" in sdl
        assert "price: Int!" in sdl
        assert "note: String" in sdl and "note: String!" not in sdl
        assert "tags: [String!]!" in sdl
        assert "mode: Mode!" in sdl
        assert "legacy" not in sdl  # ClassVar excluded

        result = await handler.execute(
            "{ t { item { name price note tags mode } } }"
        )
        assert result == {
            "data": {"t": {"item": {
                "name": "widget", "price": 10, "note": None,
                "tags": [], "mode": "FAST",
            }}}
        }, result

    async def test_nested_in_pydantic_and_vice_versa(self):
        @dataclass
        class Point:
            x: int
            y: int

        class Box(BaseModel):
            label: str
            corner: Point

        @dataclass
        class Shipment:
            box: Box
            weight: float

        app = FastAPI()

        @app.get("/shipment", response_model=Shipment, tags=["t"])
        async def shipment() -> Shipment:
            return Shipment(box=Box(label="l", corner=Point(x=1, y=2)), weight=1.5)

        handler = RouterGraphQLHandler(app)
        result = await handler.execute(
            "{ t { shipment { weight box { label corner { x y } } } } }"
        )
        assert result == {
            "data": {"t": {"shipment": {
                "weight": 1.5, "box": {"label": "l", "corner": {"x": 1, "y": 2}},
            }}}
        }, result

    async def test_recursive_dataclass_gets_true_depth(self):
        app = FastAPI()

        @app.get("/chain", response_model=Link, tags=["t"])
        async def chain() -> Link:
            node: Link = Link(name="leaf")
            for i in range(4):
                node = Link(name=f"n{i}", next=node)
            return node

        handler = RouterGraphQLHandler(app)
        sdl = handler.get_sdl()
        assert "full subtree at true depth" in sdl  # recursive contract note
        result = await handler.execute("{ t { chain { name next { name } } } }")

        def depth(node: dict) -> int:
            return 1 + (depth(node["next"]) if node.get("next") else 0)

        assert "errors" not in result, result
        assert depth(result["data"]["t"]["chain"]) == 5

    async def test_union_field_degrades_with_record(self):
        class A(BaseModel):
            a: int = 1

        class B(BaseModel):
            b: int = 2

        @dataclass
        class HasUnion:
            payload: A | B = field(default_factory=A)

        app = FastAPI()

        @app.get("/u", response_model=HasUnion, tags=["t"])
        async def u() -> HasUnion:
            return HasUnion()

        handler = RouterGraphQLHandler(app)
        assert handler.readiness().degraded_fields == (("HasUnion.payload", "A, B"),)
        sdl = handler.get_sdl()
        assert "shape is one of: A, B" in sdl


class TestInput:
    async def test_input_type_defaults_and_execution(self):
        app = FastAPI()

        @app.get("/ping", response_model=str, tags=["t"])
        async def ping() -> str:
            return "pong"

        @app.post("/create", tags=["t"])
        async def create(item: Item) -> dict:
            return {"made": item.name, "at": item.price, "mode": item.mode.value}

        handler = RouterGraphQLHandler(app, allow_mutation=True)
        sdl = handler.get_sdl()
        assert "input ItemInput" in sdl
        assert "name: String!" in sdl  # required
        assert "price: Int = 10" in sdl  # literal default carried
        assert "note: String = null" in sdl
        # enum defaults are dropped, matching the model path (_input_default
        # passes only scalar literals): the agent omits the argument and
        # FastAPI applies the dataclass default
        assert "\n  mode: Mode\n" in sdl

        result = await handler.execute(
            'mutation { t { create(item: { name: "x" }) } }'
        )
        assert result == {"data": {"t": {"create": {
            "made": "x", "at": 10, "mode": "fast",
        }}}}, result

        # a missing required field is a validation error, not a crash
        bad = await handler.execute("mutation { t { create(item: {}) } }")
        assert "errors" in bad

    async def test_dataclass_inside_model_body(self):
        app0 = FastAPI()

        @app0.get("/ping", response_model=str, tags=["t"])
        async def ping() -> str:
            return "pong"

        @dataclass
        class Size:
            w: int
            h: int

        class Order(BaseModel):
            label: str
            size: Size

        app = app0

        @app.post("/order", tags=["t"])
        async def order(body: Order) -> str:
            return f"{body.label}:{body.size.w}x{body.size.h}"

        handler = RouterGraphQLHandler(app, allow_mutation=True)
        result = await handler.execute(
            'mutation { t { order(body: { label: "L", size: { w: 3, h: 4 } }) } }'
        )
        assert result == {"data": {"t": {"order": "L:3x4"}}}, result


class TestBoundaries:
    async def test_empty_dataclass_degrades_referencing_field(self):
        @dataclass
        class Empty:
            pass

        class Holder(BaseModel):
            nothing: Empty | None = None

        app = FastAPI()

        @app.get("/h", response_model=Holder, tags=["t"])
        async def h() -> Holder:
            return Holder()

        handler = RouterGraphQLHandler(app)
        assert handler.skips == []  # the route survives…
        # …the field degrades with the ready-made reason
        assert handler.readiness().degraded_fields == (
            ("Holder.nothing", "Empty has no usable fields"),
        )

    async def test_passthrough_json_still_works(self):
        @dataclass
        class WithJson:
            name: str
            payload: dict[str, Any]

        app = FastAPI()

        @app.get("/j", response_model=WithJson, tags=["t"])
        async def j() -> WithJson:
            return WithJson(name="x", payload={"k": [1, 2]})

        handler = RouterGraphQLHandler(app)
        result = await handler.execute("{ t { j { name payload } } }")
        assert result == {"data": {"t": {"j": {
            "name": "x", "payload": {"k": [1, 2]},
        }}}}, result


@dataclass
class ChoiceA:
    a: int


@dataclass
class ChoiceB:
    b: int


@dataclass
class Cmd:
    # module level on purpose: the union's forward ref must resolve, and
    # function-local dataclasses hide their names from get_type_hints
    name: str
    blob: bytes
    either: "ChoiceA | ChoiceB"


class TestDegradedDataclassFields:
    """Unmappable and union dataclass fields degrade per-field, on both
    sides, and the readiness report names each one — same audit contract
    as Pydantic models."""

    @staticmethod
    def _app():
        app = FastAPI()

        @app.get("/ping", response_model=str, tags=["t"])
        async def ping() -> str:
            return "pong"

        @app.get("/blob", tags=["t"])
        async def blob() -> Cmd:
            return Cmd(name="x", blob=b"y", either=ChoiceA(a=1))

        @app.post("/cmd", tags=["t"])
        async def cmd(payload: Cmd) -> dict:
            return {}

        return app

    async def test_output_degraded_field_bridges_with_record(self, make_handler):
        handler = make_handler(self._app())
        sdl = handler.get_sdl()
        assert "blob: JSON" in sdl and "either: JSON" in sdl
        assert "name: String!" in sdl  # siblings stay structured
        assert handler.readiness().degraded_fields == (
            ("Cmd.blob", "bytes has no GraphQL mapping"),
            ("Cmd.either", "ChoiceA, ChoiceB"),
        )

    async def test_input_degraded_fields_execute_via_json(self, make_handler):
        handler = make_handler(self._app(), allow_mutation=True)
        result = await handler.execute(
            'mutation { t { cmd(payload: {name: "n", blob: "aGk=", '
            'either: {a: 1}}) } }'
        )
        assert result == {"data": {"t": {"cmd": {}}}}, result


class TestEmptyDataclass:
    def test_empty_dataclass_input_raises(self):
        from fastapi_mcp_neo.type_builder import TypeBuilder

        @dataclass
        class Empty:
            pass

        with pytest.raises(UnsupportedFieldTypeError, match="no usable input fields"):
            TypeBuilder().input_type(Empty)
