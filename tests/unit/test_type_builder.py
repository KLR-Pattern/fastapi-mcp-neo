"""TypeBuilder: Pydantic annotations -> graphql-core types."""

import uuid
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum, IntEnum
from typing import Any, Literal, Optional

import pytest
from graphql import (
    GraphQLEnumType,
    GraphQLInputObjectType,
    GraphQLNonNull,
    GraphQLObjectType,
)
from pydantic import BaseModel, Field

from fastapi_mcp_neo.type_builder import (
    TypeBuilder,
    UnsupportedFieldTypeError,
    describe_literal_values,
    request_wire_name,
    sanitize_graphql_name,
)


def render(t) -> str:
    return str(t)


class ModuleLevelNode(BaseModel):
    """Recursive model at module level: ForwardRefs resolve against the
    module's globals (function-local classes use pydantic's captured parent
    namespace instead — see test_recursive_model)."""

    value: int
    children: list["ModuleLevelNode"] = []


class TestScalars:
    @pytest.mark.parametrize(
        ("annotation", "expected"),
        [
            (int, "Int!"),
            (str, "String!"),
            (bool, "Boolean!"),
            (float, "Float!"),
            (datetime, "DateTime!"),
            (date, "Date!"),
            (time, "Time!"),
            (uuid.UUID, "UUID!"),
            (Decimal, "Decimal!"),
        ],
    )
    def test_scalar_matrix(self, annotation, expected):
        assert render(TypeBuilder().output_type(annotation)) == expected

    def test_optional_scalar(self):
        assert render(TypeBuilder().output_type(Optional[int])) == "Int"  # noqa: UP045

    def test_int_or_none_union_type(self):
        assert render(TypeBuilder().output_type(int | None)) == "Int"

    def test_optional_union_stays_nullable_json(self):
        # Non-Optional unions pick their member at runtime, so they bridge to
        # the JSON scalar; Optional keeps the nullable wrapper (issue #3, case 4).
        assert render(TypeBuilder().output_type(int | str | None)) == "JSON"

    def test_list_of_scalars(self):
        assert render(TypeBuilder().output_type(list[int])) == "[Int!]!"

    def test_list_of_optional(self):
        assert render(TypeBuilder().output_type(list[int | None])) == "[Int]!"

    def test_optional_list(self):
        # typing.Optional syntax must keep working alongside `X | None`
        assert render(TypeBuilder().output_type(Optional[list[str]])) == "[String!]"  # noqa: UP045


class TestLiteral:
    def test_str_literal(self):
        assert render(TypeBuilder().output_type(Literal["a", "b"])) == "String!"

    def test_int_literal(self):
        assert render(TypeBuilder().output_type(Literal[1, 2])) == "Int!"

    def test_literal_description(self):
        assert describe_literal_values(Literal["a", "b"]) == "Allowed values: 'a', 'b'"
        assert describe_literal_values(Optional[Literal["a"]]) == (  # noqa: UP045
            "Allowed values: 'a' (or null)"
        )

    def test_mixed_literal_raises(self):
        with pytest.raises(UnsupportedFieldTypeError, match="mixed Literal"):
            TypeBuilder().output_type(Literal["a", 1])

    def test_literal_as_input(self):
        assert render(TypeBuilder().input_type(Literal["a", "b"])) == "String"


class Color(str, Enum):
    RED = "red"
    BLUE = "blue"


class Priority(IntEnum):
    LOW = 1
    HIGH = 2


# Python identifiers are always legal GraphQL names, so the functional API is
# the only way to construct an illegal member name.
BadMember = Enum("BadMember", {"has space": "x"})


class TestEnums:
    def test_enum_output(self):
        t = TypeBuilder().output_type(Color)
        assert isinstance(t, GraphQLNonNull)
        assert isinstance(t.of_type, GraphQLEnumType)
        assert t.of_type.name == "Color"
        assert t.of_type.values["RED"].value == "red"

    def test_int_enum(self):
        t = TypeBuilder().output_type(Priority)
        assert t.of_type.values["LOW"].value == 1

    def test_illegal_member_name_raises(self):
        with pytest.raises(UnsupportedFieldTypeError, match="not a legal GraphQL name"):
            TypeBuilder().output_type(BadMember)


class Address(BaseModel):
    city: str
    zip_code: str = Field(description="postal code")


