"""Build graphql-core types from Pydantic models.

Maps FastAPI's Pydantic annotations onto a programmatic ``GraphQLSchema``:

- ``BaseModel`` → ``GraphQLObjectType`` (output) / ``GraphQLInputObjectType`` (input)
- ``TypedDict`` → the same object/input mapping, nullability from its
  required/optional keys (``total=False`` keys are nullable — they may be
  absent from the JSON)
- ``@dataclass`` → the same object/input mapping; output nullability from
  the annotation (defaults always materialize), input requiredness and
  literal defaults from the field definitions
- ``Enum`` → ``GraphQLEnumType``; ``Literal`` → its underlying scalar
  (enum members normalize to their values: ``Literal[M.A]`` ≡ ``Literal["a"]``)
- ``Optional``/``X | None`` → nullable; plain annotations → non-null
- Output field names follow the JSON keys FastAPI emits (alias first);
  input field names follow what FastAPI validates (validation_alias first).

Type-name collisions between same-named classes from different modules are
resolved by qualifying with the module tail (with a warning); everything else
fails fast with ``UnsupportedFieldTypeError`` so the scanner can skip the route.
Registration is transactional: a failed field build rolls the type (and its
name) back out of the shared caches, so one bad model never leaks into
another route's schema. Unmappable fields INSIDE a model or TypedDict never
fail the route: the field bridges as the JSON scalar and lands in the
readiness report's ``degraded_fields`` — only top-level annotations with no
mapping at all skip the route.
"""

from __future__ import annotations

import inspect
import logging
import re
import sys
import types
from collections.abc import Callable, Iterator
from dataclasses import MISSING, dataclass, is_dataclass
from dataclasses import fields as dataclass_fields
from enum import Enum
from typing import (
    Annotated,
    Any,
    ForwardRef,
    Literal,
    TypeVar,
    Union,
    cast,
    get_args,
    get_origin,
    get_type_hints,
)

from graphql import (
    GraphQLBoolean,
    GraphQLEnumType,
    GraphQLField,
    GraphQLInputField,
    GraphQLInputObjectType,
    GraphQLInputType,
    GraphQLInt,
    GraphQLList,
    GraphQLNonNull,
    GraphQLObjectType,
    GraphQLOutputType,
    GraphQLScalarType,
    GraphQLString,
    Undefined,
)
from pydantic import BaseModel
from pydantic.fields import FieldInfo
from typing_extensions import is_typeddict

from fastapi_mcp_neo.recursive_expand import is_direct_self_reference
from fastapi_mcp_neo.scalars import SCALAR_MAP, GraphQLJSON, json_passthrough

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[_a-zA-Z][_a-zA-Z0-9]*$")

# Literal member types that map onto a built-in GraphQL scalar.
_LITERAL_SCALARS: dict[type, GraphQLScalarType] = {
    str: GraphQLString,
    int: GraphQLInt,
    bool: GraphQLBoolean,
}

# What ``input_type`` actually returns before any NonNull wrapping — narrow
# enough for GraphQLNonNull's argument (``GraphQLInputType`` includes NonNull,
# which mypy rightly refuses to re-wrap).
_BareInput = (
    GraphQLScalarType | GraphQLEnumType | GraphQLInputObjectType | GraphQLList[Any]
)


class UnsupportedFieldTypeError(TypeError):
    """Raised when an annotation cannot be represented in GraphQL."""

    def __init__(
        self, annotation: Any, context: str, *, field_reason: str | None = None
    ) -> None:
        self.annotation = annotation
        self.context = context
        # Ready-made degraded-field reason, set when the context is itself
        # the guidance (unbound TypeVar, empty model) — consumers read the
        # attribute instead of sniffing the message text.
        self.field_reason = field_reason
        super().__init__(
            f"Cannot map {annotation!r} to a GraphQL type ({context}). "
            f"Supported: scalars (int/str/bool/float/datetime/date/time/UUID/Decimal), "
            f"Enum, Literal, Optional/list/set combinations, nested BaseModel, and "
            f"dict/Any (pass-through as the JSON scalar)."
        )


def _unsupported(annotation: Any, context: str) -> UnsupportedFieldTypeError:
    """The terminal error, with a targeted message for unbound TypeVars:
    ``Cannot map ~T`` alone does not say WHICH generic needs parameterizing
    (the context carries the model.field) or what to do about it."""
    if isinstance(annotation, TypeVar):
        guidance = (
            f"{context}: unbound TypeVar {annotation!r} — parameterize the "
            f"generic so the field has a concrete type"
        )
        return UnsupportedFieldTypeError(
            annotation, guidance, field_reason=guidance
        )
    return UnsupportedFieldTypeError(annotation, context)


def _short_name(annotation: Any) -> str:
    return getattr(annotation, "__name__", str(annotation))


def _field_unmappable_reason(exc: UnsupportedFieldTypeError) -> str:
    """Reason recorded for a degraded field: the raiser's ready-made
    guidance when it left one (unbound TypeVar, empty model), else the
    annotation's name — the fix advice lives in the field note and the
    startup warning."""
    if exc.field_reason is not None:
        return exc.field_reason
    return f"{_short_name(exc.annotation)} has no GraphQL mapping"


