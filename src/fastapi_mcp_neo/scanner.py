"""Introspect a FastAPI app's routes into GraphQL field metadata.

The scanner is the single source of truth for "what the GraphQL schema will
contain": every rule that excludes a route (no typed response, hidden route,
mutation disabled, …) lives here and produces a ``SkipRecord``; the caller
decides whether those skips are acceptable.
"""

from __future__ import annotations

import fnmatch
import inspect
import logging
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, TypeVar

from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.dependencies.utils import get_typed_return_annotation
from fastapi.params import File, Form
from fastapi.responses import Response
from fastapi.routing import APIRoute
from fastapi.utils import DefaultPlaceholder  # type: ignore[attr-defined]
from pydantic import BaseModel
from pydantic.fields import FieldInfo

from fastapi_mcp_neo.domains import domains_for
from fastapi_mcp_neo.naming import field_name_for
from fastapi_mcp_neo.type_builder import (
    TypeBuilder,
    UnsupportedFieldTypeError,
    request_wire_name,
    sanitize_graphql_name,
    union_member_names,
)

logger = logging.getLogger(__name__)


class OutputKind(Enum):
    """The four capability buckets a route's response lands in (README
    'Capability boundaries'), plus the degradation causes that collapse
    into the raw-JSON bucket."""

    VOID = "void"  # -> None: Boolean success field — its own contract
    STRUCTURED = "structured"
    TYPEVAR = "typevar"  # unbound TypeVar degraded this route at the boundary
    ANY = "any"  # no typed response
    FILTER = "filter"  # serialization kwargs reshape the JSON
    UNION = "union"  # union response


@dataclass(frozen=True)
class OutputPlan:
    """Route-response classification, computed ONCE at scan time. The plan
    is the single source behind the startup notice, ReadinessReport, and
    the SDL field note — the three can never drift apart (README: 'One
    classifier backs both')."""

    kind: OutputKind
    audit_reason: str | None = None  # readiness + startup-notice text
    sdl_note: str = ""  # agent-facing description note

    @property
    def json_scalar(self) -> bool:
        return self.kind in (
            OutputKind.TYPEVAR,
            OutputKind.ANY,
            OutputKind.FILTER,
            OutputKind.UNION,
        )


STRUCTURED_PLAN = OutputPlan(OutputKind.STRUCTURED)

# Priority when a route registers multiple verbs (e.g. @app.api_methods).
_VERB_PRIORITY = ("GET", "POST", "PUT", "PATCH", "DELETE")
_MUTATION_VERBS = {"POST", "PUT", "PATCH", "DELETE"}

# Route-level serialization kwargs that reshape the response AFTER
# validation — the JSON the resolver sees may lack keys (or use different
# keys) than the response model declares, so per-field GraphQL promises
# cannot hold. exclude_none is NOT here: it only drops Optional-valued
# keys, which the bridge maps to nullable fields anyway.
_RESPONSE_FILTERS = (
    "response_model_exclude_unset",
    "response_model_exclude_defaults",
    "response_model_include",
    "response_model_exclude",
)


def response_filter_kwarg(route: Any) -> str | None:
    """The first route kwarg that makes response fields unpredictable.

    Returns the kwarg name (for documentation), or None when the response
    shape is trustworthy. ``response_model_by_alias=False`` retargets keys
    rather than filtering them, but the consequence is identical: the
    JSON no longer matches the alias-built GraphQL field names.
    """
    for key in _RESPONSE_FILTERS:
        if getattr(route, key, None):
            return key
    if getattr(route, "response_model_by_alias", True) is False:
        return "response_model_by_alias=False"
    return None


@dataclass(frozen=True)
class ParamInfo:
    """One GraphQL-argument-worthy parameter of a route."""

    name: str
    annotation: Any
    required: bool
    default: Any = None
    embed: bool = False
    raw_name: str = ""  # function-arg name, used for path-template replacement
    description: str | None = None  # from Query()/Body() metadata
    # GraphQL-side argument name when the wire name is not a legal GraphQL
    # identifier (empty = same as ``name``); the resolver translates back.
    gname: str = ""


