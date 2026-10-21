"""fastapi-mcp-neo half of the benchmark. Writes results_ours.json.

Run from bench/:  uv run --project env_ours python run_ours.py
"""

from __future__ import annotations

import asyncio
import json
import statistics
import time
from pathlib import Path

from fastmcp import Client
from shared_app import NOTES, build_app

from fastapi_mcp_neo import FastAPIMCP

OUT = Path(__file__).parent / "results_ours.json"

NOTES_QUERY = (
    '{ notes { mine { list_notes(q: "note 1") { id title body owner } } } '
    "meta { stats { notes owners } } }"
)
NOTES_QUERY_FULL = "{ notes { mine { list_notes { id title body owner } } } }"
NOTES_QUERY_PROJECTED = "{ notes { mine { list_notes { id title } } } }"


def json_size(obj) -> int:
    return len(json.dumps(obj, ensure_ascii=False, default=str).encode())


async def raw_sdk_latency(mcp) -> list[float]:
    """Same client stack as the fastapi-mcp side: mcp SDK ClientSession."""
    import asyncio
    import contextlib

    from mcp.client.session import ClientSession
    from mcp.shared.memory import create_client_server_memory_streams

    lowlevel = mcp.mcp._mcp_server
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        task = asyncio.create_task(
            lowlevel.run(
                server_streams[0], server_streams[1],
                lowlevel.create_initialization_options(),
            )
        )
        try:
            async with ClientSession(client_streams[0], client_streams[1]) as sess:
                await sess.initialize()
                times = []
                for _ in range(200):
                    t0 = time.perf_counter()
                    await sess.call_tool("graphql_query", {"query": NOTES_QUERY_FULL})
                    times.append((time.perf_counter() - t0) * 1000)
                return times
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


def pct(sorted_ms: list[float], p: float) -> float:
    return sorted_ms[min(len(sorted_ms) - 1, int(len(sorted_ms) * p))]


async def catalog_for(route_count: int) -> dict:
    app = build_app(route_count)
    row: dict = {"routes": route_count}

    simple = FastAPIMCP(app, name="bench", allow_mutation=True, mode="simple")
    async with Client(simple.mcp) as client:
        tools = await client.list_tools()
        # full serialization, same approach as the fastapi-mcp side
        payload = {"tools": [t.model_dump(exclude_none=True) for t in tools]}
        sdl = json.loads((await client.call_tool("get_schema", {})).content[0].text)["data"]["sdl"]
    row["ours_simple"] = {
        "tool_count": len(tools),
        "catalog_bytes": json_size(payload),
        "sdl_bytes": len(sdl.encode()),
    }
    row["ours_simple"]["full_context_bytes"] = (
        row["ours_simple"]["catalog_bytes"] + row["ours_simple"]["sdl_bytes"]
    )

    progressive = FastAPIMCP(app, name="bench", allow_mutation=True, mode="progressive")
    async with Client(progressive.mcp) as client:
        tools = await client.list_tools()
        payload = {"tools": [t.model_dump(exclude_none=True) for t in tools]}
        domains = json.loads((await client.call_tool("list_domains", {})).content[0].text)
        domain = next(d["name"] for d in domains["data"]["domains"] if d["name"] == "notes")
        queries = json.loads(
            (await client.call_tool("list_queries", {"domain": domain})).content[0].text
        )
        frag = json.loads(
            (await client.call_tool("get_query_schema", {"domain": domain})).content[0].text
        )
    row["ours_progressive"] = {
        "tool_count": len(tools),
        "catalog_bytes": json_size(payload),
        "discovery_domains_bytes": json_size(domains),
        "discovery_list_bytes": json_size(queries),
        "discovery_fragment_bytes": json_size(frag),
    }
    row["ours_progressive"]["discovery_total_bytes"] = (
        row["ours_progressive"]["catalog_bytes"]
        + row["ours_progressive"]["discovery_domains_bytes"]
        + row["ours_progressive"]["discovery_list_bytes"]
        + row["ours_progressive"]["discovery_fragment_bytes"]
    )
    return row


