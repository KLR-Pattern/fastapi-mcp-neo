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
    async def test_tag_filter_narrows_the_toolset(self):
        app = FastAPI()

        @app.get("/a/{uid}", tags=["alpha"])
        async def get_a(uid: int) -> dict:
            return {"uid": uid}

        @app.get("/b/{uid}", tags=["beta"])
        async def get_b(uid: int) -> dict:
            return {"uid": uid}

        mcp = FastAPIMCP(app, name="one", mode="tools", include_tags=["alpha"])
        async with Client(mcp.mcp) as client:
            names = sorted(t.name for t in await client.list_tools())
        assert names == ["get_a"]

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


class TestEmptySelectionCap:
    """Regression (review #5): an all-capped selection level used to
    yield a truthy-but-invalid "{  }" at ANY nesting depth. Now: empty
    levels are omitted by their parent; a type with no default selection
    at all marks `fields` required and explains itself on call."""

    @pytest.fixture
    def cap_mcp(self):
        from pydantic import BaseModel

        class Deeper(BaseModel):
            v: int

        class Deep(BaseModel):
            dd: Deeper

        class Mid(BaseModel):
            d: Deep

        class Root(BaseModel):  # has a leaf: valid one-field default
            val: int
            mid: Mid

        class Root2(BaseModel):  # every chain hits the cap: no default
            mid: Mid

        app = FastAPI()

        @app.get("/mixed", tags=["cap"])
        async def mixed() -> Root:
            return Root(val=1, mid=Mid(d=Deep(dd=Deeper(v=9))))

        @app.get("/chain", tags=["cap"])
        async def chain() -> Root2:
            return Root2(mid=Mid(d=Deep(dd=Deeper(v=9))))

        return FastAPIMCP(app, name="cap", mode="tools")

    async def test_mid_level_all_capped_is_omitted_not_invalid(self, cap_mcp):
        async with Client(cap_mcp.mcp) as client:
            r = await client.call_tool("mixed", {})
        # 'mid' silently vanished into "{  }" before; now it is simply
        # omitted from the default selection and the call succeeds
        assert r.structured_content == {"data": {"val": 1}}

    async def test_no_default_selection_requires_fields(self, cap_mcp):
        async with Client(cap_mcp.mcp) as client:
            tools = {t.name: t for t in await client.list_tools()}
        assert "fields" in tools["chain"].inputSchema["required"]

    async def test_no_fields_call_gets_clear_error(self, cap_mcp):
        async with Client(cap_mcp.mcp) as client:
            with pytest.raises(ToolError, match="depth cap"):
                await client.call_tool("chain", {})

    async def test_explicit_fields_reaches_the_depth(self, cap_mcp):
        async with Client(cap_mcp.mcp) as client:
            r = await client.call_tool(
                "chain", {"fields": "{ mid { d { dd { v } } } }"}
            )
        assert r.structured_content == {"data": {"mid": {"d": {"dd": {"v": 9}}}}}


class TestPerCallerCredentials:
    """Regression (review #2): tools mode dropped per-caller headers —
    every protected route answered 401 while graphql mode worked. The
    caller's whitelisted headers now ride each tool invocation."""

    async def test_authorization_reaches_the_route(self):
        from asgi_lifespan import LifespanManager
        from fastmcp.client.transports import StreamableHttpTransport

        from tests.support.apps import auth_app
        from tests.support.mcp import asgi_client_factory

        app = auth_app()
        mcp = FastAPIMCP(
            app, name="tools-auth", mode="tools", passthrough_headers=["authorization"]
        )
        mcp.mount_to(app, "/mcp")
        transport = StreamableHttpTransport(
            url="http://testserver/mcp/",
            headers={"authorization": "Bearer jwt-alice"},
            httpx_client_factory=asgi_client_factory(app),
        )
        async with LifespanManager(app):
            async with Client(transport) as client:
                r = await client.call_tool("whoami", {})
        assert r.structured_content == {"data": {"user": "alice"}}
        await mcp.handler.aclose()

    async def test_passthrough_off_blocks_credentials(self):
        from asgi_lifespan import LifespanManager
        from fastmcp.client.transports import StreamableHttpTransport

        from tests.support.apps import auth_app
        from tests.support.mcp import asgi_client_factory

        app = auth_app()
        mcp = FastAPIMCP(app, name="tools-noauth", mode="tools", passthrough_headers=[])
        mcp.mount_to(app, "/mcp")
        transport = StreamableHttpTransport(
            url="http://testserver/mcp/",
            headers={"authorization": "Bearer jwt-alice"},
            httpx_client_factory=asgi_client_factory(app),
        )
        async with LifespanManager(app):
            async with Client(transport) as client:
                with pytest.raises(ToolError, match="401"):
                    await client.call_tool("whoami", {})
        await mcp.handler.aclose()


