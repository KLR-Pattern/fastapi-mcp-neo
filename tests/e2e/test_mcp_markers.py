"""@mcp_query / @mcp_mutation: force a route's read/write classification.

The verb default breaks on two real shapes — POST-for-read (payload
bodies beat query strings) and GET-with-side-effects. The marker flips
the classification at scan time, so schema placement, tools-mode
annotations, and the allow_mutation gate all align; the physical HTTP
verb on the wire is untouched.
"""

from fastapi import FastAPI, Request
from fastmcp import Client
from pydantic import BaseModel

from fastapi_mcp_neo import FastAPIMCP, mcp_mutation, mcp_query

# Shared fixtures -----------------------------------------------------------


class SearchIn(BaseModel):
    keywords: str


class Hits(BaseModel):
    ids: list[int]
    via_method: str


class Ok(BaseModel):
    ok: bool
    via_method: str


def build_app() -> FastAPI:
    app = FastAPI()

    @app.post("/search", tags=["search"])
    @mcp_query  # pure read; POST only for the clean payload
    async def search(q: SearchIn, request: Request) -> Hits:
        """Keyword search — a read despite the verb."""
        return Hits(ids=[1, 2, 3], via_method=request.method)

    @app.get("/admin/flush-cache", tags=["admin"])
    @mcp_mutation  # side effects despite the verb
    async def flush_cache(request: Request) -> Ok:
        """Flush caches — a write despite the verb."""
        return Ok(ok=True, via_method=request.method)

    @app.get("/health", tags=["admin"])
    async def health() -> dict:
        """Untouched control route."""
        return {"ok": True}

    return app


class TestToolsModeAnnotations:
    async def test_post_read_gets_readonly_hint(self):
        mcp = FastAPIMCP(build_app(), name="mk", mode="tools")
        async with Client(mcp.mcp) as client:
            tools = {t.name: t for t in await client.list_tools()}
        assert tools["search"].annotations.read_only_hint is True
        assert tools["search"].annotations.destructive_hint is None

    async def test_get_write_gets_destructive_hint(self):
        mcp = FastAPIMCP(build_app(), name="mk", mode="tools")
        async with Client(mcp.mcp) as client:
            tools = {t.name: t for t in await client.list_tools()}
        assert tools["flush_cache"].annotations.destructive_hint is True
        assert tools["flush_cache"].annotations.read_only_hint is None

    async def test_physical_verb_untouched(self):
        """The marker classifies; the route still executes via its REAL
        verb — a POST stays a POST on the wire."""
        mcp = FastAPIMCP(build_app(), name="mk", mode="tools")
        async with Client(mcp.mcp) as client:
            search = await client.call_tool("search", {"q": {"keywords": "x"}})
            flush = await client.call_tool("flush_cache", {})
        assert search.structured_content == {
            "data": {"ids": [1, 2, 3], "via_method": "POST"}
        }
        assert flush.structured_content == {"data": {"ok": True, "via_method": "GET"}}


class TestSchemaPlacement:
    async def test_forced_kinds_land_in_the_right_roots(self):
        mcp = FastAPIMCP(build_app(), name="mk", mode="graphql")
        sdl = mcp.handler.get_sdl()
        mutation_block = sdl.split("type Mutation", 1)[1]
        assert "search(" not in mutation_block  # POST-for-read: Query side
        # GET-with-effects: Mutation side (no args → the field has no parens)
        assert "flush_cache" in mutation_block
        await mcp.handler.aclose()

    async def test_query_tool_serves_the_post_route(self):
        mcp = FastAPIMCP(build_app(), name="mk", mode="graphql")
        result = await mcp.handler.execute(
            'query($q: SearchInInput!) { search { search(q: $q) { ids } } }',
            variables={"q": {"keywords": "x"}},
        )
        assert result["data"]["search"]["search"] == {"ids": [1, 2, 3]}
        await mcp.handler.aclose()


class TestMutationGate:
    async def test_read_only_view_keeps_the_post_read(self):
        """@mcp_query routes are reads: allow_mutation=False must NOT
        gate them (the old verb rule would have skipped /search)."""
        mcp = FastAPIMCP(build_app(), name="ro", mode="tools", allow_mutation=False)
        async with Client(mcp.mcp) as client:
            names = sorted(t.name for t in await client.list_tools())
        assert "search" in names
        assert "flush_cache" not in names  # forced write, correctly gated

    async def test_forced_mutation_is_gated_and_recorded(self):
        mcp = FastAPIMCP(build_app(), name="ro", mode="graphql", allow_mutation=False)
        report = mcp.handler.readiness()
        gated = [s.path for s in report.skips if "mutation" in s.reason]
        assert "/admin/flush-cache" in gated
        await mcp.handler.aclose()
