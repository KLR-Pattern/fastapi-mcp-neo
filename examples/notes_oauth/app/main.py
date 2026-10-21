"""App assembly: OAuth auth + protected notes + fastapi-mcp-neo mounts.

The library call is the last block — the rest is a plain FastAPI app:

    mcp = FastAPIMCP(app, name=..., allow_mutation=True,
                    exclude=["/auth/token"],   # protocol endpoint stays REST-only
                    passthrough_headers=["authorization", "cookie"],
                    auth=mcp_oauth.provider())
    mcp.mount_to(app, "/mcp", auth_at_root=True)  # endpoint at /mcp, OAuth at /
    mcp.handler.mount_graphql(app)     # GraphiQL at /graphiql + POST /graphql

Credentials flow per caller, no shared service token:

- browser    -> demo_session cookie (GitHub login)
- MCP client -> OAuth 2.1 login on the fastmcp proxy (Claude Code), or a
  Bearer token minted from the browser session; the caller's Authorization
  header is forwarded into every route call and resolved by
  ``app.credentials`` (which the MCP layer only feeds a verifier into).
"""

from __future__ import annotations

from fastapi import FastAPI

from app import credentials, mcp_oauth
from app.auth_routes import router as auth_router
from app.notes_routes import router as notes_router
from fastapi_mcp_neo import FastAPIMCP


def create_app() -> FastAPI:
    app = FastAPI(
        title="fastapi-mcp-neo demo",
        version="0.1.0",
        description="Notes API behind GitHub OAuth, exposed to agents via GraphQL + MCP.",
    )
    app.include_router(auth_router)
    app.include_router(notes_router)

    # Identity carriers: the app's own session string always; the MCP OAuth
    # proxy's tokens additionally, when the proxy is configured.
    credentials.register_bearer_verifier(credentials.session_string_verifier)
    mcp_auth = mcp_oauth.provider()
    if mcp_auth is not None:
        credentials.register_bearer_verifier(mcp_oauth.proxy_token_user)

    mcp = FastAPIMCP(
        app,
        name="notes-demo",
        allow_mutation=True,
        exclude=["/auth/token"],  # credential-minting is protocol, not business
        passthrough_headers=["authorization", "cookie"],
        auth=mcp_auth,
    )
    # auth_at_root hosts the OAuth routes at the app root — the contract
    # that lets the proxy's callback reuse the GitHub app via a redirect
    # subdirectory (see app/mcp_oauth.py).
    mcp.mount_to(app, "/mcp", auth_at_root=mcp_auth is not None)
    mcp.handler.mount_graphql(app)
    return app


app = create_app()