class TestVesselNameSanitization:
    """Regression (review #3): raw GraphQL arg names crashed construction —
    a route param named `fields` duplicated the projection parameter, and
    aliases only guaranteed GraphQL-legal names (Python keywords are legal
    GraphQL). The signature carries sanitized identifiers; the schema and
    the document keep the real names."""

    @pytest.fixture
    def san_mcp(self):
        from fastapi import Query
        from pydantic import BaseModel

        class SparseOut(BaseModel):
            q: str
            note: str

        app = FastAPI()

        @app.get("/sparse", tags=["san"])
        async def sparse(q: str = "", fields: str = "id,title") -> SparseOut:
            return SparseOut(q=q, note=fields)

        @app.get("/kw", tags=["san"])
        async def kw_alias(from_: str = Query(alias="from")) -> str:
            return from_

        return FastAPIMCP(app, name="san", mode="tools")

    async def test_construction_survives_and_names_stay_real(self, caplog):
        from fastapi import Query
        from pydantic import BaseModel

        class SparseOut(BaseModel):
            q: str
            note: str

        app = FastAPI()

        @app.get("/sparse", tags=["san"])
        async def sparse(q: str = "", fields: str = "id,title") -> SparseOut:
            return SparseOut(q=q, note=fields)

        @app.get("/kw", tags=["san"])
        async def kw_alias(from_: str = Query(alias="from")) -> str:
            return from_

        with caplog.at_level("WARNING", logger="fastapi_mcp_neo.mcp.route_tools"):
            mcp = FastAPIMCP(app, name="san", mode="tools")
        async with Client(mcp.mcp) as client:
            tools = {t.name: t for t in await client.list_tools()}
        # keyword-alias route: outside fastmcp's signature-binding ABI —
        # skipped WITH a warning, not a crash
        assert sorted(tools) == ["sparse"]
        assert "'from'" in caplog.text and "/kw" in caplog.text
        # the route's OWN fields param is an ordinary typed arg...
        assert tools["sparse"].inputSchema["properties"]["fields"] == {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "default": "id,title",
        }
        # ...and the projection parameter moved aside
        assert "select" in tools["sparse"].inputSchema["properties"]

    async def test_calls_work_through_the_renamed_projection(self, san_mcp):
        async with Client(san_mcp.mcp) as client:
            sparse = await client.call_tool(
                "sparse", {"select": "{ q }"}  # projection via renamed param
            )
        assert sparse.structured_content == {"data": {"q": ""}}  # projection drops note


class TestNameCollisions:
    """Regression (review #4): same-named functions in different domains
    silently replaced each other in the tool registry. Names now resolve
    through an identity ladder; groups skip level 1 symmetrically."""

    @pytest.fixture
    def col_mcp(self):
        from pydantic import BaseModel

        class UOut(BaseModel):
            uid: int

        app = FastAPI()

        @app.get("/a/{uid}", tags=["iam"])
        async def get_user(uid: int) -> UOut:
            return UOut(uid=uid)

        @app.get("/b/{uid}", tags=["billing"])
        async def get_user(uid: int) -> UOut:  # noqa: F811
            return UOut(uid=uid * 10)

        @app.get("/literal", tags=["misc"])
        async def iam_get_user() -> bool:
            return True

        return FastAPIMCP(app, name="collide", mode="tools")

    async def test_ladder_resolves_all_levels(self, col_mcp):
        async with Client(col_mcp.mcp) as client:
            names = sorted(t.name for t in await client.list_tools())
            a = await client.call_tool("get_user_get_a_uid", {"uid": 2})
            b = await client.call_tool("billing_get_user", {"uid": 2})
            lit = await client.call_tool("iam_get_user", {})
        # level 2 (domain), level 3 (path — level 2 stolen by the literal),
        # and the untouched unique name, all distinct
        assert names == ["billing_get_user", "get_user_get_a_uid", "iam_get_user"]
        assert a.structured_content == {"data": {"uid": 2}}
        assert b.structured_content == {"data": {"uid": 20}}
        assert lit.structured_content == {"data": True}

    async def test_long_path_collision_truncates_with_hash(self, caplog):
        from pydantic import BaseModel

        class Out(BaseModel):
            ok: bool

        app = FastAPI()
        seg = "z" * 60

        @app.get(f"/{seg}/{{uid}}", tags=["deep"])
        async def probe(uid: int) -> Out:
            return Out(ok=True)

        @app.get("/p", tags=["alt"])
        async def probe(uid: int = 0) -> Out:  # noqa: F811
            return Out(ok=False)

        @app.get("/short", tags=["misc"])
        async def deep_probe() -> bool:
            return True

        with caplog.at_level("WARNING", logger="fastapi_mcp_neo.mcp.route_tools"):
            mcp = FastAPIMCP(app, name="trunc", mode="tools")
        async with Client(mcp.mcp) as client:
            names = [t.name for t in await client.list_tools()]
        assert all(len(n) <= 64 for n in names)
        assert len(set(names)) == 3
        assert "host bound" in caplog.text


