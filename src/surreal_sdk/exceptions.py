"""
SurrealDB SDK Exceptions.

Custom exception hierarchy for the SDK.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .jsonl import ImportReport


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


class DataFormatError(SurrealDBError, ValueError):
    """Raised when a value cannot be written to, or read from, a JSON Lines file.

    At export, a value the codec cannot carry losslessly (a tuple, which is how
    the SDK decodes a geometry, a range or an out-of-range datetime; a ``set``;
    a non-finite ``Decimal``); at import, a line that is not a JSON object or
    holds a malformed tag. It is a ``ValueError`` as well, so callers that
    already catch bad input keep working.

    Attributes:
        line: The 1-based line number of the input, when known.
        path: The dotted field path of the offending value (``loc``,
            ``nested.arr[2]``), when known.
    """

    def __init__(self, message: str, *, line: int | None = None, path: str | None = None):
        self.line = line
        self.path = path
        super().__init__(message)


class DataImportError(QueryError):
    """Raised when the server rejects, or never receives, an import batch.

    Attributes:
        errors: The server's messages (or the transport error's text).
        code: The HTTP status or RPC code, when there is one.
        report: What was written before the failure, for a JSON Lines import;
            ``None`` for a database import.
    """

    def __init__(
        self,
        message: str,
        *,
        errors: list[str],
        code: int | None = None,
        report: ImportReport | None = None,
    ):
        self.errors = errors
        self.report = report
        super().__init__(message, code=code)


class TableNotFoundError(QueryError):
    """Raised when a query references a table that does not exist.

    SurrealDB 3.0 returns errors instead of empty arrays when querying
    non-existent tables.  This subclass of ``QueryError`` allows callers
    to handle this case specifically::

        try:
            results = await User.objects().all()
        except TableNotFoundError:
            # Table hasn't been created yet
            ...
    """

    _PATTERNS = [
        "table not found",
        "does not exist",
        "tb not found",
    ]

    @staticmethod
    def is_table_not_found(message: str) -> bool:
        """Check if an error message indicates a missing table."""
        msg = message.lower()
        return any(p in msg for p in TableNotFoundError._PATTERNS)


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
    # "Transaction conflict: Write conflict, retry the transaction. This
    # transaction can be retried" (3.3), whether a plain statement or the COMMIT
    # of a BEGIN/COMMIT block hits it; the SDK's and the ORM's wrappers
    # ("Transaction commit failed: ...", "... persisted after N retries ...:
    # <last error>") put the server message last. A bare "conflict" is not
    # enough: a failed ASSERT echoes the rejected value and names the field, so
    # a value such as 'conflict-resolved' or a field named `conflicts` would
    # read as a conflict and be retried (#263). The sentence itself is matched
    # only at the end of the message for the same reason: an ASSERT message
    # ends with "must conform to: <expr>", and the rejected value echoed before
    # it may contain the sentence (#448). "failed transaction" is the message
    # of the *other* statements of a failed transaction, whatever the cause.
    _CONFLICT_PATTERN = re.compile(r"this transaction can be retried\.?\s*\Z", re.IGNORECASE)

    @staticmethod
    def is_conflict_error(error: Exception) -> bool:
        """Check if an exception represents a retryable transaction conflict.

        Only the server's retryable wording, at the end of the message, counts;
        any other rejection (a failed ``ASSERT``, a cancelled statement) is not
        a conflict.
        """
        return TransactionConflictError._CONFLICT_PATTERN.search(str(error)) is not None
