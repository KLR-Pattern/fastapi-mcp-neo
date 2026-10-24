"""Route tools: one MCP tool per GraphQL field — the from_openapi shape
with a typed soul.

Every tool is a projection of the SAME GraphQL contract the constant
catalog serves: arguments carry the field's typed signature (codegen'd so
fastmcp derives a real JSON Schema), the output schema derives from the
field's GraphQL type, annotations derive from the HTTP method (GET ->
readOnlyHint, writes -> destructiveHint), and a ``fields`` argument gives
per-tool projection (``{ id title }`` instead of whole payloads).

Why this exists when the constant catalog already won the context war:
MCP's permission model is per-TOOL. Clients gate, approve and display
tools individually — a single ``graphql_query`` tool cannot be allowed
per route at the client layer. Curated subsets (include/exclude) keep
the catalog small while restoring per-route client-side least privilege.
"""

from __future__ import annotations

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
from fastapi_mcp_neo.mcp.tools import DESTRUCTIVE, READ_ONLY

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
        parts.append(f"{fname} {sel}".strip())
    return "{ " + " ".join(parts) + " }"


def _json_schema(
    t: GraphQLType, depth: int = 0, seen: frozenset[str] = frozenset()
) -> dict[str, Any]:
    """GraphQL output type -> JSON Schema (the tool's output_schema)."""
    if is_non_null_type(t) or is_list_type(t):
        if is_list_type(t):
            return {"type": "array", "items": _json_schema(t.of_type, depth, seen)}
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
            props[fname] = _json_schema(f.type, depth + 1, seen | {t.name})
        # No "required" at any level: the fields projection returns subsets
        # of these shapes, and output_schema is static per tool — properties
        # describe what MAY come back, matching GraphQL selection semantics.
        return {"type": "object", "properties": props}
    return {}


def _register_field_tool(
    mcp: Any,
    handler: RouterGraphQLHandler,
    *,
    domain: str,
    field: Any,
    field_name: str,
    is_mutation: bool,
) -> None:
    from fastmcp.tools import FunctionTool

    arg_names = list(field.args)

    async def run(payload: dict[str, Any], *, fields: str | None = None) -> dict[str, Any]:
        from fastmcp.exceptions import ToolError

        variables = {k: v for k, v in payload.items() if v is not None}
        decls = ", ".join(f"${a}: {_sdl_type(field.args[a].type)}" for a in variables)
        args = ", ".join(f"{a}: ${a}" for a in variables)
        selection = (fields or "").strip() or _full_selection(field.type)
        op = "mutation" if is_mutation else "query"
        document = f"{op}({decls}){{{domain}{{{field_name}({args}) {selection}}}}}"
        result = await handler.execute(document, variables=variables or None)
        errors = result.get("errors")
        if errors:
            # Errors ride the MCP isError channel (raising), not the
            # structured payload — output_schema describes success only.
            raise ToolError("; ".join(str(e.get("message", e)) for e in errors))
        data = (result.get("data") or {}).get(domain, {}).get(field_name)
        return {"data": data}

    # Signature codegen: typed args -> fastmcp derives a typed JSON Schema.
    # NonNull args without a GraphQL default become required parameters;
    # everything else optional (= None, filtered before execution).
    lines = [f"async def {field_name}("]
    for arg_name in arg_names:
        arg = field.args[arg_name]
        # no-default args carry Undefined, not None
        has_default = arg.default_value is not None and arg.default_value is not Undefined
        required = is_non_null_type(arg.type) and not has_default
        ann = _py_annotation(arg.type, nullable=not required)
        lines.append(f"    {arg_name}: {ann}," if required else f"    {arg_name}: {ann} = None,")
    lines.append("    fields: str | None = None,")
    lines.append(") -> dict:")
    body = "{" + ", ".join(f'"{a}": {a}' for a in arg_names) + "}"
    lines.append(f"    return await run({body}, fields=fields)")
    src = textwrap.dedent("\n".join(lines))
    ns: dict[str, Any] = {"Any": Any, "run": run}
    exec(compile(src, f"<route-tools:{field_name}>", "exec"), ns)  # noqa: S102 - generated from the schema
    fn: Callable[..., Any] = ns[field_name]

    # Each tool presents itself as a standalone GraphQL entry: typed args
    # ARE the operation's variables, ``fields`` IS the selection set, and
    # the description carries the exact operation this tool executes.
    op = "mutation" if is_mutation else "query"
    all_decls = ", ".join(f"${a}: {_sdl_type(field.args[a].type)}" for a in arg_names)
    all_args = ", ".join(f"{a}: ${a}" for a in arg_names)
    example_selection = _full_selection(field.type) or "{ ... }"
    operation = f"{op}({all_decls}) {{{domain} {{{field_name}({all_args}) {example_selection}}}}}"
    description = (field.description or "").strip()
    parts = [description] if description else []
    parts.append(
        "This tool is a standalone GraphQL entry — it executes exactly this "
        f"operation:\n`{operation}`"
    )
    parts.append(
        "Arguments map 1:1 to the operation's variables. `fields` (optional) "
        "replaces the selection set to project the response, e.g. `{ id title }` "
        "— omit it for the full selection above."
    )
    description = "\n\n".join(parts)

    # MCP spec: output schemas must be object types — wrap the field's type
    # in the {"data": ...} envelope every route tool returns on success.
    output_schema = {
        "type": "object",
        "properties": {"data": _json_schema(field.type)},
        "required": ["data"],
    }
    tool = FunctionTool.from_function(
        fn,
        name=field_name,
        description=description,
        annotations=DESTRUCTIVE if is_mutation else READ_ONLY,
        output_schema=output_schema,
    )
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
    escape_hatch: bool = False,
    allow_mutation: bool = False,
) -> int:
    """Register one tool per GraphQL field; optionally keep the executor
    tools (``graphql_query`` / ``graphql_mutation``) as the composition
    escape hatch. Returns the number of field tools registered."""
    schema: GraphQLSchema = handler.schema

    def register_group(group_type: Any, is_mutation: bool) -> int:
        count = 0
        for domain, domain_field in group_type.fields.items():
            inner = _unwrap(domain_field.type)
            if not isinstance(inner, GraphQLObjectType):
                continue
            for field_name, field in inner.fields.items():
                _register_field_tool(
                    mcp, handler, domain=domain, field=field, field_name=field_name,
                    is_mutation=is_mutation,
                )
                count += 1
        return count

    total = register_group(schema.query_type, is_mutation=False)
    if allow_mutation and schema.mutation_type:
        total += register_group(schema.mutation_type, is_mutation=True)

    if escape_hatch:
        from fastapi_mcp_neo.mcp.tools import register_executor_tools

        register_executor_tools(mcp, handler, allow_mutation=allow_mutation)
    return total