@dataclass(frozen=True)
class RouteInfo:
    """Everything the schema builder needs about one routable endpoint."""

    route: Any = field(repr=False)  # APIRoute or FastAPI >=0.142 include leaf
    method: str
    path: str
    field_name: str
    path_params: tuple[ParamInfo, ...] = ()
    query_params: tuple[ParamInfo, ...] = ()
    body_params: tuple[ParamInfo, ...] = ()
    response_annotation: Any = None
    tags: tuple[str, ...] = ()
    description: str | None = None
    deprecated: bool = False
    response_filter: str | None = None  # kwarg that unshapes the response JSON
    domains: frozenset[tuple[str, ...]] = frozenset()
    # Output classification computed once at scan time (_classify_output):
    # the single source the startup notice, ReadinessReport, and the SDL
    # field note all read.
    output: OutputPlan = STRUCTURED_PLAN

    @property
    def is_mutation(self) -> bool:
        return self.method in _MUTATION_VERBS


@dataclass(frozen=True)
class SkipRecord:
    """A route that was excluded from the schema, and why."""

    path: str
    method: str
    reason: str
    tags: tuple[str, ...] = ()  # the route's string tags (which domain it belongs to)


@dataclass(frozen=True)
class DegradedRecord:
    """A route that DID enter the schema but degraded to raw JSON (no field
    selection), and why. Still callable — just not structurally exposed."""

    path: str
    method: str
    field_name: str  # the GraphQL field carrying the raw JSON
    reason: str
    tags: tuple[str, ...] = ()  # the route's string tags (which domain it belongs to)


@dataclass(frozen=True)
class ReadinessReport:
    """The exposure audit the startup notices are built from: which routes
    the bridge skips, which it degrades to raw JSON, and which model fields
    degrade. Pure data — get one from ``RouterScanner(app).readiness()``
    (standalone, no handler) or ``handler.readiness()`` (over stored
    results, no re-scan); the assembly itself is an internal detail."""

    skips: tuple[SkipRecord, ...]  # never entered the schema
    degraded: tuple[DegradedRecord, ...]  # in the schema, no field selection
    degraded_fields: tuple[tuple[str, str], ...]  # (Model.field, reason)

    @property
    def ready(self) -> bool:
        """True when nothing is skipped or degraded — assert this
        in CI to pin the exposure you expect."""
        return not (self.skips or self.degraded or self.degraded_fields)


def build_readiness_report(
    routes: Sequence[RouteInfo], skips: Sequence[SkipRecord], types: TypeBuilder
) -> ReadinessReport:
    """Assemble the report from one scan's three products — the single
    source shared by ``RouterScanner.readiness()`` and
    ``RouterGraphQLHandler.readiness()``. Package-level (not exported):
    callers asking for a report should never need scan's intermediates."""
    return ReadinessReport(
        skips=tuple(skips),
        degraded=tuple(
            DegradedRecord(r.path, r.method, r.field_name, reason, tags=r.tags)
            for r in routes
            if (reason := _degraded_reason(r)) is not None
        ),
        degraded_fields=tuple(types.degraded_fields),
    )


