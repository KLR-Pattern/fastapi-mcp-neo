"""scanner: FastAPI routes -> RouteInfo / SkipRecord (unit altitude).

Scan-level behavior only: discovery, filtering, skips, params, readiness.
Contracts that execute through RouterGraphQLHandler live in
integration/test_scanner_contract.py.
"""

import logging
from enum import Enum
from typing import Annotated, Any

from fastapi import APIRouter, FastAPI, Form, Query, WebSocket
from pydantic import BaseModel, Field

from fastapi_mcp_neo.scanner import RouterScanner, SkipRecord
from tests.support.apps import scanner_app
from tests.support.models import Err, ItemCreate, ItemOut


def scan(app, **kw):
    return RouterScanner(app, **kw).scan()


def by_field(routes, name):
    return next(r for r in routes if r.field_name == name)


def skip_reasons(skips, path):
    return [s.reason for s in skips if s.path == path]


class TestDiscovery:
    def test_get_routes_discovered(self):
        routes, _ = scan(scanner_app())
        names = [r.field_name for r in routes]
        assert "list_items" in names
        assert "get_item" in names  # function names, no verb/param rewriting

    def test_field_names(self):
        routes, _ = scan(scanner_app(), allow_mutation=True)
        assert by_field(routes, "list_items").path == "/items"
        assert by_field(routes, "create_item").method == "POST"

    def test_depends_query_params_merged(self):
        routes, _ = scan(scanner_app())
        r = by_field(routes, "list_items")
        qnames = [p.name for p in r.query_params]
        assert "active" in qnames and "limit" in qnames


class TestMutationGating:
    def test_mutations_skipped_by_default(self):
        routes, skips = scan(scanner_app())
        assert all(not r.is_mutation for r in routes)
        assert any("/items" in s.path and s.method == "POST" for s in skips)

    def test_mutations_included_when_allowed(self):
        routes, skips = scan(scanner_app(), allow_mutation=True)
        assert by_field(routes, "create_item").is_mutation
        assert by_field(routes, "patch_item").method == "PATCH"


class TestFiltering:
    def test_include_glob(self):
        routes, _ = scan(scanner_app(), include=["/items*"])
        assert {r.path for r in routes} <= {"/items", "/items/{item_id}"}

    def test_exclude_glob(self):
        routes, _ = scan(scanner_app(), exclude=["/ping", "/raw", "/stream"])
        assert all(r.path not in {"/ping", "/raw", "/stream"} for r in routes)

    def test_exclude_wins_over_include(self):
        routes, _ = scan(scanner_app(), include=["/items*"], exclude=["/items/{item_id}"])
        assert "/items/{item_id}" not in {r.path for r in routes}


class Color(Enum):  # non-str Enum tags must be ignored by tag filtering
    red = "red"


def build_tagged_app() -> FastAPI:
    app = FastAPI()

    @app.get("/users", response_model=ItemOut, tags=["iam:users"])
    async def users():
        return ItemOut(id=1, name="a")

    @app.get("/invoices", response_model=ItemOut, tags=["iam:billing"])
    async def invoices():
        return ItemOut(id=2, name="b")

    @app.get("/reports", response_model=ItemOut, tags=["analytics"])
    async def reports():
        return ItemOut(id=3, name="c")

    @app.get("/untagged")
    async def untagged():
        return {"ok": True}

    @app.get("/enum-tag", tags=[Color.red])
    async def enum_tag():
        return {"ok": True}

    @app.post("/users", response_model=ItemOut, tags=["iam:users"])
    async def create_user(payload: ItemCreate):
        return ItemOut(id=4, name=payload.name)

    return app


