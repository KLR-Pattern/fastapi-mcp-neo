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


class TestSchemaEdges:
    """Mapper edge branches: enums, JSON scalars, nullable list items,
    and the deep-input walk cap."""

    @pytest.fixture
    def edges_mcp(self):
        from enum import Enum
        from typing import Literal

        from pydantic import BaseModel

        class Level3(BaseModel):
            v: int

        class Level2(BaseModel):
            three: Level3
            nums: list[int | None] = []

        class Level1(BaseModel):
            two: Level2

        class DeepIn(BaseModel):
            one: Level1

        class Color(str, Enum):
            red = "red"
            blue = "blue"

        class ThingOut(BaseModel):
            color: Color
            note: str

        app = FastAPI()

        @app.get("/things", tags=["edges"])
        async def list_things(kind: Literal["a", "b"] = "a") -> list[ThingOut | None]:
            return [ThingOut(color=Color.red, note="x"), None]

        class DeepOut(BaseModel):
            one: Level1

        @app.get("/blob", tags=["edges"])
        async def blob() -> dict:
            return {"any": [1, 2]}

        @app.get("/search", tags=["edges"])
        async def search(
            tags: list[str] | None = None,
            color: Color = Color.red,
            meta: dict | None = None,
        ) -> DeepOut:
            return DeepOut(one=Level1(two=Level2(three=Level3(v=1))))

        @app.post("/deep", tags=["edges"])
        async def deep(payload: DeepIn) -> bool:
            return True

        return FastAPIMCP(app, name="edges", mode="tools")

    async def test_enum_arg_enum_field_literal_scalar(self, edges_mcp):
        """Enum -> GraphQLEnumType (values in the schema); Literal -> its
        underlying scalar with the default carried (this repo's mapping)."""
        async with Client(edges_mcp.mcp) as client:
            tools = {t.name: t for t in await client.list_tools()}
        kind = tools["list_things"].inputSchema["properties"]["kind"]
        assert kind == {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "default": "a",
        }
        data = tools["list_things"].outputSchema["properties"]["data"]
        array = data["anyOf"][0] if "anyOf" in data else data  # nullable field
        item = array["items"]["anyOf"][0]  # nullable list items
        assert item["properties"]["color"]["enum"] == ["red", "blue"]

    async def test_nullable_list_items_null_union(self, edges_mcp):
        async with Client(edges_mcp.mcp) as client:
            tools = {t.name: t for t in await client.list_tools()}
        data = tools["list_things"].outputSchema["properties"]["data"]
        array = data["anyOf"][0] if "anyOf" in data else data
        items = array["items"]
        assert "anyOf" in items and {"type": "null"} in items["anyOf"]

    async def test_json_scalar_response_is_any(self, edges_mcp):
        async with Client(edges_mcp.mcp) as client:
            tools = {t.name: t for t in await client.list_tools()}
        data = tools["blob"].outputSchema["properties"]["data"]
        assert data == {"anyOf": [{}, {"type": "null"}]}

    async def test_deep_input_walk_caps(self, edges_mcp):
        async with Client(edges_mcp.mcp) as client:
            tools = {t.name: t for t in await client.list_tools()}
        payload = tools["deep"].inputSchema["properties"]["payload"]
        one = payload["properties"]["one"]
        two = one["properties"]["two"]
        three = two["properties"]["three"]
        assert three == {"type": "object"}  # capped: no properties below
        # nullable list items inside a body model carry the null-union
        nums = two["properties"]["nums"]  # nullable-with-default: union wrap
        assert nums["anyOf"][0]["items"] == {
            "anyOf": [{"type": "integer"}, {"type": "null"}]
        }

    async def test_default_full_selection_walk_caps(self, edges_mcp):
        """Calling without `fields` walks the default full selection, which
        omits object fields past the depth cap instead of recursing."""
        async with Client(edges_mcp.mcp) as client:
            r = await client.call_tool("search", {})
        one = r.structured_content["data"]["one"]
        # the default selection OMITS object fields past the depth cap
        # (a bare object field would be an invalid document); leaves stay
        assert "three" not in one["two"]
        assert one["two"]["nums"] == []

    async def test_list_enum_json_args(self, edges_mcp):
        async with Client(edges_mcp.mcp) as client:
            tools = {t.name: t for t in await client.list_tools()}
        props = tools["search"].inputSchema["properties"]
        assert props["tags"] == {
            "anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "null"}],
            "default": None,
        }
        assert props["color"] == {
            "anyOf": [{"type": "string", "enum": ["red", "blue"]}, {"type": "null"}],
            "default": "red",
        }
        assert props["meta"] == {
            "anyOf": [{}, {"type": "null"}],
            "default": None,
        }  # JSON scalar: any shape

    async def test_deep_output_walk_caps(self, edges_mcp):
        async with Client(edges_mcp.mcp) as client:
            tools = {t.name: t for t in await client.list_tools()}
        data = tools["search"].outputSchema["properties"]["data"]
        data = data["anyOf"][0] if "anyOf" in data else data
        one = data["properties"]["one"]
        two = one["properties"]["two"]
        three = two["properties"]["three"]
        assert three == {"type": "object"}  # capped: no properties below


class TestMultiSegmentTags:
    """Regression (review #1): multi-segment tags (iam:users) nest group
    types arbitrarily deep. Registration is route-driven and follows the
    pre-split domain path, so tools register per ROUTE regardless of the
    group tree's depth — never per subgroup, never blind-firing a group."""

    @pytest.fixture
    def seg_mcp(self):
        from pydantic import BaseModel

        class UserOut(BaseModel):
            uid: int
            name: str

        class RoleOut(BaseModel):
            name: str

        app = FastAPI()

        @app.get("/users/{uid}", tags=["iam:users"])
        async def get_user(uid: int) -> UserOut:
            return UserOut(uid=uid, name="alice")

        @app.delete("/users/{uid}", tags=["iam:users"])
        async def delete_user(uid: int) -> bool:
            return True

        @app.get("/roles", tags=["iam:roles"])
        async def list_roles() -> list[RoleOut]:
            return [RoleOut(name="admin")]

        return FastAPIMCP(app, name="multi", mode="tools")

    async def test_tools_register_per_route_not_per_subgroup(self, seg_mcp):
        async with Client(seg_mcp.mcp) as client:
            names = sorted(t.name for t in await client.list_tools())
        assert names == ["delete_user", "get_user", "list_roles"]

    async def test_deeply_nested_paths_call_their_own_route(self, seg_mcp):
        async with Client(seg_mcp.mcp) as client:
            r = await client.call_tool("get_user", {"uid": 7})
            roles = await client.call_tool("list_roles", {})
            mut = await client.call_tool("delete_user", {"uid": 7})
        assert r.structured_content == {"data": {"uid": 7, "name": "alice"}}
        assert roles.structured_content == {"data": [{"name": "admin"}]}
        assert mut.structured_content == {"data": True}

    async def test_untagged_default_domain_still_works(self, mcp):
        async with Client(mcp.mcp) as client:
            names = sorted(t.name for t in await client.list_tools())
        assert "get_user" in names  # users_app single-segment, unchanged
