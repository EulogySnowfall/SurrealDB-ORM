"""Unit tests for surreal_sdk.exceptions — SDK exception hierarchy."""

from surreal_sdk.exceptions import (
    AuthenticationError,
    ChangeFeedError,
    ConnectionError,
    LiveQueryError,
    QueryError,
    SurrealDBError,
    TimeoutError,
    TransactionConflictError,
    TransactionError,
    ValidationError,
)


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
        assert TransactionConflictError.is_conflict_error(Exception("This can be retried"))

    def test_is_conflict_error_failed_transaction(self) -> None:
        assert TransactionConflictError.is_conflict_error(Exception("failed transaction"))

    def test_is_conflict_error_conflict(self) -> None:
        assert TransactionConflictError.is_conflict_error(Exception("Write conflict detected"))

    def test_is_conflict_error_document_changed(self) -> None:
        assert TransactionConflictError.is_conflict_error(Exception("document changed"))

    def test_is_conflict_error_unrelated(self) -> None:
        assert not TransactionConflictError.is_conflict_error(Exception("connection refused"))

    def test_is_conflict_error_case_insensitive(self) -> None:
        assert TransactionConflictError.is_conflict_error(Exception("FAILED TRANSACTION"))

    def test_is_conflict_error_empty_message(self) -> None:
        assert not TransactionConflictError.is_conflict_error(Exception(""))
