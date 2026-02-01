"""
Type definitions for SurrealDB SDK responses.

Provides strongly-typed wrappers around SurrealDB responses instead of raw Any types.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ResponseStatus(str, Enum):
    """Status of a SurrealDB response."""

    OK = "OK"
    ERR = "ERR"


@dataclass
class QueryResult:
    """
    Result of a single query statement.

    Attributes:
        status: OK or ERR
        result: The query result data (records, scalar, etc.)
        time: Execution time as reported by SurrealDB
    """

    status: ResponseStatus
    result: list[dict[str, Any]] | dict[str, Any] | str | int | float | bool | None
    time: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "QueryResult":
        """Parse a query result from raw response dict."""
        status = ResponseStatus(data.get("status", "OK"))
        result = data.get("result")
        time = data.get("time", "")
        return cls(status=status, result=result, time=time)

    @property
    def is_ok(self) -> bool:
        """Check if query succeeded."""
        return self.status == ResponseStatus.OK

    @property
    def is_error(self) -> bool:
        """Check if query failed."""
        return self.status == ResponseStatus.ERR

    @property
    def records(self) -> list[dict[str, Any]]:
        """Get result as list of records. Returns empty list if not applicable."""
        if isinstance(self.result, list):
            return self.result
        return []

    @property
    def first(self) -> dict[str, Any] | None:
        """Get first record or None."""
        records = self.records
        return records[0] if records else None

    @property
    def scalar(self) -> str | int | float | bool | None:
        """Get result as scalar value."""
        if isinstance(self.result, (str, int, float, bool)):
            return self.result
        return None


@dataclass
class QueryResponse:
    """
    Response from a SurrealDB query operation.

    Contains one or more QueryResult objects (one per statement in the query).
    """

    results: list[QueryResult] = field(default_factory=list)
    raw: dict[str, Any] | list[Any] = field(default_factory=dict)

    @classmethod
    def from_rpc_result(cls, data: Any) -> "QueryResponse":
        """Parse query response from RPC result."""
        results: list[QueryResult] = []

        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and "status" in item:
                    results.append(QueryResult.from_dict(item))
                elif isinstance(item, dict):
                    # Direct result without status wrapper
                    results.append(QueryResult(status=ResponseStatus.OK, result=item))
                else:
                    results.append(QueryResult(status=ResponseStatus.OK, result=item))
        elif isinstance(data, dict):
            if "status" in data:
                results.append(QueryResult.from_dict(data))
            else:
                results.append(QueryResult(status=ResponseStatus.OK, result=data))

        return cls(results=results, raw=data if data else {})

    @property
    def is_ok(self) -> bool:
        """Check if all results succeeded."""
        return all(r.is_ok for r in self.results)

    @property
    def first_result(self) -> QueryResult | None:
        """Get first query result."""
        return self.results[0] if self.results else None

    @property
    def all_records(self) -> list[dict[str, Any]]:
        """Get all records from all results."""
        records: list[dict[str, Any]] = []
        for result in self.results:
            records.extend(result.records)
        return records


@dataclass
class RecordResponse:
    """
    Response for single record operations (create, select one, update, etc.).
    """

    record: dict[str, Any] | None = None
    raw: Any = None

    @classmethod
    def from_rpc_result(cls, data: Any) -> "RecordResponse":
        """Parse record response from RPC result."""
        record: dict[str, Any] | None = None

        if isinstance(data, dict):
            record = data
        elif isinstance(data, list) and len(data) > 0:
            if isinstance(data[0], dict):
                record = data[0]

        return cls(record=record, raw=data)

    @property
    def exists(self) -> bool:
        """Check if record exists."""
        return self.record is not None

    def get(self, key: str, default: Any = None) -> Any:
        """Get field from record."""
        if self.record:
            return self.record.get(key, default)
        return default

    @property
    def id(self) -> str | None:
        """Get record ID."""
        value = self.get("id")
        return str(value) if value is not None else None


@dataclass
class RecordsResponse:
    """
    Response for multiple records operations (select all, etc.).
    """

    records: list[dict[str, Any]] = field(default_factory=list)
    raw: Any = None

    @classmethod
    def from_rpc_result(cls, data: Any) -> "RecordsResponse":
        """Parse records response from RPC result."""
        records: list[dict[str, Any]] = []

        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    records.append(item)
        elif isinstance(data, dict):
            records.append(data)

        return cls(records=records, raw=data)

    @property
    def count(self) -> int:
        """Get number of records."""
        return len(self.records)

    @property
    def is_empty(self) -> bool:
        """Check if no records returned."""
        return len(self.records) == 0

    @property
    def first(self) -> dict[str, Any] | None:
        """Get first record or None."""
        return self.records[0] if self.records else None

    def __iter__(self) -> Any:
        """Iterate over records."""
        return iter(self.records)

    def __len__(self) -> int:
        """Get number of records."""
        return len(self.records)


@dataclass
class AuthResponse:
    """
    Response from authentication operations.
    """

    token: str | None = None
    success: bool = False
    raw: Any = None

    @classmethod
    def from_rpc_result(cls, data: Any) -> "AuthResponse":
        """Parse auth response from RPC result."""
        token: str | None = None
        success = False

        if isinstance(data, str):
            token = data
            success = True
        elif data is None:
            # signin/signup with no token return = success
            success = True

        return cls(token=token, success=success, raw=data)


@dataclass
class InfoResponse:
    """
    Response from INFO operations.
    """

    data: dict[str, Any] = field(default_factory=dict)
    raw: Any = None

    @classmethod
    def from_rpc_result(cls, data: Any) -> "InfoResponse":
        """Parse info response from RPC result."""
        info_data: dict[str, Any] = {}

        if isinstance(data, dict):
            info_data = data
        elif isinstance(data, list) and len(data) > 0:
            if isinstance(data[0], dict):
                info_data = data[0]

        return cls(data=info_data, raw=data)

    @property
    def tables(self) -> dict[str, Any]:
        """Get tables info."""
        result = self.data.get("tables", self.data.get("tb", {}))
        return result if isinstance(result, dict) else {}

    @property
    def namespaces(self) -> dict[str, Any]:
        """Get namespaces info."""
        result = self.data.get("namespaces", self.data.get("ns", {}))
        return result if isinstance(result, dict) else {}

    @property
    def databases(self) -> dict[str, Any]:
        """Get databases info."""
        result = self.data.get("databases", self.data.get("db", {}))
        return result if isinstance(result, dict) else {}


@dataclass
class LiveQueryId:
    """
    Wrapper for Live Query UUID.
    """

    uuid: str

    def __str__(self) -> str:
        return self.uuid

    @classmethod
    def from_rpc_result(cls, data: Any) -> "LiveQueryId":
        """Parse live query ID from RPC result."""
        if isinstance(data, str):
            return cls(uuid=data)
        elif isinstance(data, list) and len(data) > 0:
            first = data[0]
            if isinstance(first, str):
                return cls(uuid=first)
            elif isinstance(first, dict) and "result" in first:
                return cls(uuid=str(first["result"]))
        raise ValueError(f"Cannot parse live query ID from: {data}")


@dataclass
class DeleteResponse:
    """
    Response from delete operations.
    """

    deleted: list[dict[str, Any]] = field(default_factory=list)
    raw: Any = None

    @classmethod
    def from_rpc_result(cls, data: Any) -> "DeleteResponse":
        """Parse delete response from RPC result."""
        deleted: list[dict[str, Any]] = []

        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    deleted.append(item)
        elif isinstance(data, dict):
            deleted.append(data)

        return cls(deleted=deleted, raw=data)

    @property
    def count(self) -> int:
        """Number of deleted records."""
        return len(self.deleted)

    @property
    def success(self) -> bool:
        """Check if any records were deleted."""
        return len(self.deleted) > 0
