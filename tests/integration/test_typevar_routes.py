"""Route-boundary TypeVars: normalize when the constraints/bound say what
the type IS, degrade to raw JSON when nothing does — never skip (issue #5).

Inside a model, a TypeVar field already degrades to the JSON scalar; at a
route boundary the same TypeVar used to remove the whole route. A
constrained TypeVar is pydantic's spelling of "one of these" and a bound
TypeVar of "this or narrower" — both normalize before mapping, so they
map exactly like the union / bound type would.
"""

from typing import Generic, TypeVar

from fastapi import FastAPI
from pydantic import BaseModel

from fastapi_mcp_neo.handler import RouterGraphQLHandler
from tests.support.apps import add_ping


class TextUnit(BaseModel):
    value: str


class NumberUnit(BaseModel):
    value: int


ConstrainedT = TypeVar("ConstrainedT", TextUnit, NumberUnit)
BoundT = TypeVar("BoundT", bound=TextUnit)
BareT = TypeVar("BareT")


class TestNormalization:
    async def test_constrained_typevar_output_route_survives(self):
        """list[list[T]] with T constrained to two models: the constraints
        normalize to a union, whose elements bridge as JSON — the route
        stays callable instead of disappearing."""
        app = FastAPI()

        @app.get("/nested", response_model=list[list[ConstrainedT]], tags=["t"])
        async def nested() -> list:
            return [[{"value": "ok"}]]

        handler = RouterGraphQLHandler(app)
        assert handler.skips == []
        sdl = handler.get_sdl()
        assert "nested: [[JSON!]!]" in sdl
        result = await handler.execute("{ t { nested } }")
        assert result == {"data": {"t": {"nested": [[{"value": "ok"}]]}}}, result

    async def test_constrained_typevar_input_route_survives(self):
        app = FastAPI()
        add_ping(app)

        @app.post("/accept", tags=["t"])
        async def accept(payload: list[list[ConstrainedT]]) -> int:
            return len(payload[0][0].value)  # pydantic 实例化为 TextUnit

        handler = RouterGraphQLHandler(app, allow_mutation=True)
        assert handler.skips == []
        result = await handler.execute(
            'mutation { t { accept(payload: [[{ value: "abcd" }]]) } }'
        )
        assert result == {"data": {"t": {"accept": 4}}}, result

    async def test_bound_typevar_maps_structured(self):
        """A bound TypeVar normalizes to its bound: full field selection,
        no degradation anywhere."""
        app = FastAPI()

        @app.get("/bound", response_model=list[BoundT], tags=["t"])
        async def bound() -> list:
            return [{"value": "ok"}]

        handler = RouterGraphQLHandler(app)
        assert handler.skips == []
        report = handler.readiness()
        assert report.degraded == ()
        result = await handler.execute("{ t { bound { value } } }")
        assert result == {"data": {"t": {"bound": [{"value": "ok"}]}}}, result

    async def test_constrained_typevar_field_records_as_union(self):
        """Inside a model, a constrained TypeVar field now bridges with the
        union note (it IS pydantic's union spelling) instead of the
        unbound-TypeVar guidance — the audit names the real shape."""

        class Envelope(BaseModel, Generic[ConstrainedT]):
            item: ConstrainedT

        app = FastAPI()

        @app.get("/env", response_model=Envelope, tags=["t"])
        async def env() -> dict:
            return {"item": {"value": "ok"}}

        handler = RouterGraphQLHandler(app)
        assert handler.skips == []
        assert handler.readiness().degraded_fields == (
            ("Envelope.item", "TextUnit, NumberUnit"),
        )


class TestUnboundedFallback:
    async def test_bare_typevar_route_degrades_not_skips(self):
        """A fully unbound TypeVar at a route boundary degrades the route
        to raw JSON with the parameterize guidance — same fallback a
        TypeVar field gets inside a model; readiness distinguishes the
        degradation from a removal."""
        app = FastAPI()
        add_ping(app)

        @app.get("/out", response_model=list[BareT], tags=["t"])
        async def out() -> list:
            return [{"anything": 1}]

        @app.post("/in", response_model=str, tags=["t"])
        async def in_(payload: list[BareT]) -> str:
            return "ok"

        handler = RouterGraphQLHandler(app, allow_mutation=True)
        routes = {(r.method, r.path) for r in handler.routes}
        assert ("GET", "/out") in routes and ("POST", "/in") in routes
        assert handler.skips == []

        report = handler.readiness()
        assert report.skips == ()
        reasons = [r.reason for r in report.degraded]
        assert any("response of /out" in r and "unbound TypeVar" in r for r in reasons), reasons
        assert any(
            "parameter 'payload'" in r and "unbound TypeVar" in r for r in reasons
        ), reasons
        # the parameterize guidance rides along
        assert any("parameterize" in r for r in reasons), reasons

        # the SDL note carries the REAL reason (single-classifier fix): a
        # TypeVar-degraded route must not point agents at "add a return
        # annotation" guidance for an endpoint that declares one.
        sdl = handler.get_sdl()
        out_line = next(ln for ln in sdl.splitlines() if "out:" in ln and "JSON" in ln)
        assert "unbound TypeVar" in out_line, out_line
        assert "declares no response type" not in sdl

        # and the degraded routes still execute end to end
        result = await handler.execute("{ t { out } }")
        assert result == {"data": {"t": {"out": [{"anything": 1}]}}}, result
