from typing import Any, Literal, Self, cast, TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, model_validator

from .connection_manager import SurrealDBConnectionManager
from .types import SchemaMode, TableType

if TYPE_CHECKING:
    from surreal_sdk.transaction import BaseTransaction, HTTPTransaction

import logging


class SurrealDbError(Exception):
    """Error from SurrealDB operations."""

    pass


logger = logging.getLogger(__name__)

# Global registry of all SurrealDB models for migration introspection
_MODEL_REGISTRY: list[type["BaseSurrealModel"]] = []


def get_registered_models() -> list[type["BaseSurrealModel"]]:
    """
    Get all registered SurrealDB models.

    Returns:
        List of all model classes that inherit from BaseSurrealModel
    """
    return _MODEL_REGISTRY.copy()


def clear_model_registry() -> None:
    """
    Clear the model registry. Useful for testing.
    """
    _MODEL_REGISTRY.clear()


def _parse_record_id(record_id: Any) -> str | None:
    """
    Parse a record ID from various formats.
    SurrealDB returns IDs as 'table:id' strings.
    """
    if record_id is None:
        return None
    record_str = str(record_id)
    if ":" in record_str:
        return record_str.split(":", 1)[1]
    return record_str


class SurrealConfigDict(ConfigDict):
    """
    SurrealConfigDict is a configuration dictionary for SurrealDB models.

    Extends Pydantic's ConfigDict with SurrealDB-specific options for
    table types, schema modes, and authentication settings.

    Attributes:
        primary_key: The primary key field name for the model
        table_name: Override the default table name (default: class name)
        table_type: Table classification (NORMAL, USER, STREAM, HASH)
        schema_mode: Schema enforcement mode (SCHEMAFULL, SCHEMALESS)
        changefeed: Changefeed duration for STREAM tables (e.g., "7d")
        permissions: Table-level permissions dict {"select": "...", "update": "..."}
        identifier_field: Field used for signin (USER type, default: "email")
        password_field: Field containing password (USER type, default: "password")
        token_duration: JWT token duration (USER type, default: "15m")
        session_duration: Session duration (USER type, default: "12h")
    """

    primary_key: str | None
    table_name: str | None
    table_type: TableType | None
    schema_mode: SchemaMode | None
    changefeed: str | None
    permissions: dict[str, str] | None
    identifier_field: str | None
    password_field: str | None
    token_duration: str | None
    session_duration: str | None


