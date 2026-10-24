"""Route tools: one MCP tool per GraphQL field — the from_openapi shape
with a typed soul.

Every tool is a projection of the SAME GraphQL contract the constant
catalog serves: the input schema derives from the field's GraphQL
argument types (InputObjectType fields included — the GraphQL schema is
the typing source, the codegen'd Python signature is only the execution
vessel), the output schema derives from the field's GraphQL type,
annotations derive from the HTTP method (GET -> readOnlyHint, writes ->
destructiveHint), and a ``fields`` argument gives per-tool projection
(``{ id title }`` instead of whole payloads).

Why this exists when the constant catalog already won the context war:
MCP's permission model is per-TOOL. Clients gate, approve and display
tools individually — a single ``graphql_query`` tool cannot be allowed
per route at the client layer. Curated subsets (include/exclude) keep
the catalog small while restoring per-route client-side least privilege.
"""

from __future__ import annotations

import keyword
import logging
import textwrap
from collections.abc import Callable
from typing import Any

from graphql import (
    GraphQLEnumType,
    GraphQLInputObjectType,
    GraphQLObjectType,
    GraphQLScalarType,
    GraphQLSchema,
    GraphQLType,
    Undefined,
    is_list_type,
    is_non_null_type,
)

from fastapi_mcp_neo.handler import RouterGraphQLHandler
from fastapi_mcp_neo.mcp.tools import DESTRUCTIVE, READ_ONLY, per_call_passthrough_headers

logger = logging.getLogger(__name__)

# Selection/JSON-Schema walk caps: past this object depth and past one
# repetition of a recursive type, nested object fields are omitted (a
# default-selection convenience, not a contract — the agent can select
# deeper explicitly via ``fields``).
_MAX_WALK_DEPTH = 3

_PY_SCALARS = {"Int": "int", "Float": "float", "String": "str", "Boolean": "bool", "ID": "str"}
_JSON_SCALARS = {
    "Int": "integer",
    "Float": "number",
    "Boolean": "boolean",
    "String": "string",
    "ID": "string",
}


def _unwrap(t: GraphQLType) -> GraphQLType:
    while is_non_null_type(t) or is_list_type(t):
        t = t.of_type
    return t


def _py_annotation(t: GraphQLType, nullable: bool) -> str:
    """GraphQL type -> Python annotation string, for signature codegen."""
    if is_non_null_type(t):
        return _py_annotation(t.of_type, nullable=False)
    if is_list_type(t):
        ann = f"list[{_py_annotation(t.of_type, nullable=True)}]"
    else:
        base = _unwrap(t)
        if isinstance(base, GraphQLInputObjectType):
            ann = "dict[str, Any]"
        else:
            # custom scalars (Date/UUID/Decimal/JSON) and enums bridge as str
            ann = _PY_SCALARS.get(getattr(base, "name", ""), "str")
    return ann if not nullable else f"{ann} | None"


def _full_selection(t: GraphQLType, depth: int = 0, seen: frozenset[str] = frozenset()) -> str:
    """Default selection: every field, down to the walk caps."""
    base = _unwrap(t)
    if not isinstance(base, GraphQLObjectType):
        return ""
    if depth >= _MAX_WALK_DEPTH or base.name in seen:
        return ""  # empty selection => caller omits the object field
    parts = []
    for fname, f in base.fields.items():
        sel = _full_selection(f.type, depth + 1, seen | {base.name})
        if sel:
            parts.append(f"{fname} {sel}")
        elif not isinstance(_unwrap(f.type), GraphQLObjectType):
            parts.append(fname)  # leaf: scalar/list/enum, no selection needed
        # else: object field at the depth cap (or recursive repeat) — a
        # bare object field would be an invalid document; omit it (the
        # agent can select it explicitly via `fields`)
    #
    # Empty parts => "" too: an all-capped level has no valid selection,
    # so the parent omits this object like any other capped one (a
    # truthy "{  }" here used to embed an invalid empty selection set).
    return "{ " + " ".join(parts) + " }" if parts else ""