def _classify_output(
    annotation: Any,
    response_filter: str | None,
    typevar_response: str | None,
    param_reasons: list[str],
) -> OutputPlan:
    """Classify one route's response once, at scan time. Bucket priority
    follows the audit's historical first-match order; TypeVar reasons (the
    response's own and its params') ride along as audit text in every
    bucket, exactly as the old joined scan_degraded_reason did."""
    reasons = [r for r in (typevar_response, *param_reasons) if r]
    audit = "; ".join(reasons) if reasons else None
    if annotation is type(None):
        # A bodyless response has nothing to filter or union over — the
        # Boolean success bucket is its own contract, never a degradation.
        return OutputPlan(OutputKind.VOID, audit_reason=audit)
    if typevar_response is not None:
        # The annotation was rewritten to Any at the trial; the plan keeps
        # the REAL reason so the SDL note never points at the wrong fix.
        return OutputPlan(
            OutputKind.TYPEVAR,
            audit_reason=audit,
            sdl_note=f"Returns raw JSON: {typevar_response}.",
        )
    if annotation is Any:
        return OutputPlan(
            OutputKind.ANY,
            audit_reason=audit
            or (
                "no typed response — bridged as raw JSON; add a return "
                "annotation or response_model for a structured type"
            ),
            sdl_note=(
                "Returns raw JSON: this endpoint declares no response type. "
                "Add a return annotation or response_model for a structured, "
                "field-selectable type."
            ),
        )
    if response_filter:
        return OutputPlan(
            OutputKind.FILTER,
            audit_reason=audit or f"response filtered via {response_filter}",
            sdl_note=(
                "Returns a raw JSON blob without field selection: this route "
                f"filters its response via {response_filter}, so the "
                "GraphQL schema makes no per-field promises."
            ),
        )
    if (names := union_member_names(annotation)):
        return OutputPlan(
            OutputKind.UNION,
            audit_reason=audit
            or (
                f"union response ({'|'.join(names)}) — restructure into one "
                "model per shape to regain field selection"
            ),
            sdl_note=(
                f"Returns raw JSON whose shape is one of: {', '.join(names)} — "
                "union responses vary at runtime; select the field bare and "
                "inspect the result."
            ),
        )
    return OutputPlan(OutputKind.STRUCTURED, audit_reason=audit)


def _degraded_reason(route: RouteInfo) -> str | None:
    """Why one scanned route degrades to raw JSON (loses field selection),
    or None when its response stays structured. Reads the plan computed at
    scan time — the single source shared by the startup notice and
    ReadinessReport."""
    return route.output.audit_reason


def _mf_wire(p: Any) -> str:
    """Wire name of a FastAPI ModelField: validation_alias, else alias, else
    the field name (the same precedence FastAPI validates by)."""
    return str(p.validation_alias or p.alias or p.name)


def _param_required(field_info: FieldInfo) -> bool:
    try:
        return bool(field_info.is_required())
    except Exception:  # pragma: no cover - defensive for fastapi internals
        return field_info.default is None


def _make_param(
    name: str,
    annotation: Any,
    required: bool,
    default: Any = None,
    *,
    raw_name: str = "",
    description: str | None = None,
    embed: bool = False,
) -> ParamInfo:
    """The single ParamInfo constructor — and the single gname producer:
    the GraphQL argument name is the sanitized wire name, computed exactly
    here so scan-time and schema-time can never disagree."""
    gname = sanitize_graphql_name(name, what="argument name")
    return ParamInfo(
        name=name,
        annotation=annotation,
        required=required,
        default=None if required else default,
        embed=embed,
        raw_name=raw_name,
        description=description,
        gname="" if gname == name else gname,
    )


def _expand_query_model(model: type[BaseModel]) -> list[ParamInfo]:
    """Flatten a Query Parameter Model into individual query params.

    Mirrors FastAPI's wire format for a lone ``Annotated[Model, Query()]``
    parameter: each model field becomes one query key (alias-aware), validated
    by the model's own field metadata.
    """
    params: list[ParamInfo] = []
    for field_name, info in model.model_fields.items():
        required = bool(info.is_required())
        params.append(
            _make_param(
                request_wire_name(info, field_name),
                info.annotation,
                required,
                info.get_default(call_default_factory=False),
                raw_name=field_name,
                description=info.description,
            )
        )
    return params


