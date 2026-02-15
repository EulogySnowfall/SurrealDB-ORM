"""
SurrealDB RPC Protocol Implementation.

Handles the JSON-RPC style messaging format used by SurrealDB.
Supports both JSON and CBOR serialization formats.
"""

import json
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from uuid import UUID

from . import cbor as cbor_module


def _strip_none_values(data: Any) -> Any:
    """
    Recursively strip None values from dicts for JSON protocol.

    JSON encodes ``None`` as ``null``, which SurrealDB interprets as ``NULL``.
    SurrealDB's ``option<T>`` on SCHEMAFULL tables rejects ``NULL`` — it expects
    ``NONE`` (absent field).  Since JSON has no ``NONE`` concept, the safest
    approach is to omit keys whose value is ``None``.
    """
    if isinstance(data, dict):
        return {k: _strip_none_values(v) for k, v in data.items() if v is not None}
    if isinstance(data, list):
        return [_strip_none_values(item) for item in data]
    return data


class SurrealJSONEncoder(json.JSONEncoder):
    """
    Custom JSON encoder for SurrealDB types.

    Handles serialization of Python types that are not natively JSON serializable:
    - datetime → ISO 8601 string
    - date → ISO 8601 string
    - time → ISO 8601 string
    - Decimal → float
    - UUID → string
    """

    def default(self, obj: Any) -> Any:
        """Encode non-standard types to JSON-serializable values."""
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, date):
            return obj.isoformat()
        if isinstance(obj, time):
            return obj.isoformat()
        if isinstance(obj, Decimal):
            return float(obj)
        if isinstance(obj, UUID):
            return str(obj)
        return super().default(obj)


@dataclass
class RPCRequest:
    """
    RPC Request message format.

    Attributes:
        id: Unique request identifier for response matching
        method: RPC method name (query, select, create, etc.)
        params: Method parameters as list or dict
    """

    method: str
    params: list[Any] | dict[str, Any] = field(default_factory=list)
    id: int = 1

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "method": self.method,
            "params": self.params if isinstance(self.params, list) else [self.params],
        }

    def to_json(self) -> str:
        """Serialize to JSON string with custom encoder for datetime, UUID, etc.

        None values inside params are stripped (omitted) because JSON ``null``
        maps to SurrealDB ``NULL``, which is rejected by ``option<T>`` on
        SCHEMAFULL tables.  Omitting the key produces ``NONE`` (absent).
        """
        data = self.to_dict()
        data["params"] = _strip_none_values(data["params"])
        return json.dumps(data, cls=SurrealJSONEncoder)

    def to_cbor(self) -> bytes:
        """
        Serialize to CBOR bytes.

        CBOR encoding is recommended for SurrealDB as it properly handles
        binary data and avoids string interpretation issues (e.g., 'data:xxx'
        being interpreted as record links).

        Raises:
            ImportError: If cbor2 is not installed
        """
        return cbor_module.encode(self.to_dict())

    @classmethod
    def query(cls, sql: str, vars: dict[str, Any] | None = None, request_id: int = 1) -> "RPCRequest":
        """Create a query request."""
        return cls(method="query", params=[sql, vars or {}], id=request_id)

    @classmethod
    def select(cls, thing: str, request_id: int = 1) -> "RPCRequest":
        """Create a select request."""
        return cls(method="select", params=[thing], id=request_id)

    @classmethod
    def create(cls, thing: str, data: dict[str, Any], request_id: int = 1) -> "RPCRequest":
        """Create a create request."""
        return cls(method="create", params=[thing, data], id=request_id)

    @classmethod
    def update(cls, thing: str, data: dict[str, Any], request_id: int = 1) -> "RPCRequest":
        """Create an update request."""
        return cls(method="update", params=[thing, data], id=request_id)

    @classmethod
    def merge(cls, thing: str, data: dict[str, Any], request_id: int = 1) -> "RPCRequest":
        """Create a merge request."""
        return cls(method="merge", params=[thing, data], id=request_id)

    @classmethod
    def delete(cls, thing: str, request_id: int = 1) -> "RPCRequest":
        """Create a delete request."""
        return cls(method="delete", params=[thing], id=request_id)

    @classmethod
    def signin(
        cls,
        user: str | None = None,
        password: str | None = None,
        namespace: str | None = None,
        database: str | None = None,
        access: str | None = None,
        request_id: int = 1,
    ) -> "RPCRequest":
        """Create a signin request."""
        params: dict[str, Any] = {}
        if user:
            params["user"] = user
        if password:
            params["pass"] = password
        if namespace:
            params["ns"] = namespace
        if database:
            params["db"] = database
        if access:
            params["ac"] = access
        return cls(method="signin", params=params, id=request_id)

    @classmethod
    def use(cls, namespace: str, database: str, request_id: int = 1) -> "RPCRequest":
        """Create a use request."""
        return cls(method="use", params=[namespace, database], id=request_id)

    @classmethod
    def live(cls, table: str, diff: bool = False, request_id: int = 1) -> "RPCRequest":
        """Create a live query request."""
        sql = f"LIVE SELECT * FROM {table}"
        if diff:
            sql += " DIFF"
        return cls(method="query", params=[sql, {}], id=request_id)

    @classmethod
    def kill(cls, live_id: str, request_id: int = 1) -> "RPCRequest":
        """Create a kill request for a live query."""
        return cls(method="kill", params=[live_id], id=request_id)


