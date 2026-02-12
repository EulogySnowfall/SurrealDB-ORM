"""Unit tests for surreal_orm.surreal_ql — SurrealQL query builder."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from surreal_orm.surreal_ql import SurrealQL


class TestSurrealQLInit:
    def test_initial_state(self) -> None:
        q = SurrealQL()
        assert q._query == ""
        assert q._variables == {}
        assert q.related_models == []


class TestSurrealQLSelect:
    def test_select_none(self) -> None:
        q = SurrealQL()
        result = q.select(None)
        assert q._query == "SELECT "
        assert result is q  # fluent API

    def test_select_string(self) -> None:
        q = SurrealQL()
        result = q.select("id, name")
        assert q._query == "SELECT id, name"
        assert result is q

    def test_select_list(self) -> None:
        q = SurrealQL()
        result = q.select(["id", "name", "email"])
        assert q._query == "SELECT id, name, email"
        assert result is q


class TestSurrealQLRelated:
    def test_related(self) -> None:
        q = SurrealQL()
        result = q.related("posts")
        assert q._query == "RELATE posts"
        assert result is q

    def test_to_related(self) -> None:
        q = SurrealQL()
        result = q.to_related("users")
        assert q._query == "->users"
        assert result is q

    def test_from_related(self) -> None:
        q = SurrealQL()
        result = q.from_related("users")
        assert q._query == "<-users"
        assert result is q


class TestSurrealQLFromTables:
    def test_from_tables_string(self) -> None:
        q = SurrealQL()
        result = q.from_tables("users")
        assert q._query == "FROM users"
        assert result is q

    def test_from_tables_list(self) -> None:
        q = SurrealQL()
        result = q.from_tables(["users", "posts"])
        assert q._query == "FROM users, posts"
        assert result is q


class TestSurrealQLChaining:
    def test_select_from_chain(self) -> None:
        q = SurrealQL().select(None).from_tables("users")
        assert "SELECT " in q._query
        assert "FROM users" in q._query

    def test_select_fields_from_chain(self) -> None:
        q = SurrealQL().select(["id", "name"]).from_tables("users")
        assert q._query == "SELECT id, nameFROM users"

    def test_relate_chain(self) -> None:
        q = SurrealQL().related("follows").to_related("users")
        assert q._query == "RELATE follows->users"


class TestSurrealQLExecute:
    @pytest.mark.asyncio
    async def test_execute_query(self) -> None:
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.all_records = [{"id": "users:1", "name": "Alice"}]
        mock_client.query.return_value = mock_response

        q = SurrealQL()
        with patch(
            "surreal_orm.surreal_ql.SurrealDBConnectionManager.get_client",
            return_value=mock_client,
        ):
            result = await q._execute_query("SELECT * FROM users;")
            assert result == [{"id": "users:1", "name": "Alice"}]

    @pytest.mark.asyncio
    async def test_run_query_on_client(self) -> None:
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.all_records = [{"id": "1"}]
        mock_client.query.return_value = mock_response

        q = SurrealQL()
        q._variables = {"key": "value"}
        result = await q._run_query_on_client(mock_client, "SELECT * FROM users;")
        assert result == [{"id": "1"}]
        mock_client.query.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_query_with_variables(self) -> None:
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.all_records = []
        mock_client.query.return_value = mock_response

        q = SurrealQL()
        q._variables = {"name": "Alice"}
        await q._run_query_on_client(mock_client, "SELECT * FROM users WHERE name = $name;")
        mock_client.query.assert_called_once()
