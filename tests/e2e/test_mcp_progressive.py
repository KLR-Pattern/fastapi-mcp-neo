"""MCP progressive mode: 4-layer disclosure over the domain tree."""

from typing import Annotated

import pytest
from fastapi import FastAPI, Query
from fastmcp import Client
from pydantic import BaseModel

from fastapi_mcp_neo import FastAPIMCP
from tests.support.apps import users_app
from tests.support.mcp import tool_payload


class Out(BaseModel):
    id: int
    name: str


class In(BaseModel):
    name: str


def build_app() -> FastAPI:
    app = FastAPI()

    @app.get("/users", response_model=list[Out], tags=["iam:users"])
    async def list_users():
        return [Out(id=1, name="a")]

    @app.get("/roles", response_model=Out, tags=["iam:roles"])
    async def get_role():
        return Out(id=1, name="admin")

    @app.get("/invoices", response_model=Out, tags=["billing"])
    async def list_invoices():
        return Out(id=2, name="inv")

    @app.post("/users", response_model=Out, tags=["iam:users"])
    async def create_user(payload: In):
        return Out(id=9, name=payload.name)

    return app


@pytest.fixture
def mcp():
    return FastAPIMCP(build_app(), name="prog", allow_mutation=True, mode="progressive")


class TestListDomains:
    async def test_list_domains(self, mcp):
        async with Client(mcp.mcp) as client:
            result = tool_payload(await client.call_tool("list_domains", {}))
        assert result["success"] is True
        domains = {d["name"]: d for d in result["data"]["domains"]}
        assert domains["iam"]["queries"] == 2
        assert domains["iam"]["mutations"] == 1
        assert domains["iam"]["subdomains"] == ["roles", "users"]
        assert domains["billing"]["queries"] == 1


class TestListQueries:
    async def test_subtree_fields_listed(self, mcp):
        async with Client(mcp.mcp) as client:
            result = tool_payload(await client.call_tool("list_queries", {"domain": "iam"}))
        names = [q["name"] for q in result["data"]["queries"]]
        assert "list_users" in names and "get_role" in names
        assert "invoices" not in names

    async def test_deep_domain_path(self, mcp):
        async with Client(mcp.mcp) as client:
            result = tool_payload(await client.call_tool("list_queries", {"domain": "iam:users"}))
        assert [q["name"] for q in result["data"]["queries"]] == ["list_users"]

    async def test_args_brief(self, mcp):
        app = FastAPI()

        @app.get("/things", response_model=Out, tags=["t"])
        async def things(
            limit: Annotated[int, Query(description="how many things")] = 5,
        ):
            return Out(id=1, name="x")

        m = FastAPIMCP(app, mode="progressive")
        async with Client(m.mcp) as client:
            result = tool_payload(await client.call_tool("list_queries", {"domain": "t"}))
        query = result["data"]["queries"][0]
        assert query["args"] == [
            {"name": "limit", "type": "Int", "description": "how many things"}
        ]

    async def test_deprecated_flag_in_brief(self, mcp):
        app = FastAPI()

        @app.get("/old", response_model=Out, tags=["t"], deprecated=True)
        async def old():
            return Out(id=1, name="o")

        m = FastAPIMCP(app, mode="progressive")
        async with Client(m.mcp) as client:
            result = tool_payload(await client.call_tool("list_queries", {"domain": "t"}))
        assert result["data"]["queries"][0]["deprecated"] is True

    async def test_unknown_domain_error(self, mcp):
        async with Client(mcp.mcp) as client:
            result = tool_payload(await client.call_tool("list_queries", {"domain": "nope"}))
        assert result["success"] is False
        assert result["error_type"] == "domain_not_found"
        assert "iam" in result["hint"]

    async def test_list_mutations(self, mcp):
        async with Client(mcp.mcp) as client:
            result = tool_payload(await client.call_tool("list_mutations", {"domain": "iam"}))
        assert [m["name"] for m in result["data"]["mutations"]] == ["create_user"]


class TestListMutations:
    async def test_unknown_domain_error(self, mcp):
        async with Client(mcp.mcp) as client:
            result = tool_payload(await client.call_tool("list_mutations", {"domain": "nope"}))
        assert result["success"] is False
        assert result["error_type"] == "domain_not_found"
        assert "iam" in result["hint"]


