"""Unit tests for #438 (v2): ``HTTPTransaction.insert()`` given a one-shot iterable.

``insert()`` checks the keys of every record before it queues anything (#382),
so it walks ``data`` twice. A generator is exhausted by the first walk, so the
second one queued nothing and the records were dropped without an error.

The records are materialised into a list once now; a list or a dict renders
exactly as before.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.surreal_sdk.transaction import HTTPTransaction
from src.surreal_sdk.types import QueryResponse


@pytest.fixture
def connection() -> MagicMock:
    conn = MagicMock()
    conn.query = AsyncMock(return_value=QueryResponse(results=[], raw=[]))
    return conn


async def _open(conn: MagicMock) -> HTTPTransaction:
    tx = HTTPTransaction(conn)
    await tx._begin()
    return tx


def _records(*records: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield from records


class TestInsertGenerator:
    async def test_every_record_of_a_generator_is_queued(self, connection: MagicMock) -> None:
        tx = await _open(connection)
        await tx.insert("i438_nodes", _records({"a": 1}, {"a": 2, "b": 3}))  # type: ignore[arg-type]
        assert [(s.sql, s.vars) for s in tx._statements] == [
            ("CREATE i438_nodes SET a = $r0_a;", {"r0_a": 1}),
            ("CREATE i438_nodes SET a = $r1_a, b = $r1_b;", {"r1_a": 2, "r1_b": 3}),
        ]

    async def test_bad_key_in_a_later_record_queues_nothing(self, connection: MagicMock) -> None:
        tx = await _open(connection)
        with pytest.raises(ValueError, match="'a-b'"):
            await tx.insert("i438_nodes", _records({"v": 1}, {"v": 2}, {"a-b": 3}))  # type: ignore[arg-type]
        assert tx._statements == []

    async def test_list_and_dict_unchanged(self, connection: MagicMock) -> None:
        tx = await _open(connection)
        await tx.insert("i438_nodes", {"a": 1})
        await tx.insert("i438_nodes", [{"a": 2}])
        assert [(s.sql, s.vars) for s in tx._statements] == [
            ("CREATE i438_nodes SET a = $r0_a;", {"r0_a": 1}),
            ("CREATE i438_nodes SET a = $r0_a;", {"r0_a": 2}),
        ]
