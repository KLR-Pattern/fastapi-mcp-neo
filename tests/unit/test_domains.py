"""domains: tag tree registry."""

from fastapi import FastAPI
from pydantic import BaseModel

from fastapi_mcp_neo.domains import DomainRegistry, domains_for
from fastapi_mcp_neo.scanner import RouterScanner


class Out(BaseModel):
    id: int


def make_app() -> FastAPI:
    app = FastAPI()

    @app.get("/invoices", response_model=Out, tags=["billing:invoice"])
    async def invoices():
        return Out(id=1)

    @app.get("/refunds", response_model=Out, tags=["billing:refund"])
    async def refunds():
        return Out(id=2)

    @app.get("/reports", response_model=Out, tags=["billing", "analytics"])
    async def reports():
        return Out(id=3)

    @app.post("/payments", response_model=Out, tags=["billing:payment"])
    async def payments():
        return Out(id=4)

    @app.get("/users/{user_id}", response_model=Out)
    async def users(user_id: int):
        return Out(id=user_id)

    @app.get("/", response_model=Out)
    async def root():
        return Out(id=0)

    return app


def registry() -> DomainRegistry:
    routes, _ = RouterScanner(make_app(), allow_mutation=True).scan()
    return DomainRegistry(routes)


class TestDomainsFor:
    def test_colon_splits_levels(self):
        assert domains_for(["billing:invoice"], "/x") == frozenset(
            {("billing", "invoice")}
        )

    def test_multi_tag_multi_domain(self):
        assert domains_for(["a", "b:c"], "/x") == frozenset({("a",), ("b", "c")})

    def test_untagged_first_segment(self):
        assert domains_for([], "/Users/{id}") == frozenset({("users",)})

    def test_root_falls_back_to_general(self):
        assert domains_for([], "/") == frozenset({("general",)})

    def test_segments_sanitized(self):
        assert domains_for(["Billing-Admin"], "/x") == frozenset({("billing_admin",)})


class TestRegistry:
    def test_tree_structure(self):
        reg = registry()
        assert reg.children(()) == ["analytics", "billing", "general", "users"]
        assert reg.children(("billing",)) == ["invoice", "payment", "refund"]

    def test_leaf_fields(self):
        reg = registry()
        invoice = reg.node(("billing", "invoice"))
        assert invoice is not None
        assert invoice.query_fields == {(("billing", "invoice"), "invoices")}

    def test_multi_tag_route_in_both_domains(self):
        reg = registry()
        analytics = reg.node(("analytics",))
        assert analytics is not None
        assert (("analytics",), "reports") in analytics.query_fields
        billing_root = reg.node(("billing",))
        assert billing_root is not None
        assert (("billing",), "reports") in billing_root.query_fields  # tagged 'billing' too

    def test_subtree_aggregates(self):
        reg = registry()
        queries, mutations = reg.subtree_fields(("billing",))
        assert (("billing", "invoice"), "invoices") in queries
        assert (("billing",), "reports") in queries
        assert mutations == {(("billing", "payment"), "payments")}

    def test_summary_shape(self):
        summary = registry().summary()
        billing = next(s for s in summary if s["name"] == "billing")
        assert billing["queries"] == 3  # invoices, refunds, reports
        assert billing["mutations"] == 1
        assert billing["subdomains"] == ["invoice", "payment", "refund"]

    def test_untagged_and_root(self):
        reg = registry()
        users = reg.node(("users",))
        assert users is not None and users.query_fields == {(("users",), "users")}
        general = reg.node(("general",))
        assert general is not None and general.query_fields == {(("general",), "root")}

    def test_empty_routes(self):
        reg = DomainRegistry([])
        assert reg.summary() == []
        assert reg.children(()) == []
