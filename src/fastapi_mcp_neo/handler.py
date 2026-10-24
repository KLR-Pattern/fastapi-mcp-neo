"""The non-MCP entry point: a GraphQL handler over a FastAPI app.

Constructing the handler scans the app and builds the schema eagerly, so
configuration problems surface at startup, not at first query.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping, Sequence
from inspect import isawaitable
from typing import Any

from graphql import (
    DocumentNode,
    GraphQLError,
    GraphQLSchema,
    parse,
    print_schema,
)
from graphql import (
    execute as execute_document,
)
from graphql.validation import specified_rules, validate
from opentelemetry import trace

from fastapi_mcp_neo.depth_guard import parse_guarded
from fastapi_mcp_neo.invoker import InvocationContext, RouteInvoker
from fastapi_mcp_neo.recursive_expand import (
    expand_recursive_chains,
    recursive_edges,
    unroll_exceeded,
    unroll_limit,
)
from fastapi_mcp_neo.scanner import (
    ReadinessReport,
    RouteInfo,
    RouterScanner,
    SkipRecord,
    build_readiness_report,
)
from fastapi_mcp_neo.schema_builder import GQLMCPConfigError, SchemaBuilder
from fastapi_mcp_neo.type_builder import TypeBuilder


class RouterGraphQLHandler:
    """Exposes a FastAPI app as an executable GraphQL schema."""

    def __init__(
        self,
        app: Any,
        *,
        include: Sequence[str] | None = None,
        exclude: Sequence[str] | None = None,
        include_tags: Sequence[str] | None = None,
        exclude_tags: Sequence[str] | None = None,
        allow_mutation: bool = True,
        include_hidden: bool = False,
        exclude_deprecated: bool = False,
        mutation_include: Sequence[str] | None = None,
        passthrough_headers: Sequence[str] | None = None,
        request_timeout: float | None = 30.0,
        max_concurrency: int | None = 16,
        max_depth: int | None = 10,
        validation_rules: Sequence[Any] | None = None,
        document_cache_size: int = 128,
    ) -> None:
        if max_depth is not None and max_depth < 1:
            raise ValueError("max_depth must be >= 1, or None to disable")
        if document_cache_size < 0:
            raise ValueError("document_cache_size must be >= 0, or 0 to disable")
        self._invoker = RouteInvoker(
            app, timeout=request_timeout, max_concurrency=max_concurrency
        )
        self._max_depth = max_depth
        self._validation_rules = tuple(validation_rules) if validation_rules else ()
        self._document_cache_size = document_cache_size
        # (query string) -> (document | None, validation errors, stamped) —
        # the pure, cacheable front half of every execution. Keyed by the
        # string alone: schema / max_depth / validation_rules are immutable
        # per instance. Per-instance ON PURPOSE — same-process handlers over
        # different apps (comparison bench topology) must never
        # cross-contaminate. Execution results are NOT cached: per-call
        # credentials run for real every time. Single event loop: get/set
        # have no await between them; under exotic thread callers a race
        # costs a duplicate compile (idempotent), never a wrong answer.
        self._doc_cache: OrderedDict[
            str, tuple[DocumentNode | None, tuple[GraphQLError, ...], bool]
        ] = OrderedDict()
        # Whitelist of inbound header names untrusted callers may forward into
        # route calls, lowercased at construction. None = the default
        # ("authorization",): same-app bridges speak for the caller, so the
        # caller's own credential travels by default. An explicitly empty
        # sequence disables passthrough entirely.
        if passthrough_headers is None:
            self._passthrough_headers: tuple[str, ...] = ("authorization",)
        else:
            self._passthrough_headers = tuple(
                h.strip().lower() for h in passthrough_headers if h.strip()
            )
        self._types = TypeBuilder()
        routes, skips = RouterScanner(
            app,
            include=include,
            exclude=exclude,
            include_tags=include_tags,
            exclude_tags=exclude_tags,
            allow_mutation=allow_mutation,
            include_hidden=include_hidden,
            exclude_deprecated=exclude_deprecated,
            mutation_include=mutation_include,
        ).scan(self._types)
        self._builder = SchemaBuilder(routes, self._invoker, self._types)
        self._schema = self._builder.build()
        self._routes: list[RouteInfo] = routes
        self._skips: list[SkipRecord] = skips
        # Recursive-chain unrolling (see recursive_expand): direct
        # self-reference edges, detected once from the built schema. The
        # route already computed the full tree — unrolling merely removes
        # the document-depth limitation so agents receive true depth.
        self._recursive_edges = recursive_edges(self._schema)
        # The unroll limit is PINNED here: compiled documents bake it in,
        # so the excess check must judge by the same value even if the
        # process recursion budget changes later. A raised budget serves
        # deeper trees on NEW handlers; an old one errors loudly instead
        # of silently truncating against its baked-in documents.
        self._unroll_limit = unroll_limit()
        self._recursive_edge_names = frozenset(
            fname for _, fname in self._recursive_edges
        )

    @property
    def schema(self) -> GraphQLSchema:
        return self._schema

    @property
    def routes(self) -> list[RouteInfo]:
        return list(self._routes)

    @property
    def skips(self) -> list[SkipRecord]:
        """Routes excluded from the schema, with reasons — the light lens
        over the scanner's report, so callers can assert nothing
        disappeared unexpectedly (e.g. a new endpoint silently failing to
        map in CI). A strict subset of ``readiness().skips``; prefer
        ``readiness()`` for the full audit (it also covers degraded
        routes and fields)."""
        return list(self._skips)

    def readiness(self) -> ReadinessReport:
        """The full exposure audit over THIS handler's scan results (no
        re-scan, no re-build): skipped routes, raw-JSON bridges, degraded
        model fields. ``RouterScanner(app).readiness()`` runs the same
        audit standalone, without constructing a handler."""
        return build_readiness_report(self._routes, self._skips, self._types)

    @property
    def invoker(self) -> RouteInvoker:
        return self._invoker

    @property
    def passthrough_headers(self) -> tuple[str, ...]:
        """Lowercased header names callers may forward (default: authorization only)."""
        return self._passthrough_headers

    @property
    def query_fields(self) -> dict[tuple[tuple[str, ...], str], Any]:
        """(domain path, field name) -> GraphQLField index (the schema's leaves)."""
        return self._builder.query_fields

    @property
    def mutation_fields(self) -> dict[tuple[tuple[str, ...], str], Any]:
        return self._builder.mutation_fields

    def get_sdl(self) -> str:
        """Full schema in SDL form (the MCP ``get_schema`` payload)."""
        return print_schema(self._schema)

    async def execute(
        self,
        query: str,
        *,
        variables: dict[str, Any] | None = None,
        operation_name: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Execute a GraphQL query against the app's routes.

        ``headers`` are TRUSTED per-call headers — the single credential
        channel (there is no server-side provider). Callers sourcing them
        from an untrusted origin (MCP request, /graphql endpoint) must filter
        them through ``filter_passthrough_headers`` first.
        """
        await self._invoker.start()
        document, validation_errors, stamped = self._prepared_document(query)
        if validation_errors:
            return {"errors": [error.formatted for error in validation_errors]}
        assert document is not None  # compile invariant: no errors → a document
        # The span that makes the middle layer visible: fastmcp's tool span
        # covers the call, FastAPI's route spans cover execution — this one
        # wraps the GraphQL orchestration between them (and the invoker
        # injects its trace context FROM here, so route spans nest under
        # it). No-op without an SDK installed.
        with trace.get_tracer("fastapi_mcp_neo").start_as_current_span(
            "graphql.execute"
        ) as span:
            span.set_attribute(
                "graphql.operation_name", operation_name or "_anonymous"
            )
            result = execute_document(
                self._schema,
                document,
                variable_values=variables or {},
                operation_name=operation_name,
                context_value=InvocationContext(headers=headers),
            )
            if isawaitable(result):
                result = await result
            if result.errors:
                span.set_attribute("graphql.error_count", len(result.errors))
        payload: dict[str, Any] = {}
        if result.data is not None:
            payload["data"] = result.data
        if result.errors:
            payload["errors"] = [error.formatted for error in result.errors]
        # The contract (recursive_expand): data no deeper than the limit
        # returns in full and untouched; data that exceeds the limit is
        # served as deep as the document goes, with a definite error naming
        # the limit. Only a stamped document can have cut data at the limit
        # — unstamped ones skip the walk entirely.
        if stamped and result.data is not None and unroll_exceeded(
            result.data, document, self._recursive_edge_names, self._unroll_limit
        ):
            payload.setdefault("errors", []).append(
                {
                    "message": (
                        f"recursive data exceeded the unroll limit "
                        f"({self._unroll_limit} levels): the response is "
                        f"incomplete. Raise sys.setrecursionlimit to serve "
                        f"deeper trees — the limit scales with the "
                        f"recursion budget."
                    )
                }
            )
        return payload

    # ------------------------------------------------------- document compile

    def _prepared_document(
        self, query: str
    ) -> tuple[DocumentNode | None, tuple[GraphQLError, ...], bool]:
        """Parse + depth-guard + validate — the pure front half of every
        execution, LRU-cached by the query string (agents repeat documents;
        validation over an immutable schema is a pure function of the
        document, so a hit skips straight to execution). Rejected documents
        are cached too: the same malformed query reports identically without
        re-parsing."""
        if self._document_cache_size <= 0:
            return self._compile_document(query)
        cached = self._doc_cache.get(query)
        if cached is not None:
            self._doc_cache.move_to_end(query)
            return cached
        compiled = self._compile_document(query)
        self._doc_cache[query] = compiled
        if len(self._doc_cache) > self._document_cache_size:
            self._doc_cache.popitem(last=False)
        return compiled

    def _compile_document(
        self, query: str
    ) -> tuple[DocumentNode | None, tuple[GraphQLError, ...], bool]:
        # ONE parse serves both the depth guard and execution (execute()
        # takes a pre-parsed DocumentNode) — the string is never parsed twice.
        if self._max_depth is not None:
            error, document = parse_guarded(query, self._max_depth)
            if error is not None:
                return None, (error,), False
            assert document is not None  # parse_guarded: no error → a document
        else:
            try:
                document = parse(query)
            except GraphQLError as exc:
                return None, (exc,), False
        # True-depth recursion: the guard above ran on the AGENT's document
        # (expansion must not become a runaway bypass); unrolling afterwards
        # is a system behavior, so the expanded document skips the guard.
        stamped = False
        written = document  # the agent's document, for caller-supplied rules
        if self._recursive_edges:
            document, stamped = expand_recursive_chains(
                document,
                self._schema,
                self._recursive_edges,
                self._unroll_limit,
            )
        # Standard rules validate the document the executor will actually
        # run; custom rules judge the document AS WRITTEN — unrolling is a
        # system behavior that would inflate any field/complexity count and
        # misfire cost guards on reasonable recursive selections.
        errors: tuple[GraphQLError, ...] = tuple(
            validate(self._schema, document, specified_rules)
        )
        if self._validation_rules:
            errors = (
                *errors,
                *validate(self._schema, written, self._validation_rules),
            )
        return document, errors, stamped

    async def aclose(self) -> None:
        """Release the invoker's HTTP client and app lifespan."""
        await self._invoker.aclose()

    def mount_graphql(
        self,
        app: Any,
        *,
        graphql_path: str = "/graphql",
        graphiql_path: str = "/graphiql",
    ) -> None:
        """Serve a GraphiQL playground + GraphQL HTTP endpoint on a FastAPI app.

        Mounting into the SAME app that fastapi-mcp-neo wraps disables the invoker's
        lifespan management (the app's own server lifespan drives it once).
        """
        from fastapi_mcp_neo.http_api import create_graphql_router

        if app is self._invoker.app:
            self._invoker.disable_lifespan_management()
        router = create_graphql_router(
            self, graphql_path=graphql_path, graphiql_path=graphiql_path
        )
        app.include_router(router)


__all__ = ["RouterGraphQLHandler", "GQLMCPConfigError"]
