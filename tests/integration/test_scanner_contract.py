"""Scanner-to-handler contracts: what the scanner decides must be exactly
what the handler serves (SDL, readiness, execution) — the two sides of the
bridge may never drift apart.
"""

from typing import Annotated

from fastapi import FastAPI, Query
from pydantic import BaseModel

from fastapi_mcp_neo import RouterGraphQLHandler
from fastapi_mcp_neo.scanner import RouterScanner
from tests.support.apps import scanner_app
from tests.support.models import ItemOut


class TestQueryParameterModels:
    async def test_query_model_end_to_end(self, make_handler):
        class ItemFilter(BaseModel):
            category: str
            limit: int = 2

        app = FastAPI()
        seen: dict = {}

        @app.get("/things", response_model=list[ItemOut], tags=["demo"])
        async def things(filters: Annotated[ItemFilter, Query()]):
            seen.update(filters.model_dump())
            return [ItemOut(id=i, name=filters.category) for i in range(filters.limit)]

        handler = make_handler(app)
        sdl = handler.get_sdl()
        assert "things(category: String!, limit: Int = 2): [ItemOut!]" in sdl
        result = await handler.execute(
            "{ demo { things(category: \"tools\") { name } } }"
        )
        assert result == {"data": {"demo": {"things": [{"name": "tools"}, {"name": "tools"}]}}}
        assert seen == {"category": "tools", "limit": 2}


class TestReadinessContract:
    def test_handler_readiness_matches_scanner(self):
        app = scanner_app()
        handler = RouterGraphQLHandler(app, allow_mutation=True)
        assert handler.readiness() == RouterScanner(app, allow_mutation=True).readiness()


class TestDeprecationContract:
    def test_deprecated_kept_with_mark_by_default(self):
        app = FastAPI()

        @app.get("/old", deprecated=True, response_model=ItemOut)
        async def old():
            return ItemOut(id=1, name="o")

        sdl = RouterGraphQLHandler(app).get_sdl()
        assert "old: ItemOut @deprecated" in sdl
