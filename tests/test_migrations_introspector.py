"""
Unit tests for model introspection.
"""

import pytest

from src.surreal_orm.fields import Encrypted
from src.surreal_orm.migrations.introspector import ModelIntrospector, introspect_models
from src.surreal_orm.model_base import (
    BaseSurrealModel,
    SurrealConfigDict,
    clear_model_registry,
    get_registered_models,
)
from src.surreal_orm.types import SchemaMode, TableType


@pytest.fixture(autouse=True)
def clear_registry() -> None:
    """Clear model registry before each test."""
    clear_model_registry()


class TestModelRegistry:
    """Tests for model registration."""

    def test_model_auto_registered(self) -> None:
        """Test that models are automatically registered."""
        clear_model_registry()

        class TestModel(BaseSurrealModel):
            id: str | None = None
            name: str

        models = get_registered_models()
        assert TestModel in models

    def test_clear_registry(self) -> None:
        """Test clearing the model registry."""

        class TempModel(BaseSurrealModel):
            id: str | None = None

        clear_model_registry()
        assert len(get_registered_models()) == 0


class TestModelIntrospector:
    """Tests for ModelIntrospector class."""

    def test_introspect_simple_model(self) -> None:
        """Test introspection of a simple model."""

        class SimpleModel(BaseSurrealModel):
            id: str | None = None
            name: str
            age: int

        introspector = ModelIntrospector([SimpleModel])
        state = introspector.introspect()

        assert "SimpleModel" in state.tables
        table = state.tables["SimpleModel"]
        assert "name" in table.fields
        assert "age" in table.fields
        assert table.fields["name"].field_type == "string"
        assert table.fields["age"].field_type == "int"

    def test_introspect_model_with_config(self) -> None:
        """Test introspection with custom configuration."""

        class ConfiguredModel(BaseSurrealModel):
            model_config = SurrealConfigDict(
                table_name="custom_table",
                table_type=TableType.STREAM,
                schema_mode=SchemaMode.SCHEMAFULL,
                changefeed="7d",
            )
            id: str | None = None
            data: str

        introspector = ModelIntrospector([ConfiguredModel])
        state = introspector.introspect()

        assert "custom_table" in state.tables
        table = state.tables["custom_table"]
        assert table.schema_mode == "SCHEMAFULL"
        assert table.table_type == "stream"
        assert table.changefeed == "7d"

    def test_introspect_user_table(self) -> None:
        """Test introspection of USER type table."""

        class UserModel(BaseSurrealModel):
            model_config = SurrealConfigDict(table_type=TableType.USER)
            id: str | None = None
            email: str
            password: Encrypted

        introspector = ModelIntrospector([UserModel])
        state = introspector.introspect()

        table = state.tables["UserModel"]
        # USER tables must be SCHEMAFULL
        assert table.schema_mode == "SCHEMAFULL"
        assert table.table_type == "user"
        # Should have access definition
        assert table.access is not None
        assert table.access.name == "usermodel_auth"

    def test_introspect_encrypted_field(self) -> None:
        """Test introspection of encrypted fields."""

        class SecureModel(BaseSurrealModel):
            model_config = SurrealConfigDict(table_type=TableType.USER)
            id: str | None = None
            email: str
            password: Encrypted

        introspector = ModelIntrospector([SecureModel])
        state = introspector.introspect()

        table = state.tables["SecureModel"]
        assert table.fields["password"].encrypted is True
        assert table.fields["email"].encrypted is False

    def test_introspect_optional_fields(self) -> None:
        """Test introspection of optional fields."""

        class OptionalModel(BaseSurrealModel):
            id: str | None = None
            required_field: str
            optional_field: str | None = None

        introspector = ModelIntrospector([OptionalModel])
        state = introspector.introspect()

        table = state.tables["OptionalModel"]
        assert table.fields["required_field"].nullable is False
        assert table.fields["optional_field"].nullable is True

    def test_introspect_with_defaults(self) -> None:
        """Test introspection of fields with defaults."""

        class DefaultModel(BaseSurrealModel):
            id: str | None = None
            status: str = "active"
            count: int = 0

        introspector = ModelIntrospector([DefaultModel])
        state = introspector.introspect()

        table = state.tables["DefaultModel"]
        assert table.fields["status"].default == "active"
        assert table.fields["count"].default == 0

    def test_introspect_hash_table_schemaless(self) -> None:
        """Test that HASH tables default to SCHEMALESS."""

        class CacheModel(BaseSurrealModel):
            model_config = SurrealConfigDict(table_type=TableType.HASH)
            id: str | None = None
            data: dict

        introspector = ModelIntrospector([CacheModel])
        state = introspector.introspect()

        table = state.tables["CacheModel"]
        assert table.schema_mode == "SCHEMALESS"

    def test_introspect_permissions(self) -> None:
        """Test introspection of table permissions."""

        class ProtectedModel(BaseSurrealModel):
            model_config = SurrealConfigDict(
                permissions={
                    "select": "$auth.id = owner_id",
                    "update": "$auth.id = owner_id",
                }
            )
            id: str | None = None
            owner_id: str
            data: str

        introspector = ModelIntrospector([ProtectedModel])
        state = introspector.introspect()

        table = state.tables["ProtectedModel"]
        assert table.permissions["select"] == "$auth.id = owner_id"
        assert table.permissions["update"] == "$auth.id = owner_id"

    def test_introspect_multiple_models(self) -> None:
        """Test introspection of multiple models."""

        class Model1(BaseSurrealModel):
            id: str | None = None
            field1: str

        class Model2(BaseSurrealModel):
            id: str | None = None
            field2: int

        introspector = ModelIntrospector([Model1, Model2])
        state = introspector.introspect()

        assert len(state.tables) == 2
        assert "Model1" in state.tables
        assert "Model2" in state.tables


class TestIntrospectModelsFunction:
    """Tests for introspect_models convenience function."""

    def test_introspect_models_with_list(self) -> None:
        """Test introspect_models with explicit model list."""

        class ExplicitModel(BaseSurrealModel):
            id: str | None = None
            name: str

        state = introspect_models([ExplicitModel])
        assert "ExplicitModel" in state.tables

    def test_introspect_models_from_registry(self) -> None:
        """Test introspect_models using registered models."""
        clear_model_registry()

        class RegisteredModel(BaseSurrealModel):
            id: str | None = None
            value: str

        # Should pick up from registry
        state = introspect_models()
        assert "RegisteredModel" in state.tables


class TestTypeMapping:
    """Tests for Python to SurrealDB type mapping."""

    def test_basic_types(self) -> None:
        """Test mapping of basic Python types."""

        class TypesModel(BaseSurrealModel):
            id: str | None = None
            string_field: str
            int_field: int
            float_field: float
            bool_field: bool

        introspector = ModelIntrospector([TypesModel])
        state = introspector.introspect()
        table = state.tables["TypesModel"]

        assert table.fields["string_field"].field_type == "string"
        assert table.fields["int_field"].field_type == "int"
        assert table.fields["float_field"].field_type == "float"
        assert table.fields["bool_field"].field_type == "bool"

    def test_container_types(self) -> None:
        """Test mapping of container types."""

        class ContainerModel(BaseSurrealModel):
            id: str | None = None
            list_field: list
            dict_field: dict

        introspector = ModelIntrospector([ContainerModel])
        state = introspector.introspect()
        table = state.tables["ContainerModel"]

        assert table.fields["list_field"].field_type == "array"
        assert table.fields["dict_field"].field_type == "object"
