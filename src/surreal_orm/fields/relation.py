"""
Relation field types for SurrealDB ORM.

This module provides relation field types that leverage SurrealDB's
graph capabilities for defining relationships between models.

Example:
    from surreal_orm import BaseSurrealModel
    from surreal_orm.fields import ForeignKey, ManyToMany, Relation

    class User(BaseSurrealModel):
        id: str | None = None
        name: str

        # Graph relations (SurrealDB edges)
        followers: Relation("follows", "User", reverse=True)
        following: Relation("follows", "User")

        # Traditional relations
        profile: ForeignKey("Profile", on_delete="CASCADE")
        groups: ManyToMany("Group", through="membership")
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Literal, get_args, get_origin

from pydantic import GetCoreSchemaHandler, GetJsonSchemaHandler
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema, core_schema

from surreal_orm.utils import escape_record_id

if TYPE_CHECKING:
    pass


@dataclass
class RelationInfo:
    """
    Metadata for relation fields used during schema generation and querying.

    Attributes:
        to_model: The target model name (string to allow forward references)
        relation_type: Type of relation (foreign_key, many_to_many, relation)
        edge_table: Name of the edge table for graph relations
        reverse: Whether to traverse in reverse direction (<- vs ->)
        on_delete: Delete behavior for foreign keys (CASCADE, SET_NULL, PROTECT)
        related_name: Name for the reverse relation on the target model
        through: Intermediate table for many-to-many relations
    """

    to_model: str
    relation_type: Literal["foreign_key", "many_to_many", "relation"]
    edge_table: str | None = None
    reverse: bool = False
    on_delete: Literal["CASCADE", "SET_NULL", "PROTECT"] | None = None
    related_name: str | None = None
    through: str | None = None

    @property
    def traversal_direction(self) -> str:
        """Get the SurrealDB traversal direction operator."""
        return "<-" if self.reverse else "->"

    def get_traversal_query(self, from_table: str, from_id: str) -> str:
        """
        Generate the SurrealDB traversal query.

        Args:
            from_table: Source table name
            from_id: Source record ID

        Returns:
            SurrealQL traversal query string
        """
        # Escape the ID if it starts with a digit or contains special characters
        escaped_id = escape_record_id(from_id)

        if self.relation_type == "relation":
            if self.reverse:
                return f"SELECT * FROM {from_table}:{escaped_id}<-{self.edge_table}<-{self.to_model}"
            return f"SELECT * FROM {from_table}:{escaped_id}->{self.edge_table}->{self.to_model}"
        elif self.relation_type == "foreign_key":
            return f"SELECT * FROM {self.to_model} WHERE id = {from_table}:{escaped_id}.{self.edge_table}"
        else:  # many_to_many
            through = self.through or f"{from_table}_{self.to_model}"
            return f"SELECT * FROM {from_table}:{escaped_id}->{through}->{self.to_model}"


class _ForeignKeyMarker:
    """
    Marker class for ForeignKey fields.

    Foreign keys represent a single reference to another record,
    stored as a record ID in the source table.
    """

    to: str
    on_delete: Literal["CASCADE", "SET_NULL", "PROTECT"]
    related_name: str | None

    def __init__(
        self,
        to: str,
        on_delete: Literal["CASCADE", "SET_NULL", "PROTECT"] = "CASCADE",
        related_name: str | None = None,
    ):
        self.to = to
        self.on_delete = on_delete
        self.related_name = related_name

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        source_type: Any,
        handler: GetCoreSchemaHandler,
    ) -> CoreSchema:
        """Build the Pydantic core schema for validation."""
        # ForeignKey stores a record ID (string) or None
        to_model = ""
        on_delete: Literal["CASCADE", "SET_NULL", "PROTECT"] = "CASCADE"
        related_name = None

        args = get_args(source_type)
        for arg in args:
            if isinstance(arg, _ForeignKeyMarker):
                to_model = arg.to
                on_delete = arg.on_delete
                related_name = arg.related_name
                break

        return core_schema.nullable_schema(
            core_schema.str_schema(
                metadata={
                    "relation_type": "foreign_key",
                    "to_model": to_model,
                    "on_delete": on_delete,
                    "related_name": related_name,
                    "surreal_type": "record",
                }
            )
        )

    @classmethod
    def __get_pydantic_json_schema__(
        cls,
        core_schema_obj: CoreSchema,
        handler: GetJsonSchemaHandler,
    ) -> JsonSchemaValue:
        """Generate JSON schema for OpenAPI documentation."""
        return {"type": "string", "format": "record-id", "nullable": True}


class _ManyToManyMarker:
    """
    Marker class for ManyToMany fields.

    Many-to-many relations are implemented using SurrealDB graph edges,
    with an optional intermediate table for storing relation metadata.
    """

    to: str
    through: str | None
    related_name: str | None

    def __init__(
        self,
        to: str,
        through: str | None = None,
        related_name: str | None = None,
    ):
        self.to = to
        self.through = through
        self.related_name = related_name

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        source_type: Any,
        handler: GetCoreSchemaHandler,
    ) -> CoreSchema:
        """Build the Pydantic core schema for validation."""
        to_model = ""
        through = None
        related_name = None

        args = get_args(source_type)
        for arg in args:
            if isinstance(arg, _ManyToManyMarker):
                to_model = arg.to
                through = arg.through
                related_name = arg.related_name
                break

        # ManyToMany is represented as a list of record IDs (virtual field)
        return core_schema.list_schema(
            core_schema.str_schema(),
            metadata={
                "relation_type": "many_to_many",
                "to_model": to_model,
                "through": through,
                "related_name": related_name,
                "surreal_type": "virtual",
            },
        )

    @classmethod
    def __get_pydantic_json_schema__(
        cls,
        core_schema_obj: CoreSchema,
        handler: GetJsonSchemaHandler,
    ) -> JsonSchemaValue:
        """Generate JSON schema for OpenAPI documentation."""
        return {"type": "array", "items": {"type": "string", "format": "record-id"}}


class _RelationMarker:
    """
    Marker class for graph Relation fields.

    Relations use SurrealDB's native graph capabilities with RELATE
    statements and edge traversal (-> and <-).
    """

    edge: str
    to: str
    reverse: bool

    def __init__(
        self,
        edge: str,
        to: str,
        reverse: bool = False,
    ):
        self.edge = edge
        self.to = to
        self.reverse = reverse

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        source_type: Any,
        handler: GetCoreSchemaHandler,
    ) -> CoreSchema:
        """Build the Pydantic core schema for validation."""
        edge = ""
        to_model = ""
        reverse = False

        args = get_args(source_type)
        for arg in args:
            if isinstance(arg, _RelationMarker):
                edge = arg.edge
                to_model = arg.to
                reverse = arg.reverse
                break

        # Relation is represented as a list of related objects (virtual field)
        return core_schema.list_schema(
            core_schema.any_schema(),
            metadata={
                "relation_type": "relation",
                "edge_table": edge,
                "to_model": to_model,
                "reverse": reverse,
                "surreal_type": "virtual",
            },
        )

    @classmethod
    def __get_pydantic_json_schema__(
        cls,
        core_schema_obj: CoreSchema,
        handler: GetJsonSchemaHandler,
    ) -> JsonSchemaValue:
        """Generate JSON schema for OpenAPI documentation."""
        return {"type": "array", "items": {"type": "object"}}


def ForeignKey(
    to: str,
    on_delete: Literal["CASCADE", "SET_NULL", "PROTECT"] = "CASCADE",
    related_name: str | None = None,
) -> Any:
    """
    Create a ForeignKey field type.

    A ForeignKey represents a single reference to another model,
    stored as a record ID in the database.

    Args:
        to: Target model name (string for forward references)
        on_delete: Behavior when referenced record is deleted
            - CASCADE: Delete this record too
            - SET_NULL: Set the field to null
            - PROTECT: Prevent deletion of referenced record
        related_name: Name for reverse relation on target model

    Returns:
        Annotated type for use in model definition

    Example:
        class Post(BaseSurrealModel):
            author: ForeignKey("User", related_name="posts")
    """
    return Annotated[str | None, _ForeignKeyMarker(to, on_delete, related_name)]


def ManyToMany(
    to: str,
    through: str | None = None,
    related_name: str | None = None,
) -> Any:
    """
    Create a ManyToMany field type.

    A ManyToMany relation uses SurrealDB graph edges to connect
    multiple records. An optional intermediate table can store
    additional metadata about the relationship.

    Args:
        to: Target model name (string for forward references)
        through: Intermediate edge table name (auto-generated if not specified)
        related_name: Name for reverse relation on target model

    Returns:
        Annotated type for use in model definition

    Example:
        class User(BaseSurrealModel):
            groups: ManyToMany("Group", through="membership")

        class Group(BaseSurrealModel):
            members: ManyToMany("User", through="membership", related_name="groups")
    """
    return Annotated[list, _ManyToManyMarker(to, through, related_name)]


def Relation(
    edge: str,
    to: str,
    reverse: bool = False,
) -> Any:
    """
    Create a graph Relation field type.

    A Relation uses SurrealDB's native graph capabilities for
    traversing edges between records. This is the most flexible
    relation type, supporting arbitrary graph structures.

    Args:
        edge: Name of the edge table (e.g., "follows", "likes")
        to: Target model name (string for forward references)
        reverse: Whether to traverse in reverse direction
            - False (default): Forward traversal (->)
            - True: Reverse traversal (<-)

    Returns:
        Annotated type for use in model definition

    Example:
        class User(BaseSurrealModel):
            # People this user follows (outgoing edges)
            following: Relation("follows", "User")

            # People who follow this user (incoming edges)
            followers: Relation("follows", "User", reverse=True)

    SurrealQL equivalent:
        - following: SELECT * FROM user:id->follows->User
        - followers: SELECT * FROM user:id<-follows<-User
    """
    return Annotated[list, _RelationMarker(edge, to, reverse)]


def is_relation_field(field_type: Any) -> bool:
    """
    Check if a field type is a relation type.

    Args:
        field_type: The type annotation to check

    Returns:
        True if the field is a ForeignKey, ManyToMany, or Relation
    """
    origin = get_origin(field_type)

    if origin is Annotated:
        args = get_args(field_type)
        for arg in args:
            if isinstance(arg, (_ForeignKeyMarker, _ManyToManyMarker, _RelationMarker)):
                return True

    return False


def is_foreign_key(field_type: Any) -> bool:
    """Check if a field type is a ForeignKey."""
    origin = get_origin(field_type)
    if origin is Annotated:
        args = get_args(field_type)
        for arg in args:
            if isinstance(arg, _ForeignKeyMarker):
                return True
    return False


def is_many_to_many(field_type: Any) -> bool:
    """Check if a field type is a ManyToMany relation."""
    origin = get_origin(field_type)
    if origin is Annotated:
        args = get_args(field_type)
        for arg in args:
            if isinstance(arg, _ManyToManyMarker):
                return True
    return False


def is_graph_relation(field_type: Any) -> bool:
    """Check if a field type is a graph Relation."""
    origin = get_origin(field_type)
    if origin is Annotated:
        args = get_args(field_type)
        for arg in args:
            if isinstance(arg, _RelationMarker):
                return True
    return False


def get_relation_info(field_type: Any) -> RelationInfo | None:
    """
    Extract relation information from a field type.

    Args:
        field_type: The type annotation to extract from

    Returns:
        RelationInfo if the field is a relation, None otherwise
    """
    if not is_relation_field(field_type):
        return None

    origin = get_origin(field_type)

    if origin is Annotated:
        args = get_args(field_type)
        for arg in args:
            if isinstance(arg, _ForeignKeyMarker):
                return RelationInfo(
                    to_model=arg.to,
                    relation_type="foreign_key",
                    on_delete=arg.on_delete,
                    related_name=arg.related_name,
                )
            elif isinstance(arg, _ManyToManyMarker):
                return RelationInfo(
                    to_model=arg.to,
                    relation_type="many_to_many",
                    through=arg.through,
                    related_name=arg.related_name,
                )
            elif isinstance(arg, _RelationMarker):
                return RelationInfo(
                    to_model=arg.to,
                    relation_type="relation",
                    edge_table=arg.edge,
                    reverse=arg.reverse,
                )

    return None
