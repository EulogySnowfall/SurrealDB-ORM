"""Tests for typed function call() method."""

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel

from surreal_sdk.connection.base import BaseSurrealConnection


class VoteResultPydantic(BaseModel):
    """Pydantic model for test."""

    success: bool
    new_count: int
    total_votes: int


@dataclass
class VoteResultDataclass:
    """Dataclass for test."""

    success: bool
    new_count: int
    total_votes: int


class TestTypedCall:
    """Tests for the call() method with typed returns."""

    @pytest.fixture
    def mock_connection(self) -> MagicMock:
        """Create a mock connection."""
        conn = MagicMock(spec=BaseSurrealConnection)
        conn._convert_to_type = BaseSurrealConnection._convert_to_type.__get__(conn)
        return conn

    def test_convert_to_pydantic_model(self, mock_connection: MagicMock) -> None:
        """Test converting dict to Pydantic model."""
        value = {"success": True, "new_count": 5, "total_votes": 10}

        result = mock_connection._convert_to_type(value, VoteResultPydantic)

        assert isinstance(result, VoteResultPydantic)
        assert result.success is True
        assert result.new_count == 5
        assert result.total_votes == 10

    def test_convert_to_dataclass(self, mock_connection: MagicMock) -> None:
        """Test converting dict to dataclass."""
        value = {"success": True, "new_count": 5, "total_votes": 10}

        result = mock_connection._convert_to_type(value, VoteResultDataclass)

        assert isinstance(result, VoteResultDataclass)
        assert result.success is True
        assert result.new_count == 5
        assert result.total_votes == 10

    def test_convert_to_simple_type(self, mock_connection: MagicMock) -> None:
        """Test converting to simple types."""
        assert mock_connection._convert_to_type("42", int) == 42
        assert mock_connection._convert_to_type(42, str) == "42"
        assert mock_connection._convert_to_type("3.14", float) == 3.14

    def test_convert_passthrough_on_failure(self, mock_connection: MagicMock) -> None:
        """Test that value is returned as-is if conversion fails."""
        value = {"complex": "object"}

        # Can't convert dict to int
        result = mock_connection._convert_to_type(value, int)

        assert result == value


class TestCallFunctionNameNormalization:
    """Tests for function name normalization in call()."""

    @pytest.mark.asyncio
    async def test_call_adds_fn_prefix(self) -> None:
        """Test that fn:: prefix is added when missing."""
        # Create a mock connection with async query
        conn = MagicMock(spec=BaseSurrealConnection)
        conn.query = AsyncMock(return_value=MagicMock(first_result=MagicMock(result={"success": True})))

        # Use the actual call implementation
        conn.call = BaseSurrealConnection.call.__get__(conn, BaseSurrealConnection)

        await conn.call("cast_vote", params={"user": "alice"})

        # Verify the query was called with fn:: prefix
        conn.query.assert_called_once()
        sql = conn.query.call_args[0][0]
        assert "fn::cast_vote" in sql

    @pytest.mark.asyncio
    async def test_call_keeps_existing_prefix(self) -> None:
        """Test that existing fn:: prefix is kept."""
        conn = MagicMock(spec=BaseSurrealConnection)
        conn.query = AsyncMock(return_value=MagicMock(first_result=MagicMock(result={"success": True})))
        conn.call = BaseSurrealConnection.call.__get__(conn, BaseSurrealConnection)

        await conn.call("fn::cast_vote", params={"user": "alice"})

        sql = conn.query.call_args[0][0]
        # Should not have double prefix
        assert "fn::fn::" not in sql
        assert "fn::cast_vote" in sql

    @pytest.mark.asyncio
    async def test_call_keeps_other_namespaces(self) -> None:
        """Test that other namespaces like math:: are kept."""
        conn = MagicMock(spec=BaseSurrealConnection)
        conn.query = AsyncMock(return_value=MagicMock(first_result=MagicMock(result=4.0)))
        conn.call = BaseSurrealConnection.call.__get__(conn, BaseSurrealConnection)

        await conn.call("math::sqrt", params={"value": 16})

        sql = conn.query.call_args[0][0]
        assert "math::sqrt" in sql
        assert "fn::math" not in sql
