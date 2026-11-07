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


class FilesNotEnabledError(QueryError):
    """Raised when the server was started without file storage.

    Experimental: follows SurrealDB's experimental ``files`` capability.
    SurrealDB 3.2 and 3.3 keep file storage behind ``--allow-experimental=files``
    (or ``SURREAL_CAPS_ALLOW_EXPERIMENTAL=files``), which the official image
    does not enable. Without it a ``file::*`` call is a statement error and a
    ``DEFINE BUCKET`` is a parse error, returned as an RPC error; both reach
    the caller as this exception, whose message names the flag and ends with
    the server's own text. It is a ``QueryError``, so existing handlers keep
    catching it.
    """

    # What the server says when the capability is off (case-sensitive,
    # measured on 3.2.6 and 3.3.5): a ``file::*`` call, a ``type::file()``
    # cast on 3.3, and the parse error of ``DEFINE BUCKET``.
    _TEXTS = (
        "Experimental capability `files` is not enabled",
        "Experimental feature files is not enabled",
        "expected the experimental files feature to be enabled",
    )

    @staticmethod
    def is_files_not_enabled(message: str) -> bool:
        """Check if a server error message says the ``files`` capability is off."""
        return any(text in message for text in FilesNotEnabledError._TEXTS)

    @classmethod
    def _from_server_text(cls, text: str, *, query: str | None = None, code: int | None = None) -> FilesNotEnabledError:
        """Build the error for the server's message *text*."""
        return cls(
            "File storage is not enabled on this SurrealDB server: start it with --allow-experimental=files "
            f"(server said: {text})",
            query=query,
            code=code,
        )


class PayloadTooLargeError(QueryError):
    """Raised when a request is over the size limit of its transport.

    Experimental: follows SurrealDB's experimental ``files`` capability.
    File uploads need a server started with ``--allow-experimental=files``.
    Raised by ``file_put()`` before anything is sent, when its estimate of
    the request exceeds the connection's ``max_request_size``, and by the HTTP
    transport when the server answers ``413`` (its body limit,
    ``SURREAL_HTTP_MAX_RPC_BODY_SIZE``, 4 MiB by default), whatever the
    request. It is a ``QueryError`` with ``code`` 413, so existing handlers
    keep catching it.

    Attributes:
        size: The estimated size of the refused request in bytes (the data,
            the file pointer and 512 bytes for the request envelope), or
            ``None`` when the server refused it.
        limit: The connection's ``max_request_size``, or ``None`` when the
            server refused the request.
    """

    def __init__(
        self,
        message: str,
        *,
        size: int | None = None,
        limit: int | None = None,
        query: str | None = None,
    ):
        self.size = size
        self.limit = limit
        super().__init__(message, query=query, code=413)


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