def _to_param_info(model_field: Any, *, path_param: bool = False) -> ParamInfo:
    field_info: FieldInfo = model_field.field_info
    # Request-side name: FastAPI validates against validation_alias/alias/name.
    # FastAPI's ModelField.validation_alias is str-only and .alias already
    # falls back to the field name — no AliasChoices flattening applies here.
    required = path_param or _param_required(field_info)
    return _make_param(
        _mf_wire(model_field),
        field_info.annotation,
        required,
        field_info.get_default(call_default_factory=False),
        raw_name=model_field.name,
        description=getattr(field_info, "description", None),
        embed=bool(getattr(field_info, "embed", False)),
    )


@dataclass(frozen=True)
class _FlatParams:
    """One route's FastAPI params by location (name-deduped ModelFields).
    Header/cookie entries exist only for the required-parameter gate — they
    never become GraphQL arguments."""

    path: tuple[Any, ...]
    query: tuple[Any, ...]
    header: tuple[Any, ...]
    cookie: tuple[Any, ...]
    body: tuple[Any, ...]


def _flatten_params(dependant: Dependant) -> _FlatParams:
    """Walk the dependency tree collecting params by location (name-deduped)."""
    path_params: list[Any] = []
    query_params: list[Any] = []
    header_params: list[Any] = []
    cookie_params: list[Any] = []
    body_params: list[Any] = []
    seen: set[int] = set()
    stack: list[Dependant] = [dependant]
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        path_params.extend(current.path_params)
        query_params.extend(current.query_params)
        header_params.extend(current.header_params)
        cookie_params.extend(current.cookie_params)
        body_params.extend(current.body_params)
        stack.extend(reversed(current.dependencies))

    def dedup(params: list[Any]) -> list[Any]:
        by_name: dict[str, Any] = {}
        for p in params:
            name = _mf_wire(p)
            by_name.setdefault(name, p)
        return list(by_name.values())

    return _FlatParams(
        path=tuple(dedup(path_params)),
        query=tuple(dedup(query_params)),
        header=tuple(dedup(header_params)),
        cookie=tuple(dedup(cookie_params)),
        body=tuple(dedup(body_params)),
    )


def _body_embeds(body_params: Sequence[ParamInfo]) -> bool:
    """Replicates fastapi.dependencies.utils._should_embed_body_fields
    (JSON body params only — Form/File are not bridged): a single body
    parameter without an explicit ``Body(embed=True)`` takes the WHOLE
    body as its value, whatever its type (BaseModel, dict, scalar) —
    embedding applies to multiple body params or explicit embed only."""
    if len({p.name for p in body_params}) > 1:
        return True
    return bool(body_params[0].embed)

def _response_annotation(route: Any) -> Any:
    response_model = route.response_model
    # Both an explicit response_model=None (Response-returning routes) and
    # the DefaultPlaceholder (unset) fall back to the typed return annotation.
    if response_model is None or isinstance(response_model, DefaultPlaceholder):
        raw = inspect.signature(route.endpoint).return_annotation
        # `-> None` (and its string form under future-annotations) is an
        # EXPLICIT "no response body" contract — bridged as a Boolean
        # success field. No annotation at all still bridges, as the JSON
        # scalar via the Any marker: available rather than skipped, and
        # flagged in the degraded-routes startup notice so an annotation
        # lost to refactoring stays visible.
        if raw is None or raw == "None":
            return type(None)
        typed = get_typed_return_annotation(route.endpoint)
        return Any if typed is None else typed
    return response_model


# --------------------------------------------------------------- input gates


def _required_header_cookie_reason(flat: _FlatParams) -> str | None:
    for p in flat.header + flat.cookie:
        if _param_required(p.field_info):
            return (
                f"required header/cookie parameter "
                f"'{_mf_wire(p)}' cannot be a GraphQL "
                f"argument — make it optional (caller credentials ride "
                f"passthrough_headers instead of GraphQL arguments)"
            )
    return None


def _path_model_reason(path_p: tuple[Any, ...]) -> str | None:
    for p in path_p:
        annotation = p.field_info.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            return (
                f"path parameter model '{annotation.__name__}' is not supported "
                f"— use scalar path parameters (str/int/uuid) and accept the "
                f"model inside the handler"
            )
    return None


