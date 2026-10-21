"""L3 regression: one MCP query -> ONE trace across tool, GraphQL and route.

Before the bridge injected W3C trace context, fastmcp's ``tools/call`` span
and FastAPI's native route spans landed as two unrelated traces (route spans
were orphans — no traceparent on the in-process ASGI call). These tests lock
the merged waterfall in.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import FastAPI, Request
from fastmcp import Client
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from fastapi_mcp_neo import FastAPIMCP

_exporter = InMemorySpanExporter()
_provider_installed = False


@pytest.fixture
def spans():
    global _provider_installed
    if not _provider_installed:
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(_exporter))
        trace.set_tracer_provider(provider)
        _provider_installed = True
    _exporter.clear()
    yield _exporter


def build_app() -> FastAPI:
    app = FastAPI()

    @app.get("/echo-headers", tags=["t"])
    async def echo_headers(request: Request) -> dict:
        """Echo inbound request headers (dict -> JSON pass-through)."""
        return dict(request.headers)

    return app


def by_name(exporter, name: str):
    return [s for s in exporter.get_finished_spans() if s.name == name]


QUERY = "{ t { echo_headers } }"


class TestTraceUnity:
    async def test_one_trace_from_tool_to_route(self, spans):
        mcp = FastAPIMCP(build_app(), name="otel-e2e")
        async with Client(mcp.mcp) as client:
            await client.call_tool("graphql_query", {"query": QUERY})

        tools = by_name(spans, "tools/call graphql_query")  # client+server side
        gqls = by_name(spans, "graphql.execute")
        routes = by_name(spans, "GET /echo-headers")
        assert tools and gqls and routes, [s.name for s in spans.get_finished_spans()]

        gql = gqls[0]
        # graphql.execute nests under the SERVER-side tools/call span …
        parent_tool = next(
            (t for t in tools if t.context.span_id == gql.parent.span_id), None
        )
        assert parent_tool is not None, "graphql.execute must nest under tools/call"
        # … the route span nests under graphql.execute …
        route = routes[0]
        assert route.parent.span_id == gql.context.span_id
        # … and all three share ONE trace.
        assert len({parent_tool.context.trace_id, gql.context.trace_id,
                    route.context.trace_id}) == 1

    async def test_traceparent_reaches_the_route(self, spans):
        mcp = FastAPIMCP(build_app(), name="otel-e2e")
        async with Client(mcp.mcp) as client:
            result = json.loads(
                (await client.call_tool("graphql_query", {"query": QUERY})).content[0].text
            )
        headers = result["data"]["data"]["t"]["echo_headers"]
        assert "traceparent" in headers

    async def test_injection_is_independent_of_passthrough_whitelist(self, spans):
        # bridge-GENERATED context must flow even when NOTHING may be
        # forwarded from the caller
        mcp = FastAPIMCP(build_app(), name="otel-e2e", passthrough_headers=[])
        async with Client(mcp.mcp) as client:
            result = json.loads(
                (await client.call_tool("graphql_query", {"query": QUERY})).content[0].text
            )
        headers = result["data"]["data"]["t"]["echo_headers"]
        assert "traceparent" in headers

    async def test_timeout_records_span_event(self, spans):
        app = FastAPI()

        @app.get("/slow", tags=["t"])
        async def slow() -> dict:
            """Deliberately slow route (dict -> JSON pass-through)."""
            await asyncio.sleep(2)
            return {}

        mcp = FastAPIMCP(app, name="otel-e2e", request_timeout=0.1)
        async with Client(mcp.mcp) as client:
            result = json.loads(
                (
                    await client.call_tool(
                        "graphql_query", {"query": "{ t { slow } }"}
                    )
                ).content[0].text
            )
        assert result["data"]["data"]["t"]["slow"] is None  # field nulled, not the query
        gql = by_name(spans, "graphql.execute")
        assert gql, [s.name for s in spans.get_finished_spans()]
        events = [e.name for s in gql for e in s.events]
        assert "route.timeout" in events
