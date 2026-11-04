"""Unit tests for #486 (v2 line): follow-ups to the 0.21.33 / 0.21.35 fixes.

* ``is_conflict_error()`` reads the server's retryable sentence only at the end
  of a message: a failed ``ASSERT`` echoes the rejected value, which may
  contain that sentence.
* The change-feed reader and ``HTTPTransaction`` share one definition of the
  messages a failed transaction reports for its cancelled statements.
* Annotation arguments validated at construction are validated again when
  reassigned.

The server messages are copied from SurrealDB 2.7.0, the pinned server.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.surreal_orm.aggregations import Avg, Max, Min, Sum
from src.surreal_orm.geo import GeoDistance
from src.surreal_orm.search import SearchHighlight, SearchScore
from src.surreal_orm.utils import retry_on_conflict
from surreal_sdk.exceptions import ChangeFeedError, QueryError, TransactionConflictError
from surreal_sdk.streaming.change_feed import ChangeFeedStream
from surreal_sdk.types import QueryResponse

RETRYABLE = "This transaction can be retried"
NOT_EXECUTED = "The query was not executed due to a failed transaction"
#: 2.7 reports a write conflict this way on a plain statement and on every
#: statement of a BEGIN/COMMIT block that fails at commit time.
WRITE_CONFLICT = f"{NOT_EXECUTED}. Failed to commit transaction due to a read or write conflict. {RETRYABLE}"
#: The aborted COMMIT of servers that report one (HTTPTransaction handles it).
COMMIT_WRITE_CONFLICT = f"Cannot COMMIT: Failed to commit transaction due to a read or write conflict. {RETRYABLE}"
#: A failed ASSERT whose rejected value holds the retryable sentence (2.7.0).
ASSERT_VALUE_RETRYABLE = (
    "Found 'Write conflict. This transaction can be retried' for field `note`, with record `i486_t:a`, "
    "but field must conform to: string::len($value) < 5"
)


class TestConflictSentenceAtTheEnd:
    @pytest.mark.parametrize(
        "message",
        [
            WRITE_CONFLICT,
            COMMIT_WRITE_CONFLICT,
            f"Transaction commit failed: {WRITE_CONFLICT}",
            f"Transaction conflict persisted after 3 retries in work: {WRITE_CONFLICT}",
            f"{WRITE_CONFLICT}.",
            f"{WRITE_CONFLICT}\n",
            WRITE_CONFLICT.upper(),
        ],
    )
    def test_retryable_messages_still_match(self, message: str) -> None:
        assert TransactionConflictError.is_conflict_error(Exception(message))

    def test_assert_with_the_sentence_in_the_value_is_not_a_conflict(self) -> None:
        assert not TransactionConflictError.is_conflict_error(Exception(ASSERT_VALUE_RETRYABLE))

    @pytest.mark.asyncio
    async def test_retry_on_conflict_does_not_retry_that_assert(self) -> None:
        calls = 0

        @retry_on_conflict(max_retries=3, base_delay=0.001)
        async def work() -> None:
            nonlocal calls
            calls += 1
            raise QueryError(ASSERT_VALUE_RETRYABLE)

        with pytest.raises(QueryError) as excinfo:
            await work()
        assert not isinstance(excinfo.value, TransactionConflictError)
        assert calls == 1


class _CommitConflictConnection:
    """Answers every query with a transaction failed by a conflict reported on its COMMIT."""

    def __init__(self, commit_error: str) -> None:
        self.commit_error = commit_error

    async def query(self, sql: str, vars: dict[str, Any] | None = None) -> QueryResponse:
        return QueryResponse.from_rpc_result(
            [
                {"status": "OK", "result": None},
                {"status": "ERR", "result": NOT_EXECUTED},
                {"status": "ERR", "result": NOT_EXECUTED},
                {"status": "ERR", "result": self.commit_error},
            ]
        )


class TestChangeFeedReportsACommitConflict:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("commit_error", [WRITE_CONFLICT, COMMIT_WRITE_CONFLICT])
    async def test_commit_conflict_is_the_reported_cause(self, commit_error: str) -> None:
        feed = ChangeFeedStream(_CommitConflictConnection(commit_error), "i486_orders")  # type: ignore[arg-type]

        with pytest.raises(ChangeFeedError) as excinfo:
            await feed.get_changes(since=0)

        assert commit_error in str(excinfo.value)


BAD_FIELD = "amount) AS x, (SELECT * FROM secrets"


def _assign_then_render(annotation: Any, attribute: str, value: Any) -> None:
    """Assigning *value* must raise, and leave the annotation rendering as before.

    Only ``setattr()`` sits inside ``pytest.raises``: a check that ran at
    render time alone would let a bad value be stored and still pass.
    """
    before = annotation.to_surql("alias")
    with pytest.raises((TypeError, ValueError)):
        setattr(annotation, attribute, value)
    assert annotation.to_surql("alias") == before


class TestReassignedAnnotationArgumentsAreChecked:
    @pytest.mark.parametrize("ref", ["0) AS x, (1", -1, True, 1.5])
    def test_search_score_ref(self, ref: Any) -> None:
        _assign_then_render(SearchScore(0), "ref", ref)

    @pytest.mark.parametrize("ref", ["0) AS x, (1", -1, True, 1.5])
    def test_search_highlight_ref(self, ref: Any) -> None:
        _assign_then_render(SearchHighlight("<b>", "</b>", 0), "ref", ref)

    @pytest.mark.parametrize("cls", [Sum, Avg, Min, Max])
    def test_aggregation_field(self, cls: type[Sum]) -> None:
        _assign_then_render(cls("amount"), "field", BAD_FIELD)

    def test_geo_distance_field(self) -> None:
        _assign_then_render(GeoDistance("location", (-73.98, 40.74)), "field", BAD_FIELD)

    @pytest.mark.parametrize("point", [("0", "0) AS x, (1"), (True, 0), (float("nan"), 0), (1, 2, 3), "12"])
    def test_geo_distance_point(self, point: Any) -> None:
        _assign_then_render(GeoDistance("location", (-73.98, 40.74)), "point", point)

    def test_same_errors_as_the_constructor(self) -> None:
        with pytest.raises(TypeError):
            SearchScore(0).ref = "1"  # type: ignore[assignment]
        with pytest.raises(ValueError):
            SearchHighlight().ref = -1
        with pytest.raises(ValueError):
            Max("price").field = BAD_FIELD
        with pytest.raises(TypeError):
            GeoDistance("location", (1, 2)).point = (True, 0)  # type: ignore[assignment]
        with pytest.raises(ValueError):
            GeoDistance("location", (1, 2)).point = (float("inf"), 0)

    def test_failed_assignment_keeps_the_previous_value(self) -> None:
        total = Sum("amount")
        with pytest.raises(ValueError):
            total.field = BAD_FIELD
        assert total.to_surql("t") == "math::sum(amount) AS t"

    def test_valid_reassignment_renders(self) -> None:
        score = SearchScore(0)
        score.ref = 2
        assert score.to_surql("s") == "search::score(2) AS s"
        highlight = SearchHighlight("<b>", "</b>", 0)
        highlight.ref = 1
        assert highlight.to_surql("h") == "search::highlight('<b>', '</b>', 1) AS h"
        for cls, function in ((Sum, "math::sum"), (Avg, "math::mean"), (Min, "math::min"), (Max, "math::max")):
            aggregation = cls("amount")
            aggregation.field = "order.total"
            assert aggregation.field == "order.total"
            assert aggregation.to_surql("t") == f"{function}(order.total) AS t"
        distance = GeoDistance("location", (1, 2))
        distance.field = "shop.location"
        distance.point = [3.5, 4]  # type: ignore[assignment]
        assert distance.point == (3.5, 4)
        assert isinstance(distance.point, tuple)
        assert distance.to_surql("d") == "geo::distance(shop.location, (3.5, 4)) AS d"
        assert distance == GeoDistance("shop.location", (3.5, 4))
        assert hash(distance) == hash(GeoDistance("shop.location", (3.5, 4)))
        assert repr(distance) == "GeoDistance('shop.location', (3.5, 4))"
