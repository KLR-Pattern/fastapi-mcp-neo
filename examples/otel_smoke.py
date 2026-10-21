"""Observability smoke test: OpenTelemetry spans around a GraphQL-over-MCP call.

Demonstrates what the observability stack gives you TODAY, with zero bridge
code — install an OpenTelemetry SDK, point it at an exporter, and spans
appear from two places that emit them natively:

1. fastmcp (the MCP face): ``tools/call graphql_query`` and friends.
2. FastAPI >= 0.142 (the wrapped app): ``GET /things`` plus
   ``fastapi.dependencies`` / ``fastapi.endpoint`` /
   ``fastapi.serialization`` — the route-level spans.
3. the bridge itself: ``graphql.execute`` (orchestration layer) and W3C
   trace context injected into each route call, stitching 1+2 into one
   trace (a no-op without an SDK installed).

All three verified live (2026-10-04). The bridge injects a W3C
``traceparent`` into every in-process route call and emits its own
``graphql.execute`` span, so the three layers land as ONE waterfall:
``tools/call graphql_query > graphql.execute > GET /things``.

Run it (no project deps touched — everything rides on ``--with``):

    # Mode 1 — console: spans printed as JSON to stdout (30s setup)
    uv run --with opentelemetry-sdk python examples/otel_smoke.py --mode console

    # Mode 2 — Jaeger: visual trace waterfalls in the browser
    docker run -d --name jaeger-smoke \\
        -p 16686:16686 -p 4317:4317 jaegertracing/all-in-one:latest
    # (Docker Hub unreachable? prefix the image with a mirror registry,
    #  e.g. docker.m.daocloud.io/jaegertracing/all-in-one:latest)
    uv run --with opentelemetry-sdk --with opentelemetry-exporter-otlp \\
        python examples/otel_smoke.py --mode otlp
    # then open http://localhost:16686 -> service "fastapi-mcp-neo-smoke"

Cleanup afterwards:

    docker stop jaeger-smoke && docker rm jaeger-smoke
"""

from __future__ import annotations

import argparse
import asyncio

# The SDK wiring below is the ONLY observability code needed — the business
# part (app + FastAPIMCP) stays untouched. Without an SDK installed, the
# opentelemetry-api layer is a no-op (NonRecordingSpan), so this script
# degrades to a plain query roundtrip if you drop the --with flags.
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)

SERVICE_NAME = "fastapi-mcp-neo-smoke"


def install_provider(mode: str, endpoint: str) -> TracerProvider:
    provider = TracerProvider(
        resource=Resource.create({"service.name": SERVICE_NAME})
    )
    if mode == "otlp":
        # grpc exporter rides on opentelemetry-exporter-otlp
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )

        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=endpoint, insecure=True)
            )
        )
    else:
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)
    return provider


async def run_query() -> None:
    from fastapi import FastAPI
    from fastmcp import Client
    from pydantic import BaseModel

    from fastapi_mcp_neo import FastAPIMCP

    class Out(BaseModel):
        id: int

    app = FastAPI()

    @app.get("/things", response_model=list[Out], tags=["t"])
    async def things() -> list[Out]:
        return [Out(id=1)]

    mcp = FastAPIMCP(app)

    async with Client(mcp.mcp) as client:
        result = await client.call_tool(
            "graphql_query", {"query": "{ t { things { id } } }"}
        )
        print("query result:", result.data)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode", choices=["console", "otlp"], default="console",
        help="console: print spans as JSON; otlp: send to Jaeger",
    )
    parser.add_argument(
        "--endpoint", default="http://localhost:4317",
        help="OTLP gRPC endpoint (otlp mode)",
    )
    args = parser.parse_args()

    provider = install_provider(args.mode, args.endpoint)
    asyncio.run(run_query())
    provider.shutdown()  # flush BatchSpanProcessor before exit


if __name__ == "__main__":
    main()