def _degraded_field_note(reason: str) -> str:
    return (
        f"Raw JSON: {reason} — select bare; give the field a "
        "JSON-compatible annotation to regain field selection."
    )


def _union_json_bridge(annotation: Any) -> tuple[str, str] | None:
    """(reason, note) when ``annotation`` is a non-Optional union: GraphQL
    has no union inputs and cannot promise one output member, so the field
    bridges as raw JSON. Centralized so every field builder (model
    output/input, TypedDict output/input) records the degradation
    identically — the readiness report must never depend on which side a
    union field happens to sit on."""
    normalized = _normalize_typevar(strip_annotated(annotation))
    if union_members(normalized) is None:
        return None
    names = ", ".join(union_member_names(normalized))
    note = (
        f"Raw JSON whose shape is one of: {names} (union field — "
        "select bare; GraphQL cannot promise one member)."
    )
    return names, note


def append_note(description: str | None, note: str) -> str:
    return f"{description}\n\n{note}" if description else note


def _wire_key_resolver(wire_key: str) -> Callable[..., Any]:
    """Reads a field's value under its WIRE key. GraphQL names are
    legal-identifier sanitized while FastAPI serializes by the original
    alias — the resolver closes the gap the default dict lookup cannot."""

    def resolve(source: Any, _info: Any) -> Any:
        return source.get(wire_key) if isinstance(source, dict) else None

    return resolve


def _wire_key_out_type(wire: dict[str, str]) -> Callable[[Any], Any]:
    """Input twin of ``_wire_key_resolver``: rewrites an input object's
    coerced dict from GraphQL field names to wire keys before the value
    reaches FastAPI."""

    def out(value: Any) -> Any:
        if isinstance(value, dict):
            return {wire.get(k, k): v for k, v in value.items()}
        return value

    return out


def _normalize_typevar(annotation: Any) -> Any:
    """A constrained TypeVar means "one of these"; a bound TypeVar means
    "this or narrower" — pydantic validates exactly that way, so the
    GraphQL mapping normalizes to the constraint union / the bound before
    dispatching. An unconstrained TypeVar passes through to the
    unbound-TypeVar guidance (field level) or the route-level JSON
    fallback."""
    if not isinstance(annotation, TypeVar):
        return annotation
    if annotation.__constraints__:
        return Union[annotation.__constraints__]  # noqa: UP007 — dynamic tuple
    if annotation.__bound__ is not None:
        return annotation.__bound__
    return annotation


def first_wire_name(raw: Any, fallback: str) -> str:
    """A non-str alias (pydantic ``AliasChoices``) still names real wire
    keys; FastAPI accepts any of them, so one canonical name — the first
    string choice — is enough."""
    for choice in getattr(raw, "choices", ()) or ():
        if isinstance(choice, str) and choice:
            return choice
    return fallback


def request_wire_name(info: FieldInfo, field_name: str) -> str:
    """The wire key FastAPI VALIDATES a body/query-model field by:
    validation_alias, else alias, else the field name. A non-str alias
    (pydantic ``AliasChoices``) still names real wire keys — one canonical
    name, the first string choice, is enough."""
    raw = info.validation_alias or info.alias or field_name
    return raw if isinstance(raw, str) else first_wire_name(raw, field_name)


def output_wire_name(info: FieldInfo, field_name: str) -> str:
    """The wire key FastAPI SERIALIZES a response field by: serialization_
    alias, else alias, else the field name. AliasChoices has no single
    serialization key, so it falls back to the field name (FastAPI
    serializes unaliased output by field name too)."""
    raw = info.serialization_alias or info.alias or field_name
    return raw if isinstance(raw, str) else field_name


def graphql_default(default: Any) -> Any:
    """A GraphQL argument default must be a JSON literal; anything else
    (enums, datetimes, factories) stays Undefined and FastAPI materializes
    it when the argument is absent."""
    if default is None or isinstance(default, str | int | float | bool):
        return default
    return Undefined


@dataclass(frozen=True)
class FieldSpec:
    """One field of a composite (model / TypedDict / dataclass), family-normalized.

    The six per-family field builders differed only in how they produce
    these values; ``field_name`` and ``wire`` are distinct because the
    degradation audit keys by the PYTHON field name while GraphQL names and
    wire-key translation key by the alias.
    """

    field_name: str  # Python-side name: audit key ("Model.field"), error context
    wire: str  # wire key FastAPI validates/serializes this field by
    annotation: Any  # already resolved (resolve_annotation / _typeddict_hints)
    description: str | None = None  # family description BEFORE degraded/union notes
    required: bool = False  # input: NonNull argument iff True
    default: Any = Undefined  # input: literal default or graphql Undefined
    bare: bool = False  # output: nullable outer level (key may be absent)