class TestTagFiltering:
    def test_include_tags_glob_prefix(self):
        routes, _ = scan(build_tagged_app(), include_tags=["iam:*"])
        assert {r.path for r in routes} == {"/users", "/invoices"}

    def test_include_tags_exact(self):
        routes, _ = scan(build_tagged_app(), include_tags=["analytics"])
        assert {r.path for r in routes} == {"/reports"}

    def test_include_tags_drops_untagged_silently(self):
        routes, skips = scan(build_tagged_app(), include_tags=["iam:*"])
        paths = {r.path for r in routes}
        assert "/untagged" not in paths and "/enum-tag" not in paths
        assert not any(s.path in {"/untagged", "/enum-tag"} for s in skips)

    def test_exclude_tags(self):
        routes, _ = scan(build_tagged_app(), exclude_tags=["iam:*"])
        assert {r.path for r in routes} == {"/reports", "/untagged", "/enum-tag"}

    def test_exclude_tags_wins_over_include_tags(self):
        routes, _ = scan(
            build_tagged_app(), include_tags=["iam:*"], exclude_tags=["iam:billing"]
        )
        assert {r.path for r in routes} == {"/users"}

    def test_tag_filters_and_with_path_filters(self):
        routes, _ = scan(
            build_tagged_app(), include=["/users", "/invoices"], include_tags=["analytics"]
        )
        assert routes == []
        routes, _ = scan(
            build_tagged_app(), include=["/users", "/reports"], include_tags=["iam:*"]
        )
        assert {r.path for r in routes} == {"/users"}

    def test_enum_tag_ignored(self):
        routes, _ = scan(build_tagged_app())  # no filter: enum-tag route scans fine
        assert "/enum-tag" in {r.path for r in routes}
        routes, _ = scan(build_tagged_app(), include_tags=["iam:*"])
        assert "/enum-tag" not in {r.path for r in routes}

    def test_include_tags_empty_drops_everything(self):
        routes, skips = scan(build_tagged_app(), include_tags=[])
        assert routes == [] and skips == []

    def test_tag_filter_precedes_mutation_gating(self):
        # POST /users is tag-filtered out before mutation gating: no skip.
        _, skips = scan(build_tagged_app(), include_tags=["iam:billing"])
        assert not any(s.method == "POST" for s in skips)
        routes, _ = scan(
            build_tagged_app(), include_tags=["iam:*"], allow_mutation=True
        )
        assert by_field(routes, "create_user").is_mutation

    def test_no_tag_skip_records(self):
        _, skips = scan(
            build_tagged_app(),
            include_tags=["iam:*"],
            exclude_tags=["iam:billing"],
        )
        assert not any("tag" in s.reason.lower() for s in skips)


class TestSkips:
    def test_untyped_response_bridges_as_any(self):
        routes, skips = scan(scanner_app())
        assert by_field(routes, "ping").response_annotation is Any
        assert not any("no typed response" in r for r in skip_reasons(skips, "/ping"))

    def test_raw_response_skipped(self):
        _, skips = scan(scanner_app())
        assert any("raw Response" in r for r in skip_reasons(skips, "/raw"))
        assert any("raw Response" in r for r in skip_reasons(skips, "/stream"))

    def test_hidden_route_skipped(self):
        _, skips = scan(scanner_app())
        assert any("hidden" in r for r in skip_reasons(skips, "/hidden"))

    def test_hidden_route_included_when_asked(self):
        routes, _ = scan(scanner_app(), include_hidden=True)
        assert any(r.path == "/hidden" for r in routes)

    def test_required_header_skipped(self):
        _, skips = scan(scanner_app())
        assert any("header/cookie" in r for r in skip_reasons(skips, "/needs-header"))

    def test_head_options_only_route_ignored(self):
        """HEAD/OPTIONS register no bridgable verb: the route vanishes
        without a skip — there is nothing the operator could fix."""
        app = FastAPI()

        @app.api_route("/x", methods=["HEAD", "OPTIONS"])
        async def x():
            return {}

        routes, skips = RouterScanner(app).scan()
        assert routes == []
        assert skips == []

    def test_form_only_scalar_skipped(self):
        """G12 regression: Annotated[str, Form()] has a perfectly valid
        GraphQL annotation, so the type check alone would let it through
        into a field that always 422s at runtime (invoker sends JSON)."""
        app = FastAPI()

        @app.post("/ff", response_model=ItemOut)
        async def ff(name: Annotated[str, Form()]):
            return ItemOut(id=1, name=name)

        routes, skips = scan(app, allow_mutation=True)
        assert routes == []
        assert any(
            "form/file parameter 'name'" in r for r in skip_reasons(skips, "/ff")
        )

    def test_optional_header_does_not_trigger_header_skip(self):
        # Optional headers are simply not sent; the route is skipped for its
        # untyped response instead, never for the optional header itself.
        _, skips = scan(scanner_app())
        assert not any("header/cookie" in r for r in skip_reasons(skips, "/optional-header"))


