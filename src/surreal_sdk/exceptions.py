"""
SurrealDB SDK Exceptions.

Custom exception hierarchy for the SDK.
"""


class SurrealDBError(Exception):
    """Base exception for all SurrealDB SDK errors."""

    def __init__(self, message: str, code: int | None = None):
        self.message = message
        self.code = code
        super().__init__(message)


class ConnectionError(SurrealDBError):
    """Raised when connection to SurrealDB fails."""

    pass


class AuthenticationError(SurrealDBError):
    """Raised when authentication fails."""

    pass


class QueryError(SurrealDBError):
    """Raised when a query execution fails."""

    def __init__(self, message: str, query: str | None = None, code: int | None = None):
        self.query = query
        super().__init__(message, code)


class TimeoutError(SurrealDBError):
    """Raised when an operation times out."""

    pass


class ValidationError(SurrealDBError):
    """Raised when data validation fails."""

    pass


class LiveQueryError(SurrealDBError):
    """Raised when a live query operation fails."""

    pass


class ChangeFeedError(SurrealDBError):
    """Raised when a change feed operation fails."""

    pass


class TransactionError(SurrealDBError):
    """Raised when a transaction operation fails."""

    def __init__(
        self,
        message: str,
        code: int | None = None,
        rollback_succeeded: bool | None = None,
    ):
        self.rollback_succeeded = rollback_succeeded
        super().__init__(message, code)


class TransactionConflictError(TransactionError):
    """Raised when a transaction fails due to a retryable conflict.

    SurrealDB returns specific error messages when concurrent modifications
    conflict. These errors are safe to retry with backoff.
    """

    _CONFLICT_PATTERNS = [
        "can be retried",
        "failed transaction",
        "conflict",
        "document changed",
    ]

    @staticmethod
    def is_conflict_error(error: Exception) -> bool:
        """Check if an exception represents a retryable transaction conflict."""
        msg = str(error).lower()
        return any(p in msg for p in TransactionConflictError._CONFLICT_PATTERNS)
