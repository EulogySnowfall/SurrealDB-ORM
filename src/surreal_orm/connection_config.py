"""
Connection configuration dataclass for multi-database support.

Provides an immutable configuration container used by the connection
registry in ``SurrealDBConnectionManager``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ConnectionConfig:
    """
    Immutable configuration for a SurrealDB connection.

    Used by ``SurrealDBConnectionManager.add_connection()`` to store
    named connection settings.

    Attributes:
        url: The URL of the SurrealDB instance.
        user: The username for authentication.
        password: The password for authentication.
        namespace: The namespace to use.
        database: The database to use.
        protocol: Serialization protocol ("json" or "cbor").
    """

    url: str
    user: str
    password: str
    namespace: str
    database: str
    protocol: Literal["json", "cbor"] = "cbor"


__all__ = ["ConnectionConfig"]
