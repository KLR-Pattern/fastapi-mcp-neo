"""FastAPIMCP: the one-line MCP server over a FastAPI app."""

from __future__ import annotations

import logging
import warnings
from collections.abc import Sequence
from typing import Any, Literal

from fastapi import FastAPI

from fastapi_mcp_neo.domains import DomainRegistry
from fastapi_mcp_neo.handler import GQLMCPConfigError, RouterGraphQLHandler
from fastapi_mcp_neo.mcp.tools import register_simple_tools

logger = logging.getLogger(__name__)

# Route count above which "auto" mode prefers progressive disclosure (P2).
PROGRESSIVE_THRESHOLD = 25


def _compose_lifespan(host: FastAPI, sub_http_app: Any) -> None:
    """Chain a mounted sub-app's lifespan into the host app's lifespan.

    Starlette never runs lifespans of mounted apps, but fastmcp's streamable
    HTTP session manager requires one ("Task group is not initialized").
    """
    from collections.abc import AsyncIterator
    from contextlib import AsyncExitStack, asynccontextmanager

    host_lifespan = host.router.lifespan_context
    sub_lifespan = getattr(sub_http_app, "lifespan", None) or sub_http_app.router.lifespan_context

    @asynccontextmanager
    async def combined(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(host_lifespan(app))
            await stack.enter_async_context(sub_lifespan(app))
            yield

    host.router.lifespan_context = combined


class FastAPIMCP:
    """Expose a FastAPI app as an MCP server backed by a GraphQL schema.

    Args:
        app: The FastAPI application whose routes become the schema.
        name: MCP server name shown to clients.
        instructions: Usage guide agents receive with the ``initialize``
            handshake (MCP protocol field) — a short map of the domains and
            how to query. ``None`` (default) sends nothing.
        include/exclude: fnmatch globs over route paths (exclude wins).
        include_tags/exclude_tags: fnmatch globs over route tags
            (exclude_tags wins). A route matches when ANY of its string
            tags matches ANY pattern — "iam:*" matches tags=["iam:users"].
            Enum tags are ignored. include_tags is a strict whitelist:
            untagged routes are dropped; with only exclude_tags set,
            untagged routes stay. Tag filters AND with path filters (a
            route must pass both); dropping is silent, like path filters.
        allow_mutation: Expose POST/PUT/PATCH/DELETE routes as GraphQL
            mutations. Default False (read-only).
        mutation_include: fnmatch globs limiting WHICH write routes become
            mutations (requires allow_mutation=True).
        passthrough_headers: Whitelist of inbound header names (case
            insensitive) MCP/GraphQL callers may forward into route calls.
            ``None`` (default) forwards ``authorization`` — each client acts
            as its own JWT user, the FastAPI security schemes doing the
            verifying. Session cookies ride the ``Cookie:`` request header,
            so cookie-authenticated apps add the literal name:
            ``passthrough_headers=["authorization", "cookie"]`` (wired
            example: examples/notes_oauth). Pass ``[]`` to disable
            forwarding entirely. With no HTTP request context (in-memory
            client) nothing is forwarded: protected routes answer 401.
            Protocol headers (``content-type`` / ``accept``) are refused
            even if whitelisted — the invoker owns them, and a forwarded
            value would retype the JSON request.
        request_timeout: Per-route-call timeout in seconds (default 30,
            ``None`` disables). Enforced with asyncio.wait_for — httpx's
            own timeout is inert on the in-process ASGI transport.
        max_concurrency: Bound on in-flight route calls across all queries
            (default 16, ``None`` disables). Sibling GraphQL fields resolve
            concurrently, so one wide query fans out; this protects the
            wrapped app's upstream from being hammered by its own bridge.
            Queueing for a slot counts against ``request_timeout``.
        max_depth: Maximum GraphQL selection-set nesting accepted per
            document (default 10, ``None`` disables). Recursive models make
            depth unbounded, and an MCP caller is an LLM that can emit
            runaway nesting — overly deep documents are rejected before
            execution with a validation-style error.
        document_cache_size: LRU capacity for the parse+validate front half
            of execution, keyed by the query string (default 128, 0
            disables). Agents repeat documents constantly; a hit skips
            straight to execution. Execution results are never cached —
            per-call credentials run for real every time.
        auth: Optional ``fastmcp`` auth provider (e.g.
            ``fastmcp.server.auth.providers.github.GitHubProvider``). Passed
            through to ``FastMCP`` untouched: the MCP endpoint then answers
            401 with OAuth discovery metadata, and clients' Bearer tokens
            reach the routes via ``passthrough_headers`` like any other
            caller's. The bridge itself verifies nothing. A bare
            ``fastmcp.server.auth.auth.TokenVerifier`` subclass (override
            ``verify_token``) gates the endpoint the same way, without the
            OAuth machinery.
        mode: ``simple`` registers get_schema + graphql_query;
            ``progressive`` registers the 4-layer tag-based disclosure
            (list_domains -> list_queries -> get_query_schema ->
            graphql_query); ``auto`` picks progressive once the schema
            carries more than ``progressive_threshold`` routes (counted
            after path and tag filtering).
        include_hidden: Also scan routes with ``include_in_schema=False``.
        exclude_deprecated: Drop routes declared ``deprecated=True`` — a
            config-level silent drop, like the path/tag filters. Routes
            that stay keep their GraphQL-native deprecation mark.
    """

    def __init__(
        self,
        app: FastAPI,
        *,
        name: str = "fastapi-mcp-neo API",
        instructions: str | None = None,
        include: Sequence[str] | None = None,
        exclude: Sequence[str] | None = None,
        include_tags: Sequence[str] | None = None,
        exclude_tags: Sequence[str] | None = None,
        allow_mutation: bool = False,
        mode: Literal["auto", "simple", "progressive"] = "auto",
        include_hidden: bool = False,
        exclude_deprecated: bool = False,
        progressive_threshold: int = PROGRESSIVE_THRESHOLD,
        mutation_include: Sequence[str] | None = None,
        passthrough_headers: Sequence[str] | None = None,
        auth: Any | None = None,
        request_timeout: float | None = 30.0,
        max_concurrency: int | None = 16,
        max_depth: int | None = 10,
        document_cache_size: int = 128,
    ) -> None:
        self._mode = mode
        self._instructions = instructions
        self._progressive_threshold = progressive_threshold
        self._handler = RouterGraphQLHandler(
            app,
            include=include,
            exclude=exclude,
            include_tags=include_tags,
            exclude_tags=exclude_tags,
            allow_mutation=allow_mutation,
            include_hidden=include_hidden,
            exclude_deprecated=exclude_deprecated,
            mutation_include=mutation_include,
            passthrough_headers=passthrough_headers,
            request_timeout=request_timeout,
            max_concurrency=max_concurrency,
            max_depth=max_depth,
            document_cache_size=document_cache_size,
        )
        self._resolved_mode = self._resolve_mode(mode)
        self._domains = DomainRegistry(self._handler.routes)
        self._mcp = self._build_mcp(name, allow_mutation, auth, instructions)

    def _resolve_mode(
        self, mode: Literal["auto", "simple", "progressive"]
    ) -> Literal["simple", "progressive"]:
        if mode == "simple":
            return "simple"
        if mode == "progressive":
            return "progressive"
        # auto: progressive disclosure pays off once the SDL is too big to
        # hand an agent in one shot. Count the routes that ENTER the schema
        # (include/exclude applied, include_router wrappers resolved by the
        # scanner) — raw app.routes undercounts on FastAPI >= 0.142, where
        # include_router results are wrapped in non-APIRoute objects, and
        # overcounts when include/exclude narrows the schema.
        route_count = len(self._handler.routes)
        if route_count > self._progressive_threshold:
            logger.info(
                "Schema carries %d routes (> %d): using progressive disclosure",
                route_count,
                self._progressive_threshold,
            )
            return "progressive"
        return "simple"

    @property
    def resolved_mode(self) -> Literal["simple", "progressive"]:
        """The mode actually in effect — ``auto`` is already resolved here,
        so this never returns ``auto`` (unlike the ``mode`` constructor
        argument, which records what was requested)."""
        return self._resolved_mode

    def _build_mcp(
        self,
        name: str,
        allow_mutation: bool,
        auth: Any | None = None,
        instructions: str | None = None,
    ) -> Any:
        # fastmcp is an optional extra; import lazily like nexusx does.
        from fastmcp import FastMCP

        mcp = FastMCP(name, auth=auth, instructions=instructions)
        if self._resolved_mode == "progressive":
            from fastapi_mcp_neo.mcp.progressive_tools import register_progressive_tools

            register_progressive_tools(
                mcp, self._handler, self._domains, allow_mutation=allow_mutation
            )
        else:
            register_simple_tools(mcp, self._handler, allow_mutation=allow_mutation)
        return mcp

    @property
    def handler(self) -> RouterGraphQLHandler:
        return self._handler

    @property
    def domains(self) -> DomainRegistry:
        return self._domains

    @property
    def mcp(self) -> Any:
        return self._mcp

    def run(
        self, *, host: str = "127.0.0.1", port: int = 8000, stateless_http: bool = False
    ) -> None:
        """Run the MCP server over streamable HTTP.

        HTTP is the only transport: the wrapped app is a service whose routes
        speak HTTP, and per-caller credential passthrough needs the HTTP
        request context that stdio has no notion of. Use ``mount_to`` to
        serve MCP on the app's own port instead of opening a second one.

        ``stateless_http=True`` runs one transport per request (no session
        affinity): safe behind multi-worker deployments and non-sticky load
        balancers, at the cost of per-request session setup.
        """
        self._mcp.run(
            transport="http", host=host, port=port, stateless_http=stateless_http
        )

    def mount_to(
        self,
        app: FastAPI,
        path: str = "/mcp",
        *,
        auth_at_root: bool = False,
        stateless_http: bool = False,
    ) -> None:
        """Mount the MCP server into a FastAPI app (streamable HTTP).

        The endpoint is served at ``{path}/`` — e.g. ``/mcp/`` by default.
        Mounting into the SAME app that fastapi-mcp-neo wraps disables the invoker's
        lifespan management (the app's own uvicorn lifespan drives it once).
        Mounting into a different app leaves it managed, but that app will not
        run the wrapped app's startup hooks — prefer mounting into the same app.

        ``auth_at_root`` (requires ``auth=``): serve the MCP endpoint at
        ``path`` but host the auth provider's routes — OAuth endpoints,
        consent page, ``/.well-known/*`` discovery — at the host app's ROOT
        instead of under the mount. Use this when the upstream IdP's
        registered callback lives at the root domain (e.g. reusing an OAuth
        app whose callback is ``/auth/callback`` via a ``redirect_path``
        subdirectory).

        ``stateless_http=True`` runs one transport per request (no session
        affinity): the right mode when several workers / pods serve the
        mount behind a non-sticky load balancer — stateful streamable HTTP
        sessions otherwise 404 when a request lands on a worker that did
        not create them.
        """
        if auth_at_root and getattr(self._mcp, "auth", None) is None:
            raise GQLMCPConfigError(
                "auth_at_root requires FastAPIMCP(..., auth=...) — no auth provider set"
            )
        if auth_at_root:
            http_app = self._mcp.http_app(path=path, stateless_http=stateless_http)
            if app is self._handler.invoker.app:
                self._handler.invoker.disable_lifespan_management()
            else:
                logger.warning(
                    "Mounting fastapi-mcp-neo into a different app than the one it wraps: "
                    "the wrapped app's lifespan events will not fire unless "
                    "something else runs them."
                )
            _compose_lifespan(app, http_app)
            # No Mount("") here: it is a catch-all, so any host route added
            # AFTER this call would silently 404 behind it. Instead, splice
            # the routes (appended last — host routes keep precedence) and
            # copy the sub-app's whole middleware stack.
            #
            # fastmcp private surface this relies on — VERIFY EACH on any
            # fastmcp major bump (pyproject pins <5 for exactly this):
            #   1. http_app.routes carrying a per-route RequireAuthMiddleware
            #      guard on the MCP endpoint;
            #   2. app-level AuthenticationMiddleware (verify_token setting
            #      scope["user"]) on the sub-app's user_middleware stack;
            #   3. RequestContextMiddleware (captures inbound headers so
            #      tool calls can forward them), also from user_middleware;
            #   4. auth.get_well_known_routes() returning re-exposable
            #      /.well-known/* routes.
            # Dropping any of these fails soft-but-visible: 401s, or
            # per-caller headers silently stop reaching the routes.
            for route in http_app.routes:
                app.router.routes.append(route)
            for middleware in getattr(http_app, "user_middleware", []):
                app.add_middleware(middleware.cls, **(middleware.kwargs or {}))
            return
        if app is self._handler.invoker.app:
            # Same app: its server lifespan already (or will) run; a second
            # LifespanManager would drive startup twice.
            self._handler.invoker.disable_lifespan_management()
        else:
            logger.warning(
                "Mounting fastapi-mcp-neo into a different app than the one it wraps: "
                "the wrapped app's lifespan events will not fire unless "
                "something else runs them."
            )
        # http_app's internal route defaults to "/mcp"; re-root it to "/" so the
        # mount path itself is the endpoint.
        http_app = self._mcp.http_app(path="/", stateless_http=stateless_http)
        # Starlette does NOT run lifespans of mounted sub-apps; fastmcp's
        # streamable HTTP session manager needs one, so compose it into the
        # host app's lifespan.
        _compose_lifespan(app, http_app)
        app.mount(path, http_app)
        # OAuth discovery (RFC 9728): the 401 challenge advertises the
        # protected-resource metadata at a HOST-ROOT well-known URL, but the
        # mount above shifts the auth routes under `path` — re-expose the
        # well-known ones at the root where clients expect them.
        auth = getattr(self._mcp, "auth", None)
        if auth is not None:
            existing = {getattr(r, "path", None) for r in app.router.routes}
            for route in auth.get_well_known_routes():
                if route.path not in existing:
                    app.router.routes.append(route)


class RouterMCP(FastAPIMCP):
    """Deprecated alias for :class:`FastAPIMCP` — will be removed at 1.0.

    The old name said Router while the class wraps a whole FastAPI app;
    FastAPIMCP names what it is. Kept as a working subclass so existing
    imports keep functioning (with a DeprecationWarning)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        warnings.warn(
            "RouterMCP is deprecated; use FastAPIMCP (same behavior, "
            "the class wraps a FastAPI app, not a router).",
            DeprecationWarning,
            stacklevel=2,
        )
        super().__init__(*args, **kwargs)
