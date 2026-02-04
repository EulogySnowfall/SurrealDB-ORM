"""
SurrealDB SDK Protocol Module.

Implements the RPC protocol for SurrealDB communication.
Supports both JSON and CBOR serialization formats.
"""

from .rpc import RPCRequest, RPCResponse, RPCError
from .cbor import (
    CBOR_AVAILABLE,
    RecordId,
    Table,
    Duration,
    encode as cbor_encode,
    decode as cbor_decode,
    is_available as cbor_is_available,
)

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
