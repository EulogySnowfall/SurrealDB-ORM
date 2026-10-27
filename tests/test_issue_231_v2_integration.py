"""Integration tests for #231: datetimes from SurrealDB 2.x reach callers as datetimes.

Over CBOR RPC the 2.x server sends every datetime as tag 12 holding a compact
``[seconds, nanoseconds]`` pair, never the ISO 8601 string the SDK decoder
handled. A ``datetime`` model field was rescued by the ORM's own parsing, but
anything else (a ``raw_query()`` result, a datetime nested in an object field)
came back as a list of two ints.

Run with: pytest -m integration tests/test_issue_231_v2_integration.py
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime
from typing import Any

import pytest

from src.surreal_orm import BaseSurrealModel, SurrealConfigDict, SurrealDBConnectionManager
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "test_issue_231_v2"
TABLE = "i231_events"

AT_TEXT = "2025-02-13T05:00:00.123456789Z"
#: ``AT_TEXT`` at Python's precision: the last three digits are dropped.
AT = datetime(2025, 2, 13, 5, 0, 0, 123_456, tzinfo=UTC)
#: SurrealDB floors the seconds, so this one arrives as ``[-1, 500000000]``.
BEFORE_EPOCH_TEXT = "1969-12-31T23:59:59.5Z"
BEFORE_EPOCH = datetime(1969, 12, 31, 23, 59, 59, 500_000, tzinfo=UTC)


class I231Event(BaseSurrealModel):
    """A typed datetime field next to an untyped object field."""

    model_config = SurrealConfigDict(table_name=TABLE)

    id: str | None = None
    name: str = ""
    at: datetime | None = None
    day: date | None = None
    meta: dict[str, Any] | None = None


@pytest.fixture(scope="module", autouse=True)
async def setup_connection() -> AsyncGenerator[None, Any]:
    """Point the ORM at a database of its own for this module."""
    SurrealDBConnectionManager.set_connection(
        SURREALDB_URL,
        SURREALDB_USER,
        SURREALDB_PASS,
        SURREALDB_NAMESPACE,
        SURREALDB_DATABASE,
    )
    yield
    await SurrealDBConnectionManager.close_connection()
    await SurrealDBConnectionManager.unset_connection()


@pytest.fixture(autouse=True)
async def events() -> AsyncGenerator[None, Any]:
    """``one``: a datetime at the top level and others nested in an object; ``dated``: a date."""
    client = await SurrealDBConnectionManager.get_client()
    await client.query(f"REMOVE TABLE IF EXISTS {TABLE};")
    response = await client.query(
        f"CREATE {TABLE}:one SET name = 'one', at = <datetime>'{AT_TEXT}', "
        f"meta = {{ seen: <datetime>'{AT_TEXT}', history: [<datetime>'{AT_TEXT}', "
        f"<datetime>'{BEFORE_EPOCH_TEXT}'], deeper: {{ at: <datetime>'{AT_TEXT}' }} }};"
        f"CREATE {TABLE}:dated SET name = 'dated', day = <datetime>'2026-02-01T00:00:00Z';"
    )
    assert response.is_ok, response.raw
    yield
    await client.query(f"REMOVE TABLE IF EXISTS {TABLE};")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_raw_query_returns_a_top_level_datetime() -> None:
    rows = await I231Event.raw_query(f"SELECT VALUE at FROM {TABLE}:one;")

    assert rows == [AT]
    assert isinstance(rows[0], datetime)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_raw_query_returns_datetimes_in_a_row_and_nested_in_an_object_field() -> None:
    rows = await I231Event.raw_query(f"SELECT * FROM {TABLE}:one;")

    assert len(rows) == 1
    row = rows[0]
    assert row["at"] == AT
    assert row["meta"]["seen"] == AT
    assert row["meta"]["history"] == [AT, BEFORE_EPOCH]
    assert row["meta"]["deeper"]["at"] == AT


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_bound_datetime_round_trips_through_raw_query() -> None:
    """The value written by the encoder's ISO form comes back from the compact form."""
    rows = await I231Event.raw_query("RETURN [$when, { when: $when }];", {"when": AT})

    assert rows == [AT, {"when": AT}]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_model_loads_datetimes_nested_in_its_dict_field() -> None:
    """Only the typed field was decoded before; the dict field held lists of ints."""
    event = await I231Event.objects().get("one")

    assert event.at == AT
    assert event.meta is not None
    assert event.meta["seen"] == AT
    assert event.meta["history"] == [AT, BEFORE_EPOCH]
    assert event.meta["deeper"]["at"] == AT


@pytest.mark.integration
@pytest.mark.asyncio
async def test_saving_a_loaded_dict_field_keeps_its_datetimes_datetimes() -> None:
    """The lists of ints used to be written back as they were: arrays, not datetimes."""
    event = await I231Event.objects().get("one")
    assert event.meta is not None
    event.meta = {**event.meta, "extra": 1}
    await event.save()

    rows = await I231Event.raw_query(
        f"SELECT VALUE [type::is::datetime(meta.seen), type::is::datetime(meta.deeper.at)] FROM {TABLE}:one;"
    )

    assert rows == [[True, True]]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_date_field_loads_a_stored_midnight_datetime() -> None:
    """SurrealDB has no date type; a ``date`` field could not validate a list of two ints."""
    event = await I231Event.objects().get("dated")

    assert event.day == date(2026, 2, 1)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_datetime_python_cannot_hold_does_not_fail_the_response() -> None:
    """SurrealDB accepts year -1; Python stops at year 1.

    That one value stays as the server sent it, and every other datetime in the
    same response still decodes.
    """
    rows = await I231Event.raw_query("RETURN [<datetime>'-0001-01-01T00:00:00Z', $when];", {"when": AT})

    assert rows == [[-62_198_755_200, 0], AT]
