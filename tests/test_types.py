"""
Unit tests for types.py - Enums and type definitions.
"""

import pytest

from src.surreal_orm.types import (
    EncryptionAlgorithm,
    FieldType,
    PYTHON_TO_SURREAL_TYPE,
    SchemaMode,
    TableType,
)


class TestTableType:
    """Tests for TableType enum."""

    def test_table_type_values(self) -> None:
        """Test that TableType has expected values."""
        assert TableType.NORMAL == "normal"
        assert TableType.USER == "user"
        assert TableType.STREAM == "stream"
        assert TableType.HASH == "hash"

    def test_table_type_is_string(self) -> None:
        """Test that TableType values are strings."""
        assert isinstance(TableType.NORMAL, str)
        assert isinstance(TableType.USER, str)

    def test_table_type_from_string(self) -> None:
        """Test creating TableType from string."""
        assert TableType("normal") == TableType.NORMAL
        assert TableType("user") == TableType.USER

    def test_table_type_invalid(self) -> None:
        """Test that invalid values raise ValueError."""
        with pytest.raises(ValueError):
            TableType("invalid")


class TestSchemaMode:
    """Tests for SchemaMode enum."""

    def test_schema_mode_values(self) -> None:
        """Test that SchemaMode has expected values."""
        assert SchemaMode.SCHEMAFULL == "SCHEMAFULL"
        assert SchemaMode.SCHEMALESS == "SCHEMALESS"

    def test_schema_mode_uppercase(self) -> None:
        """Test that SchemaMode values are uppercase for SurrealQL."""
        assert SchemaMode.SCHEMAFULL.isupper()
        assert SchemaMode.SCHEMALESS.isupper()


class TestFieldType:
    """Tests for FieldType enum."""

    def test_field_type_basic_values(self) -> None:
        """Test basic FieldType values."""
        assert FieldType.STRING == "string"
        assert FieldType.INT == "int"
        assert FieldType.FLOAT == "float"
        assert FieldType.BOOL == "bool"

    def test_field_type_advanced_values(self) -> None:
        """Test advanced FieldType values."""
        assert FieldType.DATETIME == "datetime"
        assert FieldType.ARRAY == "array"
        assert FieldType.OBJECT == "object"
        assert FieldType.RECORD == "record"

    def test_field_type_any(self) -> None:
        """Test that ANY type exists for flexible fields."""
        assert FieldType.ANY == "any"


class TestEncryptionAlgorithm:
    """Tests for EncryptionAlgorithm enum."""

    def test_encryption_algorithm_values(self) -> None:
        """Test supported encryption algorithms."""
        assert EncryptionAlgorithm.ARGON2 == "argon2"
        assert EncryptionAlgorithm.BCRYPT == "bcrypt"
        assert EncryptionAlgorithm.PBKDF2 == "pbkdf2"
        assert EncryptionAlgorithm.SCRYPT == "scrypt"

    def test_default_algorithm_is_argon2(self) -> None:
        """Test that argon2 is the recommended default."""
        # Argon2 is the most modern and secure
        assert EncryptionAlgorithm.ARGON2.value == "argon2"


class TestPythonToSurrealTypeMapping:
    """Tests for Python to SurrealDB type mapping."""

    def test_basic_type_mapping(self) -> None:
        """Test mapping of basic Python types."""
        assert PYTHON_TO_SURREAL_TYPE[str] == FieldType.STRING
        assert PYTHON_TO_SURREAL_TYPE[int] == FieldType.INT
        assert PYTHON_TO_SURREAL_TYPE[float] == FieldType.FLOAT
        assert PYTHON_TO_SURREAL_TYPE[bool] == FieldType.BOOL

    def test_container_type_mapping(self) -> None:
        """Test mapping of container types."""
        assert PYTHON_TO_SURREAL_TYPE[list] == FieldType.ARRAY
        assert PYTHON_TO_SURREAL_TYPE[dict] == FieldType.OBJECT

    def test_bytes_type_mapping(self) -> None:
        """Test mapping of bytes type."""
        assert PYTHON_TO_SURREAL_TYPE[bytes] == FieldType.BYTES
