"""
Authentication result type for SurrealDB ORM.

Provides a backward-compatible result type that carries the refresh token
introduced in SurrealDB 3.0 alongside the access token and user instance.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

_U = TypeVar("_U")


@dataclass(frozen=True)
class AuthResult(Generic[_U]):
    """
    Result of an authentication operation (signup/signin).

    SurrealDB 3.0 returns both an access token and a refresh token.
    This class carries all three values and is **backward-compatible**
    with the previous ``tuple[User, str]`` return type — unpacking as
    two values still works::

        # New (recommended)
        result = await User.signup(email="a@b.com", password="secret")
        result.user          # User instance
        result.token         # JWT access token
        result.refresh_token # Refresh token (SurrealDB 3.0+, None on 2.x)

        # Backward-compatible (still works)
        user, token = await User.signup(email="a@b.com", password="secret")

    Attributes:
        user: The authenticated user model instance.
        token: JWT access token.
        refresh_token: Refresh token for obtaining new access tokens
            (SurrealDB 3.0+).  ``None`` when connecting to SurrealDB 2.x.
    """

    user: _U
    token: str
    refresh_token: str | None = None

    # ------------------------------------------------------------------
    # Backward compatibility: unpack as 2-tuple  (user, token = result)
    # ------------------------------------------------------------------

    def __iter__(self) -> Iterator[Any]:
        """Yield ``(user, token)`` for backward-compatible unpacking."""
        yield self.user
        yield self.token

    def __getitem__(self, index: int) -> Any:
        """Support ``result[0]`` / ``result[1]`` indexing."""
        if index == 0:
            return self.user
        if index == 1:
            return self.token
        raise IndexError(f"AuthResult index out of range: {index}")

    def __len__(self) -> int:
        return 2  # backward compat: behaves like a 2-tuple
