"""
Custom field types for SurrealDB ORM.

This module provides specialized field types that leverage SurrealDB's
built-in functions for encryption, validation, and other operations.
"""

from .computed import Computed, get_computed_expression, is_computed_field
from .encrypted import Encrypted, EncryptedField, EncryptedFieldInfo
from .vector import VectorField, get_vector_info, is_vector_field
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
    # Computed fields
    "Computed",
    "is_computed_field",
    "get_computed_expression",
    # Encrypted fields
    "Encrypted",
    "EncryptedField",
    "EncryptedFieldInfo",
    # Vector fields
    "VectorField",
    "is_vector_field",
    "get_vector_info",
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
