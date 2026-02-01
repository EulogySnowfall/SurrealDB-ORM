"""
SurrealDB SDK Protocol Module.

Implements the RPC protocol for SurrealDB communication.
"""

from .rpc import RPCRequest, RPCResponse, RPCError

__all__ = [
    "RPCRequest",
    "RPCResponse",
    "RPCError",
]
