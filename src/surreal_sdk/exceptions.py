"""
SurrealDB SDK Exceptions.

Custom exception hierarchy for the SDK.
"""

import re


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
    # retried" (2.7), whether a plain statement or a BEGIN/COMMIT block hits it;
    # the SDK's and the ORM's wrappers ("Transaction commit failed: ...",
    # "... persisted after N retries ...: <last error>") put the server message
    # last. A bare "conflict" is not enough: a failed ASSERT echoes the rejected
    # value and names the field, so a value such as 'conflict-resolved' or a
    # field named `conflicts` would read as a conflict and be retried (#446).
    # The sentence itself is matched only at the end of the message for the
    # same reason: an ASSERT message ends with "must conform to: <expr>", and
    # the rejected value echoed before it may contain the sentence (#486).
    # "failed transaction" is the message of the *other* statements of a
    # failed transaction, whatever the cause.
    _CONFLICT_PATTERN = re.compile(r"this transaction can be retried\.?\s*\Z", re.IGNORECASE)

    @staticmethod
    def is_conflict_error(error: Exception) -> bool:
        """Check if an exception represents a retryable transaction conflict.

        Only the server's retryable wording, at the end of the message, counts;
        any other rejection (a failed ``ASSERT``, a cancelled statement) is not
        a conflict.
        """
        return TransactionConflictError._CONFLICT_PATTERN.search(str(error)) is not None
