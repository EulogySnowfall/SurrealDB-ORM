"""
SurrealDB ORM - A Django-style ORM for SurrealDB.

This package provides:
- Model definitions with Pydantic validation
- Query building with fluent interface
- Migration system with version control
- JWT authentication support
- CLI tools for schema management
"""

from .connection_manager import SurrealDBConnectionManager
from .enum import OrderBy
from .model_base import (
    BaseSurrealModel,
    SurrealConfigDict,
    SurrealDbError,
    get_registered_models,
)
from .query_set import QuerySet
from .types import (
    EncryptionAlgorithm,
    FieldType,
    SchemaMode,
    TableType,
)
from .fields import Encrypted
from .fields import (
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
from .auth import AuthenticatedUserMixin
from .aggregations import Aggregation, Count, Sum, Avg, Min, Max

__all__ = [
    # Connection
    "SurrealDBConnectionManager",
    # Models
    "BaseSurrealModel",
    "SurrealConfigDict",
    "SurrealDbError",
    "get_registered_models",
    # Query
    "QuerySet",
    "OrderBy",
    # Aggregations
    "Aggregation",
    "Count",
    "Sum",
    "Avg",
    "Min",
    "Max",
    # Types
    "TableType",
    "SchemaMode",
    "FieldType",
    "EncryptionAlgorithm",
    # Fields
    "Encrypted",
    # Relations
    "ForeignKey",
    "ManyToMany",
    "Relation",
    "RelationInfo",
    "get_relation_info",
    "is_foreign_key",
    "is_graph_relation",
    "is_many_to_many",
    "is_relation_field",
    # Auth
    "AuthenticatedUserMixin",
]

__version__ = "0.5.2"
