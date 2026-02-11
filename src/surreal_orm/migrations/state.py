"""
Schema state tracking and diffing for migrations.

This module provides classes to represent the current schema state
and compute the differences between two states to generate migrations.
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .operations import CreateIndex, Operation


@dataclass
class FieldState:
    """
    Represents the state of a single field in a table.

    Attributes:
        name: Field name
        field_type: SurrealDB type (string, int, etc.)
        nullable: Whether the field can be null
        default: Default value if any
        assertion: Validation assertion
        encrypted: Whether the field is encrypted
        flexible: Whether the field accepts additional types
        readonly: Whether the field is read-only
        value: VALUE clause for computed fields
    """

    name: str
    field_type: str
    nullable: bool = True
    default: Any = None
    assertion: str | None = None
    encrypted: bool = False
    flexible: bool = False
    readonly: bool = False
    value: str | None = None

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, FieldState):
            return False
        return (
            self.name == other.name
            and self.field_type == other.field_type
            and self.nullable == other.nullable
            and self.default == other.default
            and self.assertion == other.assertion
            and self.encrypted == other.encrypted
            and self.flexible == other.flexible
            and self.readonly == other.readonly
            and self.value == other.value
        )

    def has_changed(self, other: "FieldState") -> bool:
        """Check if field definition has changed from other."""
        return self != other


@dataclass
class IndexState:
    """
    Represents the state of an index on a table.

    Attributes:
        name: Index name
        fields: List of field names in the index
        unique: Whether the index enforces uniqueness
        search_analyzer: Full-text search analyzer if any
        bm25: BM25 parameters as ``(k1, b)`` tuple, ``True`` for defaults, or None
        highlights: Whether FTS highlighting is enabled
        hnsw: Whether this is an HNSW vector index
        dimension: Vector dimension for HNSW indexes
        dist: Distance metric (COSINE, EUCLIDEAN, etc.)
        vector_type: Storage type (F32, F64, I16, etc.)
        efc: HNSW build-time ef parameter
        m: HNSW max connections per node
        concurrently: Whether to build the index non-blocking
    """

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

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, IndexState):
            return False
        return (
            self.name == other.name
            and self.fields == other.fields
            and self.unique == other.unique
            and self.search_analyzer == other.search_analyzer
            and self.bm25 == other.bm25
            and self.highlights == other.highlights
            and self.hnsw == other.hnsw
            and self.dimension == other.dimension
            and self.dist == other.dist
            and self.vector_type == other.vector_type
            and self.efc == other.efc
            and self.m == other.m
            and self.concurrently == other.concurrently
        )


@dataclass
class AccessState:
    """
    Represents the state of an access definition for authentication.

    Attributes:
        name: Access name
        table: Associated table
        signup_fields: Fields set during signup
        signin_where: WHERE clause for signin
        duration_token: Token duration
        duration_session: Session duration
    """

    name: str
    table: str
    signup_fields: dict[str, str]
    signin_where: str
    duration_token: str = "15m"
    duration_session: str = "12h"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, AccessState):
            return False
        return (
            self.name == other.name
            and self.table == other.table
            and self.signup_fields == other.signup_fields
            and self.signin_where == other.signin_where
            and self.duration_token == other.duration_token
            and self.duration_session == other.duration_session
        )


@dataclass
class AnalyzerState:
    """
    Represents the state of a full-text search analyzer.

    Attributes:
        name: Analyzer name
        tokenizers: List of tokenizer names (e.g., ``["blank", "class"]``)
        filters: List of filter names (e.g., ``["lowercase", "snowball(english)"]``)
    """

    name: str
    tokenizers: list[str] = field(default_factory=list)
    filters: list[str] = field(default_factory=list)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, AnalyzerState):
            return False
        return self.name == other.name and self.tokenizers == other.tokenizers and self.filters == other.filters


@dataclass
class EventState:
    """
    Represents the state of a server-side event (trigger) on a table.

    Attributes:
        name: Event name
        table: Table the event is defined on
        when: SurrealQL condition (e.g., ``"$event = 'CREATE'"``)
        then: SurrealQL action block
    """

    name: str
    table: str
    when: str
    then: str

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, EventState):
            return False
        return self.name == other.name and self.table == other.table and self.when == other.when and self.then == other.then


@dataclass
class TableState:
    """
    Represents the complete state of a table.

    Attributes:
        name: Table name
        schema_mode: SCHEMAFULL or SCHEMALESS
        table_type: Table type classification (normal, user, stream, hash, relation, any)
        fields: Dict of field name to FieldState
        indexes: Dict of index name to IndexState
        events: Dict of event name to EventState
        changefeed: Changefeed duration if enabled
        permissions: Dict of action to WHERE condition
        access: Access definition if this is a USER table
        view_query: AS SELECT ... clause for materialized views
        relation_in: IN table(s) for TYPE RELATION (pipe-separated if multiple)
        relation_out: OUT table(s) for TYPE RELATION (pipe-separated if multiple)
        enforced: Whether the relation constraint is enforced
    """

    name: str
    schema_mode: str = "SCHEMAFULL"
    table_type: str = "normal"
    fields: dict[str, FieldState] = field(default_factory=dict)
    indexes: dict[str, IndexState] = field(default_factory=dict)
    events: dict[str, "EventState"] = field(default_factory=dict)
    changefeed: str | None = None
    permissions: dict[str, str] = field(default_factory=dict)
    access: AccessState | None = None
    view_query: str | None = None
    relation_in: str | None = None
    relation_out: str | None = None
    enforced: bool = False

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, TableState):
            return False
        return (
            self.name == other.name
            and self.schema_mode == other.schema_mode
            and self.table_type == other.table_type
            and self.fields == other.fields
            and self.indexes == other.indexes
            and self.events == other.events
            and self.changefeed == other.changefeed
            and self.permissions == other.permissions
            and self.access == other.access
            and self.view_query == other.view_query
            and self.relation_in == other.relation_in
            and self.relation_out == other.relation_out
            and self.enforced == other.enforced
        )


@dataclass
class SchemaState:
    """
    Represents the complete database schema state.

    This class tracks all tables and their definitions, and can compute
    the operations needed to transform one state into another.

    Attributes:
        tables: Dict of table name to TableState
        applied_migrations: List of already applied migration names
    """

    tables: dict[str, TableState] = field(default_factory=dict)
    applied_migrations: list[str] = field(default_factory=list)
    analyzers: dict[str, "AnalyzerState"] = field(default_factory=dict)

    def diff(self, target: "SchemaState") -> list["Operation"]:
        """
        Compute operations needed to transform this state into target state.

        Args:
            target: The desired schema state

        Returns:
            List of operations to apply (CreateTable, AddField, etc.)
        """
        from .operations import (
            AddField,
            AlterField,
            CreateTable,
            DefineAccess,
            DefineAnalyzer,
            DefineEvent,
            DropField,
            DropIndex,
            DropTable,
            RemoveAccess,
            RemoveAnalyzer,
            RemoveEvent,
        )

        operations: list[Operation] = []

        # ── Analyzers to create/update first (indexes may reference them) ──
        for analyzer_name, target_analyzer in target.analyzers.items():
            if analyzer_name not in self.analyzers or self.analyzers[analyzer_name] != target_analyzer:
                operations.append(
                    DefineAnalyzer(
                        name=target_analyzer.name,
                        tokenizers=target_analyzer.tokenizers,
                        filters=target_analyzer.filters,
                    )
                )

        # Collect analyzers to remove — these are deferred until after all
        # index operations so that we don't remove an analyzer still
        # referenced by an existing index that hasn't been dropped yet.
        deferred_remove_analyzers: list[RemoveAnalyzer] = []
        for analyzer_name in self.analyzers:
            if analyzer_name not in target.analyzers:
                deferred_remove_analyzers.append(RemoveAnalyzer(name=analyzer_name))

        # Tables to create (in target but not in self)
        for table_name, target_table in target.tables.items():
            if table_name not in self.tables:
                # Create the table
                operations.append(
                    CreateTable(
                        name=table_name,
                        schema_mode=target_table.schema_mode,
                        table_type=target_table.table_type,
                        changefeed=target_table.changefeed,
                        permissions=target_table.permissions or None,
                        view_query=target_table.view_query,
                        relation_in=target_table.relation_in,
                        relation_out=target_table.relation_out,
                        enforced=target_table.enforced,
                    )
                )
                # Add all fields
                for field_name, field_state in target_table.fields.items():
                    operations.append(
                        AddField(
                            table=table_name,
                            name=field_name,
                            field_type=field_state.field_type,
                            default=field_state.default,
                            assertion=field_state.assertion,
                            encrypted=field_state.encrypted,
                            flexible=field_state.flexible,
                            readonly=field_state.readonly,
                            value=field_state.value,
                        )
                    )
                # Add all indexes
                for index_name, index_state in target_table.indexes.items():
                    operations.append(self._create_index_from_state(table_name, index_state))
                # Add all events
                for event_name, event_state in target_table.events.items():
                    operations.append(
                        DefineEvent(
                            name=event_state.name,
                            table=table_name,
                            when=event_state.when,
                            then=event_state.then,
                        )
                    )
                # Add access definition if present
                if target_table.access:
                    operations.append(
                        DefineAccess(
                            name=target_table.access.name,
                            table=table_name,
                            signup_fields=target_table.access.signup_fields,
                            signin_where=target_table.access.signin_where,
                            duration_token=target_table.access.duration_token,
                            duration_session=target_table.access.duration_session,
                        )
                    )

        # Tables to drop (in self but not in target)
        for table_name in self.tables:
            if table_name not in target.tables:
                operations.append(DropTable(name=table_name))

        # Tables to modify (in both)
        for table_name, target_table in target.tables.items():
            if table_name in self.tables:
                current_table = self.tables[table_name]

                # Check if table definition changed
                if (
                    current_table.schema_mode != target_table.schema_mode
                    or current_table.changefeed != target_table.changefeed
                    or current_table.permissions != target_table.permissions
                    or current_table.view_query != target_table.view_query
                    or current_table.relation_in != target_table.relation_in
                    or current_table.relation_out != target_table.relation_out
                    or current_table.enforced != target_table.enforced
                ):
                    # Recreate table definition (DEFINE TABLE is idempotent)
                    operations.append(
                        CreateTable(
                            name=table_name,
                            schema_mode=target_table.schema_mode,
                            table_type=target_table.table_type,
                            changefeed=target_table.changefeed,
                            permissions=target_table.permissions or None,
                            view_query=target_table.view_query,
                            relation_in=target_table.relation_in,
                            relation_out=target_table.relation_out,
                            enforced=target_table.enforced,
                        )
                    )

                # Fields to add
                for field_name, field_state in target_table.fields.items():
                    if field_name not in current_table.fields:
                        operations.append(
                            AddField(
                                table=table_name,
                                name=field_name,
                                field_type=field_state.field_type,
                                default=field_state.default,
                                assertion=field_state.assertion,
                                encrypted=field_state.encrypted,
                                flexible=field_state.flexible,
                                readonly=field_state.readonly,
                                value=field_state.value,
                            )
                        )
                    elif current_table.fields[field_name] != field_state:
                        # Field changed
                        current_field = current_table.fields[field_name]
                        operations.append(
                            AlterField(
                                table=table_name,
                                name=field_name,
                                field_type=field_state.field_type,
                                default=field_state.default,
                                assertion=field_state.assertion,
                                encrypted=field_state.encrypted,
                                flexible=field_state.flexible,
                                readonly=field_state.readonly,
                                value=field_state.value,
                                previous_type=current_field.field_type,
                                previous_default=current_field.default,
                                previous_assertion=current_field.assertion,
                            )
                        )

                # Fields to drop
                for field_name in current_table.fields:
                    if field_name not in target_table.fields:
                        operations.append(DropField(table=table_name, name=field_name))

                # Indexes to add
                for index_name, index_state in target_table.indexes.items():
                    if index_name not in current_table.indexes:
                        operations.append(self._create_index_from_state(table_name, index_state))
                    elif current_table.indexes[index_name] != index_state:
                        # Index changed - drop and recreate
                        operations.append(DropIndex(table=table_name, name=index_name))
                        operations.append(self._create_index_from_state(table_name, index_state))

                # Indexes to drop
                for index_name in current_table.indexes:
                    if index_name not in target_table.indexes:
                        operations.append(DropIndex(table=table_name, name=index_name))

                # Access definition changes
                if target_table.access and not current_table.access:
                    # Add access
                    operations.append(
                        DefineAccess(
                            name=target_table.access.name,
                            table=table_name,
                            signup_fields=target_table.access.signup_fields,
                            signin_where=target_table.access.signin_where,
                            duration_token=target_table.access.duration_token,
                            duration_session=target_table.access.duration_session,
                        )
                    )
                elif current_table.access and not target_table.access:
                    # Remove access
                    operations.append(RemoveAccess(name=current_table.access.name))
                elif target_table.access and current_table.access and target_table.access != current_table.access:
                    # Update access (remove old, add new)
                    operations.append(RemoveAccess(name=current_table.access.name))
                    operations.append(
                        DefineAccess(
                            name=target_table.access.name,
                            table=table_name,
                            signup_fields=target_table.access.signup_fields,
                            signin_where=target_table.access.signin_where,
                            duration_token=target_table.access.duration_token,
                            duration_session=target_table.access.duration_session,
                        )
                    )

                # Event changes
                for event_name, event_state in target_table.events.items():
                    if event_name not in current_table.events:
                        # New event
                        operations.append(
                            DefineEvent(
                                name=event_state.name,
                                table=table_name,
                                when=event_state.when,
                                then=event_state.then,
                            )
                        )
                    elif current_table.events[event_name] != event_state:
                        # Event changed — remove old, define new
                        operations.append(RemoveEvent(name=event_name, table=table_name))
                        operations.append(
                            DefineEvent(
                                name=event_state.name,
                                table=table_name,
                                when=event_state.when,
                                then=event_state.then,
                            )
                        )

                # Events to drop
                for event_name in current_table.events:
                    if event_name not in target_table.events:
                        operations.append(RemoveEvent(name=event_name, table=table_name))

        # ── Deferred analyzer removals (after all index ops) ────────
        operations.extend(deferred_remove_analyzers)

        return operations

    @staticmethod
    def _create_index_from_state(table_name: str, idx: IndexState) -> "CreateIndex":
        """Build a ``CreateIndex`` operation from an ``IndexState``."""
        from .operations import CreateIndex

        return CreateIndex(
            table=table_name,
            name=idx.name,
            fields=idx.fields,
            unique=idx.unique,
            search_analyzer=idx.search_analyzer,
            bm25=idx.bm25,
            highlights=idx.highlights,
            hnsw=idx.hnsw,
            dimension=idx.dimension,
            dist=idx.dist,
            vector_type=idx.vector_type,
            efc=idx.efc,
            m=idx.m,
            concurrently=idx.concurrently,
        )

    def clone(self) -> "SchemaState":
        """Create a deep copy of this schema state."""
        import copy

        return copy.deepcopy(self)

    def add_table(self, table: TableState) -> None:
        """Add a table to the schema."""
        self.tables[table.name] = table

    def remove_table(self, name: str) -> None:
        """Remove a table from the schema."""
        if name in self.tables:
            del self.tables[name]

    def get_table(self, name: str) -> TableState | None:
        """Get a table by name."""
        return self.tables.get(name)

    def __repr__(self) -> str:
        return f"SchemaState(tables={list(self.tables.keys())}, migrations={len(self.applied_migrations)})"
