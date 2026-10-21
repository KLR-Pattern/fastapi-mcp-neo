"""Per-call header passthrough over the full MCP stack: a streamable-HTTP
fastmcp Client (in-process ASGI transport) carries the caller's token to
the route, and only whitelisted headers make that trip.
"""

from asgi_lifespan import LifespanManager
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from fastapi_mcp_neo import FastAPIMCP
from tests.support.apps import WHOAMI_QUERY, auth_app
from tests.support.mcp import asgi_client_factory, tool_payload


class TestMcpEndToEnd:
    async def test_per_call_authorization_end_to_end(self):
        app = auth_app()
        mcp = FastAPIMCP(app, name="e2e", passthrough_headers=["authorization"])
        mcp.mount_to(app, "/mcp")
        transport = StreamableHttpTransport(
            url="http://testserver/mcp/",
            headers={"authorization": "Bearer jwt-alice"},
            httpx_client_factory=asgi_client_factory(app),
        )
        async with LifespanManager(app):
            async with Client(transport) as client:
                result = tool_payload(
                    await client.call_tool("graphql_query", {"query": WHOAMI_QUERY})
                )
        assert result["data"]["data"]["iam"]["whoami"] == {"user": "alice"}
        await mcp.handler.aclose()

    async def test_disabled_ignores_client_auth(self):
        app = auth_app()
        # Default forwards authorization; an explicitly empty sequence opts out.
        mcp = FastAPIMCP(app, name="e2e", passthrough_headers=[])
        mcp.mount_to(app, "/mcp")
        transport = StreamableHttpTransport(
            url="http://testserver/mcp/",
            headers={"authorization": "Bearer jwt-alice"},
            httpx_client_factory=asgi_client_factory(app),
        )
        async with LifespanManager(app):
            async with Client(transport) as client:
                result = tool_payload(
                    await client.call_tool("graphql_query", {"query": WHOAMI_QUERY})
                )
        # Client credentials do NOT leak through when passthrough is off.
        assert result["data"]["data"]["iam"]["whoami"] is None
        await mcp.handler.aclose()

    async def test_non_whitelisted_header_not_forwarded(self):
        app = auth_app()
        mcp = FastAPIMCP(app, name="e2e", passthrough_headers=["authorization"])
        mcp.mount_to(app, "/mcp")
        transport = StreamableHttpTransport(
            url="http://testserver/mcp/",
            headers={"authorization": "Bearer jwt-alice", "x-internal-token": "evil"},
            httpx_client_factory=asgi_client_factory(app),
        )
        async with LifespanManager(app):
            async with Client(transport) as client:
                result = tool_payload(
                    await client.call_tool(
                        "graphql_query",
                        {"query": "{ iam { probe { user x_internal_token_seen } } }"},
                    )
                )
        probe = result["data"]["data"]["iam"]["probe"]
        assert probe == {"user": "alice", "x_internal_token_seen": False}
        await mcp.handler.aclose()

    async def test_mutation_tool_passthrough(self):
        app = auth_app()
        mcp = FastAPIMCP(
            app,
            name="e2e",
            allow_mutation=True,
            passthrough_headers=["authorization"],
        )
        mcp.mount_to(app, "/mcp")
        transport = StreamableHttpTransport(
            url="http://testserver/mcp/",
            headers={"authorization": "Bearer jwt-alice"},
            httpx_client_factory=asgi_client_factory(app),
        )
        mutation = 'mutation { iam { create_note(payload: {text: "hi"}) { author text } } }'
        async with LifespanManager(app):
            async with Client(transport) as client:
                result = tool_payload(
                    await client.call_tool("graphql_mutation", {"mutation": mutation})
                )
        assert result["data"]["data"]["iam"]["create_note"] == {"author": "alice", "text": "hi"}
        await mcp.handler.aclose()

    async def test_in_memory_client_has_no_identity(self):
        mcp = FastAPIMCP(auth_app(), name="e2e", passthrough_headers=["authorization"])
        async with Client(mcp.mcp) as client:
            result = tool_payload(await client.call_tool("graphql_query", {"query": WHOAMI_QUERY}))
        # No HTTP request context: nothing to forward, nothing to fall back
        # on — protected routes answer 401 (single identity source).
        assert result["data"]["data"]["iam"]["whoami"] is None
        assert result["data"]["errors"][0]["extensions"]["code"] == "HTTP_401"
        await mcp.handler.aclose()