@dataclass
class RPCError:
    """
    RPC Error format.

    Attributes:
        code: Error code
        message: Error message
    """

    code: int
    message: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RPCError":
        """Create from dictionary."""
        return cls(
            code=data.get("code", -1),
            message=data.get("message", "Unknown error"),
        )


@dataclass
class RPCResponse:
    """
    RPC Response message format.

    Attributes:
        id: Request identifier this response matches
        result: Query result data (if successful)
        error: Error information (if failed)
    """

    id: int
    result: Any = None
    error: RPCError | None = None

    @property
    def is_error(self) -> bool:
        """Check if response is an error."""
        return self.error is not None

    @property
    def is_success(self) -> bool:
        """Check if response is successful."""
        return self.error is None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RPCResponse":
        """Parse from dictionary."""
        error = None
        if "error" in data:
            error = RPCError.from_dict(data["error"])

        return cls(
            id=data.get("id", 0),
            result=data.get("result"),
            error=error,
        )

    @classmethod
    def from_json(cls, json_str: str) -> "RPCResponse":
        """Parse from JSON string."""
        data = json.loads(json_str)
        return cls.from_dict(data)

    @classmethod
    def from_cbor(cls, cbor_data: bytes) -> "RPCResponse":
        """
        Parse from CBOR bytes.

        Args:
            cbor_data: CBOR-encoded response bytes

        Returns:
            Parsed RPCResponse

        Raises:
            ImportError: If cbor2 is not installed
        """
        data = cbor_module.decode(cbor_data)
        return cls.from_dict(data)


# RPC Method names as constants
class RPCMethod:
    """RPC method name constants."""

    # Authentication
    SIGNIN = "signin"
    SIGNUP = "signup"
    AUTHENTICATE = "authenticate"
    INVALIDATE = "invalidate"
    INFO = "info"

    # Connection
    USE = "use"
    PING = "ping"
    VERSION = "version"
    RESET = "reset"

    # CRUD
    SELECT = "select"
    CREATE = "create"
    INSERT = "insert"
    UPDATE = "update"
    UPSERT = "upsert"
    MERGE = "merge"
    PATCH = "patch"
    DELETE = "delete"
    RELATE = "relate"

    # Query
    QUERY = "query"
    GRAPHQL = "graphql"
    RUN = "run"

    # Live Queries (WebSocket only)
    LIVE = "live"
    KILL = "kill"

    # Variables (WebSocket only)
    LET = "let"
    UNSET = "unset"