def _mixed_query_model_reason(query_p: tuple[Any, ...]) -> str | None:
    for p in query_p:
        annotation = p.field_info.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            # FastAPI flattens a LONE BaseModel query param ("Query
            # Parameter Models") into individual query keys on the wire;
            # a model mixed with plain query params has no such shape.
            if len(query_p) != 1:
                return (
                    f"query parameter model '{annotation.__name__}' mixed with "
                    f"plain query parameters is not supported — move the "
                    f"plain parameters into the model (FastAPI itself "
                    f"rejects the mixed form on the wire)"
                )
    return None


def _body_shape_reason(body_p: tuple[Any, ...]) -> str | None:
    for p in body_p:
        # Form/File bodies are deliberately not bridged: the invoker
        # speaks JSON, and MCP tool arguments have no file channel
        # (SEP-2631 is the protocol-level fix, still draft). Skip must
        # happen HERE — a Form-only scalar annotation (str/int/...) is a
        # perfectly valid GraphQL input, so a type check would let the
        # route through into a field that always 422s.
        if isinstance(p.field_info, Form | File):
            return (
                f"form/file parameter '{_mf_wire(p)}' cannot be bridged "
                f"(the invoker sends JSON bodies only) — the route stays "
                f"available over plain HTTP"
            )
        if p.field_info.annotation is None:
            return (
                "body parameter without a typed annotation — annotate it "
                "(e.g. payload: MyModel)"
            )
    return None


def _unmappable_response_reason(response_annotation: Any) -> str | None:
    if isinstance(response_annotation, type) and issubclass(
        response_annotation, Response
    ):
        return (
            "returns a raw Response object, no typed body to expose "
            "— return a typed model (or add response_model) instead"
        )
    return None


def _route_params(
    flat: _FlatParams,
) -> tuple[list[ParamInfo], list[ParamInfo], list[ParamInfo]]:
    """(query, path, body) ParamInfos, with body embedding resolved here —
    the single place a route's input plan takes shape."""
    q = flat.query
    query_params = (
        _expand_query_model(q[0].field_info.annotation)
        if len(q) == 1
        and isinstance(q[0].field_info.annotation, type)
        and issubclass(q[0].field_info.annotation, BaseModel)
        else [_to_param_info(p) for p in q]
    )
    path_params = [_to_param_info(p, path_param=True) for p in flat.path]
    body_params = [_to_param_info(p) for p in flat.body]
    if body_params and _body_embeds(body_params):
        body_params = [replace(p, embed=True) for p in body_params]
    return query_params, path_params, body_params


def _trial_route_types(
    types: TypeBuilder,
    route_path: str,
    response_annotation: Any,
    *,
    skip_response: bool,
    params_lists: tuple[list[ParamInfo], ...],
) -> tuple[Any, str | None, list[str]]:
    """Trial-build every type this route needs. An unbound TypeVar at a
    route boundary degrades THIS route to raw JSON (still callable,
    audited) — the same fallback a TypeVar field already gets inside a
    model. Anything else unmappable re-raises so the caller can skip the
    route: no fallback can express that shape (e.g. `-> bytes`). Filtered
    responses bypass the structured type entirely (the JSON scalar carries
    whatever arrives); only their INPUT types are trialed.

    Returns (rewritten annotation, response TypeVar reason, param reasons).
    """
    typevar_response: str | None = None
    param_reasons: list[str] = []
    if not skip_response:
        try:
            types.output_type(
                response_annotation, context=f"response of {route_path}"
            )
        except UnsupportedFieldTypeError as exc:
            if not isinstance(exc.annotation, TypeVar):
                raise
            typevar_response = exc.field_reason or exc.context
            response_annotation = Any
    for params in params_lists:
        for i, param in enumerate(params):
            try:
                types.input_type(
                    param.annotation,
                    context=f"parameter '{param.name}' of {route_path}",
                )
            except UnsupportedFieldTypeError as exc:
                if not isinstance(exc.annotation, TypeVar):
                    raise
                param_reasons.append(exc.field_reason or exc.context)
                params[i] = replace(param, annotation=Any)
    return response_annotation, typevar_response, param_reasons


