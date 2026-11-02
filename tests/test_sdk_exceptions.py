"""Unit tests for surreal_sdk.exceptions — SDK exception hierarchy."""

from surreal_sdk.exceptions import (
    AuthenticationError,
    ChangeFeedError,
    ConnectionError,
    LiveQueryError,
    QueryError,
    SurrealDBError,
    TableNotFoundError,
    TimeoutError,
    TransactionConflictError,
    TransactionError,
    ValidationError,
)

# Messages SurrealDB 3.3.5 (the pinned server) returns, copied verbatim.
#: A write conflict on a plain statement (concurrent ``UPDATE`` of one record).
KV_WRITE_CONFLICT = (
    "There was a problem with the key-value store: Transaction conflict: Write conflict, "
    "retry the transaction. This transaction can be retried"
)
#: The same conflict detected when a ``BEGIN``/``COMMIT`` block commits.
COMMIT_WRITE_CONFLICT = (
    "Cannot COMMIT: Transaction conflict: Write conflict, retry the transaction. This transaction can be retried"
)
WRITE_CONFLICT = "Transaction conflict: Write conflict, retry the transaction. This transaction can be retried"
SEQUENCE_RETRY = (
    "The sequence was redefined or removed while a value was being allocated from it. This transaction can be retried"
)
#: A failed ``ASSERT`` whose rejected value contains the word "conflict".
ASSERT_VALUE_CONFLICT = (
    "Found ['p', 'conflict-resolved'] for field `tags`, with record `rmfu:1`, but field must conform to: array::len($value) < 2"
)
#: A failed ``ASSERT`` on a field named ``conflicts``.
ASSERT_FIELD_CONFLICTS = "Found 9 for field `conflicts`, with record `rmfu:1`, but field must conform to: $value < 5"
NOT_EXECUTED = "The query was not executed due to a failed transaction"
COMMIT_ABORTED = "Cannot COMMIT: the transaction was aborted due to a prior error"


class TestSurrealDBError:
    def test_init_message_only(self) -> None:
        err = SurrealDBError("something broke")
        assert err.message == "something broke"
        assert err.code is None
        assert str(err) == "something broke"

    def test_init_with_code(self) -> None:
        err = SurrealDBError("server error", code=500)
        assert err.message == "server error"
        assert err.code == 500

    def test_is_exception(self) -> None:
        err = SurrealDBError("test")
        assert isinstance(err, Exception)


class TestConnectionError:
    def test_inherits_surreal_error(self) -> None:
        err = ConnectionError("connection refused")
        assert isinstance(err, SurrealDBError)


class TestAuthenticationError:
    def test_inherits_surreal_error(self) -> None:
        err = AuthenticationError("bad credentials")
        assert isinstance(err, SurrealDBError)


class TestQueryError:
    def test_init_minimal(self) -> None:
        err = QueryError("parse error")
        assert err.message == "parse error"
        assert err.query is None
        assert err.code is None

    def test_init_full(self) -> None:
        err = QueryError("syntax error", query="SELECTT * FROM users", code=400)
        assert err.message == "syntax error"
        assert err.query == "SELECTT * FROM users"
        assert err.code == 400

    def test_inherits_surreal_error(self) -> None:
        err = QueryError("error")
        assert isinstance(err, SurrealDBError)


class TestTableNotFoundError:
    def test_inherits_query_error(self) -> None:
        err = TableNotFoundError("The table 'users' does not exist")
        assert isinstance(err, QueryError)
        assert isinstance(err, SurrealDBError)

    def test_catchable_as_query_error(self) -> None:
        """Existing code catching QueryError still catches TableNotFoundError."""
        try:
            raise TableNotFoundError("The table 'users' does not exist")
        except QueryError:
            pass  # Should be caught here

    def test_is_table_not_found_does_not_exist(self) -> None:
        assert TableNotFoundError.is_table_not_found("The table 'users' does not exist")

    def test_is_table_not_found_not_found(self) -> None:
        assert TableNotFoundError.is_table_not_found("table not found")

    def test_is_table_not_found_tb_not_found(self) -> None:
        assert TableNotFoundError.is_table_not_found("tb not found")

    def test_is_table_not_found_case_insensitive(self) -> None:
        assert TableNotFoundError.is_table_not_found("The Table 'Users' Does Not Exist")

    def test_is_table_not_found_unrelated(self) -> None:
        assert not TableNotFoundError.is_table_not_found("syntax error near SELECT")

    def test_is_table_not_found_empty(self) -> None:
        assert not TableNotFoundError.is_table_not_found("")


