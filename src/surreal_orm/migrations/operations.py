"""
Migration operations for SurrealDB schema changes.

Each operation represents a single schema modification that can be
applied (forwards) or reverted (backwards).
"""

from abc import ABC, abstractmethod
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..types import FieldType

if TYPE_CHECKING:
    from .state import FieldState, TableState


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


def _apply_nullable(field_type: str, nullable: bool) -> str:
    """
    Wrap a field type in ``option<>`` when the column is optional.

    ``FieldState.nullable`` stopped at the operation boundary: nothing wrapped
    the type, so an optional model field reached the database as a required
    column and generated migrations silently lost optionality (#170).

    The wrap is skipped when the type already carries its own optionality —
    ``option<T>``, or a union with a ``none``/``null`` member — so a hand-written
    migration is never double-wrapped. Detection mirrors ``parse_define_field``,
    the other side of the round-trip; a union *without* such a member
    (``int | string``) is not optional and is wrapped like any other type.

    Args:
        field_type: The normalized SurrealDB type
        nullable: Whether the column accepts NONE

    Returns:
        The type, wrapped in ``option<>`` when that is both needed and absent
    """
    if not nullable:
        return field_type
    lowered = field_type.lower()
    if lowered.startswith("option<") or "| null" in lowered or lowered.startswith("none |"):
        return field_type
    return f"option<{field_type}>"


#: The attributes a ``DEFINE TABLE`` statement carries. ``CreateTable``,
#: ``AlterTable`` and the renderer are all driven off this one tuple: a clause
#: added here reaches the forward statement, the rollback and the diff at once,
#: instead of being spelled out in four places and forgotten in one of them.
TABLE_DEFINITION_FIELDS = (
    "schema_mode",
    "table_type",
    "changefeed",
    "permissions",
    "comment",
    "view_query",
    "relation_in",
    "relation_out",
    "enforced",
)

#: The only table types SurrealDB itself accepts. NORMAL, USER, STREAM and HASH
#: are ORM-level concepts.
_SURQL_TABLE_TYPES = ("relation", "any")


def _surql_table_type(table_type: str | None) -> str | None:
    """
    Reduce an ORM table type to one SurrealDB will parse.

    ``TableType`` carries ORM-only classifications — USER marks an auth table,
    STREAM and HASH are hints — but ``DEFINE TABLE ... TYPE`` accepts only
    ``NORMAL``, ``RELATION`` and ``ANY``. Passing USER through produced
    ``Parse error: Unexpected token `USER``` and made any change to such a table
    unmigratable.

    Args:
        table_type: The table type as the model or the database reports it

    Returns:
        The type when SurrealDB understands it, otherwise None
    """
    if table_type and table_type.lower() in _SURQL_TABLE_TYPES:
        return table_type
    return None


def _render_permission_rule(action: str, rule: str) -> str:
    """
    Render one ``FOR <action>`` group of a PERMISSIONS clause.

    ``FULL`` and ``NONE`` are keywords, not expressions: SurrealDB accepts
    ``FOR select WHERE FULL`` but evaluates ``FULL`` as a field reference, gets
    ``NONE`` and denies — turning a world-readable table into an unreadable one
    with no error.

    Args:
        action: The action the rule governs (select, create, update, delete)
        rule: A condition expression, or the keyword FULL / NONE

    Returns:
        The rendered group
    """
    keyword = str(rule).strip().upper()
    if keyword in ("FULL", "NONE"):
        return f"FOR {action} {keyword}"
    return f"FOR {action} WHERE {rule}"


def _effective_permissions(permissions: dict[str, str] | None) -> dict[str, str]:
    """
    Drop permission entries that only restate SurrealDB's default.

    A table defined without a ``PERMISSIONS`` clause is reported back by
    ``INFO FOR DB`` as ``{"select": "NONE", "create": "NONE", ...}`` — those are
    the server's defaults, not a configured value. Comparing the raw dicts made a
    database-read state differ from a model state that configures nothing, so the
    diff re-emitted ``CreateTable`` for every table on every run (#171).

    Normalising both sides also makes an explicit ``NONE`` equal to omitting the
    clause, which is what it means, and keeps a generated rollback from carrying
    the server's three default ``NONE`` entries as if they had been configured.

    Args:
        permissions: The permissions mapping, or None

    Returns:
        The mapping without its default-valued entries
    """
    if not permissions:
        return {}
    return {action: rule for action, rule in permissions.items() if str(rule).strip().upper() != "NONE"}