def _null_union(schema: dict[str, Any]) -> dict[str, Any]:
    """Nullable in GraphQL = value may be JSON null — the schema must say so."""
    return {"anyOf": [schema, {"type": "null"}]}


def _json_schema(
    t: GraphQLType, depth: int = 0, seen: frozenset[str] = frozenset()
) -> dict[str, Any]:
    """GraphQL output type -> JSON Schema (the tool's output_schema)."""
    if is_list_type(t):
        items = _json_schema(t.of_type, depth, seen)
        if not is_non_null_type(t.of_type):
            items = _null_union(items)
        return {"type": "array", "items": items}
    if is_non_null_type(t):
        return _json_schema(t.of_type, depth, seen)
    if isinstance(t, GraphQLEnumType):
        return {"type": "string", "enum": list(t.values.keys())}
    if isinstance(t, GraphQLScalarType):
        if t.name == "JSON":
            return {}  # any JSON
        return {"type": _JSON_SCALARS.get(t.name, "string")}
    if isinstance(t, GraphQLObjectType):
        if depth >= _MAX_WALK_DEPTH or t.name in seen:
            return {"type": "object"}
        props: dict[str, Any] = {}
        for fname, f in t.fields.items():
            schema = _json_schema(f.type, depth + 1, seen | {t.name})
            if not is_non_null_type(f.type):
                schema = _null_union(schema)
            props[fname] = schema
        # No "required" at any level: the fields projection returns subsets
        # of these shapes, and output_schema is static per tool — properties
        # describe what MAY come back, matching GraphQL selection semantics.
        return {"type": "object", "properties": props}
    return {}  # pragma: no cover - unions degrade to JSON before this point


def _input_json_schema(
    t: GraphQLType, depth: int = 0, seen: frozenset[str] = frozenset()
) -> dict[str, Any]:
    """GraphQL argument type -> JSON Schema for the tool's input.

    The mirror of _json_schema on the input side: GraphQLInputObjectType
    fields carry properties and requiredness, so body parameters stay
    typed (the signature's dict is only the execution vessel)."""
    if is_list_type(t):
        items = _input_json_schema(t.of_type, depth, seen)
        if not is_non_null_type(t.of_type):
            items = _null_union(items)
        return {"type": "array", "items": items}
    if is_non_null_type(t):
        return _input_json_schema(t.of_type, depth, seen)
    if isinstance(t, GraphQLEnumType):
        return {"type": "string", "enum": list(t.values.keys())}
    if isinstance(t, GraphQLScalarType):
        if t.name == "JSON":
            return {}
        return {"type": _JSON_SCALARS.get(t.name, "string")}
    if isinstance(t, GraphQLInputObjectType):
        if depth >= _MAX_WALK_DEPTH or t.name in seen:
            return {"type": "object"}
        props: dict[str, Any] = {}
        required: list[str] = []
        for fname, f in t.fields.items():
            schema = _input_json_schema(f.type, depth + 1, seen | {t.name})
            has_default = f.default_value is not Undefined
            if is_non_null_type(f.type) and not has_default:
                required.append(fname)
            else:
                schema = _null_union(schema)
                if has_default:
                    schema["default"] = f.default_value
            props[fname] = schema
        out: dict[str, Any] = {"type": "object", "properties": props}
        if required:
            out["required"] = required
        return out
    return {}  # pragma: no cover - unions degrade to JSON before this point


