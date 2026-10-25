"""Regression suite for issue #3: type-mapping gaps (fastapi-mcp-neo#3).

Tiered by necessity — each tier is the acceptance net for one fix batch.
Every test asserts the TARGET (post-fix) behavior, so the whole file is
red until its batch lands:

- Tier 1 (P0, batch 1) — failure isolation: one bad model must skip only
  its own routes; the schema build must never crash globally.
- Tier 2 (P1, batches 2-3) — mapping capabilities: set collections,
  Literal with enum members, TypedDict.
- Tier 3 (P2, batch 4) — input-union JSON fallback (encodes option A,
  symmetric with the output side) and unbound-TypeVar diagnostics.

The issue's characterization tests (which pass on 0.9.0 by reproducing
the bugs) invert into this suite. Two existing pins flip alongside their
batches: TypeBuilder set[int] raises (test_type_builder) and input-side
unions skip (test_handler_schema.TestUnionFallback).
"""

from enum import Enum, IntEnum
from typing import Generic, Literal, Optional, TypeVar

import pytest
from fastapi import FastAPI, Query
from graphql import GraphQLInputObjectType, GraphQLObjectType
from pydantic import BaseModel, ConfigDict
from typing_extensions import TypedDict

from fastapi_mcp_neo.handler import RouterGraphQLHandler
from fastapi_mcp_neo.type_builder import (
    TypeBuilder,
    UnsupportedFieldTypeError,
    describe_literal_values,
)


class Opaque:
    """Arbitrary class: pydantic accepts it (is-instance schema, with
    arbitrary_types_allowed) but GraphQL cannot express it — and it stays
    unmapped through every planned fix batch, unlike set/Literal[enum]."""