class TestParams:
    def test_query_param_defaults(self):
        routes, _ = scan(scanner_app())
        r = by_field(routes, "list_items")
        limit = next(p for p in r.query_params if p.name == "limit")
        assert limit.required is False
        assert limit.default == 10
        active = next(p for p in r.query_params if p.name == "active")
        assert active.required is False  # dep default True... depends' Query(True)

    def test_single_body_not_embedded(self):
        routes, _ = scan(scanner_app(), allow_mutation=True)
        r = by_field(routes, "create_item")
        assert len(r.body_params) == 1
        assert r.body_params[0].embed is False

    def test_multiple_bodies_embedded(self):
        routes, _ = scan(scanner_app(), allow_mutation=True)
        r = by_field(routes, "create_bulk")
        assert all(p.embed for p in r.body_params)

    def test_response_annotation(self):
        routes, _ = scan(scanner_app())
        assert by_field(routes, "get_item").response_annotation is ItemOut

    def test_deprecated_flag_captured(self):
        app = FastAPI()

        @app.get("/old", response_model=ItemOut, deprecated=True)
        async def old():
            return ItemOut(id=1, name="o")

        @app.get("/new", response_model=ItemOut)
        async def new():
            return ItemOut(id=2, name="n")

        routes, _ = scan(app)
        assert by_field(routes, "old").deprecated is True
        assert by_field(routes, "new").deprecated is False


class TestDomains:
    def test_tag_domains(self):
        routes, _ = scan(scanner_app())
        assert by_field(routes, "list_items").domains == frozenset({("shop", "catalog")})

    def test_untagged_falls_back_to_path(self):
        routes, _ = scan(scanner_app())
        assert by_field(routes, "health").domains == frozenset({("health",)})


class TestTypeTrials:
    def test_unsupported_response_skipped(self):
        app = FastAPI()

        @app.get("/weird")
        async def weird() -> bytes:
            # Pydantic accepts it, the bridge has no scalar for it
            return b""

        routes, skips = RouterScanner(app).scan()
        assert not routes
        assert any("unsupported type" in s.reason for s in skips)

    def test_dict_response_bridged_as_json(self):
        """dict/Any declare a dynamic shape — they pass through, not skip."""
        app = FastAPI()

        @app.get("/raw")
        async def raw() -> dict[str, int]:
            return {}

        routes, skips = RouterScanner(app).scan()
        assert len(routes) == 1 and not skips

    def test_skip_record_shape(self):
        rec = SkipRecord("/x", "GET", "why")
        assert (rec.path, rec.method, rec.reason) == ("/x", "GET", "why")


class TestInputSideUnions:
    """Request-body unions bridge as the JSON scalar, symmetric with the
    output side: the agent sends either member's JSON and FastAPI's
    validation decides (422 -> field error). Issue #3, case 4."""

    def test_input_side_union_bridges_as_json(self):
        app = FastAPI()

        @app.post("/u", tags=["u"])
        async def create(payload: ItemOut | Err) -> dict:
            return {"ok": True}

        routes, skips = RouterScanner(app, allow_mutation=True).scan()
        assert len(routes) == 1
        assert skips == []


class TestMountAndSockets:
    def test_websocket_and_mount_ignored(self):
        app = FastAPI()
        sub = FastAPI()

        @sub.get("/inner", response_model=ItemOut)
        async def inner():
            return ItemOut(id=1, name="i")

        app.mount("/sub", sub)

        @app.websocket("/ws")
        async def ws(websocket: WebSocket):
            await websocket.accept()

        routes, skips = RouterScanner(app).scan()
        assert routes == []
        assert skips == []


