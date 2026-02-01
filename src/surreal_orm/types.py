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
    """

    NORMAL = "normal"
    USER = "user"
    STREAM = "stream"
    HASH = "hash"


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
    """

    STRING = "string"
    INT = "int"
    FLOAT = "float"
    BOOL = "bool"
    DATETIME = "datetime"
    DURATION = "duration"
    DECIMAL = "decimal"
    ARRAY = "array"
    OBJECT = "object"
    RECORD = "record"
    GEOMETRY = "geometry"
    ANY = "any"
    OPTION = "option"
    BYTES = "bytes"
    UUID = "uuid"


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
PYTHON_TO_SURREAL_TYPE: dict[type, FieldType] = {
    str: FieldType.STRING,
    int: FieldType.INT,
    float: FieldType.FLOAT,
    bool: FieldType.BOOL,
    list: FieldType.ARRAY,
    dict: FieldType.OBJECT,
    bytes: FieldType.BYTES,
}