async def main() -> dict:
    import importlib.metadata as md

    results: dict = {
        "versions": {
            name: md.version(name)
            for name in ("fastapi-mcp-neo", "fastmcp", "mcp", "graphql-core", "fastapi")
        }
    }

    print("== catalog ==")
    results["catalog"] = [await catalog_for(n) for n in [5, 10, 25, 50, 100]]

    app = build_app(5)
    simple = FastAPIMCP(app, name="bench", allow_mutation=True, mode="simple")

    print("== composition ==")
    run_means = []
    for _ in range(3):
        async with Client(simple.mcp) as client:
            times = []
            for _ in range(50):
                t0 = time.perf_counter()
                await client.call_tool("graphql_query", {"query": NOTES_QUERY})
                times.append((time.perf_counter() - t0) * 1000)
        run_means.append(round(statistics.mean(times), 2))
    results["composition"] = {
        "tool_calls": 1,
        "runs": 3,
        "mean_ms_per_run": run_means,
        "mean_ms": round(statistics.median(run_means), 2),
        "p95_ms": None,
    }

    print("== latency ==")
    # (a) fastmcp in-memory client — our native stack
    runs_fastmcp = []
    for _ in range(3):
        async with Client(simple.mcp) as client:
            times = []
            for _ in range(200):
                t0 = time.perf_counter()
                await client.call_tool("graphql_query", {"query": NOTES_QUERY_FULL})
                times.append((time.perf_counter() - t0) * 1000)
        runs_fastmcp.append(sorted(times))
    # (b) raw mcp-SDK ClientSession over memory streams — the SAME client
    # stack the fastapi-mcp side uses, for apples-to-apples latency
    runs_raw = []
    for _ in range(3):
        times = await raw_sdk_latency(simple)
        runs_raw.append(sorted(times))

    def med(runs, q):
        return round(statistics.median(pct(r, q) for r in runs), 2)

    results["latency"] = {
        "runs": 3,
        "ours_fastmcp_client": {"p50_ms": med(runs_fastmcp, .5), "p95_ms": med(runs_fastmcp, .95)},
        "ours_raw_sdk_client": {"p50_ms": med(runs_raw, .5), "p95_ms": med(runs_raw, .95)},
        "spread_p50_ms": {
            "fastmcp": [round(pct(r, .5), 2) for r in runs_fastmcp],
            "raw_sdk": [round(pct(r, .5), 2) for r in runs_raw],
        },
    }

    print("== response size ==")
    async with Client(simple.mcp) as client:
        full = (await client.call_tool("graphql_query", {"query": NOTES_QUERY_FULL})).content[0].text
        projected = (await client.call_tool("graphql_query", {"query": NOTES_QUERY_PROJECTED})).content[0].text
    results["response_size"] = {
        "note_count": len(NOTES),
        "full_bytes": len(full.encode()),
        "projected_bytes": len(projected.encode()),
    }

    print("== error semantics ==")
    async with Client(simple.mcp) as client:
        res = json.loads(
            (
                await client.call_tool(
                    "graphql_query",
                    {
                        "query": "{ meta { stats { notes } } "
                        "notes { mine { get_note(note_id: 9999) { id } } } }"
                    },
                )
            ).content[0].text
        )
    results["error_semantics"] = res

    print("== fidelity ==")
    async with Client(simple.mcp) as client:
        sdl = json.loads((await client.call_tool("get_schema", {})).content[0].text)["data"]["sdl"]
    results["fidelity_sdl_fragment"] = sdl[
        sdl.index("type NotesMineQuery") : sdl.index("type NoteOut")
    ].strip()

    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print(f"wrote {OUT}")
    return results


if __name__ == "__main__":
    asyncio.run(main())
