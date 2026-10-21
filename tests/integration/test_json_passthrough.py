"""JSON pass-through: endpoints/fields whose shape is dynamic by declaration.

The line this locks in:
- explicit ``dict`` / ``dict[K, V]`` / ``Any`` / ``object`` annotations →
  bridged as the ``JSON`` scalar (reachable, shape only known at runtime)
- NO annotation and no response_model → stays skipped (no contract)
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

from fastapi_mcp_neo import RouterGraphQLHandler


class WithMeta(BaseModel):
    """Model carrying a dynamic dict field."""

    id: int
    meta: dict[str, Any] = {}


def build_app() -> FastAPI:
    app = FastAPI()

    @app.get("/raw", tags=["misc"])
    async def raw() -> dict[str, Any]:
        """Dynamic-shaped payload (dict by declaration)."""
        return {"a": 1, "nested": {"b": [1, 2, 3]}, "ok": True}

    @app.get("/loose", tags=["misc"])
    async def loose() -> Any:
        """Even looser: Any."""
        return [1, "two", {"three": 3}]

    @app.get("/withmeta", response_model=WithMeta, tags=["misc"])
    async def withmeta() -> WithMeta:
        """Typed model with a dynamic dict field inside."""
        return WithMeta(id=7, meta={"k": "v", "n": 2})

    @app.post("/echo", tags=["misc"])
    async def echo(payload: dict[str, Any]) -> dict[str, Any]:
        """Arbitrary JSON body in, same shape out."""
        return payload

    @app.get("/untyped", tags=["misc"])
    async def untyped():  # deliberately no annotation
        return {"invisible": True}

    return app


async def make_handler() -> RouterGraphQLHandler:
    return RouterGraphQLHandler(build_app(), allow_mutation=True)


class TestJSONPassthrough:
    async def test_dict_and_any_fields_bridged_as_json(self):
        handler = await make_handler()
        sdl = handler.get_sdl()
        assert "raw: JSON" in sdl
        assert "loose: JSON" in sdl
        assert 'scalar JSON' in sdl

    async def test_untyped_route_bridges_as_json(self):
        """Untyped endpoints bridge as raw JSON (available, not skipped) —
        flagged in the degraded startup notice instead."""
        handler = await make_handler()
        sdl = handler.get_sdl()
        assert "untyped: JSON" in sdl

    async def test_nested_model_dict_field_becomes_json(self):
        handler = await make_handler()
        sdl = handler.get_sdl()
        assert "meta: JSON" in sdl
        result = await handler.execute("{ misc { withmeta { id meta } } }")
        assert result == {
            "data": {"misc": {"withmeta": {"id": 7, "meta": {"k": "v", "n": 2}}}}
        }

    async def test_json_output_roundtrip(self):
        handler = await make_handler()
        result = await handler.execute("{ misc { raw loose } }")
        data = result["data"]["misc"]
        assert data["raw"] == {"a": 1, "nested": {"b": [1, 2, 3]}, "ok": True}
        assert data["loose"] == [1, "two", {"three": 3}]

    async def test_json_input_roundtrip(self):
        handler = await make_handler()
        result = await handler.execute(
            "mutation($p: JSON!) { misc { echo(payload: $p) } }",
            variables={"p": {"anything": [1, 2], "goes": "here"}},
        )
        assert result == {"data": {"misc": {"echo": {"anything": [1, 2], "goes": "here"}}}}

    async def test_json_input_inline_literal(self):
        handler = await make_handler()
        result = await handler.execute(
            'mutation { misc { echo(payload: {inline: "value", n: 1}) } }'
        )
        assert result == {"data": {"misc": {"echo": {"inline": "value", "n": 1}}}}
