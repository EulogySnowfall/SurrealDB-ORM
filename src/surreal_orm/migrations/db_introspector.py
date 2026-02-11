"""
Database introspector for reverse schema extraction.

Reads the live schema from a SurrealDB database using ``INFO FOR DB``
and ``INFO FOR TABLE`` commands, then parses the returned DEFINE
statements into a ``SchemaState`` that can be compared against the
forward-introspected model state.
"""

from __future__ import annotations

import logging
from typing import Any

from surreal_sdk.connection.http import HTTPConnection

from .define_parser import (
    parse_define_access,
    parse_define_analyzer,
    parse_define_event,
    parse_define_field,
    parse_define_index,
    parse_define_table,
)
from .state import AccessState, AnalyzerState, SchemaState, TableState

logger = logging.getLogger(__name__)


class DatabaseIntrospector:
    """
    Reverse-introspects a SurrealDB database into a ``SchemaState``.

    Queries the database using ``INFO FOR DB`` to discover all tables,
    then ``INFO FOR TABLE <name>`` for each table to extract fields,
    indexes, and access definitions.

    Usage::

        introspector = DatabaseIntrospector()
        state = await introspector.introspect()
        # state.tables contains all discovered tables with fields/indexes

    Or with an explicit connection::

        conn = await SurrealDBConnectionManager.get_client()
        state = await DatabaseIntrospector(connection=conn).introspect()
    """

    def __init__(self, connection: HTTPConnection | None = None) -> None:
        """
        Initialize the database introspector.

        Args:
            connection: HTTP connection to use. If None, resolves lazily
                        via ``SurrealDBConnectionManager.get_client()``.
        """
        self._connection = connection

    async def _get_connection(self) -> HTTPConnection:
        """Resolve the HTTP connection, creating it lazily if needed."""
        if self._connection is not None:
            return self._connection

        from ..connection_manager import SurrealDBConnectionManager

        self._connection = await SurrealDBConnectionManager.get_client()
        return self._connection

    async def introspect(self) -> SchemaState:
        """
        Introspect the entire database and return a SchemaState.

        Executes ``INFO FOR DB`` to discover tables, then introspects
        each table individually.

        Returns:
            SchemaState containing all tables, fields, indexes, and
            access definitions found in the live database.
        """
        conn = await self._get_connection()
        state = SchemaState()

        # Get database-level info (tables, accesses, etc.)
        db_info = await self._query_info(conn, "INFO FOR DB")
        if db_info is None:
            return state

        # Extract table definitions
        tables_info = db_info.get("tables", db_info.get("tb", {}))
        if not isinstance(tables_info, dict):
            return state

        # Extract database-level access definitions
        accesses_info = db_info.get("accesses", db_info.get("ac", {}))

        # Extract analyzer definitions
        analyzers_info = db_info.get("analyzers", db_info.get("az", {}))
        if isinstance(analyzers_info, dict):
            for _az_name, az_define_stmt in analyzers_info.items():
                try:
                    az_props = parse_define_analyzer(az_define_stmt)
                    state.analyzers[az_props["name"]] = AnalyzerState(
                        name=az_props["name"],
                        tokenizers=az_props["tokenizers"],
                        filters=az_props["filters"],
                    )
                except Exception:
                    logger.debug(
                        "Failed to parse analyzer definition, skipping.",
                        exc_info=True,
                    )

        for table_name, table_define_stmt in tables_info.items():
            try:
                table_state = await self._introspect_table(conn, table_name, table_define_stmt)
                state.tables[table_name] = table_state
            except Exception:
                logger.warning(
                    "Failed to introspect table %r, skipping.",
                    table_name,
                    exc_info=True,
                )

        # Attach access definitions to their associated tables
        if isinstance(accesses_info, dict):
            for _access_name, access_define_stmt in accesses_info.items():
                try:
                    self._attach_access(state, access_define_stmt)
                except Exception:
                    logger.debug(
                        "Failed to parse access definition, skipping.",
                        exc_info=True,
                    )

        return state

    async def _introspect_table(
        self,
        conn: HTTPConnection,
        table_name: str,
        table_define_stmt: str,
    ) -> TableState:
        """
        Introspect a single table using its DEFINE statement and
        ``INFO FOR TABLE``.

        Args:
            conn: HTTP connection.
            table_name: Name of the table.
            table_define_stmt: DEFINE TABLE statement string from
                ``INFO FOR DB``.

        Returns:
            Populated TableState.
        """
        # Parse the DEFINE TABLE statement
        table_props = parse_define_table(table_define_stmt)

        table_state = TableState(
            name=table_props["name"],
            schema_mode=table_props["schema_mode"],
            table_type=table_props["table_type"],
            changefeed=table_props["changefeed"],
            permissions=table_props["permissions"],
            view_query=table_props.get("view_query"),
            relation_in=table_props.get("relation_in"),
            relation_out=table_props.get("relation_out"),
            enforced=table_props.get("enforced", False),
        )

        # Get table-level info (fields, indexes, events)
        table_info = await self._query_info(conn, f"INFO FOR TABLE {table_name}")
        if table_info is None:
            return table_state

        # Parse fields
        fields_info = table_info.get("fields", table_info.get("fd", {}))
        if isinstance(fields_info, dict):
            for _field_name, field_define_stmt in fields_info.items():
                try:
                    field_state = parse_define_field(field_define_stmt)
                    table_state.fields[field_state.name] = field_state
                except Exception:
                    logger.debug(
                        "Failed to parse field definition: %s",
                        field_define_stmt,
                        exc_info=True,
                    )

        # Parse indexes
        indexes_info = table_info.get("indexes", table_info.get("ix", {}))
        if isinstance(indexes_info, dict):
            for _index_name, index_define_stmt in indexes_info.items():
                try:
                    index_state = parse_define_index(index_define_stmt)
                    table_state.indexes[index_state.name] = index_state
                except Exception:
                    logger.debug(
                        "Failed to parse index definition: %s",
                        index_define_stmt,
                        exc_info=True,
                    )

        # Parse events
        events_info = table_info.get("events", table_info.get("ev", {}))
        if isinstance(events_info, dict):
            for _event_name, event_define_stmt in events_info.items():
                try:
                    event_state = parse_define_event(event_define_stmt)
                    table_state.events[event_state.name] = event_state
                except Exception:
                    logger.debug(
                        "Failed to parse event definition: %s",
                        event_define_stmt,
                        exc_info=True,
                    )

        return table_state

    def _attach_access(self, state: SchemaState, access_define_stmt: str) -> None:
        """
        Parse an access definition and attach it to the relevant table.

        Args:
            state: The SchemaState being built.
            access_define_stmt: DEFINE ACCESS statement string.
        """
        access_props = parse_define_access(access_define_stmt)
        table_name = access_props["table"]

        if table_name and table_name in state.tables:
            state.tables[table_name].access = AccessState(
                name=access_props["name"],
                table=table_name,
                signup_fields=access_props["signup_fields"],
                signin_where=access_props["signin_where"],
                duration_token=access_props["duration_token"],
                duration_session=access_props["duration_session"],
            )

    async def _query_info(self, conn: HTTPConnection, query: str) -> dict[str, Any] | None:
        """
        Execute an INFO command and return the result dict.

        SurrealDB ``INFO FOR DB`` and ``INFO FOR TABLE`` return a single
        object with keys like ``tables``, ``fields``, ``indexes``, etc.

        Args:
            conn: HTTP connection.
            query: The INFO query string.

        Returns:
            Result dict, or None if the query failed.
        """
        try:
            response = await conn.query(query)
            if response.results and response.results[0].is_ok:
                result = response.results[0].result
                if isinstance(result, dict):
                    return result
        except Exception:
            logger.warning("Failed to execute %r", query, exc_info=True)

        return None


__all__ = ["DatabaseIntrospector"]