def _register_field_tool(
    mcp: Any,
    handler: RouterGraphQLHandler,
    *,
    domain_path: tuple[str, ...],
    field: Any,
    field_name: str,
    is_mutation: bool,
) -> None:
    from fastmcp.tools import FunctionTool

    arg_names = list(field.args)

    wrap = "".join(f"{seg} {{" for seg in domain_path)
    close = "}" * len(domain_path)
    # Immutable-schema-derived: compute once, close over it (review #9 —
    # this used to re-walk the type graph on every tool call).
    default_selection = _full_selection(field.type)
    needs_selection = isinstance(_unwrap(field.type), GraphQLObjectType)
    # An object type whose every field chain hits the depth cap has no
    # valid default selection — such tools REQUIRE a `fields` argument
    # (marked required in the input schema) instead of failing at call
    # time with an opaque syntax error.
    requires_fields = needs_selection and not default_selection

    async def run(payload: dict[str, Any], *, fields: str | None = None) -> dict[str, Any]:
        from fastmcp.exceptions import ToolError

        variables = {k: v for k, v in payload.items() if v is not None}
        missing = [
            a
            for a in arg_names
            if a not in variables
            and is_non_null_type(field.args[a].type)
            and field.args[a].default_value is Undefined
        ]
        if missing:
            raise ToolError(
                f"missing required argument(s): {', '.join(missing)}"
            )
        decls = ", ".join(f"${a}: {_sdl_type(field.args[a].type)}" for a in variables)
        args = ", ".join(f"{a}: ${a}" for a in variables)
        selection = (fields or "").strip()
        if not selection and requires_fields:
            raise ToolError(
                "this route's type has no fields within the selection "
                f"depth cap — pass `{proj_name}`, e.g. "
                "`{ <nested fields> }`"
            )
        if not selection:
            selection = default_selection
        op = "mutation" if is_mutation else "query"
        header = f"{op}({decls})" if decls else op
        field_call = f"{field_name}({args})" if args else field_name
        document = f"{header}{{{wrap}{field_call} {selection}{close}}}"
        # Per-caller credentials, same as graphql mode's executor: the
        # caller's whitelisted headers ride this exact invocation (review
        # #2 — tools mode used to drop them, 401-ing every protected route).
        per_call = per_call_passthrough_headers(handler.passthrough_headers)
        result = await handler.execute(
            document, variables=variables or None, headers=per_call
        )
        errors = result.get("errors")
        if errors:
            # Errors ride the MCP isError channel (raising), not the
            # structured payload — output_schema describes success only.
            raise ToolError("; ".join(str(e.get("message", e)) for e in errors))
        data: Any = result.get("data") or {}
        for seg in domain_path:
            data = (data or {}).get(seg, {})
        data = (data or {}).get(field_name)
        return {"data": data}

    # A real route param named `fields` (sparse-fieldsets convention)
    # would collide with the projection parameter — ours moves aside.
    # (fastmcp binds arguments by signature, so the signature must carry
    # the REAL names; non-identifier names are skipped at registration.)
    proj_name = "select" if "fields" in arg_names else "fields"

    lines = [f"async def {field_name}("]
    for arg_name in arg_names:
        ann = _py_annotation(field.args[arg_name].type, nullable=True)
        lines.append(f"    {arg_name}: {ann} = None,")
    lines.append(f"    {proj_name}: str | None = None,")
    lines.append(") -> dict:")
    body = "{" + ", ".join(f'"{a}": {a}' for a in arg_names) + "}"
    lines.append(f"    return await run({body}, fields={proj_name})")
    src = textwrap.dedent("\n".join(lines))
    ns: dict[str, Any] = {"Any": Any, "run": run}
    exec(compile(src, f"<route-tools:{field_name}>", "exec"), ns)  # noqa: S102 - generated from the schema
    fn: Callable[..., Any] = ns[field_name]

    # Each tool presents itself as a standalone GraphQL entry: typed args
    # ARE the field's arguments, ``fields`` IS the selection set. The
    # one-line signature suffices — a full operation document was tried
    # and dropped: it repeated what output_schema and the typed args
    # already say (~27% of the catalog, zero information gain).
    sdl_args = ", ".join(f"{a}: {_sdl_type(field.args[a].type)}" for a in arg_names)
    signature = f"{field_name}({sdl_args}): {_sdl_type(field.type)}"
    description = (field.description or "").strip()
    parts = [description] if description else []
    parts.append(f"GraphQL: `{signature}`")
    parts.append(
        f"`{proj_name}` (optional): a GraphQL selection set to project "
        "the response, e.g. `{ id title }` — omit for the full type."
    )
    description = "\n\n".join(parts)

    # MCP spec: output schemas must be object types — wrap the field's type
    # in the {"data": ...} envelope every route tool returns on success.
    data_schema = _json_schema(field.type)
    if not is_non_null_type(field.type):
        data_schema = _null_union(data_schema)
    output_schema = {
        "type": "object",
        "properties": {"data": data_schema},
        "required": ["data"],
    }
    tool = FunctionTool.from_function(
        fn,
        name=field_name,
        description=description,
        annotations=DESTRUCTIVE if is_mutation else READ_ONLY,
        output_schema=output_schema,
    )
    # Override the signature-derived schema with one generated from the
    # GraphQL argument types: InputObjectType args keep their field-level
    # properties and requiredness; the `fields` projection arg rides along.
    properties: dict[str, Any] = {
        proj_name: {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "default": None,
            "description": "GraphQL selection set to project the response, "
            "e.g. `{ id title }` — omit for the full type.",
        }
    }
    required_names: list[str] = []
    for arg_name in arg_names:
        arg = field.args[arg_name]
        schema = _input_json_schema(arg.type)
        has_default = arg.default_value is not Undefined
        if is_non_null_type(arg.type) and not has_default:
            required_names.append(arg_name)
        else:
            schema = _null_union(schema)
            if has_default:
                schema["default"] = arg.default_value
        properties[arg_name] = schema
    input_schema: dict[str, Any] = {"type": "object", "properties": properties}
    if requires_fields:
        required_names.append(proj_name)
    if required_names:
        input_schema["required"] = required_names
    # Override the signature-derived schema with the GraphQL-derived one.
    # (fastmcp's to_mcp_tool ALWAYS fills title via _default_title(name)
    # and meta via get_meta() on the wire — no None path; that ~9 tok/tool
    # is framework tax, not removable without fighting the converter.)
    tool = tool.model_copy(update={"parameters": input_schema})
    mcp.add_tool(tool)


