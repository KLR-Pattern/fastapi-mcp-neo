"""Parameter-shape matrix: every FastAPI input flavor, scanned, schema'd and
executed end-to-end.

One route per parameter form — path (int/str/uuid, convertors live in
test_invoker's B1 suite), query (plain/list/alias/exotic scalars/parameter
models), body (single model/dict, multi-embed, embed=True, optional,
list, scalar), optional header/cookie — plus the two by-design skips
(Form/File, query-model-mixed). This matrix found the custom-scalar-in-body
JSON serialization bug; it guards scanner + invoker against regressions in
any parameter shape.

Note for readers hitting "Field must not have a selection": routes typed
``-> dict`` map to the JSON scalar, which takes NO sub-selection — queries
here select the field bare and assert on the returned dict.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Annotated, Any

import pytest
from fastapi import Body, Cookie, FastAPI, File, Form, Header, Query
from pydantic import BaseModel, ConfigDict, Field

from fastapi_mcp_neo import RouterGraphQLHandler

_UUID = "12345678-1234-5678-1234-567812345678"


class Color(Enum):
    red = "red"
    blue = "blue"


def _to_camel(s: str) -> str:
    return re.sub(r"_([a-z])", lambda m: m.group(1).upper(), s)


class AliasedIn(BaseModel):
    """Body model with alias_generator: input/output sides each name by
    their own alias (G4 pin — camelCase-API shape)."""

    model_config = ConfigDict(alias_generator=_to_camel, populate_by_name=True)
    full_name: str
    max_items: int = 5


class AliasedOut(BaseModel):
    model_config = ConfigDict(alias_generator=_to_camel, populate_by_name=True)
    full_name: str
    max_items: int = 5


class ItemIn(BaseModel):
    name: str
    price: Decimal = Field(default="9.9", description="价格")


class Filter(BaseModel):
    q: str | None = None
    limit: int = 10


def build_app() -> FastAPI:
    app = FastAPI()

    # ---- path ----
    @app.get("/p1/{item_id}", tags=["t1"])
    async def path_int(item_id: int) -> dict:
        return {"v": item_id}

    @app.get("/p2/{name}", tags=["t1"])
    async def path_str(name: str) -> dict:
        return {"v": name}

    @app.get("/p3/{uid}", tags=["t1"])
    async def path_uuid(uid: uuid.UUID) -> dict:
        return {"v": str(uid)}

    # ---- query ----
    @app.get("/r1", tags=["t2"])
    async def q_plain_opt(limit: int = 5, active: bool = True) -> dict:
        return {"limit": limit, "active": active}

    @app.get("/r2", tags=["t2"])
    async def q_required(q: str) -> dict:
        return {"v": q}

    @app.get("/r3", tags=["t2"])
    async def q_list(tag: list[str] = Query(default=[])) -> dict:
        return {"v": tag}

    @app.get("/r4", tags=["t2"])
    async def q_alias(q_limit: Annotated[int, Query(alias="limit")] = 7) -> dict:
        return {"v": q_limit}

    @app.get("/r5", tags=["t2"])
    async def q_desc(limit: Annotated[int, Query(description="max rows")] = 3) -> dict:
        return {"v": limit}

    @app.get("/r6", tags=["t2"])
    async def q_scalars(
        d: date | None = None,
        ts: datetime | None = None,
        uid2: uuid.UUID | None = None,
        c: Color | None = None,
        n: Decimal | None = None,
    ) -> dict:
        return {
            "d": str(d),
            "ts": str(ts),
            "uid": str(uid2),
            "c": c.value if c else None,
            "n": str(n),
        }

    @app.get("/r7", tags=["t2"])
    async def q_model(f: Annotated[Filter, Query()]) -> dict:
        return {"q": f.q, "limit": f.limit}

    @app.get("/r8", tags=["t2"])
    async def q_model_mixed(f: Annotated[Filter, Query()], plain: int = 1) -> dict:
        return {"kind": "mixed"}

    # ---- body ----
    @app.post("/s1", tags=["t3"])
    async def b_single_model(payload: ItemIn) -> dict:
        return {"name": payload.name, "price": str(payload.price)}

    @app.post("/s2", tags=["t3"])
    async def b_single_dict(payload: dict[str, Any]) -> dict:
        return {"v": payload}

    @app.post("/s3", tags=["t3"])
    async def b_multi(a: ItemIn, note: str) -> dict:
        return {"name": a.name, "note": note}

    @app.post("/s4", tags=["t3"])
    async def b_embed(payload: Annotated[ItemIn, Body(embed=True)]) -> dict:
        return {"name": payload.name}

    @app.post("/s5", tags=["t3"])
    async def b_optional(payload: ItemIn | None = None) -> dict:
        return {"name": payload.name if payload else None}

    @app.post("/s6", tags=["t3"])
    async def b_list(items: list[ItemIn]) -> dict:
        return {"n": len(items)}

    @app.post("/s7", tags=["t3"])
    async def b_scalar(note: str) -> dict:
        return {"note": note}

    @app.post("/s8", tags=["t3"])
    async def b_alias_model(payload: AliasedIn) -> AliasedOut:
        return AliasedOut(full_name=payload.full_name, max_items=payload.max_items)

    # ---- optional header / cookie ----
    @app.get("/o1", tags=["t4"])
    async def opt_header(x_opt: Annotated[str | None, Header()] = None) -> dict:
        return {"v": x_opt}

    @app.get("/o2", tags=["t4"])
    async def opt_cookie(sid: Annotated[str | None, Cookie()] = None) -> dict:
        return {"v": sid}

    # ---- Form/File: by-design skip ----
    @app.post("/ff", tags=["t5"])
    async def upload(name: Annotated[str, Form()], f: Annotated[bytes, File()]) -> dict:
        return {"name": name, "size": len(f)}

    return app


# (id, GraphQL document, leaf predicate) — field names are the ENDPOINT
# FUNCTION names; domains come from the tags (t1..t4).
CASES: list[tuple[str, str, str, Callable[[dict], bool]]] = [
    (
        "path-int",
        "{ t1 { path_int(item_id: 42) } }",
        "t1",
        lambda r: r["v"] == 42,
    ),
    (
        "path-str-unicode",
        '{ t1 { path_str(name: "上海") } }',
        "t1",
        lambda r: r["v"] == "上海",
    ),
    (
        "path-uuid",
        f'{{ t1 {{ path_uuid(uid: "{_UUID}") }} }}',
        "t1",
        lambda r: r["v"] == _UUID,
    ),
    (
        "query-optional-defaults",
        "{ t2 { q_plain_opt } }",
        "t2",
        lambda r: r["limit"] == 5 and r["active"] is True,
    ),
    (
        "query-required",
        '{ t2 { q_required(q: "hi") } }',
        "t2",
        lambda r: r["v"] == "hi",
    ),
    (
        "query-list",
        '{ t2 { q_list(tag: ["a", "b"]) } }',
        "t2",
        lambda r: sorted(r["v"]) == ["a", "b"],
    ),
    (
        "query-alias",
        "{ t2 { q_alias(limit: 9) } }",
        "t2",
        lambda r: r["v"] == 9,
    ),
    (
        "query-description",
        "{ t2 { q_desc(limit: 1) } }",
        "t2",
        lambda r: r["v"] == 1,
    ),
    (
        "query-exotic-scalars",
        f'{{ t2 {{ q_scalars(d: "2026-10-05", uid2: "{_UUID}", c: red, n: "1.5") }} }}',
        "t2",
        lambda r: r["d"] == "2026-10-05" and r["c"] == "red" and r["n"] == "1.5",
    ),
    (
        "query-model-expanded",
        "{ t2 { q_model(q: \"x\", limit: 2) } }",
        "t2",
        lambda r: r["q"] == "x" and r["limit"] == 2,
    ),
    (
        "body-single-model",
        'mutation { t3 { b_single_model(payload: {name: "n", price: "3.5"}) } }',
        "t3",
        lambda r: r["name"] == "n" and r["price"] == "3.5",
    ),
    (
        "body-single-dict",
        "mutation { t3 { b_single_dict(payload: {k: 1}) } }",
        "t3",
        lambda r: r["v"] == {"k": 1},
    ),
    (
        "body-multi-embedded",
        'mutation { t3 { b_multi(a: {name: "m"}, note: "x") } }',
        "t3",
        lambda r: r["name"] == "m" and r["note"] == "x",
    ),
    (
        "body-embed-true",
        'mutation { t3 { b_embed(payload: {name: "e"}) } }',
        "t3",
        lambda r: r["name"] == "e",
    ),
    (
        "body-optional-omitted",
        "mutation { t3 { b_optional } }",
        "t3",
        lambda r: r["name"] is None,
    ),
    (
        "body-list-of-models",
        'mutation { t3 { b_list(items: [{name: "a"}, {name: "b"}]) } }',
        "t3",
        lambda r: r["n"] == 2,
    ),
    (
        "body-single-scalar",
        'mutation { t3 { b_scalar(note: "s") } }',
        "t3",
        lambda r: r["note"] == "s",
    ),
    (
        "body-model-alias-generator",
        'mutation { t3 { b_alias_model(payload: {fullName: "n", maxItems: 2}) '
        "{ fullName maxItems } } }",
        "t3",
        lambda r: r == {"fullName": "n", "maxItems": 2},
    ),
    (
        "body-model-alias-defaults",
        'mutation { t3 { b_alias_model(payload: {fullName: "x"}) '
        "{ fullName maxItems } } }",
        "t3",
        lambda r: r["maxItems"] == 5,
    ),
    (
        "optional-header-defaults",
        "{ t4 { opt_header } }",
        "t4",
        lambda r: r["v"] is None,
    ),
    (
        "optional-cookie-defaults",
        "{ t4 { opt_cookie } }",
        "t4",
        lambda r: r["v"] is None,
    ),
]


@pytest.fixture
async def handler():
    h = RouterGraphQLHandler(build_app(), allow_mutation=True)
    yield h
    await h.aclose()


class TestByDesignSkips:
    def test_form_file_skipped(self, handler: RouterGraphQLHandler):
        upload_skips = [s for s in handler.skips if s.path == "/ff"]
        assert len(upload_skips) == 1
        # 'name' (the Form param) is reported first; params scan in order
        assert "form/file parameter 'name'" in upload_skips[0].reason

    def test_query_model_mixed_skipped(self, handler: RouterGraphQLHandler):
        mixed_skips = [s for s in handler.skips if s.path == "/r8"]
        assert len(mixed_skips) == 1
        assert "mixed with plain query parameters" in mixed_skips[0].reason

    def test_no_other_skips(self, handler: RouterGraphQLHandler):
        """Everything else in the matrix must be in the schema — a new
        unexpected skip is a regression."""
        unexpected = [
            s.path for s in handler.skips if s.path not in {"/ff", "/r8"}
        ]
        assert unexpected == []


class TestParameterMatrix:
    @pytest.mark.parametrize(
        "label,query,domain,predicate",
        CASES,
        ids=[c[0] for c in CASES],
    )
    async def test_case(
        self,
        handler: RouterGraphQLHandler,
        label: str,
        query: str,
        domain: str,
        predicate: Callable[[dict], bool],
    ):
        result = await handler.execute(query)
        assert "errors" not in result, f"{label}: {result.get('errors')}"
        node = result["data"][domain]
        leaf = next(iter(node.values()))
        assert leaf is not None, f"{label}: field nulled"
        assert predicate(leaf), f"{label}: value mismatch: {leaf}"


# --- gap-migration tests (models at module level: this file has
# `from __future__ import annotations`, so function-local classes would
# arrive as unresolvable ForwardRefs) -------------------------------


class _GapKind(str, Enum):
    part = "part"
    service = "service"


class _GapLineIn(BaseModel):
    sku: uuid.UUID
    kind: _GapKind = _GapKind.part
    price: Decimal


class _GapOrderIn(BaseModel):
    note: str
    lines: list[_GapLineIn]


class _GapLineOut(BaseModel):
    sku: uuid.UUID
    kind: _GapKind
    price: Decimal


class _GapOrderOut(BaseModel):
    note: str
    lines: list[_GapLineOut]


class _GapGot(BaseModel):
    uid: uuid.UUID
    at: datetime


class _GapSeen(BaseModel):
    seen: list[str] | None


class TestNestedBodyExecution:
    """Migrated from fastapi_mcp's create_order shape: a body model whose
    fields are MODELS and LISTS OF MODELS (with custom scalars inside),
    executed end to end — the type mapping is unit-tested, the round trip
    through a real document was not."""

    async def test_nested_input_literal_round_trips(self):
        app = FastAPI()

        @app.get("/ping", tags=["orders"])
        async def ping() -> dict:
            return {"ok": True}

        @app.post("/orders", tags=["orders"])
        async def create_order(payload: _GapOrderIn) -> _GapOrderOut:
            return _GapOrderOut.model_validate(
                {**payload.model_dump(), "note": payload.note.upper()}
            )

        handler = RouterGraphQLHandler(app, allow_mutation=True)
        try:
            result = await handler.execute(
                "mutation($o: _GapOrderInInput!) { orders { create_order(payload: $o)"
                " { note lines { sku kind price } } } }",
                variables={
                    "o": {
                        "note": "first",
                        "lines": [
                            {
                                "sku": "123e4567-e89b-12d3-a456-426614174000",
                                "kind": "service",
                                "price": "19.99",
                            }
                        ],
                    }
                },
            )
        finally:
            await handler.aclose()
        assert not result.get("errors"), result["errors"]
        order = result["data"]["orders"]["create_order"]
        assert order["note"] == "FIRST"
        (line,) = order["lines"]
        assert line["sku"] == "123e4567-e89b-12d3-a456-426614174000"
        assert line["kind"] == "service"
        assert line["price"] == "19.99"


class TestTypedResponseScalars:
    """A response_model with UUID/datetime fields must serialize to their
    string forms on the output side (migrated from get_product)."""

    async def test_uuid_datetime_response_fields(self):
        app = FastAPI()

        @app.get("/got/{uid}", tags=["g"])
        async def get_it(uid: uuid.UUID) -> _GapGot:
            return _GapGot(uid=uid, at=datetime(2026, 10, 25, 12, 0, 0))

        handler = RouterGraphQLHandler(app, allow_mutation=False)
        try:
            result = await handler.execute(
                "query($u: UUID!) { g { get_it(uid: $u) { uid at } } }",
                variables={"u": "123e4567-e89b-12d3-a456-426614174000"},
            )
        finally:
            await handler.aclose()
        got = result["data"]["g"]["get_it"]
        assert got["uid"] == "123e4567-e89b-12d3-a456-426614174000"
        assert got["at"].startswith("2026-10-25T12:00")


class TestWholeNoneListQuery:
    """A list query param whose DEFAULT is None (not []) must simply not
    be sent when omitted (migrated from list_products)."""

    async def test_none_list_query_omitted(self):
        app = FastAPI()

        @app.get("/ls", tags=["l"])
        async def ls(tag: list[str] | None = None) -> _GapSeen:
            return _GapSeen(seen=tag)

        handler = RouterGraphQLHandler(app, allow_mutation=False)
        try:
            omitted = await handler.execute("{ l { ls { seen } } }")
            passed = await handler.execute(
                "query($t: [String!]) { l { ls(tag: $t) { seen } } }",
                variables={"t": ["a", "b"]},
            )
        finally:
            await handler.aclose()
        assert omitted["data"]["l"]["ls"] == {"seen": None}
        assert passed["data"]["l"]["ls"] == {"seen": ["a", "b"]}