class TestGetQuerySchema:
    async def test_unknown_domain_error(self, mcp):
        async with Client(mcp.mcp) as client:
            result = tool_payload(
                await client.call_tool("get_query_schema", {"domain": "nope"})
            )
        assert result["success"] is False
        assert result["error_type"] == "domain_not_found"
        assert "iam" in result["hint"]

    async def test_fragment_scoped_to_domain(self, mcp):
        async with Client(mcp.mcp) as client:
            result = tool_payload(await client.call_tool("get_query_schema", {"domain": "iam"}))
        sdl = result["data"]["sdl"]
        assert "list_users" in sdl and "get_role" in sdl
        assert "invoices" not in sdl
        assert "type Out {" in sdl  # reachable shared type included
        assert "type Mutation {" in sdl  # subtree mutation included
        assert "create_user" in sdl

    async def test_leaf_fragment_excludes_siblings(self, mcp):
        async with Client(mcp.mcp) as client:
            result = tool_payload(
                await client.call_tool("get_query_schema", {"domain": "iam:roles"})
            )
        sdl = result["data"]["sdl"]
        assert "roles" in sdl
        assert "list_users" not in sdl
        assert "create_user" not in sdl


class TestExecutionNotScoped:
    async def test_graphql_query_runs_full_schema(self, mcp):
        # Discovery is scoped, execution is not: fields from different domains
        # combine in one query.
        async with Client(mcp.mcp) as client:
            result = tool_payload(
                await client.call_tool(
                    "graphql_query",
                    {
                        "query": "{ iam { users { list_users { name } }"
                        " roles { get_role { name } } }"
                        " billing { list_invoices { name } } }"
                    },
                )
            )
        data = result["data"]["data"]
        assert data["iam"]["users"]["list_users"][0]["name"] == "a"
        assert data["iam"]["roles"]["get_role"]["name"] == "admin"
        assert data["billing"]["list_invoices"]["name"] == "inv"

    async def test_mutation_tool_available(self, mcp):
        async with Client(mcp.mcp) as client:
            result = tool_payload(
                await client.call_tool(
                    "graphql_mutation",
                    {
                        "mutation": 'mutation { iam { users '
                        '{ create_user(payload: {name: "n"}) { id } } } }'
                    },
                )
            )
        assert result["data"]["data"]["iam"]["users"]["create_user"]["id"] == 9


class TestToolAnnotations:
    async def test_progressive_mode_hints(self):
        mcp = FastAPIMCP(
            users_app(), name="prog", mode="progressive", allow_mutation=True
        )
        async with Client(mcp.mcp) as client:
            tools = {t.name: t for t in await client.list_tools()}
        assert tools["list_domains"].annotations.read_only_hint is True
        assert tools["graphql_mutation"].annotations.destructive_hint is True


class TestSearchFields:
    async def test_search_hits_target_field(self, mcp):
        async with Client(mcp.mcp) as client:
            result = tool_payload(
                await client.call_tool(
                    "search_fields", {"domain": "iam", "query": "users"}
                )
            )
        assert result["success"] is True
        names = [r["name"] for r in result["data"]["results"]]
        assert "list_users" in names
        assert all(r["score"] > 0 for r in result["data"]["results"])
        assert result["data"]["total_fields"] >= 2

    async def test_search_covers_mutations(self, mcp):
        async with Client(mcp.mcp) as client:
            result = tool_payload(
                await client.call_tool(
                    "search_fields", {"domain": "iam", "query": "create users"}
                )
            )
        ops = [r.get("operation") for r in result["data"]["results"]]
        assert "mutation" in ops, ops  # create_user is reachable via search

    async def test_search_no_match_returns_empty_with_hint(self, mcp):
        async with Client(mcp.mcp) as client:
            result = tool_payload(
                await client.call_tool(
                    "search_fields", {"domain": "billing", "query": "banana"}
                )
            )
        assert result["success"] is True
        assert result["data"]["results"] == []
        assert "list_queries" in result["hint"]

    async def test_search_unknown_domain(self, mcp):
        async with Client(mcp.mcp) as client:
            result = tool_payload(
                await client.call_tool(
                    "search_fields", {"domain": "nope", "query": "x"}
                )
            )
        assert result["success"] is False
        assert result["error_type"] == "domain_not_found"

    async def test_search_top_k_bounded(self, mcp):
        async with Client(mcp.mcp) as client:
            result = tool_payload(
                await client.call_tool(
                    "search_fields",
                    {"domain": "iam", "query": "users", "top_k": 1},
                )
            )
        assert len(result["data"]["results"]) <= 1

    async def test_search_description_terms_match(self, mcp):
        """Terms from docstring descriptions (the arg-less brief text) hit."""
        async with Client(mcp.mcp) as client:
            result = tool_payload(
                await client.call_tool(
                    "search_fields", {"domain": "billing", "query": "invoices"}
                )
            )
        assert result["data"]["results"], "billing has list_invoices, must match"