def _model_specs(model: type[BaseModel], *, input_side: bool) -> Iterator[FieldSpec]:
    """BaseModel fields. Field(exclude=True) never serializes — building it
    into the schema would promise a key the JSON never carries (selecting it
    nulls the whole object); excluded fields stay valid INPUT fields, since
    exclude is serialization-only and validation still reads them."""
    ns = _model_namespace(model)
    for field_name, info in model.model_fields.items():
        if not input_side and info.exclude is True:
            continue
        annotation = resolve_annotation(info.annotation, ns)
        yield FieldSpec(
            field_name=field_name,
            wire=request_wire_name(info, field_name)
            if input_side
            else output_wire_name(info, field_name),
            annotation=annotation,
            description=_field_description(info, annotation),
            required=bool(info.is_required()),
            default=graphql_default(info.get_default(call_default_factory=False))
            if input_side
            else Undefined,
        )


def _typeddict_specs(td: Any, *, input_side: bool) -> Iterator[FieldSpec]:
    """TypedDict fields: annotations from get_type_hints (no FieldInfo to
    consult); nullability/requiredness from the required/optional key set —
    an optional key may be ABSENT from the JSON, so its output field is
    nullable even when its annotation is not Optional (no FastAPI
    ``required`` flag exists on the input side: the key set IS the
    requiredness)."""
    required = td.__required_keys__
    for field_name, annotation in _typeddict_hints(td).items():
        yield FieldSpec(
            field_name=field_name,
            wire=field_name,
            annotation=annotation,
            description=describe_literal_values(annotation),
            required=field_name in required,
            bare=field_name not in required,
        )


def _dataclass_specs(dc: Any, *, input_side: bool) -> Iterator[FieldSpec]:
    """Dataclass fields: annotations resolved like a TypedDict's
    (``_typeddict_hints`` is the same job for any hints-bearing class;
    ``dataclasses.fields()`` already excludes ClassVar and InitVar).
    Output nullability follows the ANNOTATION — every dataclass field is
    always present in a materialized instance (defaults fill in), so only
    Optional makes it nullable. Input requiredness: no default and no
    default_factory; a defaulted field carries its literal default
    (factory-only defaults carry none — calling the factory at schema-build
    time would be a side effect; FastAPI materializes it when the argument
    is absent, and a 422 surfaces as a field error)."""
    hints = _typeddict_hints(dc)
    for f in dataclass_fields(dc):
        required = f.default is MISSING and f.default_factory is MISSING
        annotation = hints.get(f.name, f.type)
        yield FieldSpec(
            field_name=f.name,
            wire=f.name,
            annotation=annotation,
            description=describe_literal_values(annotation),
            required=required,
            default=(
                graphql_default(f.default)
                if input_side and not required and f.default is not MISSING
                else Undefined
            ),
            bare=is_optional_annotation(annotation),
        )


def sanitize_graphql_name(name: str, *, what: str = "name") -> str:
    """Coerce an identifier into a legal GraphQL name (warns when changed)."""
    sanitized = re.sub(r"[^_a-zA-Z0-9]", "_", name)
    if not sanitized or sanitized[0].isdigit():
        sanitized = f"_{sanitized or 'unnamed'}"
    if sanitized != name:
        logger.warning("Sanitized GraphQL %s %r -> %r", what, name, sanitized)
    return sanitized


def is_annotated(annotation: Any) -> bool:
    return hasattr(annotation, "__metadata__")


def strip_annotated(annotation: Any) -> Any:
    while is_annotated(annotation):
        annotation = get_args(annotation)[0]
    return annotation


def is_optional_annotation(annotation: Any) -> bool:
    annotation = strip_annotated(annotation)
    origin = get_origin(annotation)
    if origin is Union or origin is types.UnionType:
        return type(None) in get_args(annotation)
    return False


def union_members(annotation: Any) -> list[Any] | None:
    """Members of a NON-Optional union (2+ after stripping None), else None.

    ``Item | Error`` and ``Item | Error | None`` are unions whose runtime
    member varies; ``X | None`` is plain Optional and not a union here.
    """
    annotation = strip_annotated(annotation)
    origin = get_origin(annotation)
    if origin is Union or origin is types.UnionType:
        members = [a for a in get_args(annotation) if a is not type(None)]
        if len(members) >= 2:
            return members
    return None


def union_member_names(annotation: Any) -> list[str]:
    """Renderable member names for union documentation notes."""
    members = union_members(annotation) or []
    return [getattr(m, "__name__", str(m)) for m in members]


def graphql_model_name(model: type[BaseModel]) -> str:
    """Base GraphQL type name for a model, generic-parameter aware.

    A parametrized generic's ``__name__`` is its source spelling —
    ``Page[Item].__name__ == "Page[Item]"`` — which the name sanitizer
    would mangle into ``Page_Item_`` with a warning. Rendering the origin
    plus argument names directly (``Page_Item``) is clean, unique per
    parameterization, and dedups through ``_register_name`` like any name.

    Pydantic v2 parametrized generics carry their origin/args in
    ``__pydantic_generic_metadata__`` (``typing.get_origin`` reads None
    on the dynamic subclass); plain ``Generic`` fallback uses get_origin.
    """
    meta = getattr(model, "__pydantic_generic_metadata__", None)
    if isinstance(meta, dict) and meta.get("origin") is not None:
        origin: Any = meta["origin"]
        args = "_".join(
            getattr(a, "__name__", str(a)) for a in meta.get("args") or ()
        )
        return f"{origin.__name__}_{args}" if args else origin.__name__
    origin = get_origin(model)
    if origin is not None and isinstance(origin, type):
        args = "_".join(getattr(a, "__name__", str(a)) for a in get_args(model))
        return f"{origin.__name__}_{args}" if args else origin.__name__
    return model.__name__


