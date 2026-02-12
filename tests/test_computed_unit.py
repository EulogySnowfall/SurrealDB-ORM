"""
Unit tests for Computed field type.

Tests the Computed field type, introspection, server_fields auto-inclusion,
and migration VALUE clause generation — all without a running database.
"""

from typing import Annotated, get_args, get_origin

import pytest
from pydantic import Field

from src.surreal_orm.fields.computed import (
    Computed,
    _ComputedDefault,
    _ComputedMarker,
    _get_computed_marker,
    get_computed_expression,
    is_computed_field,
)
from src.surreal_orm.migrations.introspector import ModelIntrospector
from src.surreal_orm.migrations.operations import AddField
from src.surreal_orm.model_base import (
    BaseSurrealModel,
    SurrealConfigDict,
    clear_model_registry,
)


@pytest.fixture(autouse=True)
def clear_registry() -> None:
    """Clear model registry before each test."""
    clear_model_registry()


# ---------------------------------------------------------------------------
# Type-level tests
# ---------------------------------------------------------------------------


class TestComputedType:
    """Tests for Computed[T] type annotation."""

    def test_computed_str_is_annotated(self) -> None:
        """Computed[str] should produce an Annotated type."""
        t = Computed[str]
        assert get_origin(t) is Annotated

    def test_computed_str_inner_args(self) -> None:
        """Computed[str] should wrap str | None with _ComputedMarker."""
        t = Computed[str]
        args = get_args(t)
        # First arg is str | None, second is _ComputedMarker
        assert any(isinstance(a, _ComputedMarker) for a in args)

    def test_computed_int(self) -> None:
        """Computed[int] should work with int inner type."""
        t = Computed[int]
        marker = _get_computed_marker(t)
        assert marker is not None
        assert marker.inner_type is int

    def test_computed_float(self) -> None:
        """Computed[float] should work with float inner type."""
        t = Computed[float]
        marker = _get_computed_marker(t)
        assert marker is not None
        assert marker.inner_type is float


class TestComputedDefault:
    """Tests for Computed("expression") default value."""

    def test_computed_call_returns_sentinel(self) -> None:
        """Computed("expr") should return _ComputedDefault."""
        result = Computed("math::sum(items)")
        assert isinstance(result, _ComputedDefault)

    def test_computed_default_expression(self) -> None:
        """_ComputedDefault should store the expression."""
        result = Computed("string::concat(a, b)")
        assert result.expression == "string::concat(a, b)"

    def test_computed_default_repr(self) -> None:
        """_ComputedDefault should have a readable repr."""
        result = Computed("time::now()")
        assert "time::now()" in repr(result)


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------


class TestDetectionHelpers:
    """Tests for is_computed_field() and get_computed_expression()."""

    def test_is_computed_field_true(self) -> None:
        """is_computed_field should detect Computed types."""
        t = Computed[str]
        assert is_computed_field(t) is True

    def test_is_computed_field_false_for_str(self) -> None:
        """is_computed_field should return False for plain str."""
        assert is_computed_field(str) is False

    def test_is_computed_field_false_for_none(self) -> None:
        """is_computed_field should return False for None."""
        assert is_computed_field(None) is False

    def test_get_computed_expression_empty(self) -> None:
        """get_computed_expression returns None when expression is empty."""
        t = Computed[str]
        # Marker exists but expression is empty (not set by __init_subclass__)
        assert get_computed_expression(t) is None

    def test_get_computed_expression_with_value(self) -> None:
        """get_computed_expression returns the expression when set."""
        t = Computed[str]
        marker = _get_computed_marker(t)
        assert marker is not None
        marker.expression = "string::concat(a, b)"
        assert get_computed_expression(t) == "string::concat(a, b)"

    def test_get_computed_expression_non_computed(self) -> None:
        """get_computed_expression returns None for non-computed types."""
        assert get_computed_expression(str) is None

    def test_get_computed_marker_non_annotated(self) -> None:
        """_get_computed_marker returns None for non-Annotated types."""
        assert _get_computed_marker(int) is None
        assert _get_computed_marker(None) is None


# ---------------------------------------------------------------------------
# Model-level tests
# ---------------------------------------------------------------------------