def _relation_tables(value: "str | list[str] | None") -> str | None:
    """
    Render an IN/OUT target list as SurrealDB spells it.

    ``SurrealConfigDict`` accepts a list — ``relation_out=["blog_post", "book"]``
    is the documented form — but the clause takes pipe-separated names, so a
    list reached the DDL as ``OUT ['blog_post', 'book']``.

    Args:
        value: One table name, several, or None

    Returns:
        The pipe-separated form, or None
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return " | ".join(str(v) for v in value)


def _render_define_table(
    name: str,
    *,
    overwrite: bool,
    schema_mode: str | None = None,
    table_type: str | None = None,
    changefeed: str | None = None,
    permissions: dict[str, str] | None = None,
    comment: str | None = None,
    view_query: str | None = None,
    relation_in: str | None = None,
    relation_out: str | None = None,
    enforced: bool = False,
) -> str:
    """
    Render one ``DEFINE TABLE`` statement.

    Shared by ``CreateTable`` and both directions of ``AlterTable`` so a
    rollback cannot drift from the definition it restores.

    Args:
        name: Table name
        overwrite: Emit ``DEFINE TABLE OVERWRITE``, redefining an existing table
        schema_mode: SCHEMAFULL or SCHEMALESS
        table_type: ORM table type; sanitised to what SurrealDB accepts
        changefeed: CHANGEFEED duration
        permissions: Action → rule mapping
        comment: COMMENT text
        view_query: AS SELECT ... body for a materialized view
        relation_in: IN table(s) for TYPE RELATION
        relation_out: OUT table(s) for TYPE RELATION
        enforced: Whether the relation constraint is enforced

    Returns:
        The complete statement, semicolon included
    """
    keyword = "DEFINE TABLE OVERWRITE" if overwrite else "DEFINE TABLE"

    # Materialized view — different syntax
    if view_query:
        return f"{keyword} {name} AS ({view_query});"

    parts = [f"{keyword} {name}"]

    surql_type = _surql_table_type(table_type)
    if surql_type and surql_type.upper() == "RELATION":
        type_clause = "TYPE RELATION"
        if relation_in:
            type_clause += f" IN {_relation_tables(relation_in)}"
        if relation_out:
            type_clause += f" OUT {_relation_tables(relation_out)}"
        if enforced:
            type_clause += " ENFORCED"
        parts.append(type_clause)
    elif surql_type:
        parts.append(f"TYPE {surql_type.upper()}")

    if schema_mode:
        parts.append(schema_mode)

    if changefeed:
        parts.append(f"CHANGEFEED {changefeed}")

    if comment:
        parts.append(f"COMMENT '{comment.replace(chr(39), chr(39) * 2)}'")

    # PERMISSIONS is a clause of DEFINE TABLE. It used to be emitted as a
    # second `DEFINE TABLE ... PERMISSIONS ...`, which SurrealDB rejects once
    # the table exists ("The table 'x' already exists"), so declared table
    # permissions never reached the database at all.
    if permissions:
        parts.append("PERMISSIONS " + " ".join(_render_permission_rule(a, r) for a, r in permissions.items()))

    return " ".join(parts) + ";"


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

    Supports materialized views (``view_query``) and TYPE RELATION
    constraints (``relation_in``, ``relation_out``, ``enforced``).

    Redefining a table that already exists is :class:`AlterTable`, not this
    operation: a plain ``DEFINE TABLE`` is rejected once the table exists, and
    rolling back a creation means dropping the table.

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
    view_query: str | None = None
    relation_in: str | None = None
    relation_out: str | None = None
    enforced: bool = False

    @classmethod
    def from_table_state(cls, state: "TableState") -> "CreateTable":
        """
        Build the operation that creates *state*.

        The single place that maps a ``TableState`` onto the definition fields,
        so a new clause cannot reach the diff and miss table creation.

        Args:
            state: The table definition the models describe

        Returns:
            A ``CreateTable`` carrying every definition field of *state*
        """
        return cls(
            name=state.name,
            permissions=_effective_permissions(state.permissions) or None,
            **{f: getattr(state, f) for f in TABLE_DEFINITION_FIELDS if f != "permissions"},
        )

    def forwards(self) -> str:
        return _render_define_table(
            self.name,
            overwrite=False,
            **{f: getattr(self, f) for f in TABLE_DEFINITION_FIELDS},
        )

    def backwards(self) -> str:
        return f"REMOVE TABLE {self.name};"

    def describe(self) -> str:
        return f"Create table {self.name}"


@dataclass
class AlterTable(Operation):
    """
    Redefine a table that already exists.

    Emitted by the diff when a table's definition changed. A plain
    ``DEFINE TABLE`` is not idempotent — the server rejects it once the table
    exists ("The table 'x' already exists") — so the statement is
    ``DEFINE TABLE OVERWRITE``, which preserves the table's fields, indexes,
    events and rows.

    Rolling back restores the definition that was replaced rather than dropping
    the table, which is why the previous values travel with the operation. An
    ``AlterTable`` built without them is **not** reversible: a bare
    ``DEFINE TABLE OVERWRITE t;`` resets the table to
    ``TYPE ANY SCHEMALESS PERMISSIONS NONE``, silently discarding the very
    definition a rollback is supposed to restore.

    Example:
        AlterTable(name="users", schema_mode="SCHEMAFULL",
                   previous_schema_mode="SCHEMALESS")

    Generates:
        DEFINE TABLE OVERWRITE users SCHEMAFULL;
    """

    name: str
    schema_mode: str | None = None
    table_type: str | None = None
    changefeed: str | None = None
    permissions: dict[str, str] | None = None
    comment: str | None = None
    view_query: str | None = None
    relation_in: str | None = None
    relation_out: str | None = None
    enforced: bool = False
    # Previous definition, for a non-destructive rollback
    previous_schema_mode: str | None = None
    previous_table_type: str | None = None
    previous_changefeed: str | None = None
    previous_permissions: dict[str, str] | None = None
    previous_comment: str | None = None
    previous_view_query: str | None = None
    previous_relation_in: str | None = None
    previous_relation_out: str | None = None
    previous_enforced: bool = False

    def __post_init__(self) -> None:
        # Mirrors AlterField: without the previous definition there is nothing
        # to restore, and emitting a bare OVERWRITE would reset the table.
        self.reversible = self.previous_schema_mode is not None

    @classmethod
    def from_table_state(cls, state: "TableState") -> "AlterTable":
        """
        Build the operation that redefines a table to match *state*.

        For callers that apply a model's schema directly — ``define_table()`` —
        rather than migrating between two known states. The result carries no
        previous definition and is therefore not reversible.

        Args:
            state: The table definition the model describes

        Returns:
            An ``AlterTable`` carrying only the target definition
        """
        return cls(
            name=state.name,
            permissions=_effective_permissions(state.permissions) or None,
            **{f: getattr(state, f) for f in TABLE_DEFINITION_FIELDS if f != "permissions"},
        )

    @classmethod
    def from_table_states(cls, current: "TableState", target: "TableState") -> "AlterTable":
        """
        Build the operation that redefines *current* as *target*.

        Both permission mappings go through :func:`_effective_permissions`, so
        the server's default ``NONE`` entries do not travel into generated
        migration files as though they had been configured.

        Args:
            current: The table definition the database holds
            target: The definition the models describe

        Returns:
            An ``AlterTable`` carrying both definitions
        """
        return cls(
            name=target.name,
            permissions=_effective_permissions(target.permissions) or None,
            previous_permissions=_effective_permissions(current.permissions) or None,
            **{f: getattr(target, f) for f in TABLE_DEFINITION_FIELDS if f != "permissions"},
            **{f"previous_{f}": getattr(current, f) for f in TABLE_DEFINITION_FIELDS if f != "permissions"},
        )

    def forwards(self) -> str:
        return _render_define_table(
            self.name,
            overwrite=True,
            **{f: getattr(self, f) for f in TABLE_DEFINITION_FIELDS},
        )

    def backwards(self) -> str:
        if not self.reversible:
            raise ValueError(
                f"AlterTable({self.name!r}) carries no previous definition, so it cannot be "
                f"rolled back: a bare DEFINE TABLE OVERWRITE would reset the table to "
                f"TYPE ANY SCHEMALESS PERMISSIONS NONE. Build it with from_table_states()."
            )
        return _render_define_table(
            self.name,
            overwrite=True,
            **{f: getattr(self, f"previous_{f}") for f in TABLE_DEFINITION_FIELDS},
        )

    def describe(self) -> str:
        return f"Alter table {self.name}"


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
    nullable: bool = False
    #: Emit ``DEFINE FIELD OVERWRITE``. A migration adds a field that is not
    #: there yet, so the plain form is right there and a second run *should*
    #: fail. ``define_table()`` applies a model's whole schema and has to be
    #: callable twice, so it opts in.
    overwrite: bool = False

    def __post_init__(self) -> None:
        """Validate field_type on initialization."""
        # Validate the field type (raises ValueError if invalid)
        _normalize_field_type(self.field_type)

    @classmethod
    def from_field_state(cls, table: str, state: "FieldState") -> "AddField":
        """
        Build the operation that defines *state* on *table*.

        Every attribute is wired here and only here. Enumerating them by hand at
        each call site is what let ``nullable`` go missing from every diff branch
        and from ``define_table()`` at once.

        Args:
            table: Name of the table the field belongs to
            state: The desired field state

        Returns:
            An ``AddField`` carrying the whole field state
        """
        return cls(
            table=table,
            name=state.name,
            field_type=state.field_type,
            nullable=state.nullable,
            default=state.default,
            assertion=state.assertion,
            encrypted=state.encrypted,
            flexible=state.flexible,
            readonly=state.readonly,
            value=state.value,
        )

    def forwards(self) -> str:
        keyword = "DEFINE FIELD OVERWRITE" if self.overwrite else "DEFINE FIELD"
        parts = [f"{keyword} {self.name} ON {self.table}"]

        if self.flexible:
            parts.append("FLEXIBLE")

        normalized_type = _apply_nullable(_normalize_field_type(self.field_type), self.nullable)
        parts.append(f"TYPE {normalized_type}")

        # For encrypted fields, use VALUE clause with crypto function
        if self.encrypted:
            parts.append("VALUE crypto::argon2::generate($value)")
        elif self.value:
            parts.append(f"VALUE {self.value}")

        if self.default is not None:
            if isinstance(self.default, str):
                # Check if it's a function call or variable reference
                if "::" in self.default or self.default.startswith("$"):
                    # Server-side function call or variable reference
                    parts.append(f"DEFAULT {self.default}")
                else:
                    parts.append(f"DEFAULT '{self.default.replace(chr(39), chr(39) + chr(39))}'")
            elif isinstance(self.default, bool):
                parts.append(f"DEFAULT {str(self.default).lower()}")
            else:
                parts.append(f"DEFAULT {self.default}")

        if self.assertion:
            parts.append(f"ASSERT {self.assertion}")

        if self.readonly:
            parts.append("READONLY")

        if self.comment:
            escaped_comment = self.comment.replace("'", "''")
            parts.append(f"COMMENT '{escaped_comment}'")

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
    nullable: bool = False
    previous_type: FieldType | str | None = None
    previous_default: Any = None
    previous_assertion: str | None = None
    previous_flexible: bool = False
    previous_readonly: bool = False
    previous_nullable: bool = False

    @classmethod
    def from_field_states(cls, table: str, current: "FieldState", target: "FieldState") -> "AlterField":
        """
        Build the operation that moves *current* to *target* on *table*.

        The counterpart to :meth:`AddField.from_field_state`, wiring the target
        state forwards and the current state into the ``previous_*`` attributes
        ``backwards()`` needs. Hand-copying these is what left ``FLEXIBLE`` and
        ``READONLY`` off every generated rollback.

        Args:
            table: Name of the table the field belongs to
            current: The field state the database is in
            target: The field state the models describe

        Returns:
            An ``AlterField`` carrying both states in full
        """
        return cls(
            table=table,
            name=target.name,
            field_type=target.field_type,
            nullable=target.nullable,
            default=target.default,
            assertion=target.assertion,
            encrypted=target.encrypted,
            flexible=target.flexible,
            readonly=target.readonly,
            value=target.value,
            previous_type=current.field_type,
            previous_nullable=current.nullable,
            previous_default=current.default,
            previous_assertion=current.assertion,
            previous_flexible=current.flexible,
            previous_readonly=current.readonly,
        )

    def __post_init__(self) -> None:
        """Validate field_type and set reversible based on previous state."""
        # Validate field types if provided
        if self.field_type is not None:
            _normalize_field_type(self.field_type)
        if self.previous_type is not None:
            _normalize_field_type(self.previous_type)
        object.__setattr__(self, "reversible", self.previous_type is not None)

    def forwards(self) -> str:
        # OVERWRITE is required, not optional (#162). A plain `DEFINE FIELD` over
        # an existing field does NOT update it — SurrealDB answers
        # `The field 'x' already exists` and leaves the definition untouched, so
        # every AlterField was a no-op. OVERWRITE is create-or-redefine, so it is
        # still correct when the field happens to be missing.
        parts = [f"DEFINE FIELD OVERWRITE {self.name} ON {self.table}"]

        if self.flexible:
            parts.append("FLEXIBLE")

        if self.field_type:
            normalized_type = _apply_nullable(_normalize_field_type(self.field_type), self.nullable)
            parts.append(f"TYPE {normalized_type}")

        if self.encrypted:
            parts.append("VALUE crypto::argon2::generate($value)")
        elif self.value:
            parts.append(f"VALUE {self.value}")

        if self.default is not None:
            if isinstance(self.default, str):
                # Check if it's a function call or variable reference
                if "::" in self.default or self.default.startswith("$"):
                    # Server-side function call or variable reference
                    parts.append(f"DEFAULT {self.default}")
                else:
                    parts.append(f"DEFAULT '{self.default.replace(chr(39), chr(39) + chr(39))}'")
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

        normalized_prev_type = _apply_nullable(_normalize_field_type(self.previous_type), self.previous_nullable)
        # OVERWRITE for the same reason as forwards(): a rollback re-defining the
        # previous type is otherwise a silent no-op too.
        parts = [f"DEFINE FIELD OVERWRITE {self.name} ON {self.table}"]

        if self.previous_flexible:
            parts.append("FLEXIBLE")

        parts.append(f"TYPE {normalized_prev_type}")

        if self.previous_default is not None:
            if isinstance(self.previous_default, str):
                parts.append(f"DEFAULT '{self.previous_default}'")
            else:
                parts.append(f"DEFAULT {self.previous_default}")

        if self.previous_assertion:
            parts.append(f"ASSERT {self.previous_assertion}")

        if self.previous_readonly:
            parts.append("READONLY")

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
            escaped_comment = self.comment.replace("'", "''")
            parts.append(f"COMMENT '{escaped_comment}'")

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
            escaped_comment = self.comment.replace("'", "''")
            sql += f"\n    COMMENT '{escaped_comment}'"

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


@dataclass
class DefineEvent(Operation):
    """
    Define a server-side event (trigger) on a table.

    Example:
        DefineEvent(
            name="audit_create",
            table="users",
            when="$event = 'CREATE'",
            then="CREATE audit_log SET table = 'users', action = 'create', at = time::now()",
        )

    Generates:
        DEFINE EVENT audit_create ON users WHEN $event = 'CREATE'
            THEN (CREATE audit_log SET table = 'users', action = 'create', at = time::now());
    """

    name: str
    table: str
    when: str
    then: str
    comment: str | None = None

    def forwards(self) -> str:
        parts = [f"DEFINE EVENT {self.name} ON {self.table}"]
        parts.append(f"WHEN {self.when}")
        parts.append(f"THEN ({self.then})")

        if self.comment:
            escaped_comment = self.comment.replace("'", "''")
            parts.append(f"COMMENT '{escaped_comment}'")

        return " ".join(parts) + ";"

    def backwards(self) -> str:
        return f"REMOVE EVENT {self.name} ON {self.table};"

    def describe(self) -> str:
        return f"Define event {self.name} on {self.table}"


@dataclass
class RemoveEvent(Operation):
    """
    Remove a server-side event from a table.

    Example:
        RemoveEvent(name="audit_create", table="users")

    Generates:
        REMOVE EVENT audit_create ON users;
    """

    name: str
    table: str

    def __post_init__(self) -> None:
        self.reversible = False

    def forwards(self) -> str:
        return f"REMOVE EVENT {self.name} ON {self.table};"

    def backwards(self) -> str:
        return ""

    def describe(self) -> str:
        return f"Remove event {self.name} from {self.table}"
