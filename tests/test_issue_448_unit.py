"""Unit tests for #448: follow-ups to the 0.36.1 SDK and annotation fixes.

* ``is_conflict_error()`` reads the server's retryable sentence only at the end
  of a message: a failed ``ASSERT`` echoes the rejected value, which may
  contain that sentence.
* The change-feed reader and ``HTTPTransaction`` share one definition of the
  messages a failed transaction reports for its cancelled statements.
* Annotation arguments validated at construction are validated again when
  reassigned.

The server messages are copied from SurrealDB 3.3.5, the pinned server.
"""

from __future__ import annotations

import pytest

from src.surreal_orm.utils import retry_on_conflict
from surreal_sdk.exceptions import QueryError, TransactionConflictError

RETRYABLE = "This transaction can be retried"
KV_WRITE_CONFLICT = (
    f"There was a problem with the key-value store: Transaction conflict: Write conflict, retry the transaction. {RETRYABLE}"
)
COMMIT_WRITE_CONFLICT = f"Cannot COMMIT: Transaction conflict: Write conflict, retry the transaction. {RETRYABLE}"
#: A failed ASSERT whose rejected value holds the retryable sentence.
ASSERT_VALUE_RETRYABLE = (
    "Found 'Write conflict. This transaction can be retried' for field `note`, with record `i448_t:a`, "
    "but field must conform to: string::len($value) < 5"
)


class TestConflictSentenceAtTheEnd:
    @pytest.mark.parametrize(
        "message",
        [
            KV_WRITE_CONFLICT,
            COMMIT_WRITE_CONFLICT,
            f"Transaction commit failed: {COMMIT_WRITE_CONFLICT}",
            f"Transaction conflict persisted after 3 retries in work: {KV_WRITE_CONFLICT}",
            f"{KV_WRITE_CONFLICT}.",
            f"{KV_WRITE_CONFLICT}\n",
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
