"""
Relation management for SurrealDB ORM.

This module provides classes for managing lazy loading and operations
on related objects, including graph traversal capabilities.

Example:
    # Using RelationManager through model instance
    followers = await alice.followers.all()
    await alice.following.add(bob)
    await alice.following.remove(charlie)

    # Multi-hop traversal
    friends_of_friends = await alice.following.following.all()
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal

from .connection_manager import SurrealDBConnectionManager
from .fields.relation import RelationInfo
from .utils import escape_record_id

if TYPE_CHECKING:
    from .model_base import BaseSurrealModel

logger = logging.getLogger(__name__)


class RelationQuerySet:
    """
    QuerySet for chained relation traversal.

    Allows building multi-hop graph queries with filters at each level.

    Example:
        # Simple traversal
        following = await user.following.all()

        # Multi-hop
        friends_of_friends = await user.following.following.all()

        # With filters
        active_fof = await user.following.filter(active=True).following.all()
    """

    def __init__(
        self,
        instance: "BaseSurrealModel",
        relation_info: RelationInfo,
        traversal_path: list[tuple[RelationInfo, dict[str, Any]]] | None = None,
    ):
        """
        Initialize a RelationQuerySet.

        Args:
            instance: The source model instance
            relation_info: Information about the current relation
            traversal_path: List of (relation_info, filters) for chained traversals
        """
        self._instance = instance
        self._relation_info = relation_info
        self._traversal_path = traversal_path or [(relation_info, {})]
        self._filters: dict[str, Any] = {}

    def filter(self, **kwargs: Any) -> "RelationQuerySet":
        """
        Add filters to the current traversal level.

        Args:
            **kwargs: Filter conditions (field=value or field__lookup=value)

        Returns:
            RelationQuerySet for chaining
        """
        # Update filters for the current level
        new_path = self._traversal_path.copy()
        if new_path:
            current_info, current_filters = new_path[-1]
            new_filters = {**current_filters, **kwargs}
            new_path[-1] = (current_info, new_filters)

        new_qs = RelationQuerySet(
            self._instance,
            self._relation_info,
            new_path,
        )
        return new_qs

    def __getattr__(self, name: str) -> "RelationQuerySet":
        """
        Enable chained traversal via attribute access.

        Example:
            user.following.following.all()
        """
        # Try to get the relation from the target model
        # This requires model registry lookup
        from .model_base import get_registered_models

        target_model_name = self._relation_info.to_model
        target_model = None

        for model in get_registered_models():
            if model.__name__ == target_model_name:
                target_model = model
                break

        if target_model is None:
            raise AttributeError(f"Model '{target_model_name}' not found in registry")

        # Check if the target model has this relation
        if hasattr(target_model, "__annotations__"):
            from .fields.relation import get_relation_info as get_rel_info

            annotations = target_model.__annotations__
            if name in annotations:
                field_type = annotations[name]
                rel_info = get_rel_info(field_type)
                if rel_info:
                    # Chain the traversal
                    new_path = self._traversal_path.copy()
                    new_path.append((rel_info, {}))
                    return RelationQuerySet(
                        self._instance,
                        rel_info,
                        new_path,
                    )

        raise AttributeError(f"'{target_model_name}' has no relation '{name}'")

    def _build_traversal_query(self) -> tuple[str, dict[str, Any]]:
        """
        Build the SurrealQL traversal query from the path.

        Returns:
            Tuple of (query_string, variables)
        """
        source_table = self._instance.get_table_name()
        source_id = self._instance.get_id()

        if not source_id:
            raise ValueError("Cannot traverse relations from unsaved instance")

        # Build the traversal path with properly escaped ID
        escaped_id = escape_record_id(source_id)
        path_parts = [f"{source_table}:{escaped_id}"]
        variables: dict[str, Any] = {}
        where_clauses: list[str] = []
        var_counter = 0

        for rel_info, filters in self._traversal_path:
            # Add traversal direction and edge
            if rel_info.relation_type == "relation":
                direction = "<-" if rel_info.reverse else "->"
                path_parts.append(f"{direction}{rel_info.edge_table}{direction}{rel_info.to_model}")
            elif rel_info.relation_type == "many_to_many":
                through = rel_info.through or f"_{rel_info.to_model.lower()}"
                path_parts.append(f"->{through}->{rel_info.to_model}")
            else:  # foreign_key - different handling
                pass

            # Add filters for this level
            for field, value in filters.items():
                var_name = f"filter_{var_counter}"
                var_counter += 1

                # Handle lookup operators
                if "__" in field:
                    parts = field.split("__", 1)
                    field_name = parts[0]
                    operator = parts[1]
                    clause = self._get_filter_clause(field_name, operator, var_name)
                else:
                    clause = f"{field} = ${var_name}"

                where_clauses.append(clause)
                variables[var_name] = value

        query = "SELECT * FROM " + "".join(path_parts)

        if where_clauses:
            query += " WHERE " + " AND ".join(where_clauses)

        return query + ";", variables

    def _get_filter_clause(self, field: str, operator: str, var_name: str) -> str:
        """Convert filter operator to SurrealQL."""
        operator_map = {
            "exact": f"{field} = ${var_name}",
            "gt": f"{field} > ${var_name}",
            "gte": f"{field} >= ${var_name}",
            "lt": f"{field} < ${var_name}",
            "lte": f"{field} <= ${var_name}",
            "in": f"{field} IN ${var_name}",
            "contains": f"{field} CONTAINS ${var_name}",
            "startswith": f"string::starts_with({field}, ${var_name})",
            "endswith": f"string::ends_with({field}, ${var_name})",
            "isnull": f"{field} IS NULL",
        }
        return operator_map.get(operator, f"{field} = ${var_name}")

    async def all(self) -> list[Any]:
        """
        Execute the traversal and return all results.

        Returns:
            List of related model instances
        """
        query, variables = self._build_traversal_query()

        client = await SurrealDBConnectionManager.get_client()
        result = await client.query(query, variables)

        # Convert results to model instances
        from .model_base import get_registered_models

        target_model_name = self._traversal_path[-1][0].to_model
        target_model = None

        for model in get_registered_models():
            if model.__name__ == target_model_name:
                target_model = model
                break

        if target_model and result.all_records:
            return [target_model.from_db(record) for record in result.all_records]

        return list(result.all_records) if result.all_records else []

    async def first(self) -> Any | None:
        """
        Execute the traversal and return the first result.

        Returns:
            First related model instance or None
        """
        results = await self.all()
        return results[0] if results else None

    async def count(self) -> int:
        """
        Count the related records.

        Returns:
            Number of related records
        """
        results = await self.all()
        return len(results)


class RelationManager:
    """
    Manages lazy loading and operations on related objects.

    Provides a Django-like interface for working with relations,
    including add/remove operations and query methods.

    Example:
        # Operations
        await alice.following.add(bob)
        await alice.following.add(charlie, david)
        await alice.following.remove(bob)
        await alice.following.set([charlie, david])
        await alice.following.clear()

        # Queries
        followers = await alice.followers.all()
        active = await alice.followers.filter(active=True)
        count = await alice.followers.count()
        is_following = await alice.following.contains(bob)
    """

    def __init__(
        self,
        instance: "BaseSurrealModel",
        relation_info: RelationInfo,
        field_name: str,
    ):
        """
        Initialize a RelationManager.

        Args:
            instance: The model instance owning this relation
            relation_info: Metadata about the relation
            field_name: Name of the relation field
        """
        self._instance = instance
        self._relation_info = relation_info
        self._field_name = field_name
        self._cache: list[Any] | None = None

    def __repr__(self) -> str:
        return f"<RelationManager({self._instance.__class__.__name__}.{self._field_name})>"

    # ==================== Operations ====================

    async def add(self, *objects: "BaseSurrealModel", **edge_data: Any) -> None:
        """
        Add objects to this relation.

        For graph relations, creates RELATE edges.
        For many-to-many, creates through table records.

        Args:
            *objects: Model instances to relate to
            **edge_data: Additional data to store on the edge

        Example:
            await alice.following.add(bob)
            await alice.following.add(charlie, david, since="2025-01-01")
        """
        if not self._instance.get_id():
            raise ValueError("Cannot add relations to unsaved instance")

        client = await SurrealDBConnectionManager.get_client()
        source_table = self._instance.get_table_name()
        source_id = self._instance.get_id()

        for obj in objects:
            if not obj.get_id():
                raise ValueError("Cannot relate to unsaved instance")

            target_table = obj.get_table_name()
            target_id = obj.get_id()

            if self._relation_info.relation_type == "relation":
                # Use RELATE for graph relations
                edge = self._relation_info.edge_table
                if edge is None:
                    raise ValueError("Relation edge_table is required for graph relations")
                if self._relation_info.reverse:
                    # Reverse relation: target -> edge -> source
                    await client.relate(
                        f"{target_table}:{target_id}",
                        edge,
                        f"{source_table}:{source_id}",
                        edge_data if edge_data else None,
                    )
                else:
                    # Forward relation: source -> edge -> target
                    await client.relate(
                        f"{source_table}:{source_id}",
                        edge,
                        f"{target_table}:{target_id}",
                        edge_data if edge_data else None,
                    )
            elif self._relation_info.relation_type == "many_to_many":
                # Use intermediate table for many-to-many
                through = self._relation_info.through or f"{source_table}_{target_table}"
                await client.relate(
                    f"{source_table}:{source_id}",
                    through,
                    f"{target_table}:{target_id}",
                    edge_data if edge_data else None,
                )

        # Invalidate cache
        self._cache = None

    async def remove(self, *objects: "BaseSurrealModel") -> None:
        """
        Remove objects from this relation.

        Deletes the edge records connecting the objects.

        Args:
            *objects: Model instances to unrelate

        Example:
            await alice.following.remove(bob)
        """
        if not self._instance.get_id():
            raise ValueError("Cannot remove relations from unsaved instance")

        client = await SurrealDBConnectionManager.get_client()
        source_table = self._instance.get_table_name()
        source_id = self._instance.get_id()

        for obj in objects:
            if not obj.get_id():
                continue

            target_table = obj.get_table_name()
            target_id = obj.get_id()

            if self._relation_info.relation_type == "relation":
                edge = self._relation_info.edge_table
                if self._relation_info.reverse:
                    # Delete edges where target -> edge -> source
                    query = f"DELETE {edge} WHERE in = {target_table}:{target_id} AND out = {source_table}:{source_id};"
                else:
                    # Delete edges where source -> edge -> target
                    query = f"DELETE {edge} WHERE in = {source_table}:{source_id} AND out = {target_table}:{target_id};"
                await client.query(query)
            elif self._relation_info.relation_type == "many_to_many":
                through = self._relation_info.through or f"{source_table}_{target_table}"
                query = f"DELETE {through} WHERE in = {source_table}:{source_id} AND out = {target_table}:{target_id};"
                await client.query(query)

        # Invalidate cache
        self._cache = None

    async def set(self, objects: list["BaseSurrealModel"]) -> None:
        """
        Replace all relations with the given objects.

        Clears existing relations and adds the new ones.

        Args:
            objects: List of model instances to set as relations

        Example:
            await alice.following.set([bob, charlie])
        """
        await self.clear()
        if objects:
            await self.add(*objects)

    async def clear(self) -> None:
        """
        Remove all relations.

        Example:
            await alice.following.clear()
        """
        if not self._instance.get_id():
            raise ValueError("Cannot clear relations from unsaved instance")

        client = await SurrealDBConnectionManager.get_client()
        source_table = self._instance.get_table_name()
        source_id = self._instance.get_id()

        if self._relation_info.relation_type == "relation":
            edge = self._relation_info.edge_table
            if self._relation_info.reverse:
                # Delete all edges pointing to this record
                query = f"DELETE {edge} WHERE out = {source_table}:{source_id};"
            else:
                # Delete all edges from this record
                query = f"DELETE {edge} WHERE in = {source_table}:{source_id};"
            await client.query(query)
        elif self._relation_info.relation_type == "many_to_many":
            through = self._relation_info.through or f"{source_table}_"
            query = f"DELETE {through} WHERE in = {source_table}:{source_id};"
            await client.query(query)

        # Invalidate cache
        self._cache = None

    # ==================== Queries ====================

    async def all(self) -> list[Any]:
        """
        Get all related objects.

        Returns:
            List of related model instances
        """
        if self._cache is not None:
            return self._cache

        qs = RelationQuerySet(self._instance, self._relation_info)
        results = await qs.all()
        self._cache = results
        return results

    def filter(self, **kwargs: Any) -> RelationQuerySet:
        """
        Filter related objects.

        Returns a RelationQuerySet that can be further filtered or traversed.

        Args:
            **kwargs: Filter conditions

        Returns:
            RelationQuerySet for chaining

        Example:
            active_followers = await alice.followers.filter(active=True)
        """
        qs = RelationQuerySet(self._instance, self._relation_info)
        return qs.filter(**kwargs)

    async def count(self) -> int:
        """
        Count related objects.

        Returns:
            Number of related records
        """
        results = await self.all()
        return len(results)

    async def contains(self, obj: "BaseSurrealModel") -> bool:
        """
        Check if an object is in this relation.

        Args:
            obj: Model instance to check

        Returns:
            True if the object is related
        """
        if not obj.get_id():
            return False

        results = await self.all()
        target_id = obj.get_id()

        for related in results:
            if hasattr(related, "get_id") and related.get_id() == target_id:
                return True

        return False

    async def first(self) -> Any | None:
        """
        Get the first related object.

        Returns:
            First related model instance or None
        """
        results = await self.all()
        return results[0] if results else None

    async def exists(self) -> bool:
        """
        Check if any related objects exist.

        Returns:
            True if there are any related records
        """
        count = await self.count()
        return count > 0

    # ==================== Chained Traversal ====================

    def __getattr__(self, name: str) -> Any:
        """
        Enable chained traversal via attribute access.

        Example:
            friends_of_friends = await alice.following.following.all()
        """
        qs = RelationQuerySet(self._instance, self._relation_info)
        return getattr(qs, name)


class RelationDescriptor:
    """
    Descriptor for transparent relation access on models.

    This descriptor is automatically applied to relation fields,
    enabling access like `user.followers` to return a RelationManager.

    Example:
        class User(BaseSurrealModel):
            followers: Relation("follows", "User", reverse=True)

        # Access returns RelationManager
        manager = user.followers
        followers = await manager.all()
    """

    def __init__(self, field_name: str, relation_info: RelationInfo):
        """
        Initialize the descriptor.

        Args:
            field_name: Name of the relation field
            relation_info: Metadata about the relation
        """
        self.field_name = field_name
        self.relation_info = relation_info
        self._cache_attr = f"_relation_cache_{field_name}"

    def __get__(
        self,
        obj: "BaseSurrealModel | None",
        objtype: type["BaseSurrealModel"] | None = None,
    ) -> "RelationManager | RelationDescriptor":
        """
        Get the RelationManager for this field.

        Args:
            obj: Model instance (None if accessed on class)
            objtype: Model class

        Returns:
            RelationManager if accessed on instance, self if on class
        """
        if obj is None:
            # Accessed on class, return descriptor
            return self

        # Check for cached manager
        if not hasattr(obj, self._cache_attr):
            manager = RelationManager(obj, self.relation_info, self.field_name)
            setattr(obj, self._cache_attr, manager)

        cached_manager: RelationManager = getattr(obj, self._cache_attr)
        return cached_manager

    def __set__(
        self,
        obj: "BaseSurrealModel",
        value: Any,
    ) -> None:
        """
        Setting relation values directly is not supported.

        Use the RelationManager methods (add, remove, set) instead.
        """
        raise AttributeError(
            f"Cannot set '{self.field_name}' directly. Use await {obj.__class__.__name__}.{self.field_name}.set([...]) instead."
        )


def get_related_objects(
    instance: "BaseSurrealModel",
    relation_name: str,
    direction: Literal["out", "in", "both"] = "out",
) -> RelationQuerySet:
    """
    Get related objects through a relation.

    This is a utility function for programmatic access to relations.

    Args:
        instance: Source model instance
        relation_name: Name of the edge table
        direction: Traversal direction ("out", "in", or "both")

    Returns:
        RelationQuerySet for the relation
    """
    rel_info = RelationInfo(
        to_model="",  # Will be determined by query results
        relation_type="relation",
        edge_table=relation_name,
        reverse=(direction == "in"),
    )
    return RelationQuerySet(instance, rel_info)
