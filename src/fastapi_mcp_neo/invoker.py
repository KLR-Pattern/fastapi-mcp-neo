"""Execute GraphQL field resolvers by calling FastAPI routes in-process.

Requests travel through the real ASGI app (httpx ``ASGITransport``), so
``Depends``/middleware/authentication behave exactly as they would over the
network. The app's lifespan is managed with ``asgi-lifespan`` — started lazily
on first use and held open until ``aclose()``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from datetime import time as dt_time
from decimal import Decimal
from typing import Any
from urllib.parse import quote
from uuid import UUID

from asgi_lifespan import LifespanManager
from graphql import GraphQLError
from httpx import ASGITransport, AsyncClient, Response
from opentelemetry import trace

from fastapi_mcp_neo.scanner import RouteInfo

logger = logging.getLogger(__name__)

_BASE_HEADERS: dict[str, str] = {"accept": "application/json"}


def _inject_trace_context(headers: dict[str, str]) -> None:
    """W3C trace context (traceparent/tracestate/baggage) into the request.

    Bridge-GENERATED, not caller-forwarded: it applies regardless of the
    passthrough_headers whitelist, and is a no-op without an SDK
    (opentelemetry-api defaults to a non-recording tracer). FastAPI >=
    0.142's native route spans extract it, so each route call nests under
    the calling span instead of landing as an orphan trace.
    """
    from opentelemetry.propagate import inject

    inject(headers)
_MAX_ERROR_BODY = 500
# Headers the invoker owns: the bridge always speaks JSON, and a forwarded
# value here would retype the request (content-type: text/plain + JSON body
# -> FastAPI 422). Refused even when whitelisted.
_PROTECTED_HEADERS = frozenset({"content-type", "accept"})


class GQLMCPRuntimeError(RuntimeError):
    """Raised when the invoker is used outside its lifecycle."""


@dataclass(frozen=True)
class RequestPlan:
    """A materialized HTTP request, pure data for easy testing."""

    method: str
    path: str
    params: tuple[tuple[str, str], ...]
    json_body: Any
    headers: dict[str, str]


@dataclass(frozen=True)
class InvocationContext:
    """Per-execution context threaded through graphql-core's ``context_value``.

    graphql() has no native per-call header concept; context_value is the
    standard side channel. Carrying headers here keeps the once-built schema
    reusable across concurrent executions with different callers (instance
    state would race, a module ContextVar would be implicit and untestable).
    """

    headers: Mapping[str, str] | None = None


def filter_passthrough_headers(
    headers: Mapping[str, str], allowed: Collection[str]
) -> dict[str, str]:
    """Keep only whitelisted header names, normalized to lowercase.

    This is the security boundary between untrusted caller headers and the
    wrapped app: a name not in ``allowed`` never reaches a route, so an MCP
    client cannot smuggle internal headers (x-internal-token and friends).
    Matching is case-insensitive on both sides — HTTP/2 lowercases all field
    names, and user config may not. Protocol headers (``content-type`` /
    ``accept``) are always refused, whitelist or not: the invoker owns them,
    and a forwarded value would retype the JSON body request.
    """
    allowed_lower = {h.strip().lower() for h in allowed} - _PROTECTED_HEADERS
    return {k.lower(): v for k, v in headers.items() if k.lower() in allowed_lower}


def build_request(
    route: RouteInfo,
    kwargs: Mapping[str, Any],
    extra_headers: Mapping[str, str] | None = None,
) -> RequestPlan:
    """Translate GraphQL resolver kwargs into an HTTP request plan."""
    path_values: dict[str, Any] = {}
    for param in route.path_params:
        value = kwargs.get(param.name, param.default)
        rendered = _render_param(value)
        if rendered is None:
            raise GraphQLError(
                f"Missing required path parameter '{param.name}' "
                f"for {route.method} {route.path}",
                extensions={"code": "BAD_REQUEST"},
            )
        # The path template holds the raw function-arg name, not any alias.
        # Starlette's convertor to_string() returns values RAW (no percent-
        # encoding), and httpx re-parses the assembled path as a URL — a
        # literal '?'/'#' in a value would start the query/fragment and
        # silently truncate it. Encode here so metacharacters stay inside
        # the segment; httpx decodes into scope["path"] and the route
        # receives the original value (tests/integration/
        # test_path_param_metachars.py).
        path_values[param.raw_name] = quote(str(rendered), safe="")

    # Let Starlette render the URL from its own route definition: it owns the
    # template syntax (path convertors like {id:int} / {p:path}) and the
    # per-convertor value encoding, so no string surgery on route.path here.
    try:
        url = route.route.url_path_for(route.field_name, **path_values)
    except AssertionError as exc:  # convertor rejects the value (e.g. int < 0)
        raise GraphQLError(
            f"Invalid path parameter for {route.method} {route.path}: {exc}",
            extensions={"code": "BAD_REQUEST"},
        ) from exc
    if url is None:  # pragma: no cover - param names come from the same route
        raise GraphQLError(
            f"Route {route.method} {route.path} rejected its path parameters",
            extensions={"code": "BAD_REQUEST"},
        )
    path = str(url)

    params: list[tuple[str, str]] = []

    for param in route.query_params:
        value = kwargs.get(param.name, param.default)
        rendered = _render_param(value)
        if rendered is None:
            continue  # optional params are simply not sent
        if isinstance(rendered, list):
            params.extend((param.name, str(item)) for item in rendered)
        else:
            params.append((param.name, str(rendered)))

    json_body: Any = None
    if route.body_params:
        if any(p.embed for p in route.body_params):
            json_body = {p.name: kwargs.get(p.name) for p in route.body_params}
        else:
            json_body = kwargs.get(route.body_params[0].name)
        # The custom scalars' parse_value produces typed Python objects
        # (Decimal/UUID/datetime...) inside resolver kwargs; json.dumps cannot
        # encode them. Query/path params are already stringified by
        # _render_param — the body (and anything under it, e.g. variables)
        # gets the same wire conversion here.
        json_body = _json_safe(json_body)

    headers = dict(_BASE_HEADERS)
    if json_body is not None:
        headers["content-type"] = "application/json"
    if extra_headers:
        headers.update(dict(extra_headers))

    return RequestPlan(
        method=route.method,
        path=path,
        params=tuple(params),
        json_body=json_body,
        headers=headers,
    )


def _json_safe(value: Any) -> Any:
    """Convert typed scalar objects to their JSON wire forms, recursively.

    FastAPI/pydantic re-parses these strings on the receiving side, so the
    round trip is lossless for the app.
    """
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime | date | dt_time):
        return value.isoformat()
    return value


def _render_param(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, list):
        # Mixed lists drop their None items rather than stringifying them
        # ("None" as a query value is never what the caller meant); an
        # all-None list means "not provided" and is not sent at all.
        rendered = [r for r in (_render_param(v) for v in value) if r is not None]
        return rendered or None
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


class RouteInvoker:
    """Calls routes through the ASGI app, managing its lifespan."""

    def __init__(
        self,
        app: Any,
        *,
        timeout: float | None = 30.0,
        manage_lifespan: bool = True,
        max_concurrency: int | None = 16,
    ) -> None:
        self._app = app
        self._timeout = timeout
        self._manage_lifespan = manage_lifespan
        if max_concurrency is not None and max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1, or None to disable")
        self._max_concurrency = max_concurrency
        # One slot per in-flight route call across ALL queries — the wrapped
        # app's upstream (DB, external APIs) is what needs protecting, so the
        # bound is invoker-global, not per-document.
        self._semaphore: asyncio.Semaphore | None = (
            asyncio.Semaphore(max_concurrency) if max_concurrency is not None else None
        )
        self._client: AsyncClient | None = None
        self._lifespan: LifespanManager | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def app(self) -> Any:
        return self._app

    @property
    def manage_lifespan(self) -> bool:
        return self._manage_lifespan

    @property
    def timeout(self) -> float | None:
        return self._timeout

    @property
    def max_concurrency(self) -> int | None:
        return self._max_concurrency

    def disable_lifespan_management(self) -> None:
        """Stop managing the app lifespan (when the host server runs it)."""
        self._manage_lifespan = False

    async def start(self) -> None:
        """Idempotently start the lifespan (when managed) and the HTTP client."""
        if self._closed:
            raise GQLMCPRuntimeError("RouteInvoker has been closed")
        if self._client is not None:
            return
        async with self._lock:
            if self._client is not None:
                return
            if self._manage_lifespan:
                # LifespanManager only drives startup/shutdown; requests always
                # go to the original app (it is not an ASGI wrapper).
                manager = LifespanManager(self._app)
                await manager.__aenter__()
                self._lifespan = manager
            self._client = AsyncClient(
                transport=ASGITransport(app=self._app),
                base_url="http://fastapi-mcp-neo.local",
                timeout=self._timeout,
            )

    async def aclose(self) -> None:
        """Shut the client and lifespan down; the invoker is terminal afterwards."""
        self._closed = True
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        if self._lifespan is not None:
            await self._lifespan.__aexit__(None, None, None)
            self._lifespan = None

    async def invoke(
        self,
        route: RouteInfo,
        kwargs: Mapping[str, Any],
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> Any:
        """Execute one route and return its JSON response."""
        await self.start()
        client = self._client
        if client is None:  # pragma: no cover - start() guarantees a client
            raise GQLMCPRuntimeError("RouteInvoker failed to start")

        # Credentials ride exclusively in per-call headers (single identity
        # source: the caller). There is no server-side provider to merge.
        plan = build_request(
            route,
            kwargs,
            {str(k): str(v) for k, v in extra_headers.items()} if extra_headers else None,
        )
        _inject_trace_context(plan.headers)
        # Sibling GraphQL fields resolve concurrently, so one wide query is a
        # fan-out of route calls. The semaphore bounds that fan-out; it is
        # acquired INSIDE the timeout so queueing time counts against it (a
        # call stuck waiting for a slot must not outlive its own deadline).
        _t_queue = time.monotonic()

        async def call() -> Response:
            if self._semaphore is None:
                return await client.request(
                    plan.method,
                    plan.path,
                    params=list(plan.params),
                    json=plan.json_body,
                    headers=plan.headers,
                )
            async with self._semaphore:
                waited_ms = (time.monotonic() - _t_queue) * 1000
                if waited_ms >= 1.0:  # only when it actually queued
                    trace.get_current_span().add_event(
                        "route.queue", {"wait_ms": round(waited_ms, 2)}
                    )
                return await client.request(
                    plan.method,
                    plan.path,
                    params=list(plan.params),
                    json=plan.json_body,
                    headers=plan.headers,
                )

        # ASGITransport does NOT enforce httpx timeouts (in-process calls
        # bypass httpcore) — asyncio.wait_for is the actual enforcement.
        try:
            response = await asyncio.wait_for(call(), timeout=self._timeout)
        except asyncio.TimeoutError as exc:
            trace.get_current_span().add_event(
                "route.timeout", {"path": route.path, "timeout_s": self._timeout}
            )
            raise GraphQLError(
                f"{route.method} {route.path} timed out after {self._timeout}s",
                extensions={"code": "TIMEOUT", "http_status": 504},
            ) from exc
        if response.status_code >= 400:
            raise _http_error(route, response)
        if response.status_code == 204 or not response.content:
            return None  # bodyless (204 / explicit -> None): nothing to parse
        if "application/json" in response.headers.get("content-type", ""):
            return response.json()
        return {"_raw": response.text}


def _http_error(route: RouteInfo, response: Response) -> GraphQLError:
    body: Any = response.text
    if "application/json" in response.headers.get("content-type", ""):
        body = response.json()
    rendered = str(body)
    if len(rendered) > _MAX_ERROR_BODY:
        rendered = rendered[:_MAX_ERROR_BODY] + "…"
    return GraphQLError(
        f"{route.method} {route.path} -> {response.status_code}: {rendered}",
        extensions={
            "code": f"HTTP_{response.status_code}",
            "http_status": response.status_code,
        },
    )
