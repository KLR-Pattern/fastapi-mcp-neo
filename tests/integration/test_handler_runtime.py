"""handler.execute runtime behavior: timeouts, concurrency, fan-out, scalars.

Everything here drives RouterGraphQLHandler.execute (not the invoker
directly) — the seams between schema resolvers and the invoker are what
these classes pin.
"""

import asyncio

import pytest
from fastapi import FastAPI
from pydantic import BaseModel

from fastapi_mcp_neo.handler import RouterGraphQLHandler
from tests.support.models import Ok


class TestTimeoutEnforcement:
    """R1: httpx timeouts are INERT on ASGITransport — asyncio.wait_for in
    invoke() is the real enforcement, wired through request_timeout."""

    @staticmethod
    def _slow_app(seconds: float) -> FastAPI:
        app = FastAPI()

        @app.get("/slow", response_model=Ok)
        async def slow() -> Ok:
            await asyncio.sleep(seconds)
            return Ok(ok=True)

        return app

    async def test_slow_route_times_out(self, make_handler):
        handler = make_handler(self._slow_app(0.5), request_timeout=0.05)
        assert handler.invoker.timeout == 0.05
        result = await handler.execute("{ slow { slow { ok } } }")
        assert "errors" in result
        assert "timed out after 0.05s" in result["errors"][0]["message"]
        assert result["errors"][0]["extensions"]["code"] == "TIMEOUT"

    async def test_none_disables_enforcement(self, make_handler):
        handler = make_handler(self._slow_app(0.2), request_timeout=None)
        result = await handler.execute("{ slow { slow { ok } } }")
        assert result == {"data": {"slow": {"slow": {"ok": True}}}}


class TestConcurrencyLimits:
    """P0-2: sibling fields fan out concurrently; max_concurrency bounds it
    invoker-globally (the wrapped app's upstream is what needs protecting)."""

    @staticmethod
    def _fanout_app(n: int, state: dict) -> FastAPI:
        app = FastAPI()

        def make(i: int):
            async def endpoint() -> Ok:
                state["current"] += 1
                state["peak"] = max(state["peak"], state["current"])
                await asyncio.sleep(0.15)
                state["current"] -= 1
                return Ok(ok=True)

            endpoint.__name__ = f"job_{i}"
            return endpoint

        for i in range(n):
            app.get(f"/job{i}", response_model=Ok, tags=["jobs"])(make(i))
        return app

    @staticmethod
    def _query(n: int) -> str:
        fields = " ".join(f"job_{i} {{ ok }}" for i in range(n))
        return f"{{ jobs {{ {fields} }} }}"

    async def test_fanout_bounded_by_max_concurrency(self, make_handler):
        state = {"current": 0, "peak": 0}
        handler = make_handler(self._fanout_app(4, state), max_concurrency=2)
        assert handler.invoker.max_concurrency == 2
        result = await handler.execute(self._query(4))
        assert result["data"]["jobs"]["job_3"] == {"ok": True}
        assert state["peak"] <= 2, f"fan-out exceeded the bound: {state}"

    async def test_unbounded_runs_in_parallel(self, make_handler):
        state = {"current": 0, "peak": 0}
        handler = make_handler(self._fanout_app(4, state), max_concurrency=None)
        result = await handler.execute(self._query(4))
        assert result["data"]["jobs"]["job_0"] == {"ok": True}
        assert state["peak"] >= 2, "sibling fields should resolve concurrently"

    def test_invalid_max_concurrency_rejected(self):
        state = {"current": 0, "peak": 0}
        with pytest.raises(ValueError, match="max_concurrency"):
            RouterGraphQLHandler(self._fanout_app(1, state), max_concurrency=0)


class TestSameRouteAliasFanout:
    """One route queried several times via aliases: each alias resolves the
    SAME stateless resolver closure with its own kwargs (build_request is
    pure), so an aliased selection on one route fans out exactly like
    distinct-route siblings — concurrently, semaphore-bounded."""

    @staticmethod
    def _app(state: dict) -> FastAPI:
        class Item(BaseModel):
            item_id: int

        app = FastAPI()

        @app.get("/items/{item_id}", response_model=Item, tags=["shop"])
        async def get_item(item_id: int) -> Item:
            state["current"] += 1
            state["peak"] = max(state["peak"], state["current"])
            await asyncio.sleep(0.15)
            state["current"] -= 1
            return Item(item_id=item_id)

        return app

    @staticmethod
    def _aliased(n: int) -> str:
        fields = " ".join(f"x{i}: get_item(item_id: {i}) {{ item_id }}" for i in range(n))
        return f"{{ shop {{ {fields} }} }}"

    async def test_aliases_concurrent_and_isolated(self, make_handler):
        state = {"current": 0, "peak": 0}
        handler = make_handler(self._app(state))
        result = await handler.execute(self._aliased(3))
        assert result == {
            "data": {
                "shop": {
                    "x0": {"item_id": 0},
                    "x1": {"item_id": 1},
                    "x2": {"item_id": 2},
                }
            }
        }
        assert state["peak"] == 3, "aliased siblings should resolve concurrently"

    async def test_alias_fanout_bounded_by_max_concurrency(self, make_handler):
        state = {"current": 0, "peak": 0}
        handler = make_handler(self._app(state), max_concurrency=2)
        result = await handler.execute(self._aliased(4))
        assert result["data"]["shop"] == {
            f"x{i}": {"item_id": i} for i in range(4)
        }
        assert state["peak"] <= 2, f"same-route fan-out escaped the bound: {state}"


class TestScalarBodyRoundTrip:
    """Custom scalars' parse_value yields typed objects (Decimal/UUID/
    datetime); the body must cross json.dumps — regression for the
    "Object of type Decimal is not JSON serializable" field error."""

    @staticmethod
    def _app() -> FastAPI:
        from datetime import datetime
        from decimal import Decimal
        from uuid import UUID

        class Payment(BaseModel):
            amount: Decimal
            ref: UUID
            at: datetime
            lines: list[Decimal] = []

        app = FastAPI()

        @app.get("/ping", response_model=dict, tags=["misc"])
        async def ping() -> dict:
            return {"ok": True}

        @app.post("/pay", response_model=dict, tags=["misc"])
        async def pay(payload: Payment) -> dict:
            return {
                "amount": str(payload.amount),
                "ref": str(payload.ref),
                "at": payload.at.isoformat(),
                "n_lines": len(payload.lines),
            }

        return app

    async def test_body_with_decimal_uuid_datetime(self, make_handler):
        handler = make_handler(self._app(), allow_mutation=True)
        result = await handler.execute(
            'mutation { misc { pay(payload: {'
            'amount: "12.34", '
            'ref: "12345678-1234-5678-1234-567812345678", '
            'at: "2026-10-05T10:00:00Z", '
            'lines: ["1.5", "2.5"]'
            '}) } }'
        )
        assert result == {
            "data": {
                "misc": {
                    "pay": {
                        "amount": "12.34",
                        "ref": "12345678-1234-5678-1234-567812345678",
                        "at": "2026-10-05T10:00:00+00:00",
                        "n_lines": 2,
                    }
                }
            }
        }
