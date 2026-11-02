"""Execution overhead of the bridge layers vs calling the route directly.

Answers "beyond token savings, what does the GraphQL layer cost per
query?" (fastmcp discussion #5981, Q4). Three paths over the SAME route
of shared_app, 20-item response:

1. direct_asgi      — httpx ASGITransport straight at GET /api/notes
2. graphql_cached   — RouterGraphQLHandler.execute, steady state
                      (document LRU cache hit: parse+validate skipped)
3. graphql_first    — first execute on a fresh handler (one-time
                      parse+validate+recursive-stamp cost per document)
4. mcp_tool_call    — full chain: fastmcp in-memory Client ->
                      graphql_query tool -> handler

Both sides of (1) vs (2) ride the same httpx ASGITransport — the bridge's
invoker calls routes through it too — so the delta between them is pure
GraphQL orchestration (execute, projection, error mapping), not an extra
network hop.

Run from bench/:  uv run --project env_ours python run_direct_vs_gql.py
Writes results_direct_vs_gql.json next to this file.
"""

from __future__ import annotations

import asyncio
import json
import platform
import statistics
import subprocess
import time
from datetime import date
from pathlib import Path

from fastmcp import Client
from httpx import ASGITransport, AsyncClient
from shared_app import NOTES, build_app

from fastapi_mcp_neo import FastAPIMCP, RouterGraphQLHandler

OUT = Path(__file__).parent / "results_direct_vs_gql.json"

# Same shape on every path: all 20 notes, four fields each.
NOTES_QUERY_FULL = "{ notes { mine { list_notes { id title body owner } } } }"

ITERS = 300
RUNS = 3
FIRST_CALL_SAMPLES = 20  # fresh-handler first executes (scan/build excluded)


def pct(sorted_ms: list[float], p: float) -> float:
    return sorted_ms[min(len(sorted_ms) - 1, int(len(sorted_ms) * p))]


def cpu_name() -> str:
    if platform.system() == "Darwin":
        return subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"],
            capture_output=True, text=True,
        ).stdout.strip()
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:  # pragma: no cover - non-Linux/non-Darwin
        pass
    return platform.processor() or "unknown"


def summarize(runs: list[list[float]]) -> dict:
    return {
        "p50_ms": round(statistics.median(pct(r, 0.5) for r in runs), 3),
        "p95_ms": round(statistics.median(pct(r, 0.95) for r in runs), 3),
        "p50_spread": [round(pct(r, 0.5), 3) for r in runs],
    }


async def time_calls(coro_factory) -> list[float]:
    """Time ITERS awaited calls; each call must return a 20-item result."""
    times = []
    for _ in range(ITERS):
        t0 = time.perf_counter()
        n = await coro_factory()
        times.append((time.perf_counter() - t0) * 1000)
        assert n == len(NOTES), f"expected {len(NOTES)} items, got {n}"
    return sorted(times)


async def main() -> dict:
    import importlib.metadata as md

    app = build_app(5)

    # ---- correctness first: all four paths return the same 20 notes ----
    direct = AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://bench.local",
    )
    handler = RouterGraphQLHandler(app)
    first = await handler.execute(NOTES_QUERY_FULL)
    assert "errors" not in first, first
    http = await direct.get("/api/notes")
    assert http.status_code == 200 and len(http.json()) == len(NOTES)
    assert (
        len(first["data"]["notes"]["mine"]["list_notes"]) == len(NOTES)
    ), "graphql and direct paths disagree"

    # ---- (1) direct ASGI call — the baseline the question asks about ----
    runs_direct = [
        await time_calls(lambda: _direct_notes(direct)) for _ in range(RUNS)
    ]

    # ---- (2) GraphQL layer, steady state (document cache warm) ----
    runs_cached = [
        await time_calls(lambda: _gql_notes(handler)) for _ in range(RUNS)
    ]

    # ---- (3) first execute on a fresh handler: the one-time cost ----
    # Handler construction (scan+build) is OUTSIDE the timer — this is the
    # per-document compile a caller pays once, then path (2) forever after.
    runs_first: list[list[float]] = []
    for _ in range(RUNS):
        samples = []
        for _ in range(FIRST_CALL_SAMPLES):
            h = RouterGraphQLHandler(app)  # fresh cache per sample
            await h.invoker.start()  # lifespan+client up front: not a per-document cost
            t0 = time.perf_counter()
            n = await _gql_notes(h)
            samples.append((time.perf_counter() - t0) * 1000)
            assert n == len(NOTES)
            await h.aclose()
        runs_first.append(sorted(samples))

    # ---- (4) full MCP chain (same stack as run_ours.py latency) ----
    mcp = FastAPIMCP(app, name="bench", allow_mutation=True, mode="graphql")
    runs_mcp: list[list[float]] = []
    for _ in range(RUNS):
        async with Client(mcp.mcp) as client:
            times = []
            for _ in range(ITERS):
                t0 = time.perf_counter()
                res = await client.call_tool("graphql_query", {"query": NOTES_QUERY_FULL})
                times.append((time.perf_counter() - t0) * 1000)
                # wire shape: {"data": <handler.execute result>}
                inner = json.loads(res.content[0].text)["data"]
                assert "errors" not in inner, inner
                n = len(inner["data"]["notes"]["mine"]["list_notes"])
                assert n == len(NOTES)
            runs_mcp.append(sorted(times))

    await handler.aclose()
    await direct.aclose()

    results = {
        "method": {
            "hardware": cpu_name(),
            "python": platform.python_version(),
            "date": date.today().isoformat(),
            "versions": {
                name: md.version(name)
                for name in ("fastapi-mcp-neo", "fastmcp", "graphql-core", "fastapi", "httpx")
            },
            "iters": ITERS,
            "runs": RUNS,
            "first_call_samples": FIRST_CALL_SAMPLES,
            "measurement": (
                "same route (GET /api/notes, 20 items, 4 fields) through four "
                "paths; both direct_asgi and the bridge's invoker ride httpx "
                "ASGITransport, so direct_asgi vs graphql_cached isolates "
                "GraphQL orchestration. graphql_first times the first "
                "execute() per fresh handler (per-document parse+validate; "
                "scan/build excluded). mcp_tool_call is the fastmcp "
                "in-memory client chain, matching run_ours.py latency."
            ),
        },
        "latency": {
            "direct_asgi": summarize(runs_direct),
            "graphql_cached": summarize(runs_cached),
            "graphql_first": summarize(runs_first),
            "mcp_tool_call": summarize(runs_mcp),
        },
        "overhead": {
            # derived, rounded from the summarized p50s above
            "graphql_layer_vs_direct_ms": None,  # filled after summarize
        },
    }
    results["overhead"]["graphql_layer_vs_direct_ms"] = round(
        results["latency"]["graphql_cached"]["p50_ms"]
        - results["latency"]["direct_asgi"]["p50_ms"],
        3,
    )

    OUT.write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results["latency"], indent=2))
    print(
        f"\nGraphQL layer overhead (steady state, vs direct ASGI): "
        f"{results['overhead']['graphql_layer_vs_direct_ms']:+.3f} ms/query"
    )
    print(f"written to {OUT}")
    return results


async def _direct_notes(client: AsyncClient) -> int:
    resp = await client.get("/api/notes")
    assert resp.status_code == 200
    return len(resp.json())


async def _gql_notes(h: RouterGraphQLHandler) -> int:
    payload = await h.execute(NOTES_QUERY_FULL)
    assert "errors" not in payload, payload
    return len(payload["data"]["notes"]["mine"]["list_notes"])


if __name__ == "__main__":
    asyncio.run(main())
