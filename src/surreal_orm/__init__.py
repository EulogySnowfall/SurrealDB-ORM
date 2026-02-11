"""
SurrealDB ORM - A Django-style ORM for SurrealDB.

This package provides:
- Model definitions with Pydantic validation
- Query building with fluent interface
- Migration system with version control
- JWT authentication support
- CLI tools for schema management
"""

from .connection_config import ConnectionConfig
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
from .fields import (
    Computed,
    Encrypted,
    GeoField,
    LineStringField,
    MultiPointField,
    PointField,
    PolygonField,
    VectorField,
)
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
from .q import Q
from .subquery import Subquery
from .cache import QueryCache
from .prefetch import Prefetch
from .search import SearchScore, SearchHighlight
from .surreal_function import SurrealFunc
from .utils import retry_on_conflict
from .live import LiveModelStream, ModelChangeEvent, ChangeModelStream
from .geo import GeoDistance
from .introspection import generate_models_from_db, schema_diff
from .migrations.operations import DefineAnalyzer, DefineEvent, RemoveEvent
from .signals import (
    Signal,
    pre_save,
    post_save,
    pre_delete,
    post_delete,
    pre_update,
    post_update,
    post_live_change,
    # Around signals (generator-based middleware pattern)
    AroundSignal,
    around_save,
    around_delete,
    around_update,
)

# Re-export LiveAction from SDK for convenience
from surreal_sdk.streaming.live_select import LiveAction

__all__ = [
    # Connection
    "ConnectionConfig",
    "SurrealDBConnectionManager",
    # Models
    "BaseSurrealModel",
    "SurrealConfigDict",
    "SurrealDbError",
    "get_registered_models",
    # Query
    "QuerySet",
    "OrderBy",
    "Q",
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
    "Computed",
    "Encrypted",
    "VectorField",
    # Geometry
    "GeoField",
    "PointField",
    "PolygonField",
    "LineStringField",
    "MultiPointField",
    "GeoDistance",
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
    # Signals
    "Signal",
    "pre_save",
    "post_save",
    "pre_delete",
    "post_delete",
    "pre_update",
    "post_update",
    "post_live_change",
    # Around Signals (generator-based)
    "AroundSignal",
    "around_save",
    "around_delete",
    "around_update",
    # Live / Real-time
    "LiveModelStream",
    "ModelChangeEvent",
    "ChangeModelStream",
    "LiveAction",
    # Subqueries
    "Subquery",
    # Cache
    "QueryCache",
    # Prefetch
    "Prefetch",
    # Search
    "SearchScore",
    "SearchHighlight",
    # Server-side functions
    "SurrealFunc",
    # Introspection
    "generate_models_from_db",
    "schema_diff",
    # Migrations
    "DefineAnalyzer",
    "DefineEvent",
    "RemoveEvent",
    # Utilities
    "retry_on_conflict",
]

__version__ = "0.13.0"