def _matches(path: str, patterns: Sequence[str] | None) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns or ())


def _tags_match(tags: Sequence[str], patterns: Sequence[str] | None) -> bool:
    """A route matches when ANY of its string tags fnmatches ANY pattern
    ("iam:*" matches "iam:users"; an exact pattern matches too). Empty tags
    match nothing, so include_tags is a strict whitelist (untagged drops)."""
    return any(_matches(tag, patterns) for tag in tags)


def _iter_api_routes(routes: Iterable[Any]) -> Iterator[Any]:
    """Flatten app.routes into scannable route objects (duck-typed).

    FastAPI >= 0.142 wraps ``include_router`` results in a private
    ``_IncludedRouter`` that resolves lazily; its ``effective_candidates()``
    leaves (``_EffectiveRouteContext``) carry the PREFIX-RESOLVED path plus
    the usual metadata (methods/response_model/dependant/tags). Older
    FastAPI copies plain ``APIRoute`` objects flat into ``app.routes``.
    Both shapes are yielded uniformly; everything else (Mount, WebSocket,
    docs routes) is skipped.
    """
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        elif hasattr(route, "effective_candidates"):
            # include_router wrapper (nested includes recurse the same way)
            yield from _iter_api_routes(route.effective_candidates())
        elif hasattr(route, "dependant") and hasattr(route, "methods"):
            # _EffectiveRouteContext leaf with the resolved path
            yield route


