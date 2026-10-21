"""O1/O2: the compile front half (parse + depth guard + validate) runs once
per unique query string, LRU-cached on the handler; execution never caches.

Counters monkeypatch the parse/validate symbols in BOTH modules that import
them (handler for the unguarded path, depth_guard for the guarded path), so
the "one parse" assertions hold regardless of which branch runs.
"""

from typing import Any

import pytest
from fastapi import FastAPI
from graphql import parse as real_parse
from graphql.language import DocumentNode
from graphql.validation import validate as real_validate
from pydantic import BaseModel

import fastapi_mcp_neo.depth_guard as depth_guard_mod
import fastapi_mcp_neo.handler as handler_mod
from fastapi_mcp_neo import RouterGraphQLHandler


class Out(BaseModel):
    id: int
    name: str


def build_app() -> FastAPI:
    app = FastAPI()

    @app.get("/items/{item_id}", response_model=Out, tags=["shop"])
    async def get_item(item_id: int) -> Out:
        return Out(id=item_id, name=f"n{item_id}")

    @app.get("/items", response_model=list[Out], tags=["shop"])
    async def list_items(limit: int = 2) -> list[Out]:
        return [Out(id=i, name=f"i{i}") for i in range(limit)]

    return app


@pytest.fixture
def counters(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    calls = {"parse": 0, "validate": 0}

    def counting_parse(source: Any) -> DocumentNode:
        calls["parse"] += 1
        return real_parse(source)

    def counting_validate(*args: Any, **kwargs: Any) -> list[Any]:
        calls["validate"] += 1
        return real_validate(*args, **kwargs)

    monkeypatch.setattr(handler_mod, "parse", counting_parse)
    monkeypatch.setattr(depth_guard_mod, "parse", counting_parse)
    monkeypatch.setattr(handler_mod, "validate", counting_validate)
    return calls


QUERY = "{ shop { get_item(item_id: 1) { id name } } }"


class TestCompileCache:
    async def test_repeated_query_parses_and_validates_once(self, counters):
        handler = RouterGraphQLHandler(build_app())
        r1 = await handler.execute(QUERY)
        r2 = await handler.execute(QUERY)
        assert r1 == r2 == {"data": {"shop": {"get_item": {"id": 1, "name": "n1"}}}}
        assert counters["parse"] == 1, "same document must not re-parse (O2+O1)"
        assert counters["validate"] == 1, "validation over an immutable schema caches"
        await handler.aclose()

    async def test_execution_never_cached_variables_matter(self, counters):
        handler = RouterGraphQLHandler(build_app())
        doc = "query($id: Int!) { shop { get_item(item_id: $id) { id } } }"
        r1 = await handler.execute(doc, variables={"id": 1})
        r2 = await handler.execute(doc, variables={"id": 2})
        assert r1["data"]["shop"]["get_item"] == {"id": 1}
        assert r2["data"]["shop"]["get_item"] == {"id": 2}
        assert counters["parse"] == 1  # compile cached...
        assert r1 != r2  # ...but execution ran for real both times
        await handler.aclose()

    async def test_operation_name_not_in_key(self, counters):
        handler = RouterGraphQLHandler(build_app())
        doc = (
            "query A { shop { get_item(item_id: 1) { id } } } "
            "query B { shop { list_items { id } } }"
        )
        r1 = await handler.execute(doc, operation_name="A")
        r2 = await handler.execute(doc, operation_name="B")
        assert r1["data"]["shop"]["get_item"] == {"id": 1}
        assert r2["data"]["shop"]["list_items"] == [{"id": 0}, {"id": 1}]
        assert counters["parse"] == 1
        await handler.aclose()

    async def test_rejected_documents_cache_their_errors(self, counters):
        handler = RouterGraphQLHandler(build_app())
        bad = "{ shop { nope } }"
        r1 = await handler.execute(bad)
        r2 = await handler.execute(bad)
        assert "Cannot query field 'nope'" in r1["errors"][0]["message"]
        assert r1 == r2
        assert counters["parse"] == 1, "malformed/rejected documents cache too"
        assert counters["validate"] == 1
        await handler.aclose()

    async def test_depth_rejections_cache_without_validating(self, counters):
        handler = RouterGraphQLHandler(build_app())
        deep = "{ shop { get_node(item_id: 1) { children { children { x } } } } }"
        r1 = await handler.execute(deep)
        assert "exceeds max_depth" not in r1["errors"][0]["message"]  # unknown field
        too_deep = (
            "{ shop { get_item(item_id: 1) "
            "{ name { a { b { c { d { e { f { g { h } } } } } } } } } } }"
        )  # depth 12 > 10: rejected by the guard BEFORE field validation runs
        d1 = await handler.execute(too_deep)
        d2 = await handler.execute(too_deep)
        assert "exceeds max_depth=10" in d1["errors"][0]["message"]
        assert d1 == d2
        await handler.aclose()

    async def test_cache_disabled_with_zero(self, counters):
        handler = RouterGraphQLHandler(build_app(), document_cache_size=0)
        await handler.execute(QUERY)
        await handler.execute(QUERY)
        assert counters["parse"] == 2  # every call recompiles
        await handler.aclose()

    def test_negative_size_rejected(self):
        with pytest.raises(ValueError, match="document_cache_size"):
            RouterGraphQLHandler(build_app(), document_cache_size=-1)

    async def test_capacity_evicts_oldest(self, counters):
        handler = RouterGraphQLHandler(build_app(), document_cache_size=2)
        q1, q2, q3 = (
            "{ shop { get_item(item_id: 1) { id } } }",
            "{ shop { get_item(item_id: 2) { id } } }",
            "{ shop { list_items { id } } }",
        )
        await handler.execute(q1)
        await handler.execute(q2)
        await handler.execute(q3)  # evicts q1
        await handler.execute(q3)  # still cached
        await handler.execute(q1)  # evicted → recompile
        assert counters["parse"] == 4  # q1, q2, q3, q1-again
        await handler.aclose()

    async def test_cache_is_per_handler_instance(self, counters):
        """Same process, two handlers over different schemas (comparison-bench
        topology): a query valid for A must not leak B's compiled verdict."""
        h1 = RouterGraphQLHandler(build_app())
        other = FastAPI()

        @other.get("/users", response_model=Out, tags=["iam"])
        async def list_users() -> list[Out]:
            return [Out(id=1, name="u")]

        h2 = RouterGraphQLHandler(other)
        await h1.execute(QUERY)
        result = await h2.execute(QUERY)  # 'shop' does not exist on h2
        assert "Cannot query field 'shop'" in result["errors"][0]["message"]
        await h1.aclose()
        await h2.aclose()


class TestO2SingleParse:
    """The document produced by the depth guard is reused by execute() — the
    query string is never parsed twice within one compile."""

    async def test_guarded_path_single_parse(self, counters):
        handler = RouterGraphQLHandler(build_app())  # max_depth=10 → guarded path
        await handler.execute(QUERY)
        assert counters["parse"] == 1

    async def test_unguarded_path_single_parse(self, counters):
        handler = RouterGraphQLHandler(build_app(), max_depth=None)
        await handler.execute(QUERY)
        assert counters["parse"] == 1
        await handler.aclose()