class UserOut(BaseModel):
    id: int
    name: str
    address: Address
    tags: list[str] = []
    nickname: str | None = None
    status: Literal["active", "frozen"] = Field(description="account status")


class TestObjects:
    def test_nested_model(self):
        t = TypeBuilder().output_type(UserOut)
        inner = t.of_type
        assert isinstance(inner, GraphQLObjectType)
        assert inner.name == "UserOut"
        fields = inner.fields
        assert str(fields["id"].type) == "Int!"
        assert str(fields["address"].type) == "Address!"
        assert str(fields["tags"].type) == "[String!]!"
        assert str(fields["nickname"].type) == "String"
        assert str(fields["status"].type) == "String!"
        assert "Allowed values: 'active', 'frozen'" in (fields["status"].description or "")
        assert fields["address"].type.of_type.fields["zip_code"].description == "postal code"

    def test_recursive_model(self):
        class Node(BaseModel):
            value: int
            child: Optional["Node"] = None
            children: list["Node"] = []

        t = TypeBuilder().output_type(Node)
        assert str(t.of_type.fields["child"].type) == "Node"
        assert str(t.of_type.fields["children"].type) == "[Node!]!"

    def test_recursive_model_module_level(self):
        t = TypeBuilder().output_type(ModuleLevelNode)
        # children: [ModuleLevelNode!]! — NonNull(List(NonNull(obj)))
        children = t.of_type.fields["children"].type
        assert str(children) == "[ModuleLevelNode!]!"
        # the recursive element IS the enclosing type object (a true cycle)
        assert children.of_type.of_type.of_type is t.of_type

    def test_alias_output_field_name(self):
        class Aliased(BaseModel):
            user_name: str = Field(alias="userName")
            hidden: str = Field(serialization_alias="shown")

        fields = TypeBuilder().output_type(Aliased).of_type.fields
        assert set(fields) == {"userName", "shown"}

    def test_input_output_split(self):
        b = TypeBuilder()
        out = b.output_type(UserOut)
        inp = b.input_type(UserOut)
        assert isinstance(out.of_type, GraphQLObjectType)
        assert out.of_type.name == "UserOut"
        assert isinstance(inp, GraphQLInputObjectType)
        assert inp.name == "UserOutInput"

    def test_input_field_nullability_follows_required(self):
        inp = TypeBuilder().input_type(UserOut)
        fields = inp.fields
        assert str(fields["id"].type) == "Int!"
        assert str(fields["nickname"].type) == "String"
        assert str(fields["tags"].type) == "[String!]"

    def test_input_field_uses_validation_alias(self):
        class Body(BaseModel):
            page_size: int = Field(validation_alias="pageSize", default=10)

        fields = TypeBuilder().input_type(Body).fields
        assert set(fields) == {"pageSize"}
        assert fields["pageSize"].default_value == 10

    def test_same_name_different_class_qualified(self, caplog):
        import sys
        from types import ModuleType

        mod_a = ModuleType("m_a")
        exec("from pydantic import BaseModel\nclass Item(BaseModel):\n    a: int\n", mod_a.__dict__)
        mod_b = ModuleType("m_b")
        exec("from pydantic import BaseModel\nclass Item(BaseModel):\n    b: int\n", mod_b.__dict__)
        sys.modules.update({"m_a": mod_a, "m_b": mod_b})

        b = TypeBuilder()
        t1 = b.output_type(mod_a.Item)
        t2 = b.output_type(mod_b.Item)
        assert t1.of_type.name == "Item"
        assert t2.of_type.name != "Item"
        assert t2.of_type.name.startswith("Item_")
        with caplog.at_level("WARNING"):
            b.output_type(mod_b.Item)
        assert any("collision" in r.message for r in caplog.records)

    def test_unsupported_type_raises(self):
        with pytest.raises(UnsupportedFieldTypeError):
            TypeBuilder().output_type(bytes)  # no GraphQL scalar for bytes

    def test_dict_maps_to_json_scalar(self):
        from fastapi_mcp_neo.scalars import GraphQLJSON

        builder = TypeBuilder()
        assert builder.bare_output_type(dict[str, int]) is GraphQLJSON
        assert builder.bare_output_type(dict) is GraphQLJSON
        assert builder.input_type(dict[str, Any]) is GraphQLJSON

    def test_unsupported_nested_field_degrades_with_path(self):
        """Field-level fallback: the bad field bridges as JSON, the path and
        reason land in degraded_fields (the whole-route raise is gone)."""
        class Bad(BaseModel):
            ok: int
            payload: bytes  # no GraphQL scalar for bytes

        builder = TypeBuilder()
        fields = builder.output_type(Bad).of_type.fields
        assert str(fields["payload"].type) == "JSON!"
        assert "bytes has no GraphQL mapping" in (fields["payload"].description or "")
        assert builder.degraded_fields == [
            ("Bad.payload", "bytes has no GraphQL mapping")
        ]

    def test_model_without_fields_raises(self):
        class Empty(BaseModel):
            pass

        with pytest.raises(UnsupportedFieldTypeError, match="no usable fields"):
            TypeBuilder().output_type(Empty)

    def test_output_list_of_models(self):
        assert str(TypeBuilder().output_type(list[UserOut])) == "[UserOut!]!"