class TestJsonFallbackNotices:
    """Routes degraded to raw JSON (no field selection) get startup notices
    naming the CAUSE and the way out — a silent downgrade is an
    undiagnosable downgrade. Top-level union responses and serialization
    filters report per-route; union fields nested in models report per-field."""

    def test_union_response_notice_names_members_and_remedy(self, caplog):
        app = FastAPI()

        @app.get("/risky", tags=["u"])
        async def risky(ok: bool = True) -> ItemOut | Err:
            return ItemOut(id=1, name="n")

        with caplog.at_level(logging.WARNING, logger="fastapi_mcp_neo.scanner"):
            routes, _ = scan(app)
        assert len(routes) == 1  # degraded, not skipped
        rendered = caplog.text
        assert "bridged 1 route(s) as raw JSON" in rendered
        assert "GET /risky: union response (ItemOut|Err)" in rendered
        assert "restructure into one model per shape" in rendered

    def test_nested_union_field_notice_names_the_field(self, caplog):
        class Wrapped(BaseModel):
            result: ItemOut | Err

        app = FastAPI()

        @app.get("/wrapped", response_model=Wrapped, tags=["u"])
        async def wrapped() -> Wrapped:
            return Wrapped(result=ItemOut(id=1, name="w"))

        with caplog.at_level(logging.WARNING, logger="fastapi_mcp_neo.scanner"):
            scan(app)
        assert "bridged 1 model field(s) as raw JSON" in caplog.text
        assert "restructure the union away" in caplog.text
        assert "Wrapped.result (ItemOut, Err)" in caplog.text

    def test_response_filter_notice(self, caplog):
        app = FastAPI()

        @app.get("/sparse", response_model=ItemOut,
                 response_model_exclude_unset=True)
        async def sparse() -> ItemOut:
            return ItemOut(id=1, name="s")

        with caplog.at_level(logging.WARNING, logger="fastapi_mcp_neo.scanner"):
            scan(app)
        assert "response filtered via response_model_exclude_unset" in caplog.text

    def test_no_notice_for_structured_routes(self, caplog):
        app = FastAPI()

        @app.get("/clean", response_model=ItemOut)
        async def clean() -> ItemOut:
            return ItemOut(id=1, name="c")

        with caplog.at_level(logging.WARNING, logger="fastapi_mcp_neo.scanner"):
            scan(app)
        assert "raw JSON" not in caplog.text


class TestQueryParameterModels:
    def test_lone_query_model_expanded(self):
        class ItemFilter(BaseModel):
            category: str
            min_price: float = 0.0
            page_size: int = Field(default=20, validation_alias="pageSize")

        app = FastAPI()

        @app.get("/filtered", response_model=ItemOut, tags=["demo"])
        async def filtered(filters: Annotated[ItemFilter, Query()]):
            return ItemOut(id=1, name="x")

        routes, skips = RouterScanner(app).scan()
        assert not skips
        r = by_field(routes, "filtered")
        q = {p.name: p for p in r.query_params}
        assert set(q) == {"category", "min_price", "pageSize"}
        assert q["category"].required is True
        assert q["min_price"].default == 0.0
        assert q["pageSize"].default == 20

    def test_query_model_mixed_with_plain_param_skipped(self):
        class ItemFilter(BaseModel):
            category: str

        app = FastAPI()

        @app.get("/mixed", response_model=ItemOut)
        async def mixed(filters: Annotated[ItemFilter, Query()], limit: int = 5):
            return ItemOut(id=1, name="x")

        routes, skips = RouterScanner(app).scan()
        assert routes == []
        assert any("mixed with" in s.reason for s in skips)


class TestIncludeRouter:
    def test_routers_via_include_router_are_discovered(self):
        inner = APIRouter()

        @inner.get("/inner", response_model=ItemOut)
        async def inner_route():
            return ItemOut(id=1, name="i")

        outer = APIRouter(prefix="/outer")
        outer.include_router(inner)  # nested include

        @outer.get("/own", response_model=ItemOut)
        async def outer_route():
            return ItemOut(id=2, name="o")

        app = FastAPI()
        app.include_router(outer)

        routes, skips = RouterScanner(app).scan()
        assert not skips
        assert {r.path for r in routes} == {"/outer/inner", "/outer/own"}
        assert {r.field_name for r in routes} == {"inner_route", "outer_route"}


class CatOut(BaseModel):
    id: int


class DogOut(BaseModel):
    id: int


class SearchOut(BaseModel):
    hit: CatOut | DogOut  # union field inside a model: only this field degrades