class TestComputedOnModel:
    """Tests for Computed fields on BaseSurrealModel subclasses."""

    def test_model_with_computed_field(self) -> None:
        """Model with Computed field should accept None as default."""

        class UserModel(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="users")
            id: str | None = None
            first_name: str
            last_name: str
            full_name: Computed[str] = Computed("string::concat(first_name, ' ', last_name)")

        user = UserModel(first_name="Alice", last_name="Smith")
        assert user.full_name is None  # Default is None (server computes it)

    def test_model_computed_field_accepts_value(self) -> None:
        """Computed field should accept a value from the DB."""

        class UserModel(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="users")
            id: str | None = None
            first_name: str
            last_name: str
            full_name: Computed[str] = Computed("string::concat(first_name, ' ', last_name)")

        user = UserModel(first_name="Alice", last_name="Smith", full_name="Alice Smith")
        assert user.full_name == "Alice Smith"

    def test_computed_expressions_class_attr(self) -> None:
        """Model should have _computed_expressions set by __init_subclass__."""

        class OrderModel(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="orders")
            id: str | None = None
            items: list[dict] = Field(default_factory=list)
            subtotal: Computed[float] = Computed("math::sum(items.*.price)")
            item_count: Computed[int] = Computed("array::len(items)")

        assert hasattr(OrderModel, "_computed_expressions")
        assert OrderModel._computed_expressions == {
            "subtotal": "math::sum(items.*.price)",
            "item_count": "array::len(items)",
        }

    def test_server_fields_auto_includes_computed(self) -> None:
        """get_server_fields() should auto-include computed fields."""

        class UserModel(BaseSurrealModel):
            model_config = SurrealConfigDict(
                table_name="users",
                server_fields=["created_at"],
            )
            id: str | None = None
            first_name: str
            last_name: str
            full_name: Computed[str] = Computed("string::concat(first_name, ' ', last_name)")

        server_fields = UserModel.get_server_fields()
        assert "created_at" in server_fields
        assert "full_name" in server_fields

    def test_server_fields_computed_only(self) -> None:
        """get_server_fields() works with only computed fields (no server_fields config)."""

        class UserModel(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="users")
            id: str | None = None
            name: str
            display: Computed[str] = Computed("name")

        server_fields = UserModel.get_server_fields()
        assert "display" in server_fields

    def test_computed_excluded_from_model_dump(self) -> None:
        """Computed fields should be excluded from save data via get_server_fields."""

        class UserModel(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="users")
            id: str | None = None
            first_name: str
            last_name: str
            full_name: Computed[str] = Computed("string::concat(first_name, ' ', last_name)")

        user = UserModel(first_name="Alice", last_name="Smith")
        exclude_fields = {"id"} | UserModel.get_server_fields()
        data = user.model_dump(exclude=exclude_fields, exclude_unset=True)
        assert "full_name" not in data
        assert "first_name" in data
        assert "last_name" in data

    def test_multiple_computed_fields(self) -> None:
        """Model can have multiple Computed fields."""

        class OrderModel(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="orders")
            id: str | None = None
            items: list[dict] = Field(default_factory=list)
            discount: float = 0.0
            subtotal: Computed[float] = Computed("math::sum(items.*.price * items.*.qty)")
            total: Computed[float] = Computed("subtotal * (1 - discount)")
            item_count: Computed[int] = Computed("array::len(items)")

        order = OrderModel(items=[], discount=0.1)
        assert order.subtotal is None
        assert order.total is None
        assert order.item_count is None

        server = OrderModel.get_server_fields()
        assert {"subtotal", "total", "item_count"}.issubset(server)

    def test_model_without_computed_has_no_attr(self) -> None:
        """Model without computed fields should not have _computed_expressions."""

        class PlainModel(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="plain")
            id: str | None = None
            name: str

        assert not hasattr(PlainModel, "_computed_expressions")


# ---------------------------------------------------------------------------
# Introspector tests
# ---------------------------------------------------------------------------


