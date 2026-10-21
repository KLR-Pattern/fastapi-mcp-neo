"""MCP mounting: streamable-HTTP endpoints served from a host app, the
stateless mount flavor, and composed lifespans across multiple mounts."""

from fastapi_mcp_neo import FastAPIMCP
from tests.support.apps import multi_domain_app, users_app
from tests.support.mcp import http_client, jsonrpc_initialize


class TestMountTo:
    async def test_mounted_endpoint_serves_mcp(self):
        host = users_app()
        mcp = FastAPIMCP(host, name="mounted", include=["/users*"])
        mcp.mount_to(host, "/mcp")
        # Same-app mount must disable the invoker's own lifespan management.
        assert mcp.handler.invoker.manage_lifespan is False

        async with http_client(host, base_url="http://test") as client:
            response = await client.post(
                "/mcp/",
                json=jsonrpc_initialize(),
                headers={"Accept": "application/json, text/event-stream"},
            )
        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]

    async def test_host_routes_still_work_after_mount(self):
        host = users_app()
        mcp = FastAPIMCP(host, name="mounted2", include=["/users*"])
        mcp.mount_to(host, "/mcp")

        async with http_client(host, base_url="http://test") as client:
            response = await client.get("/users")
        assert response.status_code == 200
        assert response.json()[0]["name"] == "alice"

    async def test_two_mounts_on_one_app(self):
        """Both deployments mounted into the app they wrap, each at its own
        path; the composed lifespan (host -> sub1 -> sub2) enters each part
        exactly once."""
        app = multi_domain_app()
        iam = FastAPIMCP(app, name="iam-api", include_tags=["iam:*"])
        billing = FastAPIMCP(app, name="billing-api", include_tags=["billing*"])
        iam.mount_to(app, "/mcp-iam")
        billing.mount_to(app, "/mcp-billing")
        assert iam.handler.invoker.manage_lifespan is False
        assert billing.handler.invoker.manage_lifespan is False

        init = jsonrpc_initialize()
        responses = {}
        async with http_client(app, base_url="http://test") as client:
            for path in ("/mcp-iam/", "/mcp-billing/"):
                responses[path] = await client.post(
                    path, json=init,
                    headers={"Accept": "application/json, text/event-stream"},
                )
        for path, response in responses.items():
            assert response.status_code == 200, path
            assert "text/event-stream" in response.headers["content-type"], path


class TestStatelessMount:
    async def test_stateless_tools_list_without_initialize(self):
        """stateless_http=True: every request stands alone — tools/list works
        without a prior initialize or session id, which is exactly what a
        non-sticky multi-worker balancer delivers."""
        app = users_app()
        mcp = FastAPIMCP(app, name="stateless")
        mcp.mount_to(app, "/mcp", stateless_http=True)

        req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
        }
        async with http_client(app, base_url="http://test") as client:
            response = await client.post(
                "/mcp/",
                json=req,
                headers={"Accept": "application/json, text/event-stream"},
            )
        assert response.status_code == 200, response.text
        assert "graphql_query" in response.text
