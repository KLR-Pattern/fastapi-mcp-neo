"""MCP simple mode tools: schema exposure, graphql_query/mutation, tool
annotations and instructions — via the in-memory fastmcp Client."""

import pytest
from fastmcp import Client

from fastapi_mcp_neo import FastAPIMCP
from tests.support.apps import users_app
from tests.support.mcp import tool_payload


@pytest.fixture
def mcp():
    return FastAPIMCP(users_app(), name="test-api", allow_mutation=True)


class TestGetSchema:
    async def test_get_schema_returns_sdl(self, mcp):
        async with Client(mcp.mcp) as client:
            result = tool_payload(await client.call_tool("get_schema", {}))
        assert result["success"] is True
        assert "type Query {" in result["data"]["sdl"]
        assert "get_user(user_id: Int!): UserOut" in result["data"]["sdl"]
        assert result["hint"]


class TestGraphqlQuery:
    async def test_query_variables(self, mcp):
        async with Client(mcp.mcp) as client:
            result = tool_payload(
                await client.call_tool(
                    "graphql_query",
                    {
                        "query": "query($id: Int!) { iam { get_user(user_id: $id)"
                        " { name email } } }",
                        "variables": {"id": 1},
                    },
                )
            )
        assert result["data"]["data"]["iam"]["get_user"] == {
            "name": "alice",
            "email": "a@x.io",
        }

    async def test_field_projection_and_composition(self, mcp):
        async with Client(mcp.mcp) as client:
            result = tool_payload(
                await client.call_tool(
                    "graphql_query",
                    {
                        "query": "{ iam { a: list_users(active: false) { id } "
                        "b: get_user(user_id: 2) { name } } }"
                    },
                )
            )
        data = result["data"]["data"]
        assert data["iam"]["a"] == []
        assert data["iam"]["b"] == {"name": "bob"}

    async def test_partial_failure_keeps_siblings(self, mcp):
        async with Client(mcp.mcp) as client:
            result = tool_payload(
                await client.call_tool(
                    "graphql_query",
                    {
                        "query": "{ iam { ok: get_user(user_id: 1) { name } "
                        "missing: get_user(user_id: 99) { name } } }"
                    },
                )
            )
        data = result["data"]["data"]
        assert data["iam"]["ok"] == {"name": "alice"}
        assert data["iam"]["missing"] is None
        assert result["data"]["errors"][0]["extensions"]["code"] == "HTTP_404"

    async def test_invalid_query_error_envelope(self, mcp):
        async with Client(mcp.mcp) as client:
            result = tool_payload(
                await client.call_tool("graphql_query", {"query": "{ nonsense }"})
            )
        assert result["success"] is False
        assert result["error_type"] == "query_execution_error"
        assert "Cannot query field" in result["error"]
        assert result["hint"]

    async def test_query_rejects_mutation_document(self, mcp):
        async with Client(mcp.mcp) as client:
            result = tool_payload(
                await client.call_tool(
                    "graphql_query",
                    {"query": 'mutation { iam { create_user(payload: {name: "x"}) { id } } }'},
                )
            )
        assert result["success"] is False


class TestMutationTool:
    async def test_mutation_execution(self, mcp):
        async with Client(mcp.mcp) as client:
            result = tool_payload(
                await client.call_tool(
                    "graphql_mutation",
                    {
                        "mutation": 'mutation($p: UserCreateInput!) '
                        '{ iam { create_user(payload: $p) { id name } } }',
                        "variables": {"p": {"name": "carol"}},
                    },
                )
            )
        assert result["data"]["data"]["iam"]["create_user"]["name"] == "carol"

    async def test_no_mutation_tool_when_disabled(self):
        mcp = FastAPIMCP(users_app(), name="ro")
        async with Client(mcp.mcp) as client:
            tools = await client.list_tools()
        assert [t.name for t in tools] == ["get_schema", "graphql_query"]


class TestToolAnnotations:
    async def test_simple_mode_hints(self, mcp):
        async with Client(mcp.mcp) as client:
            tools = {t.name: t for t in await client.list_tools()}
        assert tools["get_schema"].annotations.read_only_hint is True
        assert tools["graphql_query"].annotations.read_only_hint is True
        assert tools["graphql_mutation"].annotations.destructive_hint is True


class TestInstructions:
    async def test_instructions_reach_the_handshake(self):
        text = "Domains: iam. Compose routes in one graphql_query."
        mcp = FastAPIMCP(users_app(), name="guided", instructions=text)
        async with Client(mcp.mcp) as client:
            assert client.instructions == text

    async def test_no_instructions_by_default(self, mcp):
        async with Client(mcp.mcp) as client:
            assert client.instructions is None
