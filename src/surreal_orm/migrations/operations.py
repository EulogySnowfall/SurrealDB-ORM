"""
Migration operations for SurrealDB schema changes.

Each operation represents a single schema modification that can be
applied (forwards) or reverted (backwards).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

from ..types import FieldType


def _normalize_field_type(field_type: FieldType | str) -> str:
    """
    Normalize a field type to its string representation.

    Accepts FieldType enum or string. For strings, validates that it's either
    a known FieldType value or a valid generic type (e.g., "array<string>").

    Args:
        field_type: FieldType enum or string type specification

    Returns:
        String representation of the type for SurrealQL

    Raises:
        ValueError: If the string is not a valid SurrealDB type
    """
    if isinstance(field_type, FieldType):
        return field_type.value

    # Check if it's a known base type
    try:
        return FieldType(field_type).value
    except ValueError:
        pass

    # Check if it's a generic type (e.g., "array<string>", "record<users>")
    if "<" in field_type and field_type.endswith(">"):
        base_type = field_type.split("<")[0]
        try:
            FieldType(base_type)
            return field_type  # Valid generic type
        except ValueError:
            pass

    # Check for union types (e.g., "int | null", "option<string>")
    if "|" in field_type:
        return field_type  # Allow union types

    raise ValueError(
        f"Invalid field type: '{field_type}'. "
        f"Must be a FieldType enum value, a valid SurrealDB type string, "
        f"or a generic type like 'array<string>' or 'record<users>'."
    )


@dataclass
class Operation(ABC):
    """
    Base class for all migration operations.

    Operations must implement forwards() and backwards() methods
    that return SurrealQL statements.
    """

    reversible: bool = field(default=True, init=False)

    @abstractmethod
    def forwards(self) -> str:
        """Generate forward SurrealQL statement."""
        ...

    @abstractmethod
    def backwards(self) -> str:
        """Generate rollback SurrealQL statement."""
        ...

    def describe(self) -> str:
        """Human-readable description of the operation."""
        return f"{self.__class__.__name__}"


@dataclass
class CreateTable(Operation):
    """
    Create a new table with optional schema mode and changefeed.

    Example:
        CreateTable(name="users", schema_mode="SCHEMAFULL", changefeed="7d")

    Generates:
        DEFINE TABLE users SCHEMAFULL CHANGEFEED 7d;
    """

    name: str
    schema_mode: str = "SCHEMAFULL"
    table_type: str | None = None
    changefeed: str | None = None
    permissions: dict[str, str] | None = None
    comment: str | None = None

    def forwards(self) -> str:
        parts = [f"DEFINE TABLE {self.name}"]

        if self.schema_mode:
            parts.append(self.schema_mode)

        if self.changefeed:
            parts.append(f"CHANGEFEED {self.changefeed}")

        if self.comment:
            parts.append(f"COMMENT '{self.comment}'")

        sql = " ".join(parts) + ";"

        # Add permissions if specified
        if self.permissions:
            perm_parts = []
            for action, condition in self.permissions.items():
                perm_parts.append(f"FOR {action} WHERE {condition}")
            if perm_parts:
                sql += f"\nDEFINE TABLE {self.name} PERMISSIONS {' '.join(perm_parts)};"

        return sql

    def backwards(self) -> str:
        return f"REMOVE TABLE {self.name};"

    def describe(self) -> str:
        return f"Create table {self.name}"


@dataclass
class DropTable(Operation):
    """
    Drop an existing table.

    Example:
        DropTable(name="users")

    Generates:
        REMOVE TABLE users;
    """

    name: str

    def __post_init__(self) -> None:
        self.reversible = False

    def forwards(self) -> str:
        return f"REMOVE TABLE {self.name};"

    def backwards(self) -> str:
        # Cannot reverse without knowing the original schema
        return ""

    def describe(self) -> str:
        return f"Drop table {self.name}"


@dataclass
class AddField(Operation):
    """
    Add a field to a table.

    Example:
        AddField(
            table="users",
            name="email",
            field_type=FieldType.STRING,  # or "string"
            assertion="is::email($value)"
        )

        # With generic types
        AddField(
            table="users",
            name="tags",
            field_type=FieldType.ARRAY.generic("string"),  # "array<string>"
        )

    Generates:
        DEFINE FIELD email ON users TYPE string ASSERT is::email($value);
    """

    table: str
    name: str
    field_type: FieldType | str
    default: Any = None
    assertion: str | None = None
    encrypted: bool = False
    flexible: bool = False
    readonly: bool = False
    value: str | None = None
    comment: str | None = None

    def __post_init__(self) -> None:
        """Validate field_type on initialization."""
        # Validate the field type (raises ValueError if invalid)
        _normalize_field_type(self.field_type)

    def forwards(self) -> str:
        parts = [f"DEFINE FIELD {self.name} ON {self.table}"]

        if self.flexible:
            parts.append("FLEXIBLE")

        normalized_type = _normalize_field_type(self.field_type)
        parts.append(f"TYPE {normalized_type}")

        # For encrypted fields, use VALUE clause with crypto function
        if self.encrypted:
            parts.append("VALUE crypto::argon2::generate($value)")
        elif self.value:
            parts.append(f"VALUE {self.value}")

        if self.default is not None:
            if isinstance(self.default, str):
                # Check if it's a function call or literal
                if self.default.startswith("time::") or self.default.startswith("rand::"):
                    parts.append(f"DEFAULT {self.default}")
                else:
                    parts.append(f"DEFAULT '{self.default}'")
            elif isinstance(self.default, bool):
                parts.append(f"DEFAULT {str(self.default).lower()}")
            else:
                parts.append(f"DEFAULT {self.default}")

        if self.assertion:
            parts.append(f"ASSERT {self.assertion}")

        if self.readonly:
            parts.append("READONLY")

        if self.comment:
            parts.append(f"COMMENT '{self.comment}'")

        return " ".join(parts) + ";"

    def backwards(self) -> str:
        return f"REMOVE FIELD {self.name} ON {self.table};"

    def describe(self) -> str:
        return f"Add field {self.name} to {self.table}"


@dataclass
class DropField(Operation):
    """
    Remove a field from a table.

    Example:
        DropField(table="users", name="old_field")

    Generates:
        REMOVE FIELD old_field ON users;
    """

    table: str
    name: str

    def __post_init__(self) -> None:
        self.reversible = False

    def forwards(self) -> str:
        return f"REMOVE FIELD {self.name} ON {self.table};"

    def backwards(self) -> str:
        # Cannot reverse without knowing the original field definition
        return ""

    def describe(self) -> str:
        return f"Drop field {self.name} from {self.table}"


@dataclass
class AlterField(Operation):
    """
    Alter an existing field's definition.

    Example:
        AlterField(
            table="users",
            name="email",
            field_type=FieldType.STRING,  # or "string"
            assertion="is::email($value)"
        )

    Generates:
        DEFINE FIELD email ON users TYPE string ASSERT is::email($value);
    """

    table: str
    name: str
    field_type: FieldType | str | None = None
    default: Any = None
    assertion: str | None = None
    encrypted: bool = False
    flexible: bool = False
    readonly: bool = False
    value: str | None = None
    # Store previous definition for rollback
    previous_type: FieldType | str | None = None
    previous_default: Any = None
    previous_assertion: str | None = None

    def __post_init__(self) -> None:
        """Validate field_type and set reversible based on previous state."""
        # Validate field types if provided
        if self.field_type is not None:
            _normalize_field_type(self.field_type)
        if self.previous_type is not None:
            _normalize_field_type(self.previous_type)
        object.__setattr__(self, "reversible", self.previous_type is not None)

    def forwards(self) -> str:
        # DEFINE FIELD is idempotent - it creates or updates
        parts = [f"DEFINE FIELD {self.name} ON {self.table}"]

        if self.flexible:
            parts.append("FLEXIBLE")

        if self.field_type:
            normalized_type = _normalize_field_type(self.field_type)
            parts.append(f"TYPE {normalized_type}")

        if self.encrypted:
            parts.append("VALUE crypto::argon2::generate($value)")
        elif self.value:
            parts.append(f"VALUE {self.value}")

        if self.default is not None:
            if isinstance(self.default, str):
                if self.default.startswith("time::") or self.default.startswith("rand::"):
                    parts.append(f"DEFAULT {self.default}")
                else:
                    parts.append(f"DEFAULT '{self.default}'")
            elif isinstance(self.default, bool):
                parts.append(f"DEFAULT {str(self.default).lower()}")
            else:
                parts.append(f"DEFAULT {self.default}")

        if self.assertion:
            parts.append(f"ASSERT {self.assertion}")

        if self.readonly:
            parts.append("READONLY")

        return " ".join(parts) + ";"

    def backwards(self) -> str:
        if not self.previous_type:
            return ""

        normalized_prev_type = _normalize_field_type(self.previous_type)
        parts = [f"DEFINE FIELD {self.name} ON {self.table} TYPE {normalized_prev_type}"]

        if self.previous_default is not None:
            if isinstance(self.previous_default, str):
                parts.append(f"DEFAULT '{self.previous_default}'")
            else:
                parts.append(f"DEFAULT {self.previous_default}")

        if self.previous_assertion:
            parts.append(f"ASSERT {self.previous_assertion}")

        return " ".join(parts) + ";"

    def describe(self) -> str:
        return f"Alter field {self.name} on {self.table}"


@dataclass
class CreateIndex(Operation):
    """
    Create an index on a table.

    Supports standard, unique, full-text search (BM25), and vector (HNSW) indexes.

    Example:
        # Unique index
        CreateIndex(table="users", name="email_idx", fields=["email"], unique=True)

        # Full-text search index
        CreateIndex(
            table="posts", name="ft_title", fields=["title"],
            search_analyzer="my_analyzer", bm25=True, highlights=True,
        )

        # HNSW vector index
        CreateIndex(
            table="documents", name="vec_idx", fields=["embedding"],
            hnsw=True, dimension=1536, dist="COSINE", vector_type="F32",
        )

    Generates:
        DEFINE INDEX email_idx ON users FIELDS email UNIQUE;
        DEFINE INDEX ft_title ON posts FIELDS title SEARCH ANALYZER my_analyzer BM25 HIGHLIGHTS;
        DEFINE INDEX vec_idx ON documents FIELDS embedding HNSW DIMENSION 1536 DIST COSINE TYPE F32;
    """

    table: str
    name: str
    fields: list[str]
    unique: bool = False
    search_analyzer: str | None = None
    bm25: tuple[float, float] | bool | None = None
    highlights: bool = False
    hnsw: bool = False
    dimension: int | None = None
    dist: str | None = None
    vector_type: str | None = None
    efc: int | None = None
    m: int | None = None
    concurrently: bool = False
    comment: str | None = None

    def forwards(self) -> str:
        fields_str = ", ".join(self.fields)
        parts = [f"DEFINE INDEX {self.name} ON {self.table} FIELDS {fields_str}"]

        if self.unique:
            parts.append("UNIQUE")

        if self.search_analyzer:
            parts.append(f"SEARCH ANALYZER {self.search_analyzer}")

            if self.bm25 is True:
                parts.append("BM25")
            elif isinstance(self.bm25, tuple):
                parts.append(f"BM25({self.bm25[0]},{self.bm25[1]})")

            if self.highlights:
                parts.append("HIGHLIGHTS")

        if self.hnsw:
            parts.append("HNSW")

            if self.dimension is not None:
                parts.append(f"DIMENSION {self.dimension}")

            if self.dist:
                parts.append(f"DIST {self.dist}")

            if self.vector_type:
                parts.append(f"TYPE {self.vector_type}")

            if self.efc is not None:
                parts.append(f"EFC {self.efc}")

            if self.m is not None:
                parts.append(f"M {self.m}")

            if self.concurrently:
                parts.append("CONCURRENTLY")

        if self.comment:
            parts.append(f"COMMENT '{self.comment}'")

        return " ".join(parts) + ";"

    def backwards(self) -> str:
        return f"REMOVE INDEX {self.name} ON {self.table};"

    def describe(self) -> str:
        return f"Create index {self.name} on {self.table}"


@dataclass
class DropIndex(Operation):
    """
    Remove an index from a table.

    Example:
        DropIndex(table="users", name="email_idx")

    Generates:
        REMOVE INDEX email_idx ON users;
    """

    table: str
    name: str

    def __post_init__(self) -> None:
        self.reversible = False

    def forwards(self) -> str:
        return f"REMOVE INDEX {self.name} ON {self.table};"

    def backwards(self) -> str:
        return ""

    def describe(self) -> str:
        return f"Drop index {self.name} from {self.table}"


@dataclass
class DefineAccess(Operation):
    """
    Define access control for authentication (DEFINE ACCESS ... TYPE RECORD).

    Example:
        DefineAccess(
            name="user_auth",
            table="User",
            signup_fields={"email": "$email", "password": "crypto::argon2::generate($password)"},
            signin_where="email = $email AND crypto::argon2::compare(password, $password)"
        )

    Generates:
        DEFINE ACCESS user_auth ON DATABASE TYPE RECORD
            SIGNUP (CREATE User SET email = $email, password = crypto::argon2::generate($password))
            SIGNIN (SELECT * FROM User WHERE email = $email AND crypto::argon2::compare(password, $password))
            DURATION FOR TOKEN 15m, FOR SESSION 12h;
    """

    name: str
    table: str
    signup_fields: dict[str, str]
    signin_where: str
    duration_token: str = "15m"
    duration_session: str = "12h"
    comment: str | None = None

    def forwards(self) -> str:
        signup_sets = ", ".join(f"{field} = {expr}" for field, expr in self.signup_fields.items())

        sql = f"""DEFINE ACCESS {self.name} ON DATABASE TYPE RECORD
    SIGNUP (CREATE {self.table} SET {signup_sets})
    SIGNIN (SELECT * FROM {self.table} WHERE {self.signin_where})
    DURATION FOR TOKEN {self.duration_token}, FOR SESSION {self.duration_session}"""

        if self.comment:
            sql += f"\n    COMMENT '{self.comment}'"

        return sql + ";"

    def backwards(self) -> str:
        return f"REMOVE ACCESS {self.name} ON DATABASE;"

    def describe(self) -> str:
        return f"Define access {self.name} for {self.table}"


@dataclass
class RemoveAccess(Operation):
    """
    Remove an access definition.

    Example:
        RemoveAccess(name="user_auth")

    Generates:
        REMOVE ACCESS user_auth ON DATABASE;
    """

    name: str

    def __post_init__(self) -> None:
        self.reversible = False

    def forwards(self) -> str:
        return f"REMOVE ACCESS {self.name} ON DATABASE;"

    def backwards(self) -> str:
        return ""

    def describe(self) -> str:
        return f"Remove access {self.name}"


@dataclass
class DataMigration(Operation):
    """
    Execute data transformations (UPDATE, DELETE operations on records).

    Used for the 'upgrade' command to transform existing data.

    Example:
        DataMigration(
            forwards_sql="UPDATE User SET status = 'active' WHERE status IS NULL;",
            backwards_sql="UPDATE User SET status = NULL WHERE status = 'active';"
        )

    Or with async functions:
        DataMigration(
            forwards_func=async_migrate_passwords,
            backwards_func=None  # Irreversible
        )
    """

    forwards_sql: str | None = None
    backwards_sql: str | None = None
    forwards_func: Callable[[], Coroutine[Any, Any, None]] | None = None
    backwards_func: Callable[[], Coroutine[Any, Any, None]] | None = None
    description: str = "Data migration"

    def __post_init__(self) -> None:
        if not self.forwards_sql and not self.forwards_func:
            raise ValueError("DataMigration requires either forwards_sql or forwards_func")
        self.reversible = bool(self.backwards_sql or self.backwards_func)

    def forwards(self) -> str:
        return self.forwards_sql or ""

    def backwards(self) -> str:
        return self.backwards_sql or ""

    @property
    def has_func(self) -> bool:
        """Check if this migration uses async functions."""
        return self.forwards_func is not None

    def describe(self) -> str:
        return self.description


@dataclass
class RawSQL(Operation):
    """
    Execute raw SurrealQL statements.

    Use with caution - prefer structured operations when possible.

    Example:
        RawSQL(
            sql="DEFINE EVENT user_created ON TABLE User WHEN $event = 'CREATE' THEN (CREATE log SET action = 'user_created');",
            reverse_sql="REMOVE EVENT user_created ON TABLE User;"
        )
    """

    sql: str
    reverse_sql: str = ""
    description: str = "Raw SQL"

    def __post_init__(self) -> None:
        self.reversible = bool(self.reverse_sql)

    def forwards(self) -> str:
        return self.sql

    def backwards(self) -> str:
        return self.reverse_sql

    def describe(self) -> str:
        return self.description


@dataclass
class DefineAnalyzer(Operation):
    """
    Define a full-text search analyzer.

    Example:
        DefineAnalyzer(
            name="my_analyzer",
            tokenizers=["blank", "class"],
            filters=["lowercase", "snowball(english)"],
        )

    Generates:
        DEFINE ANALYZER my_analyzer TOKENIZERS blank, class FILTERS lowercase, snowball(english);
    """

    name: str
    tokenizers: list[str] = field(default_factory=list)
    filters: list[str] = field(default_factory=list)

    def forwards(self) -> str:
        parts = [f"DEFINE ANALYZER {self.name}"]

        if self.tokenizers:
            parts.append(f"TOKENIZERS {', '.join(self.tokenizers)}")

        if self.filters:
            parts.append(f"FILTERS {', '.join(self.filters)}")

        return " ".join(parts) + ";"

    def backwards(self) -> str:
        return f"REMOVE ANALYZER {self.name};"

    def describe(self) -> str:
        return f"Define analyzer {self.name}"


@dataclass
class RemoveAnalyzer(Operation):
    """
    Remove a full-text search analyzer.

    Example:
        RemoveAnalyzer(name="my_analyzer")

    Generates:
        REMOVE ANALYZER my_analyzer;
    """

    name: str

    def __post_init__(self) -> None:
        self.reversible = False

    def forwards(self) -> str:
        return f"REMOVE ANALYZER {self.name};"

    def backwards(self) -> str:
        return ""

    def describe(self) -> str:
        return f"Remove analyzer {self.name}"
