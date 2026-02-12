"""
SurrealDB SDK Protocol Module.

Implements the RPC protocol for SurrealDB communication.
Supports both JSON and CBOR serialization formats.
"""

from .cbor import (
    CBOR_AVAILABLE,
    Duration,
    RecordId,
    Table,
)
from .cbor import (
    decode as cbor_decode,
)
from .cbor import (
    encode as cbor_encode,
)
from .cbor import (
    is_available as cbor_is_available,
)
from .rpc import RPCError, RPCRequest, RPCResponse

__all__ = [
    # RPC
    "RPCRequest",
    "RPCResponse",
    "RPCError",
    # CBOR
    "CBOR_AVAILABLE",
    "RecordId",
    "Table",
    "Duration",
    "cbor_encode",
    "cbor_decode",
    "cbor_is_available",
]