def build_degraded_app() -> FastAPI:
    """One route per exposure outcome: clean, bridged (3 causes), and a
    model whose union field degrades without affecting its route."""
    app = FastAPI()

    @app.get("/clean", response_model=CatOut, tags=["shop:catalog"])
    async def clean():
        return CatOut(id=1)

    @app.get("/untyped", tags=["iam:misc"])
    async def untyped():
        return {"ok": True}

    @app.get("/filtered", response_model=list[CatOut],
             response_model_exclude_unset=True, tags=["iam:billing"])
    async def filtered():
        return [CatOut(id=2)]

    @app.get("/union", response_model=CatOut | DogOut, tags=["analytics"])
    async def union_route():
        return CatOut(id=3)

    @app.get("/search", response_model=SearchOut, tags=["shop:search"])
    async def search():
        return SearchOut(hit=CatOut(id=4))

    return app


class TestReadiness:
    def test_clean_app_ready(self):
        app = FastAPI()

        @app.get("/fine", response_model=ItemOut)
        async def fine():
            return ItemOut(id=1, name="a")

        report = RouterScanner(app).readiness()
        assert report.ready
        assert report.skips == () and report.degraded == ()
        assert report.degraded_fields == ()

    def test_bridged_reasons_name_the_cause(self):
        report = RouterScanner(build_degraded_app()).readiness()
        by_path = {b.path: b for b in report.degraded}
        assert set(by_path) == {"/untyped", "/filtered", "/union"}
        assert "no typed response" in by_path["/untyped"].reason
        assert "response_model_exclude_unset" in by_path["/filtered"].reason
        assert "union response (CatOut|DogOut)" in by_path["/union"].reason
        assert by_path["/untyped"].field_name == "untyped"

    def test_degraded_fields_carry_model_and_field(self):
        report = RouterScanner(build_degraded_app()).readiness()
        assert report.degraded_fields == (("SearchOut.hit", "CatOut, DogOut"),)

    def test_skips_section_lists_exclusions(self):
        report = RouterScanner(scanner_app()).readiness()
        paths = {s.path for s in report.skips}
        assert {"/raw", "/stream", "/needs-header", "/hidden"} <= paths
        assert not report.ready

    def test_records_carry_tags(self):
        """Skip/bridge records name the domain a route belongs to, so a
        multi-deployment review can route each finding to its owner."""
        report = RouterScanner(build_degraded_app(), allow_mutation=False).readiness()
        by_path = {b.path: b for b in report.degraded}
        assert by_path["/untyped"].tags == ("iam:misc",)
        assert by_path["/filtered"].tags == ("iam:billing",)

        report = RouterScanner(build_tagged_app()).readiness()
        rec = next(s for s in report.skips if s.path == "/users")  # POST
        assert rec.tags == ("iam:users",)

        # Enum tags stay ignored; untagged routes report no tags.
        by_path = {b.path: b.tags for b in RouterScanner(build_tagged_app()).readiness().degraded}
        assert by_path["/untagged"] == ()

        routes_scoped = RouterScanner(
            build_tagged_app(), include_tags=["iam:billing"]
        ).readiness()
        assert routes_scoped.ready

    def test_readiness_respects_filters(self):
        report = RouterScanner(build_degraded_app(), include=["/clean"]).readiness()
        assert report.ready
        # Tag filtering runs before mutation gating: POST /users (iam) drops
        # silently, and /untagged (a raw-JSON bridge) is out of the whitelist.
        report = RouterScanner(
            build_tagged_app(), include_tags=["iam:billing"]
        ).readiness()
        assert report.ready

    def test_startup_notice_texts_match_report_reasons(self, caplog):
        """The notice and the report share one classifier: every report
        reason appears verbatim in the startup warning."""
        with caplog.at_level(logging.WARNING, logger="fastapi_mcp_neo.scanner"):
            report = RouterScanner(build_degraded_app()).readiness()
        rendered = next(
            r.getMessage()
            for r in caplog.records
            if "bridged" in r.getMessage() and "route(s)" in r.getMessage()
        )
        for b in report.degraded:
            assert f"{b.method} {b.path}: {b.reason}" in rendered


class TestDeprecatedFiltering:
    def test_exclude_deprecated_drops_silently(self):
        app = FastAPI()

        @app.get("/old", deprecated=True, response_model=ItemOut)
        async def old():
            return ItemOut(id=1, name="o")

        @app.get("/new", response_model=ItemOut)
        async def new():
            return ItemOut(id=2, name="n")

        routes, skips = scan(app, exclude_deprecated=True)
        assert {r.path for r in routes} == {"/new"}
        assert not any(s.path == "/old" for s in skips)  # config drop, not a skip
