"""
Custom field types for SurrealDB ORM.

This module provides specialized field types that leverage SurrealDB's
built-in functions for encryption, validation, and other operations.
"""

from .encrypted import Encrypted, EncryptedField, EncryptedFieldInfo
from .relation import (
    ForeignKey,
    ManyToMany,
    Relation,
    RelationInfo,
    get_relation_info,
    is_foreign_key,
    is_graph_relation,
    is_many_to_many,
    is_relation_field,
)

__all__ = [
    # Encrypted fields
    "Encrypted",
    "EncryptedField",
    "EncryptedFieldInfo",
    # Relation fields
    "ForeignKey",
    "ManyToMany",
    "Relation",
    "RelationInfo",
    "get_relation_info",
    "is_foreign_key",
    "is_graph_relation",
    "is_many_to_many",
    "is_relation_field",
]
