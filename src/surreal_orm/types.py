"""
Type definitions for SurrealDB ORM.

This module contains enums and type definitions used throughout the ORM
for table types, schema modes, and field types.
"""

from enum import StrEnum


class TableType(StrEnum):
    """
    Table type classification for migration behavior and connection preferences.

    Each table type has specific characteristics:
    - NORMAL: Standard table with default behavior
    - USER: Authentication table with enforced SCHEMAFULL, required password field
    - STREAM: Real-time optimized table with CHANGEFEED enabled, WebSocket preferred
    - HASH: Lookup/cache table, SCHEMALESS by default
    - RELATION: Graph edge table with TYPE RELATION IN/OUT constraints
    - ANY: Table accepting any record type
    """

    NORMAL = "normal"
    USER = "user"
    STREAM = "stream"
    HASH = "hash"
    RELATION = "relation"
    ANY = "any"


class SchemaMode(StrEnum):
    """
    Schema enforcement mode for SurrealDB tables.

    - SCHEMAFULL: Strict schema enforcement, only defined fields allowed
    - SCHEMALESS: Flexible schema, any fields accepted
    """

    SCHEMAFULL = "SCHEMAFULL"
    SCHEMALESS = "SCHEMALESS"


class FieldType(StrEnum):
    """
    SurrealDB field types for schema definitions.

    Maps to SurrealDB's native type system.
    See: https://surrealdb.com/docs/surrealql/datamodel

    Numeric Types:
        - INT: 64-bit signed integer (-9223372036854775808 to 9223372036854775807)
        - FLOAT: 64-bit double-precision floating point
        - DECIMAL: Arbitrary precision decimal (for financial calculations)
        - NUMBER: Auto-detected numeric type (stores using minimal bytes)

    Primitive Types:
        - STRING: Text data
        - BOOL: Boolean true/false
        - DATETIME: RFC 3339 timestamp with timezone
        - DURATION: Time length (e.g., "1h30m", "7d")
        - BYTES: Binary data / byte array
        - UUID: Universal unique identifier

    Collection Types:
        - ARRAY: Ordered collection (can be typed: array<string>)
        - SET: Unique collection (auto-deduplicated)
        - OBJECT: Flexible JSON-like container

    Special Types:
        - ANY: Accepts any value type
        - OPTION: Optional value (can be typed: option<string>)
        - RECORD: Reference to another record (can be typed: record<users>)
        - GEOMETRY: GeoJSON spatial data (point, line, polygon, etc.)
        - REGEX: Compiled regular expression

    Generic Type Syntax:
        For typed collections/references, use the generic() class method:
        - FieldType.ARRAY.generic("string") -> "array<string>"
        - FieldType.RECORD.generic("users") -> "record<users>"
        - FieldType.OPTION.generic("int") -> "option<int>"
        - FieldType.GEOMETRY.generic("point") -> "geometry<point>"
    """

    # Numeric types
    INT = "int"
    FLOAT = "float"
    DECIMAL = "decimal"
    NUMBER = "number"

    # Primitive types
    STRING = "string"
    BOOL = "bool"
    DATETIME = "datetime"
    DURATION = "duration"
    BYTES = "bytes"
    UUID = "uuid"

    # Collection types
    ARRAY = "array"
    SET = "set"
    OBJECT = "object"

    # Special types
    ANY = "any"
    OPTION = "option"
    RECORD = "record"
    GEOMETRY = "geometry"
    REGEX = "regex"

    def generic(self, inner_type: str) -> str:
        """
        Create a generic type string for parameterized types.

        Args:
            inner_type: The inner type parameter (e.g., "string", "users", "point")

        Returns:
            Formatted type string (e.g., "array<string>", "record<users>")

        Examples:
            >>> FieldType.ARRAY.generic("string")
            'array<string>'
            >>> FieldType.RECORD.generic("users")
            'record<users>'
            >>> FieldType.GEOMETRY.generic("point|polygon")
            'geometry<point|polygon>'
        """
        return f"{self.value}<{inner_type}>"

    @classmethod
    def from_python_type(cls, python_type: type) -> "FieldType":
        """
        Map a Python type to a SurrealDB FieldType.

        Args:
            python_type: A Python type (str, int, float, bool, list, dict, bytes)

        Returns:
            The corresponding FieldType

        Raises:
            ValueError: If the type cannot be mapped
        """
        mapping: dict[type, FieldType] = {
            str: cls.STRING,
            int: cls.INT,
            float: cls.FLOAT,
            bool: cls.BOOL,
            list: cls.ARRAY,
            dict: cls.OBJECT,
            bytes: cls.BYTES,
        }
        if python_type in mapping:
            return mapping[python_type]
        raise ValueError(f"Cannot map Python type {python_type} to SurrealDB FieldType")


class EncryptionAlgorithm(StrEnum):
    """
    Supported encryption algorithms for password hashing.

    All algorithms use SurrealDB's built-in crypto functions.
    """

    ARGON2 = "argon2"
    BCRYPT = "bcrypt"
    PBKDF2 = "pbkdf2"
    SCRYPT = "scrypt"


# Type mapping from Python types to SurrealDB types
# Deprecated: Use FieldType.from_python_type() instead
PYTHON_TO_SURREAL_TYPE: dict[type, FieldType] = {
    str: FieldType.STRING,
    int: FieldType.INT,
    float: FieldType.FLOAT,
    bool: FieldType.BOOL,
    list: FieldType.ARRAY,
    dict: FieldType.OBJECT,
    bytes: FieldType.BYTES,
}
