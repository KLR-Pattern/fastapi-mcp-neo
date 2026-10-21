"""MCP server auth wiring: fastmcp auth providers, OAuth discovery at the
host root (RFC 9728/8414), and the auth_at_root mount flavor.

Complementary to e2e/test_oauth_flow.py: that file drives the full caller
OAuth dance; this one pins how the server exposes the provider.
"""

import pytest
from fastapi import FastAPI
from fastmcp.server.auth.providers.in_memory import InMemoryOAuthProvider

from fastapi_mcp_neo import FastAPIMCP
from tests.support.apps import users_app
from tests.support.mcp import http_client, jsonrpc_initialize


class TestAuthProviderPassthrough:
    def test_auth_provider_reaches_fastmcp(self):
        provider = InMemoryOAuthProvider(base_url="http://t")
        mcp = FastAPIMCP(users_app(), name="auth-api", auth=provider)
        assert mcp.mcp.auth is provider

    def test_no_auth_by_default(self):
        mcp = FastAPIMCP(users_app(), name="plain-api")
        assert mcp.mcp.auth is None

    async def test_mount_exposes_well_known_at_host_root(self):
        """The 401 challenge advertises PR metadata at a host-root URL; the
        mount must keep that promise (RFC 9728/8414)."""
        app = FastAPI()
        mcp = FastAPIMCP(
            users_app(), name="auth-api", auth=InMemoryOAuthProvider(base_url="http://localhost:9")
        )
        mcp.mount_to(app, "/mcp")

        well_known = [
            r.path
            for r in app.router.routes
            if getattr(r, "path", "").startswith("/.well-known/")
        ]
        assert well_known, "well-known routes must exist at the host root"

        async with http_client(app, base_url="http://localhost:9") as c:
            for path in well_known:
                r = await c.get(path)
                assert r.status_code == 200, (path, r.status_code)

    def test_auth_at_root_requires_auth(self):
        mcp = FastAPIMCP(users_app(), name="no-auth")
        with pytest.raises(Exception, match="auth_at_root requires"):
            mcp.mount_to(FastAPI(), "/mcp", auth_at_root=True)

    async def test_auth_at_root_serves_endpoint_and_oauth_at_root(self):
        """auth_at_root: MCP endpoint at `path`, OAuth/discovery at the host
        root, the sub-app's auth middleware intact, host routes keep
        precedence."""
        host = FastAPI()

        @host.get("/auth/callback")
        async def host_callback():
            return {"host": "route-wins"}

        mcp = FastAPIMCP(
            users_app(),
            name="auth-api",
            auth=InMemoryOAuthProvider(base_url="http://localhost:9"),
        )
        mcp.mount_to(host, "/mcp", auth_at_root=True)

        init = jsonrpc_initialize("2025-06-18")

        async with http_client(host, base_url="http://localhost:9") as c:
            base = {
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
            }
            r = await c.post("/mcp", headers=base, json=init)
            assert r.status_code == 401, "endpoint protected by the provider"
            assert "error=" not in r.headers["www-authenticate"]
            # present-but-invalid token: only a running auth backend can
            # tell the two 401s apart (RFC 6750 §3.1)
            r = await c.post(
                "/mcp", headers={**base, "authorization": "Bearer not-a-token"}, json=init
            )
            assert r.status_code == 401
            assert 'error="invalid_token"' in r.headers["www-authenticate"]
            r = await c.get("/authorize")
            assert r.status_code in (400, 302), "authorize reachable at root"
            r = await c.get("/.well-known/oauth-authorization-server")
            assert r.status_code == 200, "discovery metadata at host root"
            r = await c.get("/auth/callback")
            assert r.json() == {"host": "route-wins"}