def _sdl_type(t: GraphQLType) -> str:
    if is_non_null_type(t):
        return f"{_sdl_type(t.of_type)}!"
    if is_list_type(t):
        return f"[{_sdl_type(t.of_type)}]"
    return getattr(t, "name", "JSON")


def register_route_tools(
    mcp: Any,
    handler: RouterGraphQLHandler,
    *,
    allow_mutation: bool = False,
) -> int:
    """Register one tool per route — the whole toolset of tools mode.

    Domain structure is INERT here (the agent never writes documents),
    so registration iterates the ROUTE list and follows each route's
    domain path — already split on multi-segment tags at scan time
    ("iam:users" -> ("iam", "users")) — only to locate the field in the
    schema and wrap the internal document. The group tree's depth is
    irrelevant by construction. Returns the number of tools registered."""
    schema: GraphQLSchema = handler.schema
    count = 0
    for route in handler.routes:
        root = schema.mutation_type if route.is_mutation else schema.query_type
        if root is None:  # pragma: no cover - the scanner gates mutations already
            continue
        # A multi-tag route lives under several domain paths in the schema;
        # tools mode is one-tool-per-route, so pick one (deterministic:
        # sorted first) purely to wrap the internal document.
        domain_path = sorted(route.domains)[0] if route.domains else ()
        node: Any = root
        for seg in domain_path:
            node = _unwrap(node.fields[seg].type)
        field = node.fields.get(route.field_name)
        if field is None:  # pragma: no cover - scanner and schema agree
            continue
        # fastmcp BINDS tool arguments by the signature (the overridden
        # parameters schema is advertisement only), so argument names must
        # be legal Python identifiers. Aliases only guarantee GraphQL-legal
        # names — Query(alias="from") is outside fastmcp's tool ABI. Skip
        # with a warning (degrade-and-notify); graphql mode still serves it.
        bad = [
            a
            for a in field.args
            if not a.isidentifier() or keyword.iskeyword(a)
        ]
        if bad:
            logger.warning(
                "tools mode skipped %s %s: argument name(s) %s are not "
                "valid Python identifiers (fastmcp binds arguments by "
                "signature); the route stays available in graphql/"
                "progressive mode",
                route.method,
                route.path,
                ", ".join(repr(a) for a in bad),
            )
            continue
        _register_field_tool(
            mcp,
            handler,
            domain_path=domain_path,
            field=field,
            field_name=route.field_name,
            is_mutation=route.is_mutation,
        )
        count += 1
    return count
