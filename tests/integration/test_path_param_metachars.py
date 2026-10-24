"""Repro: tadata-org/fastapi_mcp#354 analog — path parameter values
containing URL metacharacters must not change which route runs, and must
not leak into the query string.

The bridge under test builds the URL via Starlette's url_path_for (no
manual string surgery) and dispatches in-process through ASGITransport,
so httpx re-parses the path string: a '?' inside a value could start a
query string, '#' a fragment, '/' extra segments. These tests pin the
actual behavior for each metacharacter.
"""

import asyncio

from fastapi import FastAPI
from pydantic import BaseModel

from fastapi_mcp_neo.handler import RouterGraphQLHandler


class FileOut(BaseModel):
    name: str
    q: str


class TreeOut(BaseModel):
    rest: str


def build_app() -> FastAPI:
    app = FastAPI()

    @app.get("/files/{name}", tags=["files"])
    async def get_file(name: str, q: str = "none") -> FileOut:
        return FileOut(name=name, q=q)

    @app.get("/admin", tags=["files"])
    async def admin() -> dict:
        return {"admin": True}

    @app.get("/tree/{rest:path}", tags=["files"])
    async def tree(rest: str) -> TreeOut:
        return TreeOut(rest=rest)

    return app


QUERY = (
    "query($n: String!, $q: String) { files { get_file(name: $n, q: $q) "
    "{ name q } } }"
)


async def _get_file(name_value: str, q_value: str = "own") -> dict:
    handler = RouterGraphQLHandler(build_app())
    try:
        return await handler.execute(
            QUERY, variables={"n": name_value, "q": q_value}
        )
    finally:
        await handler.aclose()


def _data(result: dict) -> dict | None:
    assert not result.get("errors"), result["errors"]
    return result["data"]["files"]["get_file"]


def test_space_value_round_trips():
    data = _data(asyncio.run(_get_file("report 2026.txt")))
    assert data == {"name": "report 2026.txt", "q": "own"}


def test_slash_in_value_does_not_reach_another_route():
    result = asyncio.run(_get_file("a/b"))
    # Acceptable: field error (route match fails -> 404). NOT acceptable:
    # a successful call to a different endpoint.
    errors = result.get("errors")
    assert errors, f"a/b unexpectedly resolved: {result['data']}"
    assert "a/b" not in str(result.get("data", ""))


def test_question_mark_stays_inside_the_path_parameter():
    data = _data(asyncio.run(_get_file("x?q=injected")))
    assert data["name"] == "x?q=injected", f"name truncated: {data!r}"
    assert data["q"] == "own", f"query injected: {data!r}"


def test_dot_dot_does_not_reach_admin():
    result = asyncio.run(_get_file("../admin"))
    errors = result.get("errors")
    assert errors, f"../admin unexpectedly resolved: {result['data']}"
    assert "admin" not in str(result.get("data", ""))


def test_fragment_in_value():
    data = _data(asyncio.run(_get_file("a#b")))
    assert data["name"] == "a#b", f"fragment dropped: {data!r}"


def test_path_convertor_value_round_trips():
    # {p:path} legitimately carries slashes: safe="" encoding must not
    # break multi-segment values (they decode back before routing).

    async def run() -> dict:
        handler = RouterGraphQLHandler(build_app())
        try:
            return await handler.execute(
                "query($r: String!) { files { tree(rest: $r) { rest } } }",
                variables={"r": "a/b/c?q=1"},
            )
        finally:
            await handler.aclose()

    result = asyncio.run(run())
    assert not result.get("errors"), result["errors"]
    assert result["data"]["files"]["tree"] == {"rest": "a/b/c?q=1"}


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                print(f"FAIL {name}: {e}")