class BaseSurrealModel(BaseModel):
    """
    Base class for models interacting with SurrealDB.

    All models that interact with SurrealDB should inherit from this class.
    Models are automatically registered for migration introspection.

    Example:
        class User(BaseSurrealModel):
            model_config = SurrealConfigDict(
                table_type=TableType.USER,
                schema_mode=SchemaMode.SCHEMAFULL,
            )

            id: str | None = None
            email: str
            password: Encrypted
    """

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Register subclasses in the model registry for migration introspection."""
        super().__init_subclass__(**kwargs)
        # Only register concrete models, not intermediate base classes
        if cls.__name__ != "BaseSurrealModel" and not cls.__name__.startswith("_"):
            if cls not in _MODEL_REGISTRY:
                _MODEL_REGISTRY.append(cls)

    @classmethod
    def get_table_name(cls) -> str:
        """
        Get the table name for the model.

        Returns the table_name from model_config if set,
        otherwise returns the class name.
        """
        if hasattr(cls, "model_config"):
            table_name = cls.model_config.get("table_name", None)
            if isinstance(table_name, str):
                return table_name
        return cls.__name__

    @classmethod
    def get_table_type(cls) -> TableType:
        """
        Get the table type classification for the model.

        Returns:
            TableType enum value (default: NORMAL)
        """
        if hasattr(cls, "model_config"):
            table_type = cls.model_config.get("table_type", None)
            if isinstance(table_type, TableType):
                return table_type
        return TableType.NORMAL

    @classmethod
    def get_schema_mode(cls) -> SchemaMode:
        """
        Get the schema mode for the model.

        USER tables are always SCHEMAFULL.
        HASH tables default to SCHEMALESS.
        All others default to SCHEMAFULL.

        Returns:
            SchemaMode enum value
        """
        table_type = cls.get_table_type()

        # USER tables must be SCHEMAFULL
        if table_type == TableType.USER:
            return SchemaMode.SCHEMAFULL

        if hasattr(cls, "model_config"):
            schema_mode = cls.model_config.get("schema_mode", None)
            if isinstance(schema_mode, SchemaMode):
                return schema_mode

        # HASH tables default to SCHEMALESS
        if table_type == TableType.HASH:
            return SchemaMode.SCHEMALESS

        return SchemaMode.SCHEMAFULL

    @classmethod
    def get_changefeed(cls) -> str | None:
        """
        Get the changefeed duration for the model.

        Returns:
            Changefeed duration string (e.g., "7d") or None
        """
        if hasattr(cls, "model_config"):
            changefeed = cls.model_config.get("changefeed", None)
            return str(changefeed) if changefeed is not None else None
        return None

    @classmethod
    def get_permissions(cls) -> dict[str, str]:
        """
        Get the table permissions for the model.

        Returns:
            Dict of permission type to condition expression
        """
        if hasattr(cls, "model_config"):
            permissions = cls.model_config.get("permissions", None)
            if isinstance(permissions, dict):
                return permissions
        return {}

    @classmethod
    def get_identifier_field(cls) -> str:
        """
        Get the identifier field for USER type tables.

        Returns:
            Field name used for signin (default: "email")
        """
        if hasattr(cls, "model_config"):
            field = cls.model_config.get("identifier_field", None)
            if isinstance(field, str):
                return field
        return "email"

    @classmethod
    def get_password_field(cls) -> str:
        """
        Get the password field for USER type tables.

        Returns:
            Field name containing password (default: "password")
        """
        if hasattr(cls, "model_config"):
            field = cls.model_config.get("password_field", None)
            if isinstance(field, str):
                return field
        return "password"

    @classmethod
    def get_index_primary_key(cls) -> str | None:
        """
        Get the primary key field name for the model.
        """
        if hasattr(cls, "model_config"):  # pragma: no cover
            primary_key = cls.model_config.get("primary_key", None)
            if isinstance(primary_key, str):
                return primary_key

        return None

    def get_id(self) -> str | None:
        """
        Get the ID of the model instance.
        """
        if hasattr(self, "id"):
            id_value = getattr(self, "id")
            return str(id_value) if id_value is not None else None

        if hasattr(self, "model_config"):
            primary_key = self.model_config.get("primary_key", None)
            if isinstance(primary_key, str) and hasattr(self, primary_key):
                primary_key_value = getattr(self, primary_key)
                return str(primary_key_value) if primary_key_value is not None else None

        return None  # pragma: no cover

    @classmethod
    def from_db(cls, record: dict | list | None) -> Self | list[Self]:
        """
        Create an instance from a SurrealDB record.
        """
        if record is None:
            raise cls.DoesNotExist("Record not found.")

        if isinstance(record, list):
            return [cls.from_db(rs) for rs in record]  # type: ignore

        return cls(**record)

    @model_validator(mode="before")
    @classmethod
    def set_data(cls, data: Any) -> Any:
        """
        Parse the ID from SurrealDB format (table:id) to just id.
        """
        if isinstance(data, dict):  # pragma: no cover
            if "id" in data:
                data["id"] = _parse_record_id(data["id"])
            return data

    async def refresh(self) -> None:
        """
        Refresh the model instance from the database.
        """
        if not self.get_id():
            raise SurrealDbError("Can't refresh data, not recorded yet.")  # pragma: no cover

        client = await SurrealDBConnectionManager.get_client()
        result = await client.select(f"{self.get_table_name()}:{self.get_id()}")

        # SDK returns RecordsResponse with .records list
        if result.is_empty:
            raise SurrealDbError("Can't refresh data, no record found.")  # pragma: no cover

        record = result.first
        if record is None:
            raise SurrealDbError("Can't refresh data, no record found.")  # pragma: no cover

        # Update instance fields from the record
        for key, value in record.items():
            if key == "id":
                value = _parse_record_id(value)
            if hasattr(self, key):
                setattr(self, key, value)
        return None

    async def save(self, tx: "BaseTransaction | None" = None) -> Self:
        """
        Save the model instance to the database.

        Args:
            tx: Optional transaction to use for this operation.
                If provided, the operation will be part of the transaction.

        Example:
            # Without transaction
            await user.save()

            # With transaction
            async with SurrealDBConnectionManager.transaction() as tx:
                await user.save(tx=tx)
        """
        if tx is not None:
            # Use transaction
            data = self.model_dump(exclude={"id"})
            id = self.get_id()
            table = self.get_table_name()

            if id is not None:
                thing = f"{table}:{id}"
                await tx.create(thing, data)
                return self

            # Auto-generate ID - create without specific ID
            await tx.create(table, data)
            return self

        # Original behavior without transaction
        client = await SurrealDBConnectionManager.get_client()
        data = self.model_dump(exclude={"id"})
        id = self.get_id()
        table = self.get_table_name()

        if id is not None:
            thing = f"{table}:{id}"
            await client.create(thing, data)
            return self

        # Auto-generate the ID
        result = await client.create(table, data)  # pragma: no cover

        # SDK returns RecordResponse
        if not result.exists:
            raise SurrealDbError("Can't save data, no record returned.")  # pragma: no cover

        obj = self.from_db(cast(dict | list | None, result.record))
        if isinstance(obj, type(self)):
            self = obj
            return self

        raise SurrealDbError("Can't save data, no record returned.")  # pragma: no cover

    async def update(self, tx: "BaseTransaction | None" = None) -> Any:
        """
        Update the model instance to the database.

        Args:
            tx: Optional transaction to use for this operation.
        """
        data = self.model_dump(exclude={"id"})
        id = self.get_id()

        if id is None:
            raise SurrealDbError("Can't update data, no id found.")

        thing = f"{self.__class__.__name__}:{id}"

        if tx is not None:
            await tx.update(thing, data)
            return None

        client = await SurrealDBConnectionManager.get_client()
        result = await client.update(thing, data)
        return result.records

    @classmethod
    def get(cls, item: str) -> str:
        """
        Get the table name for the model.
        """
        return f"{cls.__name__}:{item}"

    async def merge(self, tx: "BaseTransaction | None" = None, **data: Any) -> Any:
        """
        Merge (partial update) the model instance in the database.

        Args:
            tx: Optional transaction to use for this operation.
            **data: Fields to update.
        """
        data_set = {key: value for key, value in data.items()}

        id = self.get_id()
        if not id:
            raise SurrealDbError(f"No Id for the data to merge: {data}")

        thing = f"{self.get_table_name()}:{id}"

        if tx is not None:
            await tx.merge(thing, data_set)
            return

        client = await SurrealDBConnectionManager.get_client()
        await client.merge(thing, data_set)
        await self.refresh()

    async def delete(self, tx: "BaseTransaction | None" = None) -> None:
        """
        Delete the model instance from the database.

        Args:
            tx: Optional transaction to use for this operation.
        """
        id = self.get_id()
        thing = f"{self.get_table_name()}:{id}"

        if tx is not None:
            await tx.delete(thing)
            logger.info(f"Record deleted (in transaction) -> {thing}.")
            return

        client = await SurrealDBConnectionManager.get_client()
        result = await client.delete(thing)

        if not result.success:
            raise SurrealDbError(f"Can't delete Record id -> '{id}' not found!")

        logger.info(f"Record deleted -> {result.deleted!r}.")

    @model_validator(mode="after")
    def check_config(self) -> Self:
        """
        Check the model configuration.
        """

        if not self.get_index_primary_key() and not hasattr(self, "id"):
            raise SurrealDbError(  # pragma: no cover
                "Can't create model, the model needs either 'id' field or primary_key in 'model_config'."
            )

        return self

    @classmethod
    def objects(cls) -> Any:
        """
        Return a QuerySet for the model class.
        """
        from .query_set import QuerySet

        return QuerySet(cls)

    @classmethod
    async def transaction(cls) -> "HTTPTransaction":
        """
        Create a transaction context manager for atomic operations.

        This is a convenience method that delegates to SurrealDBConnectionManager.

        Usage:
            async with User.transaction() as tx:
                user1 = User(id="1", name="Alice")
                await user1.save(tx=tx)
                user2 = User(id="2", name="Bob")
                await user2.save(tx=tx)
                # Auto-commit on success, auto-rollback on exception

        Returns:
            HTTPTransaction context manager
        """
        return await SurrealDBConnectionManager.transaction()

    # ==================== Graph Relation Methods ====================

    async def relate(
        self,
        relation: str,
        to: "BaseSurrealModel",
        tx: "BaseTransaction | None" = None,
        **edge_data: Any,
    ) -> dict[str, Any]:
        """
        Create a graph relation (edge) to another record.

        This method creates a SurrealDB RELATE edge between this record
        and the target record. Optional edge data can be stored on the relation.

        Args:
            relation: Name of the edge table (e.g., "follows", "likes")
            to: Target model instance to relate to
            tx: Optional transaction to use for this operation
            **edge_data: Additional data to store on the edge record

        Returns:
            dict: The created edge record

        Example:
            # Simple relation
            await alice.relate("follows", bob)

            # With edge data
            await alice.relate("follows", bob, since="2025-01-01", strength="strong")

            # In a transaction
            async with User.transaction() as tx:
                await alice.relate("follows", bob, tx=tx)
                await alice.relate("follows", charlie, tx=tx)

        SurrealQL equivalent:
            RELATE users:alice->follows->users:bob SET since = '2025-01-01';
        """
        source_id = self.get_id()
        target_id = to.get_id()

        if not source_id:
            raise SurrealDbError("Cannot create relation from unsaved instance")
        if not target_id:
            raise SurrealDbError("Cannot create relation to unsaved instance")

        source_table = self.get_table_name()
        target_table = to.get_table_name()

        from_thing = f"{source_table}:{source_id}"
        to_thing = f"{target_table}:{target_id}"

        if tx is not None:
            await tx.relate(from_thing, relation, to_thing, edge_data if edge_data else None)
            return {"in": from_thing, "out": to_thing, **edge_data}

        client = await SurrealDBConnectionManager.get_client()
        result = await client.relate(
            from_thing,
            relation,
            to_thing,
            edge_data if edge_data else None,
        )

        if result.exists and result.record:
            return dict(result.record)
        return {"in": from_thing, "out": to_thing, **edge_data}

    async def remove_relation(
        self,
        relation: str,
        to: "BaseSurrealModel",
        tx: "BaseTransaction | None" = None,
    ) -> None:
        """
        Remove a graph relation (edge) to another record.

        This method deletes the edge record(s) between this record
        and the target record.

        Args:
            relation: Name of the edge table (e.g., "follows", "likes")
            to: Target model instance to unrelate
            tx: Optional transaction to use for this operation

        Example:
            # Remove relation
            await alice.remove_relation("follows", bob)

            # In a transaction
            async with User.transaction() as tx:
                await alice.remove_relation("follows", bob, tx=tx)
                await alice.remove_relation("follows", charlie, tx=tx)
        """
        source_id = self.get_id()
        target_id = to.get_id()

        if not source_id:
            raise SurrealDbError("Cannot remove relation from unsaved instance")
        if not target_id:
            raise SurrealDbError("Cannot remove relation to unsaved instance")

        source_table = self.get_table_name()
        target_table = to.get_table_name()

        # Delete edge where in=source and out=target
        query = f"DELETE {relation} WHERE in = {source_table}:{source_id} AND out = {target_table}:{target_id};"

        if tx is not None:
            await tx.query(query)
            return

        client = await SurrealDBConnectionManager.get_client()
        await client.query(query)

    async def get_related(
        self,
        relation: str,
        direction: Literal["out", "in", "both"] = "out",
        model_class: type["BaseSurrealModel"] | None = None,
    ) -> list["BaseSurrealModel"] | list[dict[str, Any]]:
        """
        Get records related through a graph relation.

        This method queries SurrealDB's graph traversal capabilities
        to find related records.

        Args:
            relation: Name of the edge table (e.g., "follows", "likes")
            direction: Traversal direction
                - "out": Outgoing edges (this record -> relation -> target)
                - "in": Incoming edges (source -> relation -> this record)
                - "both": Both directions
            model_class: Optional model class to convert results to instances

        Returns:
            List of related model instances or dicts if model_class is None

        Example:
            # Get users this user follows
            following = await alice.get_related("follows", direction="out")

            # Get users who follow this user
            followers = await alice.get_related("follows", direction="in")

            # With model class for typed results
            followers = await alice.get_related("follows", direction="in", model_class=User)

        SurrealQL equivalent:
            - out: SELECT out FROM follows WHERE in = users:alice FETCH out;
            - in: SELECT in FROM follows WHERE out = users:alice FETCH in;
        """
        source_id = self.get_id()
        if not source_id:
            raise SurrealDbError("Cannot query relations from unsaved instance")

        source_table = self.get_table_name()
        source_thing = f"{source_table}:{source_id}"

        client = await SurrealDBConnectionManager.get_client()
        records: list[dict[str, Any]] = []

        # Query edge table and fetch related records
        # For outgoing: get 'out' field where 'in' matches source
        # For incoming: get 'in' field where 'out' matches source
        if direction == "out":
            query = f"SELECT out FROM {relation} WHERE in = {source_thing} FETCH out;"
            result = await client.query(query)
            for row in result.all_records or []:
                if isinstance(row.get("out"), dict):
                    records.append(row["out"])
        elif direction == "in":
            query = f"SELECT in FROM {relation} WHERE out = {source_thing} FETCH in;"
            result = await client.query(query)
            for row in result.all_records or []:
                if isinstance(row.get("in"), dict):
                    records.append(row["in"])
        else:  # both
            # Get both outgoing and incoming relations
            query_out = f"SELECT out FROM {relation} WHERE in = {source_thing} FETCH out;"
            query_in = f"SELECT in FROM {relation} WHERE out = {source_thing} FETCH in;"
            result_out = await client.query(query_out)
            result_in = await client.query(query_in)
            for row in result_out.all_records or []:
                if isinstance(row.get("out"), dict):
                    records.append(row["out"])
            for row in result_in.all_records or []:
                if isinstance(row.get("in"), dict):
                    records.append(row["in"])

        if model_class is not None:
            instances: list[BaseSurrealModel] = []
            for record in records:
                instance = model_class.from_db(record)
                if isinstance(instance, list):
                    instances.extend(instance)
                else:
                    instances.append(instance)
            return instances

        return records

    class DoesNotExist(Exception):
        """
        Exception raised when a model instance does not exist.
        """

        pass
