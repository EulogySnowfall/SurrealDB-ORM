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

    # SurrealDB ends every error it considers retryable with this sentence, e.g.
    # "The query was not executed due to a failed transaction. Failed to commit
    # transaction due to a read or write conflict. This transaction can be
    # retried" (2.7), whether a plain statement or a BEGIN/COMMIT block hits it.
    # A bare "conflict" is not enough: a failed ASSERT echoes the rejected value
    # and names the field, so a value such as 'conflict-resolved' or a field
    # named `conflicts` would read as a conflict and be retried (#446).
    # "failed transaction" is the message of the *other* statements of a failed
    # transaction, whatever the cause.
    _CONFLICT_PATTERNS = [
        "this transaction can be retried",
    ]

    @staticmethod
    def is_conflict_error(error: Exception) -> bool:
        """Check if an exception represents a retryable transaction conflict.

        Only the server's retryable wording counts (case-insensitive); any other
        rejection (a failed ``ASSERT``, a cancelled statement) is not a conflict.
        """
        msg = str(error).lower()
        return any(p in msg for p in TransactionConflictError._CONFLICT_PATTERNS)
