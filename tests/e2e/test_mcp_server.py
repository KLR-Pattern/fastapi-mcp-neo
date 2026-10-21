"""FastAPIMCP server behavior: mode resolution (auto/simple/progressive),
mutation whitelists, tag scoping, the RouterMCP deprecation alias, and
deployment-scoped readiness — config altitude, with the big-app and
multi-domain factories from tests.support.
"""

import pytest
from fastapi import APIRouter, FastAPI
from fastmcp import Client
from pydantic import BaseModel

from fastapi_mcp_neo import FastAPIMCP, RouterMCP
from tests.support.apps import big_app, multi_domain_app, users_app
from tests.support.mcp import tool_payload


class TestServerBehavior:
    async def test_auto_mode_small_app_is_simple(self):
        mcp = FastAPIMCP(users_app(), name="ro")
        assert mcp.resolved_mode == "simple"

    async def test_auto_mode_big_app_goes_progressive(self):
        mcp = FastAPIMCP(big_app(30), name="big", progressive_threshold=25)
        assert mcp.resolved_mode == "progressive"
        async with Client(mcp.mcp) as client:
            tools = [t.name for t in await client.list_tools()]
        assert "list_domains" in tools and "graphql_query" in tools

    async def test_auto_mode_counts_include_router_routes(self):
        """FastAPI >= 0.142 wraps include_router results in _IncludedRouter
        (not APIRoute instances); the threshold decision must follow the
        SCANNED route count, not isinstance(app.routes, APIRoute)."""
        class Out(BaseModel):
            id: int

        sub = APIRouter()
        for i in range(30):
            def make_handler(n: int):
                async def handler() -> Out:
                    return Out(id=n)

                handler.__name__ = f"thing_{n}"
                return handler

            sub.get(f"/thing{i}", response_model=Out)(make_handler(i))

        app = FastAPI()
        app.include_router(sub, prefix="/things")

        mcp = FastAPIMCP(app, name="inc", progressive_threshold=25)
        assert len(mcp.handler.routes) == 30
        assert mcp.resolved_mode == "progressive"

    async def test_auto_mode_respects_include_filter(self):
        """Route count for the threshold is the count that ENTERS the schema
        (include/exclude applied), not the raw app.route count."""
        mcp = FastAPIMCP(
            big_app(30), name="filtered", include=["/thing0", "/thing1", "/thing2"]
        )
        assert len(mcp.handler.routes) == 3
        assert mcp.resolved_mode == "simple"

    async def test_domains_registry_built(self):
        mcp = FastAPIMCP(users_app(), name="test-api")
        summary = mcp.domains.summary()
        assert summary and summary[0]["name"] == "iam"


class TestMutationWhitelist:
    async def test_mutation_include_filters_writes(self):
        app = users_app()  # POST /users is the only write route

        mcp = FastAPIMCP(
            app,
            name="wl",
            allow_mutation=True,
            mutation_include=["/nope*"],
        )
        sdl = mcp.handler.get_sdl()
        assert "Mutation" not in sdl  # no write route survived the whitelist

        mcp2 = FastAPIMCP(
            app,
            name="wl2",
            allow_mutation=True,
            mutation_include=["/users"],
        )
        assert "create_user" in mcp2.handler.get_sdl()

    async def test_reads_unaffected_by_whitelist(self):
        app = users_app()
        mcp = FastAPIMCP(app, name="wl3", allow_mutation=True, mutation_include=["/none"])
        sdl = mcp.handler.get_sdl()
        assert "list_users" in sdl


class TestTagFiltering:
    async def test_get_schema_scoped_to_tags(self):
        mcp = FastAPIMCP(
            multi_domain_app(), name="iam-api", include_tags=["iam:*"],
            allow_mutation=True,
        )
        sdl = mcp.handler.get_sdl()
        assert "list_users" in sdl and "create_user" in sdl
        assert "list_invoices" not in sdl
        assert "ping" not in sdl  # untagged drops under the whitelist
        summary = mcp.domains.summary()
        assert [d["name"] for d in summary] == ["iam"]

    async def test_query_executes_included_route(self):
        mcp = FastAPIMCP(multi_domain_app(), name="iam-api", include_tags=["iam:*"])
        async with Client(mcp.mcp) as client:
            result = tool_payload(
                await client.call_tool(
                    "graphql_query",
                    # tag "iam:users" nests: IamQuery.users.list_users
                    {"query": "{ iam { users { list_users { id name } } } }"},
                )
            )
        assert result["data"]["data"]["iam"]["users"]["list_users"] == [
            {"id": 1, "name": "alice"}
        ]

    async def test_mutation_filtered_out_by_tags(self):
        mcp = FastAPIMCP(
            multi_domain_app(), name="billing-api", include_tags=["billing*"],
            allow_mutation=True,
        )
        assert "Mutation" not in mcp.handler.get_sdl()

    async def test_auto_mode_counts_post_tag_filter(self):
        class Out(BaseModel):
            id: int

        big = FastAPI()
        for i in range(30):
            tagged = i < 26
            name = f"{'misc' if tagged else 'core'}{i}"
            tags = ["misc"] if tagged else ["core"]

            def make_handler(n: int):
                async def handler() -> Out:
                    return Out(id=n)

                handler.__name__ = f"handler_{n}"
                return handler

            big.get(f"/{name}", response_model=Out, tags=tags)(make_handler(i))

        mcp = FastAPIMCP(big, name="core-only", include_tags=["core"])
        assert len(mcp.handler.routes) == 4
        assert mcp.resolved_mode == "simple"

    async def test_two_instances_same_app_different_tags(self):
        """The motivating scenario: one app, one MCP deployment per use
        case, each scoped by its own tag set."""
        app = multi_domain_app()
        iam = FastAPIMCP(app, name="iam-api", include_tags=["iam:*"])
        billing = FastAPIMCP(app, name="billing-api", include_tags=["billing*"])

        async with Client(iam.mcp) as client:
            result = tool_payload(await client.call_tool("get_schema", {}))
            sdl = result["data"]["sdl"]
            assert "list_users" in sdl and "list_invoices" not in sdl

        async with Client(billing.mcp) as client:
            result = tool_payload(await client.call_tool("get_schema", {}))
            sdl = result["data"]["sdl"]
            assert "list_invoices" in sdl and "list_users" not in sdl


class TestDeprecatedAlias:
    def test_router_mcp_alias_warns_and_works(self):
        with pytest.warns(DeprecationWarning, match="FastAPIMCP"):
            mcp = RouterMCP(users_app(), name="alias")
        assert isinstance(mcp, FastAPIMCP)
        assert mcp.resolved_mode == "simple"


class TestReadiness:
    async def test_report_scoped_to_the_deployment(self):
        """The checklist reflects what THIS deployment would expose: /ping
        (a raw-JSON bridge) is tag-filtered out and never reported."""
        mcp = FastAPIMCP(multi_domain_app(), name="iam-api", include_tags=["iam:*"])
        report = mcp.handler.readiness()
        assert [s.path for s in report.skips] == ["/users"]  # POST, mutation off
        assert report.skips[0].tags == ("iam:users",)
        assert report.degraded == ()
        assert report.degraded_fields == ()
        assert not report.ready