def unwrap_optional(annotation: Any) -> Any:
    annotation = strip_annotated(annotation)
    origin = get_origin(annotation)
    if origin is Union or origin is types.UnionType:
        remaining = [a for a in get_args(annotation) if a is not type(None)]
        if len(remaining) == 1:
            return remaining[0]
    return annotation


def is_collection_annotation(annotation: Any) -> bool:
    """Collections that serialize to a JSON array — pydantic emits sets as
    arrays, so set/frozenset map onto GraphQL lists exactly like list/tuple."""
    return get_origin(strip_annotated(annotation)) in (list, tuple, set, frozenset)


def is_heterogeneous_tuple(annotation: Any) -> bool:
    """Fixed-length tuples whose positions carry DIFFERENT types.

    JSON has no tuples — pydantic serializes them as arrays — so
    tuple[X, ...] (variadic) and all-same fixed tuples are wire-accurate
    as lists. Differing positions are not: a list would promise one
    element type while position 2 breaks it (tuple[Item, int] mapped as
    [Item!] silently dropped the int). Those degrade to the JSON scalar
    like unions do. Empty tuples (tuple[()]) also degrade."""
    stripped = strip_annotated(annotation)
    if get_origin(stripped) is not tuple:
        return False
    args = get_args(stripped)
    if not args:
        return True
    if args[-1] is Ellipsis:  # tuple[X, ...]
        return False
    return not all(a == args[0] for a in args)


def literal_values(annotation: Any) -> tuple[Any, ...] | None:
    """Return Literal values, or None if the annotation is not a Literal."""
    annotation = unwrap_optional(annotation)
    return get_args(annotation) if get_origin(annotation) is Literal else None


def literal_member_values(annotation: Any) -> tuple[Any, ...] | None:
    """Literal values with enum members normalized to their underlying values
    — ``Literal[Mode.A]`` maps exactly like ``Literal["a"]``: the member's
    runtime type is the enum class, but the value pydantic validates and
    FastAPI serializes is ``Mode.A.value``."""
    values = literal_values(annotation)
    if values is None:
        return None
    return tuple(v.value if isinstance(v, Enum) else v for v in values)


def literal_scalar(annotation: Any) -> GraphQLScalarType | None:
    """Map ``Literal["a", "b"]`` onto its underlying scalar, else None."""
    values = literal_member_values(annotation)
    if values is None:
        return None
    if not values:
        raise UnsupportedFieldTypeError(annotation, "empty Literal")
    member_types = {type(v) for v in values}
    if len(member_types) != 1:
        raise UnsupportedFieldTypeError(
            annotation, f"mixed Literal member types {sorted(t.__name__ for t in member_types)}"
        )
    scalar = _LITERAL_SCALARS.get(next(iter(member_types)))
    if scalar is None:
        raise UnsupportedFieldTypeError(
            annotation, f"Literal of {next(iter(member_types)).__name__} has no GraphQL scalar"
        )
    return scalar


def describe_literal_values(annotation: Any) -> str | None:
    """Human-readable allowed-values suffix for Literal fields (mirrors nexusx)."""
    values = literal_member_values(annotation)
    if values is None:
        return None
    rendered = ", ".join(repr(v) if isinstance(v, str) else str(v) for v in values)
    nullable = is_optional_annotation(annotation)
    suffix = " (or null)" if nullable else ""
    return f"Allowed values: {rendered}{suffix}"


def _own_doc(cls: type) -> str | None:
    """A class's own docstring only — inherited ones (e.g. BaseModel's) are noise."""
    doc = cls.__dict__.get("__doc__")
    return inspect.cleandoc(doc).strip() if doc else None


def _field_description(field: FieldInfo, annotation: Any) -> str | None:
    parts = [field.description] if field.description else []
    literal_note = describe_literal_values(annotation)
    if literal_note:
        parts.append(literal_note)
    return "\n\n".join(parts) if parts else None


# ------------------------------------------------------- forward-ref repair
#
# CPython < 3.11 leaves ``list["Node"]`` with the PLAIN STRING inside the
# PEP 585 generic (only typing.Union converts str args to ForwardRef), and
# every evaluator — typing's own as well as pydantic's — only evaluates
# ForwardRef instances, so pydantic's ``FieldInfo.annotation`` stays
# unresolved on 3.10 while 3.11+ resolves it. Rewrapping string args as
# ForwardRef and evaluating against the model's namespaces makes recursive
# models map identically on every supported Python.