class SharedBad(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    payload: Opaque


class InnerBad(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    payload: Opaque


class OuterGood(BaseModel):
    inner: InnerBad
    ok: int


class EmptyInner(BaseModel):
    """No fields: a whole-object failure no field fallback can express."""


class OuterE(BaseModel):
    inner: EmptyInner
    ok: int


class WeirdRecord(TypedDict):
    value: str
    blob: Opaque


class FirstMode(Enum):
    FIRST = "first"


class SecondMode(Enum):
    SECOND = "second"


class Level(IntEnum):
    LOW = 1
    HIGH = 2


class TagBag(BaseModel):
    values: set[str]


class FlatRecord(TypedDict):
    value: str


class PartialRecord(TypedDict, total=False):
    value: str


class NestedRecord(TypedDict):
    value: str


class RecordContainer(BaseModel):
    nested: NestedRecord


class Chain(TypedDict):
    label: str
    child: Optional["Chain"]  # noqa: UP045


T = TypeVar("T")


class GenericEnvelope(BaseModel, Generic[T]):
    value: T


def baseline_app() -> FastAPI:
    """One always-mappable route: the canary that must survive every skip."""
    app = FastAPI()

    @app.get("/baseline", response_model=str)
    async def baseline() -> str:
        return "ok"

    return app


def _paths(handler: RouterGraphQLHandler) -> list[str]:
    return [r.path for r in handler.routes]


def _reasons(handler: RouterGraphQLHandler) -> list[str]:
    return [s.reason for s in handler.skips]


# ---------------------------------------------------------------------- tier 1


class TestTier1FieldFallback:
    """P0 (issue #1) + batch 5: a bad FIELD never costs the route. The 0.9.0
    crash (TypeError "fields cannot be resolved") is gone at the source —
    unmappable fields degrade to JSON with a readiness-report entry and a
    startup warning; whole-object failures (empty models) still roll back
    transactionally and degrade the referencing field instead."""

    def test_shared_bad_model_keeps_both_routes_as_degraded_field(self):
        app = baseline_app()

        @app.get("/case-one", response_model=SharedBad)
        async def case_one() -> dict:
            return {"payload": Opaque()}

        @app.get("/case-two", response_model=SharedBad)
        async def case_two() -> dict:
            return {"payload": Opaque()}

        handler = RouterGraphQLHandler(app)  # 0.9.0: TypeError here
        assert handler.skips == []
        assert set(_paths(handler)) == {"/baseline", "/case-one", "/case-two"}
        # the field degrades ONCE per model (fields build once, then cache)
        assert handler.readiness().degraded_fields == (
            ("SharedBad.payload", "Opaque has no GraphQL mapping"),
        )
        assert "payload: JSON!" in handler.get_sdl()

    def test_nested_bad_field_degrades_only_that_field(self):
        """OuterGood stays fully structured: InnerBad builds with its one
        bad field bridged as JSON — the 0.9.0 cascade skip is gone."""
        app = baseline_app()

        @app.get("/outer-a", response_model=OuterGood)
        async def outer_a() -> dict:
            return {"inner": {"payload": Opaque()}, "ok": 1}

        @app.get("/inner", response_model=InnerBad)
        async def inner() -> dict:
            return {"payload": Opaque()}

        handler = RouterGraphQLHandler(app)
        assert handler.skips == []
        assert set(_paths(handler)) == {"/baseline", "/outer-a", "/inner"}
        sdl = handler.get_sdl()
        assert "inner: InnerBad!" in sdl  # OuterGood.inner keeps its structure
        assert "payload: JSON!" in sdl    # only InnerBad.payload degrades
        assert handler.readiness().degraded_fields == (
            ("InnerBad.payload", "Opaque has no GraphQL mapping"),
        )

    def test_shared_bad_input_model_degrades_input_field(self):
        """The input side degrades the same way: the argument keeps the
        SharedBadInput type, its bad field becomes a JSON argument FastAPI
        validates at call time."""
        app = baseline_app()

        @app.post("/case-one", response_model=str)
        async def case_one(payload: SharedBad) -> str:
            return "ok"

        @app.post("/case-two", response_model=str)
        async def case_two(payload: SharedBad) -> str:
            return "ok"

        handler = RouterGraphQLHandler(app, allow_mutation=True)
        assert handler.skips == []
        assert set(_paths(handler)) == {"/baseline", "/case-one", "/case-two"}
        sdl = handler.get_sdl()
        assert "payload: SharedBadInput!" in sdl
        assert "payload: JSON!" in sdl
        assert handler.readiness().degraded_fields == (
            ("SharedBad.payload", "Opaque has no GraphQL mapping"),
        )

    def test_whole_object_failure_raises_and_is_not_cached(self):
        """The transactional rollback stays as the safety net for WHOLE-
        object failures (which no field fallback can express): an empty
        model raises, rolls its registration back, and a retry fails afresh
        — never a cached half-built type."""
        builder = TypeBuilder()
        with pytest.raises(UnsupportedFieldTypeError, match="no usable fields"):
            builder.output_type(EmptyInner)
        with pytest.raises(UnsupportedFieldTypeError):
            builder.output_type(EmptyInner)
        with pytest.raises(UnsupportedFieldTypeError, match="no usable input fields"):
            builder.input_type(EmptyInner)

    def test_nested_empty_model_degrades_referencing_field(self):
        """Rollback exercised end-to-end: EmptyInner registers, fails 'no
        usable fields', rolls back — OuterE.inner then degrades to JSON
        instead of skipping the route."""
        app = baseline_app()

        @app.get("/outer-e", response_model=OuterE)
        async def outer_e() -> dict:
            return {"inner": {}, "ok": 1}

        handler = RouterGraphQLHandler(app)
        assert handler.skips == []
        assert "/outer-e" in _paths(handler)
        sdl = handler.get_sdl()
        assert "inner: JSON!" in sdl
        assert "ok: Int!" in sdl
        assert handler.readiness().degraded_fields == (
            ("OuterE.inner", "EmptyInner has no usable fields"),
        )

    def test_unmappable_typeddict_field_degrades(self):
        """TypedDict fields ride the same fallback, input and output."""
        builder = TypeBuilder()
        obj = builder.output_type(WeirdRecord).of_type
        assert str(obj.fields["value"].type) == "String!"
        assert str(obj.fields["blob"].type) == "JSON!"
        inp = builder.input_type(WeirdRecord)
        assert str(inp.fields["value"].type) == "String!"
        assert str(inp.fields["blob"].type) == "JSON!"
        # output + input builds of the same field record ONE report entry
        assert builder.degraded_fields == [
            ("WeirdRecord.blob", "Opaque has no GraphQL mapping")
        ]


# ---------------------------------------------------------------------- tier 2


class TestTier2SetCollections:
    """P1 (issue #3): set-like collections map onto GraphQL lists — the
    JSON wire format pydantic emits for a set IS an array."""

    def test_set_output_type(self):
        assert str(TypeBuilder().output_type(set[str])) == "[String!]!"

    def test_frozenset_output_type(self):
        assert str(TypeBuilder().output_type(frozenset[int])) == "[Int!]!"

    def test_optional_set_output_type(self):
        assert str(TypeBuilder().output_type(set[str] | None)) == "[String!]"

    def test_set_input_type(self):
        assert str(TypeBuilder().input_type(set[str])) == "[String!]"

    def test_set_response_and_field_build(self):
        app = baseline_app()

        @app.get("/case", response_model=set[str])
        async def case() -> set[str]:
            return {"value"}

        @app.get("/bag", response_model=TagBag)
        async def bag() -> TagBag:
            return TagBag(values={"a"})

        handler = RouterGraphQLHandler(app)
        assert handler.skips == []
        sdl = handler.get_sdl()
        assert "case: [String!]" in sdl
        assert "values: [String!]!" in sdl

    async def test_set_response_executes_as_json_array(self):
        app = baseline_app()

        @app.get("/case", response_model=set[str], tags=["g"])
        async def case() -> set[str]:
            return {"value"}

        handler = RouterGraphQLHandler(app)
        result = await handler.execute("{ g { case } }")
        assert result == {"data": {"g": {"case": ["value"]}}}
        await handler.aclose()

    @staticmethod
    def _set_query_app() -> FastAPI:
        app = baseline_app()

        @app.get("/q", response_model=str, tags=["g"])
        async def q(tags: set[str] = Query()) -> str:
            return ",".join(sorted(tags))

        return app

    def test_set_query_param_builds_list_argument(self):
        handler = RouterGraphQLHandler(self._set_query_app())
        assert handler.skips == []
        assert "q(tags: [String!]!): String" in handler.get_sdl()

    async def test_set_query_param_executes(self):
        handler = RouterGraphQLHandler(self._set_query_app())
        result = await handler.execute('{ g { q(tags: ["a", "b"]) } }')
        assert result == {"data": {"g": {"q": "a,b"}}}
        await handler.aclose()


class TestTier2LiteralEnumMembers:
    """P1 (issue #2): a Literal member that is an enum instance normalizes
    to its underlying value, then rides the existing scalar path."""

    def test_str_enum_member_literal_maps_to_string(self):
        assert str(TypeBuilder().output_type(Literal[FirstMode.FIRST])) == "String!"

    def test_int_enum_member_literal_maps_to_int(self):
        assert str(TypeBuilder().output_type(Literal[Level.HIGH])) == "Int!"

    def test_enum_member_mixed_with_raw_value(self):
        # FirstMode.FIRST normalizes to "first": one member type, String
        assert str(
            TypeBuilder().output_type(Literal[FirstMode.FIRST, "extra"])
        ) == "String!"

    def test_description_renders_normalized_value(self):
        assert (
            describe_literal_values(Literal[FirstMode.FIRST])
            == "Allowed values: 'first'"
        )

    def test_literal_enum_input(self):
        assert str(TypeBuilder().input_type(Literal[FirstMode.FIRST])) == "String"

    def test_literal_enum_response_builds(self):
        app = baseline_app()

        @app.get("/case", response_model=Literal[FirstMode.FIRST], tags=["g"])
        async def case() -> FirstMode:
            return FirstMode.FIRST

        handler = RouterGraphQLHandler(app)
        assert handler.skips == []
        sdl = handler.get_sdl()
        assert "case: String" in sdl
        assert "Allowed values: 'first'" in sdl

    async def test_literal_enum_response_executes(self):
        app = baseline_app()

        @app.get("/case", response_model=Literal[FirstMode.FIRST], tags=["g"])
        async def case() -> FirstMode:
            return FirstMode.FIRST

        handler = RouterGraphQLHandler(app)
        result = await handler.execute("{ g { case } }")
        assert result == {"data": {"g": {"case": "first"}}}
        await handler.aclose()


class TestTier2TypedDict:
    """P1 (issue #6): TypedDict is a structural declaration — it maps onto
    real object/input types, with nullability from __required_keys__ /
    __optional_keys__ (total=False => all optional)."""

    def test_flat_typeddict_output(self):
        obj = TypeBuilder().output_type(FlatRecord).of_type
        assert isinstance(obj, GraphQLObjectType)
        assert obj.name == "FlatRecord"
        assert str(obj.fields["value"].type) == "String!"

    def test_total_false_fields_nullable(self):
        obj = TypeBuilder().output_type(PartialRecord).of_type
        assert str(obj.fields["value"].type) == "String"

    def test_typeddict_input(self):
        inp = TypeBuilder().input_type(FlatRecord)
        assert isinstance(inp, GraphQLInputObjectType)
        assert inp.name == "FlatRecordInput"
        assert str(inp.fields["value"].type) == "String!"

    def test_nested_typeddict_inside_model(self):
        obj = TypeBuilder().output_type(RecordContainer).of_type
        assert str(obj.fields["nested"].type) == "NestedRecord!"

    def test_recursive_typeddict(self):
        obj = TypeBuilder().output_type(Chain).of_type
        assert str(obj.fields["label"].type) == "String!"
        assert str(obj.fields["child"].type) == "Chain"

    def test_typeddict_routes_build(self):
        app = baseline_app()

        @app.get("/case-flat", response_model=FlatRecord, tags=["g"])
        async def case_flat() -> dict:
            return {"value": "flat"}

        @app.get("/case-nested", response_model=RecordContainer, tags=["g"])
        async def case_nested() -> dict:
            return {"nested": {"value": "nested"}}

        handler = RouterGraphQLHandler(app)
        assert handler.skips == []
        sdl = handler.get_sdl()
        assert "type FlatRecord" in sdl
        assert "nested: NestedRecord!" in sdl

    @staticmethod
    def _typeddict_app() -> FastAPI:
        app = baseline_app()

        @app.get("/case-nested", response_model=RecordContainer, tags=["g"])
        async def case_nested() -> dict:
            return {"nested": {"value": "nested"}}

        @app.get("/partial", response_model=PartialRecord, tags=["g"])
        async def partial() -> dict:
            return {}

        @app.post("/submit", response_model=str, tags=["g"])
        async def submit(payload: FlatRecord) -> str:
            return payload["value"]

        return app

    async def test_nested_typeddict_executes(self):
        handler = RouterGraphQLHandler(self._typeddict_app(), allow_mutation=True)
        result = await handler.execute("{ g { case_nested { nested { value } } } }")
        assert result == {"data": {"g": {"case_nested": {"nested": {"value": "nested"}}}}}
        await handler.aclose()

    async def test_total_false_missing_key_resolves_null(self):
        handler = RouterGraphQLHandler(self._typeddict_app(), allow_mutation=True)
        result = await handler.execute("{ g { partial { value } } }")
        assert result == {"data": {"g": {"partial": {"value": None}}}}
        await handler.aclose()

    async def test_typeddict_body_param_executes(self):
        handler = RouterGraphQLHandler(self._typeddict_app(), allow_mutation=True)
        result = await handler.execute(
            'mutation { g { submit(payload: {value: "x"}) } }'
        )
        assert result == {"data": {"g": {"submit": "x"}}}
        await handler.aclose()


# ---------------------------------------------------------------------- tier 3


class TestTier3InputUnionFallback:
    """P2 (issue #4): input unions bridge as the JSON scalar — option A,
    symmetric with the output side's union fallback. NOTE: this reverses
    TestUnionFallback.test_input_side_union_still_unsupported in
    test_handler_schema.py (a documented 0.9.0 decision); that pin flips
    when this batch lands. Encodes the recommended option — flip both if
    the design call goes the other way (merged input enum)."""

    def test_union_of_enums_query_param_bridges_as_json(self):
        app = baseline_app()

        @app.get("/case", response_model=str, tags=["g"])
        async def case(value: FirstMode | SecondMode = Query()) -> str:
            return value.value

        handler = RouterGraphQLHandler(app)
        assert handler.skips == []
        assert "case(value: JSON!): String" in handler.get_sdl()

    @staticmethod
    def _union_app() -> FastAPI:
        app = baseline_app()

        @app.get("/case", response_model=str, tags=["g"])
        async def case(value: FirstMode | SecondMode = Query()) -> str:
            return value.value

        @app.post("/echo", response_model=str, tags=["g"])
        async def echo(payload: FirstMode | SecondMode) -> str:
            return payload.value

        return app

    async def test_union_query_param_executes(self):
        handler = RouterGraphQLHandler(self._union_app(), allow_mutation=True)
        result = await handler.execute('{ g { case(value: "first") } }')
        assert result == {"data": {"g": {"case": "first"}}}
        await handler.aclose()

    async def test_union_body_param_bridges_and_executes(self):
        handler = RouterGraphQLHandler(self._union_app(), allow_mutation=True)
        sdl = handler.get_sdl()
        assert handler.skips == []
        assert "echo(payload: JSON!): String" in sdl
        result = await handler.execute('mutation { g { echo(payload: "second") } }')
        assert result == {"data": {"g": {"echo": "second"}}}
        await handler.aclose()


class TestUnionDegradationSymmetry:
    """A union FIELD degrades to raw JSON identically on every side —
    model output/input, TypedDict output/input — with a degraded_fields
    record and a schema note. The readiness report must never depend on
    which side of the API the field happens to sit on."""

    async def test_input_model_field_union_recorded(self):
        class UA(BaseModel):
            a: int = 1

        class UB(BaseModel):
            b: int = 2

        class OutM(BaseModel):
            payload: "UA | UB" = UA()

        class InM(BaseModel):
            payload: "UA | UB" = UA()

        OutM.model_rebuild()
        InM.model_rebuild()

        app = FastAPI()

        @app.get("/out", response_model=OutM, tags=["g"])
        async def out():
            return OutM()

        @app.post("/echo", tags=["g"])
        async def echo(item: InM) -> OutM:
            return OutM()

        handler = RouterGraphQLHandler(app, allow_mutation=True)
        assert handler.readiness().degraded_fields == (
            ("OutM.payload", "UA, UB"),
            ("InM.payload", "UA, UB"),
        )
        sdl = handler.get_sdl()
        # both sides carry the union note in their field descriptions
        assert sdl.count("shape is one of: UA, UB") == 2, sdl

    async def test_typeddict_field_unions_recorded(self):
        class TDA(TypedDict):
            a: int

        class TDB(TypedDict):
            b: int

        class OutTd(TypedDict):
            payload: TDA | TDB  # unquoted: local scopes cannot resolve
            # string annotations, and TypedDicts have no model_rebuild

        class InTd(TypedDict):
            payload: TDA | TDB

        app = FastAPI()

        @app.get("/out", response_model=OutTd, tags=["g"])
        async def out():
            return {"payload": {"a": 1}}

        @app.post("/echo", tags=["g"])
        async def echo(item: InTd) -> OutTd:
            return {"payload": {"b": 2}}

        handler = RouterGraphQLHandler(app, allow_mutation=True)
        assert handler.readiness().degraded_fields == (
            ("OutTd.payload", "TDA, TDB"),
            ("InTd.payload", "TDA, TDB"),
        )
        sdl = handler.get_sdl()
        assert sdl.count("shape is one of: TDA, TDB") == 2, sdl


class TestTier3UnboundTypeVar:
    """P2 (issue #5): an unparameterized generic degrades only its own
    field, with a diagnostic the author can act on — the model, the
    unbound TypeVar, and the way out, not just a bare `~T`."""

    def test_unparameterized_generic_degrades_with_actionable_reason(self):
        app = baseline_app()

        @app.get("/case", response_model=GenericEnvelope, tags=["g"])
        async def case() -> dict:
            return {"value": "unbound"}

        handler = RouterGraphQLHandler(app)
        assert handler.skips == []
        assert "/case" in _paths(handler)
        assert "value: JSON!" in handler.get_sdl()
        reasons = [reason for _path, reason in handler.readiness().degraded_fields]
        assert any(
            "GenericEnvelope" in r and "unbound TypeVar" in r and "parameterize" in r
            for r in reasons
        )


class PairItem(BaseModel):
    name: str


class TestHeterogeneousTuples:
    """Migrated from fastmcp's tuple schemas: a FIXED tuple with differing
    position types cannot be a GraphQL list (the list would promise one
    element type; the old mapping took args[0] and silently dropped the
    rest). Variadic tuple[X, ...] and all-same fixed tuples ARE
    wire-accurate as lists (pydantic serializes tuples as arrays)."""

    async def test_heterogeneous_return_degrades_to_json(self):
        app = FastAPI()

        @app.get("/pair", tags=["tp"])
        async def pair() -> tuple[PairItem, int]:
            return (PairItem(name="x"), 2)

        handler = RouterGraphQLHandler(app, allow_mutation=False)
        try:
            sdl = handler.get_sdl()
            assert "pair: JSON" in sdl
            result = await handler.execute("{ tp { pair } }")
            assert result["data"]["tp"]["pair"] == [{"name": "x"}, 2]
        finally:
            await handler.aclose()

    async def test_heterogeneous_body_degrades_to_json(self):
        app = FastAPI()

        @app.get("/ping", tags=["tp"])
        async def ping() -> dict:
            return {"ok": True}

        @app.post("/pairin", tags=["tp"])
        async def pairin(value: tuple[PairItem, int]) -> dict:
            return {"name": value[0].name, "count": value[1]}

        handler = RouterGraphQLHandler(app, allow_mutation=True)
        try:
            sdl = handler.get_sdl()
            assert "pairin(value: JSON" in sdl
            result = await handler.execute(
                'mutation($v: JSON!) { tp { pairin(value: $v) } }',
                variables={"v": [{"name": "y"}, 5]},
            )
            assert result["data"]["tp"]["pairin"] == {"name": "y", "count": 5}
        finally:
            await handler.aclose()

    def test_variadic_and_uniform_tuples_stay_lists(self):
        app = FastAPI()

        @app.get("/variadic", tags=["tp"])
        async def variadic() -> tuple[str, ...]:
            return ("a",)

        @app.get("/uniform", tags=["tp"])
        async def uniform() -> tuple[int, int]:
            return (1, 2)

        handler = RouterGraphQLHandler(app, allow_mutation=False)
        sdl = handler.get_sdl()
        assert "variadic: [String!]" in sdl
        assert "uniform: [Int!]" in sdl  # wire-accurate: pydantic emits arrays
