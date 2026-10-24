"""tadata-org/fastapi_mcp#287 analog: a recursive UNION inside a model
(``filters: list[Union[ComparisonFilter, "CompoundFilter"]]`` — the union
contains its own host) crashes fastapi_mcp during schema generation with
RecursionError, and the whole MCP server fails to start.

The bridge must (a) build the schema without recursion, (b) keep the
route callable — the union fields degrade to the documented raw-JSON
scalar like any other union, they do not vanish — and (c) still deliver
nested recursive payloads: FastAPI revalidates the JSON into the real
recursive objects on the receiving side.
"""


from typing import Literal, Union

from fastapi import FastAPI
from pydantic import BaseModel

from fastapi_mcp_neo.handler import RouterGraphQLHandler

# --- verbatim model graph from tadata-org/fastapi_mcp#287 ---

class ComparisonFilter(BaseModel):
    type: Literal["eq", "ne", "gt", "lt"]
    key: str
    value: str


class CompoundFilter(BaseModel):
    type: Literal["and", "or"]
    filters: list[Union[ComparisonFilter, "CompoundFilter"]]


CompoundFilter.model_rebuild()


class SearchRequest(BaseModel):
    query: str
    filters: ComparisonFilter | CompoundFilter | None = None


class SearchOut(BaseModel):
    status: str
    depth: int


def build_app() -> FastAPI:
    app = FastAPI()

    @app.get("/health", tags=["search"])
    async def health() -> dict:
        return {"ok": True}

    @app.post("/search", tags=["search"])
    async def search(request: SearchRequest) -> SearchOut:
        depth = 0
        f: object = request.filters
        while isinstance(f, CompoundFilter) and f.filters:
            depth += 1
            f = f.filters[0]
        return SearchOut(status="ok", depth=depth)

    return app


# The recursive payload: and( or( eq ) ) — two CompoundFilter levels,
# exercising the self-reference at depth 2.
NESTED_FILTERS = {
    "type": "and",
    "filters": [{"type": "or", "filters": [{"type": "eq", "key": "k", "value": "v"}]}],
}


async def test_schema_builds_without_recursion_error():
    # fastapi_mcp crashes HERE — during schema generation. Merely building
    # the handler (schema assembly happens eagerly) is the repro.
    handler = RouterGraphQLHandler(build_app(), allow_mutation=True)
    sdl = handler.get_sdl()
    assert "search" in sdl
    await handler.aclose()


async def test_union_fields_degrade_documented_not_recurse():
    handler = RouterGraphQLHandler(build_app(), allow_mutation=True)
    degraded = dict(handler.readiness().degraded_fields)
    assert degraded, "recursive union fields must degrade, not vanish silently"
    assert any("filters" in field for field in degraded), degraded
    await handler.aclose()


async def test_route_delivers_recursive_payload():
    handler = RouterGraphQLHandler(build_app(), allow_mutation=True)
    result = await handler.execute(
        "mutation($r: SearchRequestInput!) { search { search(request: $r) "
        "{ status depth } } }",
        variables={"r": {"query": "hello", "filters": NESTED_FILTERS}},
    )
    assert not result.get("errors"), result["errors"]
    # FastAPI revalidated the raw JSON into real recursive objects: the
    # route saw both CompoundFilter levels, proving the payload survived.
    assert result["data"]["search"]["search"] == {"status": "ok", "depth": 2}
    await handler.aclose()
