"""
Authentication module for SurrealDB ORM.

Provides JWT authentication support using SurrealDB's native
DEFINE ACCESS ... TYPE RECORD feature.
"""

from .access import AccessDefinition, AccessGenerator
from .mixins import AuthenticatedUserMixin
from .result import AuthResult

__all__ = [
    "AccessDefinition",
    "AccessGenerator",
    "AuthenticatedUserMixin",
    "AuthResult",
]