class TestNullVsOmitted:
    """Review #8: tools mode filtered explicit nulls together with omitted
    arguments BEFORE the engine could see them. The signature now defaults
    to a sentinel, so nulls reach the engine — which defines the semantics
    uniformly for every mode (verified against graphql mode):
      query param null == omitted (invoker sends nothing; route default)
      body field null  == a real null (travels into the route)"""

    @pytest.fixture
    def null_mcp(self):
        from pydantic import BaseModel

        class Echo(BaseModel):
            kind: str | None

        class Payload(BaseModel):
            kind: str | None = "default"

        app = FastAPI()

        @app.get("/echo", tags=["null"])
        async def echo(kind: str | None = "default") -> Echo:
            return Echo(kind=kind)

        @app.post("/echo-body", tags=["null"])
        async def echo_body(payload: Payload) -> Echo:
            return Echo(kind=payload.kind)

        return FastAPIMCP(app, name="null", mode="tools")

    async def test_omitted_query_gets_route_default(self, null_mcp):
        async with Client(null_mcp.mcp) as client:
            r = await client.call_tool("echo", {})
        assert r.structured_content == {"data": {"kind": "default"}}

    async def test_explicit_query_null_matches_graphql_mode(self, null_mcp):
        """Engine semantics: a null query param is not sent (the invoker
        drops it), exactly like graphql mode with $k: null."""
        async with Client(null_mcp.mcp) as client:
            r = await client.call_tool("echo", {"kind": None})
        assert r.structured_content == {"data": {"kind": "default"}}

    async def test_body_field_null_is_a_real_null(self, null_mcp):
        async with Client(null_mcp.mcp) as client:
            r = await client.call_tool(
                "echo_body", {"payload": {"kind": None}}
            )
        assert r.structured_content == {"data": {"kind": None}}


class TestProjectionNudge:
    """Agents omit optional parameters whose benefit is not stated, so
    tools mode ships default instructions naming `fields`, and the
    parameter description leads with the benefit."""

    async def test_default_instructions_mention_fields(self, mcp):
        assert mcp.mcp.instructions and "`fields`" in mcp.mcp.instructions

    async def test_user_instructions_win(self):
        mcp = FastAPIMCP(
            users_app(), name="custom", mode="tools", instructions="mine"
        )
        assert mcp.mcp.instructions == "mine"

    async def test_other_modes_get_no_default_instructions(self):
        mcp = FastAPIMCP(users_app(), name="gql", mode="graphql")
        assert mcp.mcp.instructions is None

    async def test_fields_description_leads_with_benefit(self, mcp):
        async with Client(mcp.mcp) as client:
            t = {x.name: x for x in await client.list_tools()}["get_user"]
        desc = t.inputSchema["properties"]["fields"]["description"]
        assert desc.startswith("Pass to fetch only these fields")


class TestServerErrorIsError:
    """e2e arm of the unhandled-exception parity: the tool call answers on
    the isError channel with the generic 500 text, not the exception."""

    async def test_500_rides_isError_without_leaking(self):
        from pydantic import BaseModel

        class Out(BaseModel):
            ok: bool

        app = FastAPI()

        @app.get("/boom", tags=["t"])
        async def boom() -> Out:
            raise RuntimeError("secret-internal-detail")

        mcp = FastAPIMCP(app, name="boom", mode="tools")
        async with Client(mcp.mcp) as client:
            with pytest.raises(ToolError, match="500"):
                await client.call_tool("boom", {})