class RouterScanner:
    """Scans ``app.routes`` into ``RouteInfo`` / ``SkipRecord`` lists."""

    def __init__(
        self,
        app: FastAPI,
        *,
        include_tags: Sequence[str] | None = None,
        exclude_tags: Sequence[str] | None = None,
        allow_mutation: bool = True,
        include_hidden: bool = False,
        exclude_deprecated: bool = False,
    ) -> None:
        self._app = app
        self._include_tags = include_tags
        self._exclude_tags = exclude_tags
        self._allow_mutation = allow_mutation
        self._include_hidden = include_hidden
        self._exclude_deprecated = exclude_deprecated

    def scan(
        self, types: TypeBuilder | None = None
    ) -> tuple[list[RouteInfo], list[SkipRecord]]:
        """Scan the app; ``types`` (shared TypeBuilder) trials response models."""
        types = types or TypeBuilder()
        routes: list[RouteInfo] = []
        skips: list[SkipRecord] = []

        for r in _iter_api_routes(self._app.routes):

            route_methods = r.methods or set()
            verbs = sorted(route_methods & set(_VERB_PRIORITY), key=_VERB_PRIORITY.index)
            if not verbs:
                continue  # HEAD / OPTIONS only
            if len(verbs) > 1:
                logger.warning(
                    "Route %s registers multiple verbs %s; using %s (priority order)",
                    r.path,
                    sorted(route_methods),
                    verbs[0],
                )
            method = verbs[0]

            # Tag filters: config-level, silent drop.
            # OpenAPI allows Enum tags; only string tags participate (the
            # same rule as domain grouping in _build_route_info).
            route_tags = tuple(t for t in (r.tags or ()) if isinstance(t, str))
            if self._exclude_tags and _tags_match(route_tags, self._exclude_tags):
                continue
            if self._include_tags is not None and not _tags_match(
                route_tags, self._include_tags
            ):
                continue

            # Deprecated filtering, same config-level silent drop. Routes that
            # stay carry a GraphQL-native deprecation mark in the schema.
            if self._exclude_deprecated and getattr(r, "deprecated", False):
                continue

            if method in _MUTATION_VERBS and not self._allow_mutation:
                skips.append(
                    SkipRecord(r.path, method, "mutation endpoints are disabled "
                              "(pass allow_mutation=True to expose them)",
                              tags=route_tags)
                )
                continue


            if not r.include_in_schema and not self._include_hidden:
                skips.append(
                    SkipRecord(
                r.path, method,
                "hidden route (include_in_schema=False; pass include_hidden=True to expose it)",
                tags=route_tags,
            )
                )
                continue

            route_info = self._build_route_info(r, method, types, skips)
            if route_info is not None:
                routes.append(route_info)

        if skips:
            rendered = "; ".join(f"{s.method} {s.path}: {s.reason}" for s in skips)
            logger.warning("fastapi-mcp-neo skipped %d route(s): %s", len(skips), rendered)

        # Degraded-but-present routes deserve a startup notice too: the field
        # is callable but has NO field selection. Naming the cause (and the
        # way out) turns a silent downgrade into an actionable one.
        degraded = [
            (r, reason) for r in routes if (reason := _degraded_reason(r)) is not None
        ]
        if degraded:
            rendered = "; ".join(f"{r.method} {r.path}: {reason}" for r, reason in degraded)
            logger.warning(
                "fastapi-mcp-neo bridged %d route(s) as raw JSON (no field "
                "selection): %s",
                len(degraded),
                rendered,
            )
        if types.degraded_fields:
            rendered = "; ".join(
                f"{path} ({reason})" for path, reason in types.degraded_fields
            )
            logger.warning(
                "fastapi-mcp-neo bridged %d model field(s) as raw JSON — "
                "restructure the union away or give the field a JSON-"
                "compatible annotation to regain field selection: %s",
                len(types.degraded_fields),
                rendered,
            )
        return routes, skips

    def readiness(self) -> ReadinessReport:
        """Scan and classify without building anything else — the same audit
        the startup notices come from, runnable on its own (CI gate, pre-
        deploy checklist). Pass the same filters your deployment uses so
        the report reflects what IT would expose."""
        types = TypeBuilder()
        routes, skips = self.scan(types)
        return build_readiness_report(routes, skips, types)

    # ----------------------------------------------------------------- helpers

    def _build_route_info(
        self,
        route: Any,
        method: str,
        types: TypeBuilder,
        skips: list[SkipRecord],
    ) -> RouteInfo | None:
        str_tags = tuple(t for t in route.tags or () if isinstance(t, str))

        def skip(reason: str) -> None:
            skips.append(SkipRecord(route.path, method, reason, tags=str_tags))

        flat = _flatten_params(route.dependant)
        response_annotation = _response_annotation(route)
        for reason in (
            _required_header_cookie_reason(flat),
            _path_model_reason(flat.path),
            _mixed_query_model_reason(flat.query),
            _body_shape_reason(flat.body),
            _unmappable_response_reason(response_annotation),
        ):
            if reason is not None:
                skip(reason)
                return None

        query_params, path_params, body_params = _route_params(flat)
        response_filter = response_filter_kwarg(route)
        try:
            response_annotation, typevar_response, param_reasons = _trial_route_types(
                types,
                route.path,
                response_annotation,
                skip_response=(
                    response_filter is not None
                    or response_annotation is type(None)
                ),
                params_lists=(path_params, query_params, body_params),
            )
        except UnsupportedFieldTypeError as exc:
            skip(f"unsupported type: {exc}")
            return None

        description = route.summary or route.description or None
        # OpenAPI's `deprecated: true` becomes GraphQL-native deprecation.
        deprecated = bool(getattr(route, "deprecated", False))
        return RouteInfo(
            route=route,
            method=method,
            path=route.path,
            field_name=field_name_for(route),
            path_params=tuple(path_params),
            query_params=tuple(query_params),
            body_params=tuple(body_params),
            response_annotation=response_annotation,
            tags=str_tags,
            description=description,
            deprecated=deprecated,
            response_filter=response_filter,
            domains=domains_for(str_tags, route.path),
            output=_classify_output(
                response_annotation, response_filter, typevar_response, param_reasons
            ),
        )