class TestIntrospectorComputed:
    """Tests for ModelIntrospector with computed fields."""

    def test_introspect_computed_field_has_value(self) -> None:
        """Introspector should populate FieldState.value for computed fields."""

        class UserModel(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="users")
            id: str | None = None
            first_name: str
            last_name: str
            full_name: Computed[str] = Computed("string::concat(first_name, ' ', last_name)")

        introspector = ModelIntrospector(models=[UserModel])
        state = introspector.introspect()
        table = state.tables["users"]

        assert "full_name" in table.fields
        field = table.fields["full_name"]
        assert field.value == "string::concat(first_name, ' ', last_name)"
        assert field.field_type == "string"
        assert field.nullable is True

    def test_introspect_computed_int_field(self) -> None:
        """Introspector maps Computed[int] to int type."""

        class OrderModel(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="orders")
            id: str | None = None
            items: list[dict] = Field(default_factory=list)
            item_count: Computed[int] = Computed("array::len(items)")

        introspector = ModelIntrospector(models=[OrderModel])
        state = introspector.introspect()
        field = state.tables["orders"].fields["item_count"]
        assert field.value == "array::len(items)"
        assert field.field_type == "int"

    def test_introspect_computed_float_field(self) -> None:
        """Introspector maps Computed[float] to float type."""

        class OrderModel(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="orders")
            id: str | None = None
            subtotal: Computed[float] = Computed("math::sum(items.*.price)")

        introspector = ModelIntrospector(models=[OrderModel])
        state = introspector.introspect()
        field = state.tables["orders"].fields["subtotal"]
        assert field.value == "math::sum(items.*.price)"
        assert field.field_type == "float"

    def test_introspect_non_computed_no_value(self) -> None:
        """Non-computed fields should have value=None."""

        class PlainModel(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="plain")
            id: str | None = None
            name: str

        introspector = ModelIntrospector(models=[PlainModel])
        state = introspector.introspect()
        field = state.tables["plain"].fields["name"]
        assert field.value is None

    def test_introspect_computed_no_default(self) -> None:
        """Computed fields should not have default value set (VALUE clause replaces it)."""

        class UserModel(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="users")
            id: str | None = None
            full_name: Computed[str] = Computed("string::concat('hello')")

        introspector = ModelIntrospector(models=[UserModel])
        state = introspector.introspect()
        field = state.tables["users"].fields["full_name"]
        assert field.default is None
        assert field.value == "string::concat('hello')"


# ---------------------------------------------------------------------------
# Migration operations tests
# ---------------------------------------------------------------------------


class TestAddFieldValueClause:
    """Tests for AddField generating VALUE clause in DDL."""

    def test_add_field_with_value(self) -> None:
        """AddField with value should generate VALUE clause."""
        op = AddField(
            table="users",
            name="full_name",
            field_type="string",
            value="string::concat(first_name, ' ', last_name)",
        )
        ddl = op.forwards()
        assert "VALUE string::concat(first_name, ' ', last_name)" in ddl
        assert "DEFINE FIELD full_name ON users" in ddl

    def test_add_field_without_value(self) -> None:
        """AddField without value should not have VALUE clause."""
        op = AddField(
            table="users",
            name="name",
            field_type="string",
        )
        ddl = op.forwards()
        assert "VALUE" not in ddl

    def test_add_field_encrypted_takes_precedence_over_value(self) -> None:
        """When both encrypted and value are set, encrypted takes priority."""
        op = AddField(
            table="users",
            name="password",
            field_type="string",
            encrypted=True,
            value="some_expression",
        )
        ddl = op.forwards()
        # Encrypted check comes first in operations.py
        assert "crypto::argon2::generate($value)" in ddl


# ---------------------------------------------------------------------------
# Access state tests (computed fields skipped in signup_fields)
# ---------------------------------------------------------------------------


class TestAccessStateSkipsComputed:
    """Tests that computed fields are excluded from signup_fields."""

    def test_computed_skipped_in_access_signup(self) -> None:
        """Computed fields should not appear in signup_fields for USER tables."""
        from src.surreal_orm.types import TableType

        class AuthUser(BaseSurrealModel):
            model_config = SurrealConfigDict(
                table_name="auth_users",
                table_type=TableType.USER,
                identifier_field="email",
                password_field="password",
            )
            id: str | None = None
            email: str
            password: str
            name: str
            display_name: Computed[str] = Computed("string::concat(name, ' (', email, ')')")

        introspector = ModelIntrospector(models=[AuthUser])
        state = introspector.introspect()
        table = state.tables["auth_users"]

        assert table.access is not None
        assert "display_name" not in table.access.signup_fields
        assert "email" in table.access.signup_fields
        assert "name" in table.access.signup_fields
