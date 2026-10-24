"""tools mode e2e: the per-route toolset over the in-memory fastmcp Client.

Pins the mode's contract: one standalone-GraphQL-entry tool per route —
typed input schema (body models included), GraphQL-derived output schema
envelope, method-derived annotations, `fields` projection, the ToolError
channel (missing args, HTTP errors, invalid selections), and the
include-filter narrowing.
"""


import pytest
from fastapi import FastAPI
from fastmcp import Client
from fastmcp.exceptions import ToolError

from fastapi_mcp_neo import FastAPIMCP
from tests.support.apps import users_app


@pytest.fixture
def mcp():
    return FastAPIMCP(users_app(), name="users", mode="tools")


class TestToolset:
    async def test_one_tool_per_route(self, mcp):
        async with Client(mcp.mcp) as client:
            tools = {t.name: t for t in await client.list_tools()}
        assert sorted(tools) == ["create_user", "get_user", "list_users"]

    async def test_annotations_derive_from_http_method(self, mcp):
        async with Client(mcp.mcp) as client:
            tools = {t.name: t for t in await client.list_tools()}
        assert tools["get_user"].annotations.read_only_hint is True
        assert tools["list_users"].annotations.read_only_hint is True
        assert tools["create_user"].annotations.destructive_hint is True

    async def test_no_executor_tools_in_this_mode(self, mcp):
        async with Client(mcp.mcp) as client:
            names = {t.name for t in await client.list_tools()}
        assert "graphql_query" not in names
        assert "graphql_mutation" not in names
        assert "get_schema" not in names


class TestInputSchema:
    async def test_path_param_required_and_typed(self, mcp):
        async with Client(mcp.mcp) as client:
            tools = {t.name: t for t in await client.list_tools()}
        schema = tools["get_user"].inputSchema
        assert schema["required"] == ["user_id"]
        assert schema["properties"]["user_id"]["type"] == "integer"

    async def test_body_model_keeps_field_typing(self, mcp):
        """The GraphQL schema is the typing source: InputObjectType args
        carry their properties (no dict blind box)."""
        async with Client(mcp.mcp) as client:
            tools = {t.name: t for t in await client.list_tools()}
        payload = tools["create_user"].inputSchema["properties"]["payload"]
        assert payload["type"] == "object"
        assert payload["properties"]["name"] == {"type": "string"}
        # nullable-with-default: null-union + default, not in required
        assert payload["properties"]["email"] == {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "default": None,
        }
        assert payload["required"] == ["name"]

    async def test_fields_rides_along_optional(self, mcp):
        async with Client(mcp.mcp) as client:
            tools = {t.name: t for t in await client.list_tools()}
        assert "user_id" in tools["get_user"].inputSchema["required"]
        assert "fields" not in tools["get_user"].inputSchema.get("required", [])


class TestOutputSchema:
    async def test_data_envelope_without_inner_required(self, mcp):
        async with Client(mcp.mcp) as client:
            tools = {t.name: t for t in await client.list_tools()}
        schema = tools["get_user"].outputSchema
        assert schema["required"] == ["data"]
        data = schema["properties"]["data"]
        if "anyOf" in data:  # nullable route field: union with null
            assert data["anyOf"][1] == {"type": "null"}
            data = data["anyOf"][0]
        assert data["type"] == "object"
        assert "required" not in data  # projection returns subsets


class TestExecution:
    async def test_call_returns_data_envelope(self, mcp):
        async with Client(mcp.mcp) as client:
            r = await client.call_tool("get_user", {"user_id": 1})
        assert r.structured_content == {
            "data": {"id": 1, "name": "alice", "email": "a@x.io"}
        }

    async def test_fields_projection(self, mcp):
        async with Client(mcp.mcp) as client:
            full = await client.call_tool("get_user", {"user_id": 1})
            proj = await client.call_tool(
                "get_user", {"user_id": 1, "fields": "{ id name }"}
            )
        assert proj.structured_content == {"data": {"id": 1, "name": "alice"}}
        assert len(str(proj.structured_content)) < len(str(full.structured_content))

    async def test_missing_required_argument_message(self, mcp):
        async with Client(mcp.mcp) as client:
            with pytest.raises(ToolError, match="missing required argument"):
                await client.call_tool("get_user", {})

    async def test_http_error_rides_isError_with_status(self, mcp):
        async with Client(mcp.mcp) as client:
            with pytest.raises(ToolError, match="404"):
                await client.call_tool("get_user", {"user_id": 999})

    async def test_invalid_selection_is_rejected(self, mcp):
        async with Client(mcp.mcp) as client:
            with pytest.raises(ToolError):
                await client.call_tool(
                    "get_user", {"user_id": 1, "fields": "{ no_such_field }"}
                )

    async def test_body_round_trip(self, mcp):
        async with Client(mcp.mcp) as client:
            r = await client.call_tool(
                "create_user", {"payload": {"name": "carol"}}
            )
        assert r.structured_content["data"]["name"] == "carol"


class TestCombination:
    async def test_include_filter_narrows_the_toolset(self):
        mcp = FastAPIMCP(
            users_app(), name="one", mode="tools", include=["/users/{user_id}"]
        )
        async with Client(mcp.mcp) as client:
            names = sorted(t.name for t in await client.list_tools())
        assert names == ["get_user"]

    async def test_no_arg_route_document_is_valid(self):
        """Regression: routes without arguments must not render `ping()`."""
        app = FastAPI()

        @app.get("/ping", tags=["misc"])
        async def ping() -> dict:
            return {"ok": True}

        mcp = FastAPIMCP(app, name="ping", mode="tools")
        async with Client(mcp.mcp) as client:
            r = await client.call_tool("ping", {})
        assert r.structured_content == {"data": {"ok": True}}