class TestEmptyTypedDict:
    def test_empty_output_and_input_raise(self):
        from typing_extensions import TypedDict

        class Empty(TypedDict):
            pass

        with pytest.raises(UnsupportedFieldTypeError, match="no usable fields"):
            TypeBuilder().output_type(Empty)
        with pytest.raises(UnsupportedFieldTypeError, match="no usable input fields"):
            TypeBuilder().input_type(Empty)


class TestOutputAliasChoices:
    def test_alias_choices_falls_back_to_field_name(self):
        # An AliasChoices alias has no single JSON key to serialize by, so
        # the GraphQL field keeps the Python name (FastAPI serializes
        # unaliased output by field name too).
        from pydantic import AliasChoices, Field

        class Weird(BaseModel):
            a: str = Field(default="", alias=AliasChoices("x", "y"))

        out = TypeBuilder().output_type(Weird)
        assert set(out.of_type.fields) == {"a"}


class TestInputListNullability:
    def test_input_list_of_optional_items_not_non_null(self):
        # Optional list ITEMS map to [T] (no NonNull), matching the output
        # side's list-of-optional contract.
        assert render(TypeBuilder().input_type(list[Optional[int]])) == "[Int]"  # noqa: UP045


class TestNameSanitization:
    def test_digit_leading_and_empty_names_prefixed(self):
        # Wire names FastAPI accepts but GraphQL rejects as identifiers:
        # sanitize prefixes an underscore (and never returns an empty name).
        assert sanitize_graphql_name("123abc") == "_123abc"
        assert sanitize_graphql_name("") == "_unnamed"
        assert sanitize_graphql_name("legal_name") == "legal_name"


class TestLiteralEdges:
    def test_float_literal_has_no_scalar(self):
        # _LITERAL_SCALARS covers str/int/bool; float members have no scalar
        # to ride, so the bridge refuses rather than guess.
        with pytest.raises(UnsupportedFieldTypeError, match="has no GraphQL scalar"):
            TypeBuilder().output_type(Literal[1.5])


class TestWireNameEdges:
    def test_alias_path_without_choices_falls_back_to_field_name(self):
        # An alias with no string choices (pydantic AliasPath names a nested
        # location, not a flat wire key) has nothing to flatten: the field
        # name is the only name FastAPI can be counted on to accept.
        from pydantic import AliasPath, Field

        info = Field(alias=AliasPath("nested", "x"))
        assert request_wire_name(info, "flat") == "flat"


class TestNameCollisions:
    def test_same_module_tail_twice_takes_counter(self):
        # Two same-named classes from different modules qualify by module
        # tail; a THIRD whose tail ALSO collides (two distinct modules both
        # named m) exhausts qualification and takes the numeric suffix.
        from types import ModuleType

        def mod_named(name: str):
            mod = ModuleType(name)
            exec(
                "from pydantic import BaseModel\n"
                "class Item(BaseModel):\n    a: int\n",
                mod.__dict__,
            )
            return mod

        first = mod_named("orig")
        other = mod_named("other")    # different tail -> qualified name
        same_tail = mod_named("other")  # tail collides with SECOND -> counter

        b = TypeBuilder()
        b.output_type(first.Item)
        second = b.output_type(other.Item).of_type
        assert second.name == "Item_other"
        third = b.output_type(same_tail.Item).of_type
        assert third.name == "Item_other_2"
