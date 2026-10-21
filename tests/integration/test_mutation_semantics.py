"""Mutation execution semantics (graphql-core spec behavior, pinned).

GraphQL guarantees mutation ROOT fields execute serially in declaration
order. With the domain-grouped schema, writes live one level below the root,
so same-group siblings execute in parallel while cross-group writes (root
fields) stay serial.
"""

import asyncio

import pytest
from fastapi import FastAPI
from pydantic import BaseModel

from fastapi_mcp_neo import RouterGraphQLHandler


class Out(BaseModel):
    id: int


@pytest.fixture
def app() -> FastAPI:
    app = FastAPI()
    log: list[str] = []

    def make(name: str, n: int):
        async def handler() -> Out:
            log.append(f"{name}:start")
            await asyncio.sleep(0.15)
            log.append(f"{name}:end")
            return Out(id=n)

        handler.__name__ = name
        return handler

    # a GET keeps the Query root legal; writes: two in "bench", one in "other"
    app.get("/read-x", response_model=Out, tags=["bench"])(make("read_x", 0))
    app.post("/create-a", response_model=Out, tags=["bench"])(make("create_a", 1))
    app.post("/create-b", response_model=Out, tags=["bench"])(make("create_b", 2))
    app.post("/create-c", response_model=Out, tags=["other"])(make("create_c", 3))
    app.state.log = log
    return app


async def test_same_group_mutations_run_in_parallel(app):
    handler = RouterGraphQLHandler(app, allow_mutation=True)
    result = await handler.execute(
        "mutation { bench { a: create_a { id } b: create_b { id } } }"
    )
    assert result["data"]["bench"] == {"a": {"id": 1}, "b": {"id": 2}}
    log = app.state.log
    # Both started before either finished: interleaved, not serial.
    assert log[0].endswith(":start") and log[1].endswith(":start")
    assert sorted(log[:2]) == ["create_a:start", "create_b:start"]
    await handler.aclose()


async def test_cross_group_mutations_run_serially(app):
    handler = RouterGraphQLHandler(app, allow_mutation=True)
    result = await handler.execute(
        "mutation { bench { create_a { id } } other { create_c { id } } }"
    )
    assert result["data"]["bench"]["create_a"]["id"] == 1
    assert result["data"]["other"]["create_c"]["id"] == 3
    # Root-level mutation fields execute strictly in declaration order.
    assert app.state.log == [
        "create_a:start", "create_a:end", "create_c:start", "create_c:end",
    ]
    await handler.aclose()