class TestTimeoutError:
    def test_inherits_surreal_error(self) -> None:
        err = TimeoutError("timed out")
        assert isinstance(err, SurrealDBError)


class TestValidationError:
    def test_inherits_surreal_error(self) -> None:
        err = ValidationError("invalid data")
        assert isinstance(err, SurrealDBError)


class TestLiveQueryError:
    def test_inherits_surreal_error(self) -> None:
        err = LiveQueryError("live query failed")
        assert isinstance(err, SurrealDBError)


class TestChangeFeedError:
    def test_inherits_surreal_error(self) -> None:
        err = ChangeFeedError("change feed error")
        assert isinstance(err, SurrealDBError)


class TestTransactionError:
    def test_init_minimal(self) -> None:
        err = TransactionError("tx failed")
        assert err.message == "tx failed"
        assert err.code is None
        assert err.rollback_succeeded is None

    def test_init_full(self) -> None:
        err = TransactionError("tx failed", code=500, rollback_succeeded=True)
        assert err.message == "tx failed"
        assert err.code == 500
        assert err.rollback_succeeded is True

    def test_init_rollback_failed(self) -> None:
        err = TransactionError("tx failed", rollback_succeeded=False)
        assert err.rollback_succeeded is False

    def test_inherits_surreal_error(self) -> None:
        err = TransactionError("error")
        assert isinstance(err, SurrealDBError)


class TestTransactionConflictError:
    def test_inherits_transaction_error(self) -> None:
        err = TransactionConflictError("conflict")
        assert isinstance(err, TransactionError)
        assert isinstance(err, SurrealDBError)

    def test_is_conflict_error_can_be_retried(self) -> None:
        assert TransactionConflictError.is_conflict_error(Exception(WRITE_CONFLICT))

    def test_is_conflict_error_conflict(self) -> None:
        """The server's conflict wording, as a plain statement and as a COMMIT report it."""
        assert TransactionConflictError.is_conflict_error(Exception(KV_WRITE_CONFLICT))
        assert TransactionConflictError.is_conflict_error(Exception(COMMIT_WRITE_CONFLICT))

    def test_is_conflict_error_sequence_retry(self) -> None:
        """The other retryable error the server words as such."""
        assert TransactionConflictError.is_conflict_error(Exception(SEQUENCE_RETRY))

    def test_is_conflict_error_unrelated(self) -> None:
        assert not TransactionConflictError.is_conflict_error(Exception("connection refused"))

    def test_is_conflict_error_case_insensitive(self) -> None:
        assert TransactionConflictError.is_conflict_error(Exception(WRITE_CONFLICT.upper()))

    def test_assert_with_conflict_in_the_value_is_not_a_conflict(self) -> None:
        """A failed ASSERT echoes the rejected value; the word 'conflict' in it is data (#263)."""
        assert not TransactionConflictError.is_conflict_error(Exception(ASSERT_VALUE_CONFLICT))

    def test_assert_on_a_field_named_conflicts_is_not_a_conflict(self) -> None:
        """A failed ASSERT names the field; a field called 'conflicts' is not a conflict (#263)."""
        assert not TransactionConflictError.is_conflict_error(Exception(ASSERT_FIELD_CONFLICTS))

    def test_cancellation_cascade_is_not_a_conflict(self) -> None:
        """The messages for the other statements of a failed transaction say nothing of the cause."""
        assert not TransactionConflictError.is_conflict_error(Exception(NOT_EXECUTED))
        assert not TransactionConflictError.is_conflict_error(Exception(COMMIT_ABORTED))

    def test_is_conflict_error_empty_message(self) -> None:
        assert not TransactionConflictError.is_conflict_error(Exception(""))
