"""Assemble the final ``GraphQLSchema`` from scanned routes.

Fields are grouped by the tag-derived domain tree (the UseCaseService-style
hierarchy): a route tagged ``shop:catalog`` lands at
``Query.shop.catalog.<function_name>``. Untagged routes fall into the domain
derived from their first path segment, so every field always has a group.

Every leaf field's resolver delegates to ``RouteInvoker.invoke``; GraphQL-level
nullability mirrors FastAPI's required flags so a request the schema accepts
is a request FastAPI will not 422.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any, cast

from graphql import (
    GraphQLArgument,
    GraphQLBoolean,
    GraphQLEnumType,
    GraphQLField,
    GraphQLInputObjectType,
    GraphQLList,
    GraphQLNonNull,
    GraphQLObjectType,
    GraphQLScalarType,
    GraphQLSchema,
    Undefined,
)

from fastapi_mcp_neo.invoker import InvocationContext, RouteInvoker
from fastapi_mcp_neo.naming import DuplicateFieldError
from fastapi_mcp_neo.scalars import GraphQLJSON
from fastapi_mcp_neo.scanner import OutputKind, ParamInfo, RouteInfo
from fastapi_mcp_neo.type_builder import (
    TypeBuilder,
    append_note,
    describe_literal_values,
    graphql_default,
)

logger = logging.getLogger(__name__)


class GQLMCPConfigError(ValueError):
    """Raised when the scanned routes cannot form a usable schema."""


class DuplicateArgError(ValueError):
    """Two parameters of one route map onto the same GraphQL argument name."""


def _argument(param: ParamInfo, types: TypeBuilder, route: RouteInfo) -> GraphQLArgument:
    bare = cast(
        "GraphQLScalarType | GraphQLEnumType | GraphQLInputObjectType | GraphQLList[Any]",
        types.input_type(param.annotation, context=f"parameter '{param.name}' of {route.path}"),
    )
    gtype: Any = GraphQLNonNull(bare) if param.required else bare
    default = Undefined if param.required else graphql_default(param.default)
    return GraphQLArgument(gtype, default_value=default, description=param.description)


def _arguments(route: RouteInfo, types: TypeBuilder) -> dict[str, GraphQLArgument]:
    args: dict[str, GraphQLArgument] = {}
    for param in (*route.path_params, *route.query_params, *route.body_params):
        gname = param.gname or param.name  # sanitized when the wire name is
        # not a legal GraphQL identifier
        if gname in args:
            raise DuplicateArgError(
                f"Route {route.method} {route.path} has two parameters mapping to "
                f"GraphQL argument '{gname}'; rename one of them."
            )
        args[gname] = _argument(param, types, route)
    return args


def _resolver(route: RouteInfo, invoker: RouteInvoker) -> Any:
    # GraphQL-name -> wire-name for sanitized arguments: the executor hands
    # kwargs keyed by the (legal) GraphQL name, the request needs the wire one.
    rename = {
        p.gname: p.name
        for p in (*route.path_params, *route.query_params, *route.body_params)
        if p.gname and p.gname != p.name
    }

    async def resolve(_root: Any, _info: Any, **kwargs: Any) -> Any:
        # Per-call headers ride in graphql-core's context_value. A foreign or
        # absent context (someone calling graphql() on handler.schema directly)
        # simply means "no per-call headers" — identical to pre-passthrough
        # behavior.
        context = _info.context
        headers = context.headers if isinstance(context, InvocationContext) else None
        if rename:
            kwargs = {rename.get(k, k): v for k, v in kwargs.items()}
        return await invoker.invoke(route, kwargs, extra_headers=headers)

    return resolve


def _deprecation_reason(route: RouteInfo) -> str | None:
    # OpenAPI `deprecated: true` maps onto GraphQL-native deprecation:
    # introspection exposes isDeprecated/deprecationReason and GraphiQL
    # strikes the field through. Deprecated fields stay executable.
    return "This endpoint is deprecated." if route.deprecated else None


def _leaf_field(
    route: RouteInfo, invoker: RouteInvoker, types: TypeBuilder
) -> GraphQLField:
    # Route responses are deliberately NULLABLE at the field level: a route
    # erroring (4xx/5xx) must null only its own field, so agents composing
    # several routes in one query keep the other results. A NonNull field
    # error would null the whole response per GraphQL spec.
    description = route.description
    plan = route.output
    if plan.kind is OutputKind.VOID:
        # Explicit `-> None` (204-style deletes and side-effect calls): the
        # CALL is the point, and "no response body" is an explicit contract —
        # so the field bridges as a Boolean success flag: true on 2xx, and
        # failures already surface as field errors through the invoker.
        note = (
            "Returns true on success — this route has no response body "
            "(-> None / 204); failures surface as field errors."
        )
        base_resolve = _resolver(route, invoker)

        async def void_resolve(_root: Any, _info: Any, **kwargs: Any) -> bool:
            await base_resolve(_root, _info, **kwargs)
            return True

        return GraphQLField(
            GraphQLBoolean,
            args=_arguments(route, types),
            resolve=void_resolve,
            description=append_note(description, note),
            deprecation_reason=_deprecation_reason(route),
        )
    if plan.json_scalar:
        # Every raw-JSON bucket (TypeVar-degraded, untyped, filtered,
        # union) tells the agent the same story the readiness report tells
        # the operator — the plan's note IS the classified reason.
        return GraphQLField(
            GraphQLJSON,
            args=_arguments(route, types),
            resolve=_resolver(route, invoker),
            description=append_note(description, plan.sdl_note),
            deprecation_reason=_deprecation_reason(route),
        )
    # Structured: route-level Literals get the same allowed-values note
    # model fields carry (_field_description) — the route field is where
    # agents read it.
    literal_note = describe_literal_values(route.response_annotation)
    if literal_note:
        description = append_note(description, literal_note)
    response_type = types.bare_output_type(
        route.response_annotation, context=f"response of {route.path}"
    )
    return GraphQLField(
        response_type,
        args=_arguments(route, types),
        resolve=_resolver(route, invoker),
        description=description,
        deprecation_reason=_deprecation_reason(route),
    )


def group_type_name(path: tuple[str, ...], *, mutation: bool) -> str:
    """Deterministic group type name: ``("shop", "catalog")`` -> ShopCatalogQuery."""
    pascal = "".join(part.title() for seg in path for part in seg.split("_"))
    return f"{pascal}{'Mutation' if mutation else 'Query'}"


class _Group:
    """Tree node: routes ending at this exact path + child segments."""

    def __init__(self) -> None:
        self.own: list[RouteInfo] = []
        self.children: dict[str, _Group] = {}

    def insert(self, path: tuple[str, ...], route: RouteInfo) -> None:
        node = self
        for seg in path:
            node = node.children.setdefault(seg, _Group())
        node.own.append(route)

    def is_empty(self) -> bool:
        return not self.own and all(child.is_empty() for child in self.children.values())

    def prune(self) -> None:
        for seg in [s for s, c in self.children.items() if c.is_empty()]:
            del self.children[seg]
        for child in self.children.values():
            child.prune()


class SchemaBuilder:
    """Builds a domain-grouped ``GraphQLSchema`` from ``RouteInfo`` items."""

    def __init__(
        self,
        routes: Sequence[RouteInfo],
        invoker: RouteInvoker,
        types: TypeBuilder | None = None,
    ) -> None:
        self._routes = list(routes)
        self._invoker = invoker
        self._types = types or TypeBuilder()
        self._used_type_names: dict[str, tuple[str, ...]] = {}
        # (domain path, field name) -> field indexes consumed by the
        # progressive-disclosure tools. The composite key is what allows the
        # same field name in different domains (GraphQL only forbids name
        # clashes within ONE object type, i.e. within one domain group).
        self._query_fields: dict[tuple[tuple[str, ...], str], GraphQLField] = {}
        self._mutation_fields: dict[tuple[tuple[str, ...], str], GraphQLField] = {}

    @property
    def types(self) -> TypeBuilder:
        return self._types

    @property
    def query_fields(self) -> dict[tuple[tuple[str, ...], str], GraphQLField]:
        return dict(self._query_fields)

    @property
    def mutation_fields(self) -> dict[tuple[tuple[str, ...], str], GraphQLField]:
        return dict(self._mutation_fields)

    # ------------------------------------------------------------------ names

    def _unique_group_name(self, path: tuple[str, ...], *, mutation: bool) -> str:
        base = group_type_name(path, mutation=mutation)
        owner = self._used_type_names.get(base)
        if owner is not None and owner != path:
            candidate, counter = base, 2
            while self._used_type_names.get(candidate) not in (None, path):
                candidate = f"{base}{counter}"
                counter += 1
            logger.warning(
                "Group type name %r already owned by %s; using %r for %s",
                base, owner, candidate, path,
            )
            base = candidate
        self._used_type_names[base] = path
        return base

    # ----------------------------------------------------------------- build

    def _group_field(
        self, node: _Group, path: tuple[str, ...], *, mutation: bool
    ) -> GraphQLField:
        fields: dict[str, GraphQLField] = {}
        index = self._mutation_fields if mutation else self._query_fields
        # Duplicate check scoped to THIS domain group — the same boundary
        # GraphQL enforces (one object type). Different domains may each have
        # their own field of the same name.
        seen: dict[str, tuple[str, str]] = {}
        for route in node.own:
            previous = seen.get(route.field_name)
            if previous is not None:
                raise DuplicateFieldError(
                    f"GraphQL field name {route.field_name!r} is used twice inside "
                    f"domain group '{':'.join(path)}' — by {previous[0]} {previous[1]} "
                    f"and {route.method} {route.path}. Names must be unique within a "
                    f"domain (the same GraphQL object type); rename one endpoint "
                    f"function or re-tag one route into another domain."
                )
            seen[route.field_name] = (route.method, route.path)
            field = _leaf_field(route, self._invoker, self._types)
            index[(path, route.field_name)] = field
            fields[route.field_name] = field
        child_segments: list[str] = []
        for seg, child in sorted(node.children.items()):
            # A child domain segment competes with leaf fields for the same
            # namespace (one GraphQL object type) — without this check the
            # group would silently REPLACE the same-named leaf field.
            previous = seen.get(seg)
            if previous is not None:
                raise DuplicateFieldError(
                    f"GraphQL field name {seg!r} is used twice inside "
                    f"domain group '{':'.join(path)}' — by leaf route "
                    f"{previous[0]} {previous[1]} and the subdomain "
                    f"'{':'.join((*path, seg))}'. Names must be unique within "
                    f"a domain (the same GraphQL object type); rename the "
                    f"endpoint function or re-tag it out of parent domain "
                    f"'{':'.join(path)}'."
                )
            fields[seg] = self._group_field(child, (*path, seg), mutation=mutation)
            child_segments.append(seg)
        obj = GraphQLObjectType(
            name=self._unique_group_name(path, mutation=mutation),
            fields=fields,
        )

        # Group namespaces never fail: resolve to a dict pre-populated with the
        # child group keys so the default resolver chains one level deeper.
        def resolve_group(
            _root: Any, _info: Any, _keys: tuple[str, ...] = tuple(child_segments)
        ) -> Any:
            return {key: {} for key in _keys}

        return GraphQLField(GraphQLNonNull(obj), resolve=resolve_group)

    def _root_type(
        self, tree: _Group, *, mutation: bool
    ) -> GraphQLObjectType | None:
        tree.prune()
        if tree.is_empty():
            return None
        fields = {}
        for seg, child in sorted(tree.children.items()):
            fields[seg] = self._group_field(child, (seg,), mutation=mutation)
        if not fields:
            return None
        return GraphQLObjectType(
            name="Mutation" if mutation else "Query", fields=fields
        )

    def build(self) -> GraphQLSchema:
        query_routes = [r for r in self._routes if not r.is_mutation]
        mutation_routes = [r for r in self._routes if r.is_mutation]

        if not query_routes and not mutation_routes:
            raise GQLMCPConfigError(
                "fastapi-mcp-neo found no routable endpoints: every route was skipped "
                "(see the fastapi-mcp-neo warning log for reasons)."
            )

        query_tree = _Group()
        for route in query_routes:
            for domain in route.domains:
                query_tree.insert(domain, route)
        mutation_tree = _Group()
        for route in mutation_routes:
            for domain in route.domains:
                mutation_tree.insert(domain, route)

        query_type = self._root_type(query_tree, mutation=False)
        mutation_type = self._root_type(mutation_tree, mutation=True)
        if query_type is None and mutation_type is None:  # pragma: no cover
            raise GQLMCPConfigError(
                "fastapi-mcp-neo found no routable endpoints: every route was skipped."
            )
        if query_type is None:
            # GraphQL requires a Query root; a mutation-only schema would fail
            # validation at first execution, so fail fast with guidance instead.
            raise GQLMCPConfigError(
                "fastapi-mcp-neo cannot build a mutation-only schema: GraphQL "
                "requires a Query root type. Expose at least one GET route "
                "(or drop allow_mutation)."
            )
        return GraphQLSchema(query=query_type, mutation=mutation_type)
