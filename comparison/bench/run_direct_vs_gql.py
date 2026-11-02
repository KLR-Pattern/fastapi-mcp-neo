"""Execution overhead of the bridge layers vs calling the route directly.

Answers "beyond token savings, what does the GraphQL layer cost per
query?" (fastmcp discussion #5981, Q4). Four paths over the SAME routes:

1. direct_asgi      — httpx ASGITransport straight at the route
2. graphql_cached   — RouterGraphQLHandler.execute, steady state
                      (document LRU cache hit: parse+validate skipped)
3. graphql_first    — first execute on a fresh handler (one-time
                      parse+validate+recursive-stamp cost per document)
4. mcp_tool_call    — full chain: fastmcp in-memory Client ->
                      graphql_query tool -> handler

Two cases:

- flat: shared_app's GET /api/notes, 20-item list response.
- recursive: a self-referencing Node chain at depth 10/15/20 — the
  true-depth unroll edge. The agent writes a SHALLOW document (3 levels);
  the bridge expands it to the data's real depth, so the response carries
  the whole subtree. First-call pays the unroll+validate of the EXPANDED
  document once; steady state executes the expanded document.

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

from fastapi import FastAPI
from fastmcp import Client
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel
from shared_app import NOTES, build_app

from fastapi_mcp_neo import FastAPIMCP, RouterGraphQLHandler

OUT = Path(__file__).parent / "results_direct_vs_gql.json"

# Same shape on every path: all 20 notes, four fields each.
NOTES_QUERY_FULL = "{ notes { mine { list_notes { id title body owner } } } }"

# The agent's document stays 3 levels deep whatever the data depth is;
# the bridge's true-depth unroll expands it at compile time.
TREE_QUERY = (
    "{ tree { get_tree(root_id: 1, depth: {depth}) "
    "{ id label children { id label children { id label } } } } }"
)

ITERS = 300
RUNS = 3
FIRST_CALL_SAMPLES = 20  # fresh-handler first executes (scan/build excluded)
TREE_DEPTHS = [10, 15, 20]


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
    """Time ITERS awaited calls; coro_factory itself asserts correctness."""
    times = []
    for _ in range(ITERS):
        t0 = time.perf_counter()
        await coro_factory()
        times.append((time.perf_counter() - t0) * 1000)
    return sorted(times)


async def run_direct(call) -> list[list[float]]:
    """(1) direct ASGI call — the baseline the question asks about."""
    return [await time_calls(call) for _ in range(RUNS)]


async def run_gql_cached(handler: RouterGraphQLHandler, call) -> list[list[float]]:
    """(2) GraphQL layer, steady state (document cache warm)."""
    return [await time_calls(call) for _ in range(RUNS)]


async def run_gql_first(build_handler, call) -> list[list[float]]:
    """(3) first execute on a fresh handler: the one-time cost.

    Handler construction (scan+build) and invoker startup (lifespan +
    client) are OUTSIDE the timer — the per-document compile a caller
    pays once, then path (2) forever after.
    """
    runs = []
    for _ in range(RUNS):
        samples = []
        for _ in range(FIRST_CALL_SAMPLES):
            h = build_handler()  # fresh cache per sample
            await h.invoker.start()
            t0 = time.perf_counter()
            await call(h)
            samples.append((time.perf_counter() - t0) * 1000)
            await h.aclose()
        runs.append(sorted(samples))
    return runs


async def run_mcp(mcp: FastAPIMCP, query: str, check_payload) -> list[list[float]]:
    """(4) full MCP chain (same stack as run_ours.py latency)."""
    runs = []
    for _ in range(RUNS):
        async with Client(mcp.mcp) as client:
            times = []
            for _ in range(ITERS):
                t0 = time.perf_counter()
                res = await client.call_tool("graphql_query", {"query": query})
                times.append((time.perf_counter() - t0) * 1000)
                # wire shape: {"data": <handler.execute result>}
                inner = json.loads(res.content[0].text)["data"]
                assert "errors" not in inner, inner
                check_payload(inner["data"])
            runs.append(sorted(times))
    return runs


# ------------------------------------------------------------- the two cases

async def _direct_notes(client: AsyncClient) -> None:
    resp = await client.get("/api/notes")
    assert resp.status_code == 200
    assert len(resp.json()) == len(NOTES)


async def _gql_notes(h: RouterGraphQLHandler) -> None:
    payload = await h.execute(NOTES_QUERY_FULL)
    assert "errors" not in payload, payload
    assert len(payload["data"]["notes"]["mine"]["list_notes"]) == len(NOTES)


def _check_notes(data: dict) -> None:
    assert len(data["notes"]["mine"]["list_notes"]) == len(NOTES)


class TreeNode(BaseModel):
    """A chain tree node — direct self-reference, unrolls to true depth."""

    id: int
    label: str
    children: list[TreeNode] = []


TreeNode.model_rebuild()


def build_chain(depth: int) -> TreeNode:
    """A linear chain: root -> child -> ... (depth nodes, depth-1 levels)."""
    node = TreeNode(id=depth, label=f"n{depth}")
    for d in range(depth - 1, 0, -1):
        node = TreeNode(id=d, label=f"n{d}", children=[node])
    return node


def build_tree_app() -> FastAPI:
    app = FastAPI()

    @app.get("/tree/{root_id}", response_model=TreeNode, tags=["tree"])
    async def get_tree(root_id: int, depth: int = 15) -> TreeNode:
        """Fetch a chain tree of the requested depth."""
        return build_chain(depth)

    return app


def chain_levels(node: dict) -> int:
    """Nested children levels under the root (depth 15 -> 14)."""
    levels = 0
    while node.get("children"):
        node = node["children"][0]
        levels += 1
    return levels


async def measure_tree_case(depth: int) -> dict:
    """All four paths over the chain-tree route at one depth.

    Helpers live in this function's scope on purpose: each depth gets its
    own app, clients and closures, nothing escapes the call.
    """
    query = TREE_QUERY.replace("{depth}", str(depth))
    app = build_tree_app()
    direct = AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://bench.local",
    )
    handler = RouterGraphQLHandler(app)

    async def _direct_tree() -> None:
        resp = await direct.get("/tree/1", params={"depth": depth})
        assert resp.status_code == 200
        assert chain_levels(resp.json()) == depth - 1

    async def _gql_tree(h: RouterGraphQLHandler) -> None:
        payload = await h.execute(query)
        assert "errors" not in payload, payload
        assert chain_levels(payload["data"]["tree"]["get_tree"]) == depth - 1

    def _check_tree(data: dict) -> None:
        assert chain_levels(data["tree"]["get_tree"]) == depth - 1

    # correctness first: the shallow document really unrolls
    await _direct_tree()
    await _gql_tree(handler)

    mcp = FastAPIMCP(app, name="bench", allow_mutation=True, mode="graphql")
    row = {
        "depth": depth,
        "direct_asgi": summarize(await run_direct(_direct_tree)),
        "graphql_cached": summarize(
            await run_gql_cached(handler, lambda: _gql_tree(handler))
        ),
        "graphql_first": summarize(
            await run_gql_first(
                lambda: RouterGraphQLHandler(app), lambda h: _gql_tree(h)
            )
        ),
        "mcp_tool_call": summarize(await run_mcp(mcp, query, _check_tree)),
    }
    await handler.aclose()
    await direct.aclose()
    return row


async def main() -> dict:
    import importlib.metadata as md

    results: dict = {
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
            "tree_depths": TREE_DEPTHS,
            "measurement": (
                "flat case: GET /api/notes (20 items, 4 fields). recursive "
                "case: GET /tree/1?depth=N — a self-referencing Node chain; "
                "the agent's document is 3 levels deep, true-depth unroll "
                "expands it to the data's depth at compile time and the "
                "response carries the whole subtree. Both direct_asgi and "
                "the bridge's invoker ride httpx ASGITransport, so "
                "direct_asgi vs graphql_cached isolates GraphQL "
                "orchestration. graphql_first times the first execute() "
                "per fresh handler (per-document parse+validate+unroll; "
                "scan/build/invoker-start excluded). mcp_tool_call is the "
                "fastmcp in-memory client chain, matching run_ours.py "
                "latency."
            ),
        }
    }

    # ============================ flat case =============================
    print("== flat (GET /api/notes, 20 items) ==")
    app = build_app(5)
    direct = AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://bench.local",
    )
    handler = RouterGraphQLHandler(app)
    first = await handler.execute(NOTES_QUERY_FULL)
    assert "errors" not in first, first
    http = await direct.get("/api/notes")
    assert http.status_code == 200 and len(http.json()) == len(NOTES)
    assert len(first["data"]["notes"]["mine"]["list_notes"]) == len(NOTES)

    mcp = FastAPIMCP(app, name="bench", allow_mutation=True, mode="graphql")
    results["flat"] = {
        "direct_asgi": summarize(await run_direct(lambda: _direct_notes(direct))),
        "graphql_cached": summarize(
            await run_gql_cached(handler, lambda: _gql_notes(handler))
        ),
        "graphql_first": summarize(
            await run_gql_first(lambda: RouterGraphQLHandler(app), lambda h: _gql_notes(h))
        ),
        "mcp_tool_call": summarize(
            await run_mcp(mcp, NOTES_QUERY_FULL, _check_notes)
        ),
    }
    await handler.aclose()
    await direct.aclose()

    # ========================== recursive case ===========================
    results["recursive"] = []
    for depth in TREE_DEPTHS:
        print(f"== recursive (chain depth {depth}, document 3 levels) ==")
        results["recursive"].append(await measure_tree_case(depth))

    OUT.write_text(json.dumps(results, indent=2) + "\n")

    def show(name: str, row: dict) -> None:
        print(f"  {name:16s} " + "  ".join(
            f"{k}={v['p50_ms']:.3f}ms" for k, v in row.items() if isinstance(v, dict)
        ))

    show("flat", results["flat"])
    for row in results["recursive"]:
        show(f"depth {row['depth']}", {k: v for k, v in row.items() if k != "depth"})
    print(f"\nwritten to {OUT}")
    return results


if __name__ == "__main__":
    asyncio.run(main())