def _materialize_str_args(annotation: Any) -> Any:
    """Rebuild an annotation tree with plain-string args as ForwardRef."""
    if isinstance(annotation, str):
        return ForwardRef(annotation)
    args = get_args(annotation)
    if not args:
        return annotation
    if get_origin(annotation) is Literal:
        return annotation  # Literal args are VALUES (Literal["a", "b"]), not types
    if is_annotated(annotation):
        # args[0] is the type; the rest is metadata that is NOT a type.
        inner = _materialize_str_args(args[0])
        if inner is args[0]:
            return annotation
        merged: tuple[Any, ...] = (inner, *args[1:])  # star-free subscript (3.10)
        return Annotated[merged]
    new_args = tuple(_materialize_str_args(a) for a in args)
    if new_args == args:
        return annotation
    origin = get_origin(annotation)
    if origin is None:  # pragma: no cover - get_args without origin
        return annotation
    try:
        return origin[new_args[0]] if len(new_args) == 1 else origin[new_args]
    except TypeError:  # pragma: no cover - exotic origins resist rebuilding
        return annotation


def _model_namespace(model: type[BaseModel]) -> dict[str, Any]:
    """The namespace a model's ForwardRefs evaluate against: the model's own
    name (self-references — the enclosing scope binds the class name only
    AFTER the class body runs, so neither module globals nor pydantic's
    parent-namespace snapshot can have it yet), its module's globals
    (module-level classes) overlaid with pydantic's captured parent frame
    locals (function-local classes)."""
    ns: dict[str, Any] = {model.__name__: model}
    module = sys.modules.get(model.__module__)
    if module is not None:
        ns.update(vars(module))
    parent = getattr(model, "__pydantic_parent_namespace__", None)
    if parent:
        ns.update(parent)
    ns[model.__name__] = model  # self-reference wins over any same-name import
    return ns


def resolve_annotation(annotation: Any, namespace: dict[str, Any]) -> Any:
    """Evaluate string/ForwardRef args; the original annotation on failure
    (unsupported types then fail through the normal UnsupportedFieldTypeError
    path with a precise message, instead of a resolver crash)."""
    candidate = _materialize_str_args(annotation)
    try:
        from pydantic._internal._typing_extra import try_eval_type
    except ImportError:  # pragma: no cover - pydantic internal moved/renamed
        return candidate
    try:
        resolved, _ok = try_eval_type(candidate, namespace, namespace)
    except Exception:
        return candidate
    return resolved


def _typeddict_hints(td: type) -> dict[str, Any]:
    """Resolve a TypedDict's annotations (string/ForwardRef keys included,
    ``Annotated`` metadata kept) against its module namespace — the job
    ``resolve_annotation`` does per model field. Falls back to the raw
    annotations; unresolved strings then fail through the normal
    unsupported-type path with a precise message."""
    try:
        return get_type_hints(td, include_extras=True)
    except Exception:
        return dict(td.__annotations__)


