"""Unit tests for surreal_orm.migrations.db_introspector — DatabaseIntrospector."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from surreal_orm.migrations.db_introspector import DatabaseIntrospector
from surreal_orm.migrations.state import SchemaState, TableState


class TestDatabaseIntrospectorInit:
    def test_init_without_connection(self) -> None:
        introspector = DatabaseIntrospector()
        assert introspector._connection is None

    def test_init_with_connection(self) -> None:
        mock_conn = MagicMock()
        introspector = DatabaseIntrospector(connection=mock_conn)
        assert introspector._connection is mock_conn


class TestGetConnection:
    @pytest.mark.asyncio
    async def test_returns_cached_connection(self) -> None:
        mock_conn = MagicMock()
        introspector = DatabaseIntrospector(connection=mock_conn)
        result = await introspector._get_connection()
        assert result is mock_conn

    @pytest.mark.asyncio
    async def test_resolves_lazy_connection(self) -> None:
        mock_conn = AsyncMock()
        introspector = DatabaseIntrospector()
        with patch(
            "surreal_orm.connection_manager.SurrealDBConnectionManager.get_client",
            new_callable=AsyncMock,
            return_value=mock_conn,
        ):
            result = await introspector._get_connection()
            assert result is mock_conn


class TestAttachAccess:
    def test_attach_access_to_existing_table(self) -> None:
        state = SchemaState()
        state.tables["users"] = TableState(name="users")

        introspector = DatabaseIntrospector()
        access_stmt = "DEFINE ACCESS user_auth ON users TYPE RECORD"

        with patch("surreal_orm.migrations.db_introspector.parse_define_access") as mock_parse:
            mock_parse.return_value = {
                "name": "user_auth",
                "table": "users",
                "signup_fields": ["email", "password"],
                "signin_where": None,
                "duration_token": "30d",
                "duration_session": None,
            }
            introspector._attach_access(state, access_stmt)

        assert state.tables["users"].access is not None
        assert state.tables["users"].access.name == "user_auth"
        assert state.tables["users"].access.signup_fields == ["email", "password"]

    def test_attach_access_unknown_table(self) -> None:
        state = SchemaState()
        introspector = DatabaseIntrospector()

        with patch("surreal_orm.migrations.db_introspector.parse_define_access") as mock_parse:
            mock_parse.return_value = {
                "name": "auth",
                "table": "nonexistent",
                "signup_fields": [],
                "signin_where": None,
                "duration_token": None,
                "duration_session": None,
            }
            # Should not crash
            introspector._attach_access(state, "DEFINE ACCESS ...")
            # No table to attach to, so nothing changes
            assert "nonexistent" not in state.tables

    def test_attach_access_none_table(self) -> None:
        state = SchemaState()
        introspector = DatabaseIntrospector()

        with patch("surreal_orm.migrations.db_introspector.parse_define_access") as mock_parse:
            mock_parse.return_value = {
                "name": "auth",
                "table": None,
                "signup_fields": [],
                "signin_where": None,
                "duration_token": None,
                "duration_session": None,
            }
            introspector._attach_access(state, "DEFINE ACCESS ...")


class TestQueryInfo:
    @pytest.mark.asyncio
    async def test_query_info_success(self) -> None:
        mock_conn = AsyncMock()
        mock_result = MagicMock()
        mock_result.is_ok = True
        mock_result.result = {"tables": {"users": "DEFINE TABLE users"}}
        mock_response = MagicMock()
        mock_response.results = [mock_result]
        mock_conn.query.return_value = mock_response

        introspector = DatabaseIntrospector(connection=mock_conn)
        result = await introspector._query_info(mock_conn, "INFO FOR DB")
        assert result == {"tables": {"users": "DEFINE TABLE users"}}

    @pytest.mark.asyncio
    async def test_query_info_empty_results(self) -> None:
        mock_conn = AsyncMock()
        mock_response = MagicMock()
        mock_response.results = []
        mock_conn.query.return_value = mock_response

        introspector = DatabaseIntrospector(connection=mock_conn)
        result = await introspector._query_info(mock_conn, "INFO FOR DB")
        assert result is None

    @pytest.mark.asyncio
    async def test_query_info_error_result(self) -> None:
        mock_conn = AsyncMock()
        mock_result = MagicMock()
        mock_result.is_ok = False
        mock_response = MagicMock()
        mock_response.results = [mock_result]
        mock_conn.query.return_value = mock_response

        introspector = DatabaseIntrospector(connection=mock_conn)
        result = await introspector._query_info(mock_conn, "INFO FOR DB")
        assert result is None

    @pytest.mark.asyncio
    async def test_query_info_non_dict_result(self) -> None:
        mock_conn = AsyncMock()
        mock_result = MagicMock()
        mock_result.is_ok = True
        mock_result.result = "not a dict"
        mock_response = MagicMock()
        mock_response.results = [mock_result]
        mock_conn.query.return_value = mock_response

        introspector = DatabaseIntrospector(connection=mock_conn)
        result = await introspector._query_info(mock_conn, "INFO FOR DB")
        assert result is None

    @pytest.mark.asyncio
    async def test_query_info_exception(self) -> None:
        mock_conn = AsyncMock()
        mock_conn.query.side_effect = Exception("connection failed")

        introspector = DatabaseIntrospector(connection=mock_conn)
        result = await introspector._query_info(mock_conn, "INFO FOR DB")
        assert result is None


class TestIntrospect:
    @pytest.mark.asyncio
    async def test_introspect_empty_db(self) -> None:
        mock_conn = AsyncMock()

        # INFO FOR DB returns None
        mock_response = MagicMock()
        mock_response.results = []
        mock_conn.query.return_value = mock_response

        introspector = DatabaseIntrospector(connection=mock_conn)
        state = await introspector.introspect()
        assert isinstance(state, SchemaState)
        assert len(state.tables) == 0

    @pytest.mark.asyncio
    async def test_introspect_non_dict_tables(self) -> None:
        """INFO FOR DB returns tables as non-dict (e.g. string)."""
        mock_conn = AsyncMock()

        mock_result = MagicMock()
        mock_result.is_ok = True
        mock_result.result = {"tables": "not_a_dict"}
        mock_response = MagicMock()
        mock_response.results = [mock_result]
        mock_conn.query.return_value = mock_response

        introspector = DatabaseIntrospector(connection=mock_conn)
        state = await introspector.introspect()
        assert len(state.tables) == 0