class TypeBuilder:
    """Registry-backed converter from Pydantic annotations to graphql-core types.

    Instances are caches keyed by Python class, so recursive models terminate:
    the type object is registered *before* its fields thunk runs.
    """

    def __init__(self) -> None:
        # (model.field, reason) for every field bridged as raw JSON — union
        # members and unmappable types alike; the scanner reports these at
        # startup so model owners know field selection was lost and the way
        # to regain it.
        self.degraded_fields: list[tuple[str, str]] = []
        self._object_types: dict[type, GraphQLObjectType] = {}
        self._input_types: dict[type, GraphQLInputObjectType] = {}
        self._enum_types: dict[type, GraphQLEnumType] = {}
        self._name_owner: dict[str, type] = {}
        self._built_output_fields: dict[type, dict[str, GraphQLField]] = {}
        self._built_input_fields: dict[type, dict[str, GraphQLInputField]] = {}
        # GraphQL-name -> wire-key translations for input objects whose
        # field names needed sanitizing (populated by the field builders,
        # consumed by _registered_input_object as the type's out_type).
        self._input_wire_map: dict[type, dict[str, str]] = {}

    # ------------------------------------------------------------------ names

    def _register_name(self, cls: type, base: str) -> str:
        if base in self._name_owner and self._name_owner[base] is not cls:
            module_tail = sanitize_graphql_name(cls.__module__.rsplit(".", 1)[-1])
            qualified = f"{base}_{module_tail}"
            candidate, counter = qualified, 2
            while candidate in self._name_owner and self._name_owner[candidate] is not cls:
                candidate = f"{qualified}_{counter}"
                counter += 1
            logger.warning(
                "GraphQL type name collision: %r already taken by %s; "
                "qualifying %s as %r",
                base,
                self._name_owner[base].__qualname__,
                cls.__qualname__,
                candidate,
            )
            self._name_owner[candidate] = cls
            return candidate
        self._name_owner[base] = cls
        return base

    def _release_name(self, name: str, cls: type) -> None:
        """Drop a ``_register_name`` claim when the owning type's build failed
        (transactional builds) — only when THIS class still owns the name."""
        if self._name_owner.get(name) is cls:
            del self._name_owner[name]

    def _record_degraded(self, path: str, reason: str) -> None:
        """One report entry per (field, reason) — the same model field can
        degrade through both its output and its input build, and the report
        is advisory, not a count."""
        if (path, reason) not in self.degraded_fields:
            self.degraded_fields.append((path, reason))

    # ---------------------------------------------------------------- output

    def output_type(self, annotation: Any, *, context: str = "field") -> GraphQLOutputType:
        """Convert an annotation to an output type, applying nullability."""
        if is_optional_annotation(annotation):
            return self._bare_output(unwrap_optional(annotation), context)
        return GraphQLNonNull(self._bare_output(strip_annotated(annotation), context))

    def bare_output_type(
        self, annotation: Any, *, context: str = "field"
    ) -> GraphQLScalarType | GraphQLObjectType | GraphQLEnumType | GraphQLList[Any]:
        """Output type without the outer NonNull — for fields whose errors must
        null only themselves (route responses)."""
        return self._bare_output(strip_annotated(unwrap_optional(annotation)), context)

    def _bare_output(
        self, annotation: Any, context: str
    ) -> GraphQLScalarType | GraphQLObjectType | GraphQLEnumType | GraphQLList[Any]:
        annotation = _normalize_typevar(strip_annotated(annotation))
        # Heterogeneous fixed tuples share the union fate: which type each
        # position carries is positional, not listable — JSON scalar.
        if is_heterogeneous_tuple(annotation):
            return GraphQLJSON
        if is_collection_annotation(annotation):
            inner = get_args(annotation)[0]
            if is_optional_annotation(inner):
                return GraphQLList(self._bare_output(unwrap_optional(inner), context))
            return GraphQLList(GraphQLNonNull(self._bare_output(inner, context)))

        scalar = literal_scalar(annotation)
        if scalar is not None:
            return scalar

        # Union fallback: which member arrives is a runtime decision, so the
        # shape is not statically promisable — bridge the whole union as the
        # JSON scalar (agent selects the field bare). A GraphQLUnionType with
        # resolve_type remains a future upgrade path; JSON never blocks it.
        if union_members(annotation) is not None:
            return GraphQLJSON

        if isinstance(annotation, type):
            if issubclass(annotation, Enum):
                return self._enum_type(annotation)
            if issubclass(annotation, BaseModel):
                return self._object_type(annotation, context)
            if is_typeddict(annotation):
                return self._typeddict_output_type(annotation, context)
            if is_dataclass(annotation):
                return self._dataclass_output_type(annotation, context)

        if annotation in SCALAR_MAP:
            return SCALAR_MAP[annotation]

        json_scalar = json_passthrough(annotation)
        if json_scalar is not None:
            return json_scalar

        raise _unsupported(annotation, context)

    def _object_type(self, model: type[BaseModel], context: str) -> GraphQLObjectType:
        return self._registered_output_object(
            model, self._built_output_fields, self._output_fields
        )

    def _typeddict_output_type(self, td: type, context: str) -> GraphQLObjectType:
        return self._registered_output_object(
            td, self._built_output_fields, self._td_output_fields
        )

    def _registered_output_object(
        self,
        cls: type,
        fields_cache: dict[type, dict[str, GraphQLField]],
        build_fields: Callable[[type], dict[str, GraphQLField]],
    ) -> GraphQLObjectType:
        """Register-then-build shell shared by BaseModel and TypedDict
        outputs (field error contexts come from the field builders). The
        field-build memo lives HERE — one cache strategy, not one wrapper
        method per builder."""
        cached = self._object_types.get(cls)
        if cached is not None:
            return cached
        name = self._register_name(
            cls, sanitize_graphql_name(graphql_model_name(cls), what="type name")
        )

        def fields_now() -> dict[str, GraphQLField]:
            built = fields_cache.get(cls)
            if built is None:
                built = build_fields(cls)
                fields_cache[cls] = built
            return built

        obj = GraphQLObjectType(
            name=name,
            description=_own_doc(cls),
            fields=fields_now,
        )
        # Register BEFORE building fields so recursive types resolve the cycle
        # to this same object, then build eagerly so unsupported nested types
        # raise UnsupportedFieldTypeError here — graphql-core's lazy `.fields`
        # would otherwise swallow it into a generic TypeError at schema time.
        # Transactional: on failure the registration (and its name) rolls back,
        # else a later route reusing the type hits the cache, skips the field
        # build, and detonates the WHOLE schema at GraphQLSchema time.
        self._object_types[cls] = obj
        try:
            fields = fields_now()
        except Exception:
            del self._object_types[cls]
            self._release_name(name, cls)
            raise
        # Direct self-reference edge (the same predicate recursive_edges
        # applies to the finished schema)? Then the type-level description
        # carries the true-depth contract ONCE — not one copy per recursive
        # field.
        if any(is_direct_self_reference(f.type, obj) for f in fields.values()):
            # One line, token-priced: local placement beats full contract
            # text (the README carries the details) — N recursive types
            # cost N x ~15 tokens instead of N paragraphs.
            note = (
                "Recursive type: full subtree at true depth; "
                "your selection repeats per level."
            )
            obj.description = (
                f"{obj.description}\n\n{note}" if obj.description else note
            )
        return obj

    def _degradable_output_type(
        self, annotation: Any, context: str, *, bare: bool = False
    ) -> tuple[GraphQLOutputType, str | None]:
        """Field type with the field-level JSON fallback: an unmappable
        annotation degrades THIS field to the JSON scalar (reason returned
        for the readiness report) instead of failing the whole model — the
        route keeps its structured fields. ``bare`` mirrors
        ``bare_output_type`` (no outer NonNull) for keys that may be absent."""
        try:
            if bare:
                return self.bare_output_type(annotation, context=context), None
            return self.output_type(annotation, context=context), None
        except UnsupportedFieldTypeError as exc:
            gtype: GraphQLOutputType = (
                GraphQLJSON
                if bare or is_optional_annotation(annotation)
                else GraphQLNonNull(GraphQLJSON)
            )
            return gtype, _field_unmappable_reason(exc)

    def _degradable_input_type(
        self, annotation: Any, context: str
    ) -> tuple[_BareInput, str | None]:
        """Input twin of ``_degradable_output_type``: an unmappable field
        degrades to a JSON argument; FastAPI validates whatever the caller
        sends (a 422 surfaces as a field error, never silently)."""
        try:
            return cast(
                _BareInput, self.input_type(annotation, context=context)
            ), None
        except UnsupportedFieldTypeError as exc:
            return GraphQLJSON, _field_unmappable_reason(exc)

    def _build_fields(
        self, cls: Any, specs: Iterator[FieldSpec], *, input_side: bool
    ) -> dict[str, Any]:
        """The ONE field loop for every composite family: sanitize the wire
        name into a GraphQL name, degrade-or-bridge the type, record the
        degradation once, and shape the GraphQLField / GraphQLInputField.
        The register-then-build shells call the family wrappers below."""
        owner = cls.__name__
        what = f"{owner} {'input field' if input_side else 'field'}"
        fields: dict[str, Any] = {}
        for spec in specs:
            gname = sanitize_graphql_name(spec.wire, what=what)
            context = f"{owner}.{spec.field_name}"
            if input_side:
                if gname != spec.wire:
                    self._input_wire_map.setdefault(cls, {})[gname] = spec.wire
                field_type, degraded = self._degradable_input_type(
                    spec.annotation, context
                )
                gtype: Any = GraphQLNonNull(field_type) if spec.required else field_type
            else:
                gtype, degraded = self._degradable_output_type(
                    spec.annotation, context, bare=spec.bare
                )
            description = spec.description
            if degraded is not None:
                description = append_note(description, _degraded_field_note(degraded))
                self._record_degraded(context, degraded)
            elif (bridge := _union_json_bridge(spec.annotation)) is not None:
                reason, note = bridge
                description = append_note(description, note)
                # Surfaced by the scanner's startup log: the owner can
                # regain field selection by restructuring the union away.
                self._record_degraded(context, reason)
            if input_side:
                fields[gname] = GraphQLInputField(
                    gtype, description=description, default_value=spec.default
                )
            elif gname != spec.wire:
                # The GraphQL name is sanitized while FastAPI serializes by
                # the wire key — the resolver closes that gap.
                fields[gname] = GraphQLField(
                    gtype, description=description, resolve=_wire_key_resolver(spec.wire)
                )
            else:
                fields[gname] = GraphQLField(gtype, description=description)
        if not fields:
            reason = f"{owner} has no usable {'input ' if input_side else ''}fields"
            raise UnsupportedFieldTypeError(cls, reason, field_reason=reason)
        return fields

    def _output_fields(self, model: type[BaseModel]) -> dict[str, GraphQLField]:
        return cast(
            "dict[str, GraphQLField]",
            self._build_fields(
                model, _model_specs(model, input_side=False), input_side=False
            ),
        )

    # ----------------------------------------------------------------- input

    def input_type(self, annotation: Any, *, context: str = "argument") -> GraphQLInputType:
        """Convert an annotation to an input type WITHOUT outer nullability.

        Whether an argument is required is decided by FastAPI's own `required`
        flag at the argument level (NonNull there), not by the annotation.
        """
        annotation = _normalize_typevar(unwrap_optional(annotation))
        # Symmetric with the output side (and the union fallback).
        if is_heterogeneous_tuple(annotation):
            return GraphQLJSON
        if is_collection_annotation(annotation):
            inner = get_args(annotation)[0]
            if is_optional_annotation(inner):
                # Optional items: [T]
                return GraphQLList(self.input_type(unwrap_optional(inner), context=context))
            # Non-null items: [T!]
            return GraphQLList(
                GraphQLNonNull(self.input_type(inner, context=context))
            )

        scalar = literal_scalar(annotation)
        if scalar is not None:
            return scalar

        # Union fallback, symmetric with the output side: GraphQL has no
        # input unions — the JSON scalar carries whichever member the caller
        # sends and FastAPI validates it (a 422 surfaces as a field error,
        # never silently).
        if union_members(annotation) is not None:
            return GraphQLJSON

        if isinstance(annotation, type):
            if issubclass(annotation, Enum):
                return self._enum_type(annotation)
            if issubclass(annotation, BaseModel):
                return self._input_object_type(annotation, context)
            if is_typeddict(annotation):
                return self._typeddict_input_type(annotation, context)
            if is_dataclass(annotation):
                return self._dataclass_input_type(annotation, context)

        if annotation in SCALAR_MAP:
            return SCALAR_MAP[annotation]

        json_scalar = json_passthrough(annotation)
        if json_scalar is not None:
            return json_scalar

        raise _unsupported(annotation, context)

    def _input_object_type(
        self, model: type[BaseModel], context: str
    ) -> GraphQLInputObjectType:
        return self._registered_input_object(
            model, self._built_input_fields, self._input_fields
        )

    def _typeddict_input_type(
        self, td: type, context: str
    ) -> GraphQLInputObjectType:
        return self._registered_input_object(
            td, self._built_input_fields, self._td_input_fields
        )

    def _registered_input_object(
        self,
        cls: type,
        fields_cache: dict[type, dict[str, GraphQLInputField]],
        build_fields: Callable[[type], dict[str, GraphQLInputField]],
    ) -> GraphQLInputObjectType:
        """Register-then-build shell shared by BaseModel and TypedDict
        inputs; register first so recursive types resolve the cycle to this
        object, build eagerly to surface errors during scanning, roll the
        registration back on failure (transactional, like the output side).
        The field-build memo lives here, as on the output side."""

        def fields_now() -> dict[str, GraphQLInputField]:
            built = fields_cache.get(cls)
            if built is None:
                built = build_fields(cls)
                fields_cache[cls] = built
            return built

        cached = self._input_types.get(cls)
        if cached is not None:
            return cached
        base = sanitize_graphql_name(graphql_model_name(cls), what="type name")
        name = self._register_name(cls, f"{base}Input")
        obj = GraphQLInputObjectType(
            name=name,
            description=_own_doc(cls),
            fields=fields_now,
        )
        self._input_types[cls] = obj
        try:
            fields_now()
        except Exception:
            # Transactional: no half-built inputs survive.
            del self._input_types[cls]
            self._release_name(name, cls)
            self._input_wire_map.pop(cls, None)
            raise
        wire = self._input_wire_map.get(cls)
        if wire:
            # graphql-core types out_type as an identity default; overriding
            # it after registration is the supported hook (the map is only
            # known once the fields have been built).
            cast(Any, obj).out_type = _wire_key_out_type(wire)
        return obj

    def _input_fields(self, model: type[BaseModel]) -> dict[str, GraphQLInputField]:
        return cast(
            "dict[str, GraphQLInputField]",
            self._build_fields(
                model, _model_specs(model, input_side=True), input_side=True
            ),
        )

    # -------------------------------------------------------------- typeddict

    def _td_output_fields(self, td: Any) -> dict[str, GraphQLField]:
        return cast(
            "dict[str, GraphQLField]",
            self._build_fields(
                td, _typeddict_specs(td, input_side=False), input_side=False
            ),
        )

    def _td_input_fields(self, td: Any) -> dict[str, GraphQLInputField]:
        return cast(
            "dict[str, GraphQLInputField]",
            self._build_fields(
                td, _typeddict_specs(td, input_side=True), input_side=True
            ),
        )

    # -------------------------------------------------------------- dataclass

    def _dataclass_output_type(self, dc: type, context: str) -> GraphQLObjectType:
        return self._registered_output_object(
            dc, self._built_output_fields, self._dataclass_output_fields
        )

    def _dataclass_input_type(self, dc: type, context: str) -> GraphQLInputObjectType:
        return self._registered_input_object(
            dc, self._built_input_fields, self._dataclass_input_fields
        )

    def _dataclass_output_fields(self, dc: Any) -> dict[str, GraphQLField]:
        return cast(
            "dict[str, GraphQLField]",
            self._build_fields(
                dc, _dataclass_specs(dc, input_side=False), input_side=False
            ),
        )

    def _dataclass_input_fields(self, dc: Any) -> dict[str, GraphQLInputField]:
        return cast(
            "dict[str, GraphQLInputField]",
            self._build_fields(
                dc, _dataclass_specs(dc, input_side=True), input_side=True
            ),
        )

    # ------------------------------------------------------------------ enum

    def _enum_type(self, enum_cls: type[Enum]) -> GraphQLEnumType:
        cached = self._enum_types.get(enum_cls)
        if cached is not None:
            return cached
        for member_name in enum_cls.__members__:
            if not _NAME_RE.match(member_name):
                raise UnsupportedFieldTypeError(
                    enum_cls, f"enum member {member_name!r} is not a legal GraphQL name"
                )
        name = self._register_name(
            enum_cls, sanitize_graphql_name(enum_cls.__name__, what="enum name")
        )
        enum_type = GraphQLEnumType(
            name=name,
            values={member.name: member.value for member in enum_cls},
            description=_own_doc(enum_cls),
        )
        self._enum_types[enum_cls] = enum_type
        return enum_type
