import logging
import re as _re
from datetime import datetime, timezone
from typing import Any, Literal, Self, TYPE_CHECKING, get_args, get_origin

from pydantic import BaseModel, ConfigDict, PrivateAttr, model_validator

from .connection_manager import SurrealDBConnectionManager
from .types import SchemaMode, TableType
from .utils import format_thing, parse_record_id
from . import signals as model_signals

if TYPE_CHECKING:
    from surreal_sdk.transaction import BaseTransaction, HTTPTransaction

_SAFE_IDENTIFIER_RE = _re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


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
    SurrealDB returns IDs as 'table:id' strings or RecordId objects (with CBOR).
    """
    if record_id is None:
        return None

    # Handle RecordId objects from CBOR responses
    # Import here to avoid circular imports
    from surreal_sdk.protocol.cbor import RecordId

    if isinstance(record_id, RecordId):
        # RecordId.id can be a string or another RecordId (nested)
        id_value = record_id.id
        if isinstance(id_value, RecordId):
            # Nested RecordId - extract the innermost id
            return str(id_value.id)
        return str(id_value)

    record_str = str(record_id)
    if ":" in record_str:
        return record_str.split(":", 1)[1]
    return record_str


def _parse_datetime(value: Any) -> Any:
    """
    Parse a datetime value from SurrealDB.

    SurrealDB returns datetime in different formats depending on the protocol:
    - JSON: ISO 8601 strings (e.g., "2026-02-02T13:21:23.641315924Z")
    - CBOR: Can be Python datetime objects, or [seconds, nanoseconds] arrays
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            # Try parsing ISO format (with or without timezone)
            # SurrealDB returns format like "2026-02-02T13:21:23.641315924Z"
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    if isinstance(value, (list, tuple)) and len(value) == 2:
        # CBOR format: [seconds_since_epoch, nanoseconds]
        try:
            seconds, nanoseconds = value
            # Convert nanoseconds to microseconds (Python datetime precision)
            microseconds = nanoseconds // 1000
            return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(microsecond=microseconds)
        except (TypeError, ValueError, OSError):
            pass
    return value  # Return as-is if we can't parse


def _is_datetime_field(field_type: Any) -> bool:
    """Check if a field type is datetime or Optional[datetime]."""
    # Handle Optional[datetime] which is Union[datetime, None]
    origin = get_origin(field_type)
    if origin is not None:
        args = get_args(field_type)
        return datetime in args
    return field_type is datetime


def _convert_record_id_to_string(value: Any) -> Any:
    """
    Convert a RecordId object to its string representation.

    For non-id fields (like foreign keys), RecordId objects should be
    converted to "table:id" string format.

    Args:
        value: Any value, possibly a RecordId object

    Returns:
        String "table:id" if value is a RecordId, otherwise the original value
    """
    # Check if value is a RecordId from surreal_sdk (duck typing with module validation)
    # This avoids import path issues between src.surreal_sdk and surreal_sdk
    if (
        hasattr(value, "table")
        and hasattr(value, "id")
        and value.__class__.__name__ == "RecordId"
        and "surreal" in value.__class__.__module__
    ):
        return str(value)  # Returns "table:id" format
    return value


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
        server_fields: List of field names that are server-generated and should
            be excluded from save/update operations (e.g., ["created_at", "updated_at"]).
            These fields are populated by SurrealDB's VALUE clause and should not be
            sent back during updates.
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
    server_fields: list[str] | None


class BaseSurrealModel(BaseModel):
    """
    Base class for models interacting with SurrealDB.

    All models that interact with SurrealDB should inherit from this class.
    Models are automatically registered for migration introspection.

    Field aliases are supported for mapping Python field names to different
    database column names:

    Example:
        class User(BaseSurrealModel):
            model_config = SurrealConfigDict(
                table_type=TableType.USER,
                schema_mode=SchemaMode.SCHEMAFULL,
            )

            id: str | None = None
            email: str
            password: Encrypted

        # With field alias (password in Python, password_hash in DB):
        class User(BaseSurrealModel):
            password: str = Field(alias="password_hash")
    """

    # Default config that enables alias support:
    # - populate_by_name: Accept both field name and alias when loading from DB
    model_config = ConfigDict(
        populate_by_name=True,
    )

    # Private attribute to track if this instance has been persisted to the database.
    # This helps distinguish between create (first save) and update (subsequent saves).
    _db_persisted: bool = PrivateAttr(default=False)

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

    @classmethod
    def get_server_fields(cls) -> set[str]:
        """
        Get the list of server-generated field names.

        Server fields are populated by SurrealDB (e.g., via VALUE time::now())
        and should be excluded from save/update operations.

        Returns:
            Set of field names to exclude from save/update operations.
        """
        if hasattr(cls, "model_config"):
            server_fields = cls.model_config.get("server_fields", None)
            if isinstance(server_fields, list):
                return set(server_fields)
        return set()

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

        This method handles type coercion for fields that need special handling:
        - datetime fields: SurrealDB may return ISO strings or Python datetime objects
          with timezone info that needs normalization for Pydantic validation.
        - id field: RecordId objects from CBOR responses are converted to strings.
        """
        if record is None:
            raise cls.DoesNotExist("Record not found.")

        if isinstance(record, list):
            return [cls.from_db(rs) for rs in record]  # type: ignore

        # Preprocess record data before Pydantic validation
        # This handles datetime parsing and RecordId conversion
        processed_record = cls._preprocess_db_record(record)

        instance = cls(**processed_record)
        instance._db_persisted = True
        # Clear fields_set so DB-loaded fields aren't considered "user-set"
        # This allows exclude_unset=True to work correctly on subsequent saves
        object.__setattr__(instance, "__pydantic_fields_set__", set())
        return instance

    @classmethod
    def _preprocess_db_record(cls, record: dict[str, Any]) -> dict[str, Any]:
        """
        Preprocess a database record before Pydantic validation.

        Handles type coercion for:
        - datetime fields: Parse ISO strings and normalize timezone-aware datetimes
        - id field: Convert RecordId objects to just the ID part (strips table prefix)
        - Other fields with RecordId: Convert to "table:id" string format

        This preprocessing ensures that values from SurrealDB (especially via CBOR)
        are in formats that Pydantic can validate correctly.
        """
        field_types = cls.model_fields
        processed: dict[str, Any] = {}

        for key, value in record.items():
            if key == "id":
                # For the 'id' field, extract just the ID part (strip table prefix)
                value = _parse_record_id(value)
            else:
                # For other fields, convert RecordId objects to "table:id" strings
                # This is needed for foreign key fields that reference other records
                value = _convert_record_id_to_string(value)

                # Check if this field is a datetime type and parse if needed
                if key in field_types:
                    field_info = field_types[key]
                    if _is_datetime_field(field_info.annotation):
                        value = _parse_datetime(value)

            processed[key] = value

        return processed

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

    def _update_from_db(self, record: dict[str, Any]) -> None:
        """
        Update instance fields from a database record without marking them as user-set.

        This preserves the original __pydantic_fields_set__ so that exclude_unset=True
        continues to work correctly on subsequent saves.

        Also handles type coercion for datetime fields (SurrealDB returns ISO strings).

        Args:
            record: Dictionary of field values from the database.
        """
        # Store original fields_set to preserve user-set tracking
        original_fields_set = self.__pydantic_fields_set__.copy()

        # Get field type annotations for datetime parsing
        field_types = self.__class__.model_fields

        for key, value in record.items():
            if key == "id":
                # For the 'id' field, extract just the ID part (strip table prefix)
                value = _parse_record_id(value)
            else:
                # For other fields, convert RecordId objects to "table:id" strings
                value = _convert_record_id_to_string(value)

                # Check if this field is a datetime type and parse if needed
                if key in field_types:
                    field_info = field_types[key]
                    if _is_datetime_field(field_info.annotation):
                        value = _parse_datetime(value)

            if hasattr(self, key):
                setattr(self, key, value)

        # Restore original fields_set - only user-set fields should be marked
        # DB-loaded fields should not be considered as "set" for exclude_unset
        object.__setattr__(self, "__pydantic_fields_set__", original_fields_set)

        # Mark as persisted since we just loaded data from DB
        self._db_persisted = True

    async def refresh(self) -> None:
        """
        Refresh the model instance from the database.
        """
        record_id = self.get_id()
        if not record_id:
            raise SurrealDbError("Can't refresh data, not recorded yet.")  # pragma: no cover

        client = await SurrealDBConnectionManager.get_client()
        thing = format_thing(self.get_table_name(), record_id)
        result = await client.select(thing)

        # SDK returns RecordsResponse with .records list
        if result.is_empty:
            raise SurrealDbError("Can't refresh data, no record found.")  # pragma: no cover

        record = result.first
        if record is None:
            raise SurrealDbError("Can't refresh data, no record found.")  # pragma: no cover

        # Update instance fields without marking them as user-set
        self._update_from_db(record)
        return None

    async def save(self, tx: "BaseTransaction | None" = None) -> Self:
        """
        Save the model instance to the database.

        For persisted records: uses merge() for partial update, only sending
        explicitly set fields (preserving server-side values like timestamps).
        For new records with ID: uses upsert() to create or fully replace.
        For new records without ID: uses create() to auto-generate an ID.

        Signals:
            - pre_save: Sent before the save operation.
            - around_save: Wraps the DB operation (generator-based).
            - post_save: Sent after the save operation completes.

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
        # Determine if this is a create or update
        created = not self._db_persisted

        # Send pre_save signal
        await model_signals.pre_save.send(
            sender=self.__class__,
            instance=self,
            created=created,
            tx=tx,
        )

        # Build exclude set: always exclude 'id' and any server-generated fields
        exclude_fields = {"id"} | self.get_server_fields()
        id = self.get_id()
        table = self.get_table_name()
        data = self.model_dump(exclude=exclude_fields, exclude_unset=True, by_alias=True)

        # Wrap the DB operation with around_save signal
        async with model_signals.around_save.wrap(
            sender=self.__class__,
            instance=self,
            created=created,
            tx=tx,
        ):
            await self._execute_save(tx, table, id, data, created)

        # Send post_save signal
        await model_signals.post_save.send(
            sender=self.__class__,
            instance=self,
            created=created,
            tx=tx,
        )
        return self

    async def _execute_save(
        self,
        tx: "BaseTransaction | None",
        table: str,
        id: str | None,
        data: dict[str, Any],
        created: bool,
    ) -> None:
        """Execute the actual save operation (wrapped by around_save signal)."""
        if tx is not None:
            # Use transaction
            if self._db_persisted and id is not None:
                # Already persisted: use merge for partial update
                thing = format_thing(table, id)
                await tx.merge(thing, data)
            elif id is not None:
                # New record with user-provided ID: use upsert
                thing = format_thing(table, id)
                await tx.upsert(thing, data)
                self._db_persisted = True
            else:
                # Auto-generate ID
                await tx.create(table, data)
                self._db_persisted = True
            return

        # Without transaction
        client = await SurrealDBConnectionManager.get_client()

        if self._db_persisted and id is not None:
            # Already persisted: use merge for partial update
            thing = format_thing(table, id)
            await client.merge(thing, data)
            return

        if id is not None:
            # New record with user-provided ID: use upsert
            thing = format_thing(table, id)
            await client.upsert(thing, data)
            self._db_persisted = True
            return

        # Auto-generate the ID
        result = await client.create(table, data)

        # SDK returns RecordResponse
        if not result.exists:
            raise SurrealDbError("Can't save data, no record returned.")  # pragma: no cover

        # Update self's attributes from the database response
        record = result.record
        if isinstance(record, dict):
            self._update_from_db(record)
            return

        raise SurrealDbError("Can't save data, no record returned.")  # pragma: no cover

    async def update(self, tx: "BaseTransaction | None" = None) -> Any:
        """
        Update the model instance to the database.

        Uses merge() to only update the specified fields, preserving
        any fields that weren't explicitly set.

        Signals:
            - pre_update: Sent before the update operation.
            - around_update: Wraps the DB operation (generator-based).
            - post_update: Sent after the update operation completes.

        Args:
            tx: Optional transaction to use for this operation.
        """
        # Build exclude set: always exclude 'id' and any server-generated fields
        exclude_fields = {"id"} | self.get_server_fields()
        data = self.model_dump(exclude=exclude_fields, exclude_unset=True, by_alias=True)
        record_id = self.get_id()

        if record_id is None:
            raise SurrealDbError("Can't update data, no id found.")

        # Send pre_update signal
        await model_signals.pre_update.send(
            sender=self.__class__,
            instance=self,
            update_fields=data,
            tx=tx,
        )

        thing = format_thing(self.get_table_name(), record_id)
        result_records: Any = None

        # Wrap the DB operation with around_update signal
        async with model_signals.around_update.wrap(
            sender=self.__class__,
            instance=self,
            update_fields=data,
            tx=tx,
        ):
            if tx is not None:
                await tx.merge(thing, data)
            else:
                client = await SurrealDBConnectionManager.get_client()
                result = await client.merge(thing, data)
                result_records = result.records

        # Send post_update signal
        await model_signals.post_update.send(
            sender=self.__class__,
            instance=self,
            update_fields=data,
            tx=tx,
        )
        return result_records

    @classmethod
    def get(cls, item: str) -> str:
        """
        Get the table name for the model.
        """
        return f"{cls.__name__}:{item}"

    async def merge(self, tx: "BaseTransaction | None" = None, **data: Any) -> Self:
        """
        Merge (partial update) the model instance in the database.

        Signals:
            - pre_update: Sent before the merge operation.
            - around_update: Wraps the DB operation (generator-based).
            - post_update: Sent after the merge operation completes.

        Args:
            tx: Optional transaction to use for this operation.
            **data: Fields to update.

        Returns:
            Self: The updated model instance.
        """
        data_set = {key: value for key, value in data.items()}

        record_id = self.get_id()
        if not record_id:
            raise SurrealDbError(f"No Id for the data to merge: {data}")

        # Send pre_update signal
        await model_signals.pre_update.send(
            sender=self.__class__,
            instance=self,
            update_fields=data_set,
            tx=tx,
        )

        thing = format_thing(self.get_table_name(), record_id)

        # Wrap the DB operation with around_update signal
        async with model_signals.around_update.wrap(
            sender=self.__class__,
            instance=self,
            update_fields=data_set,
            tx=tx,
        ):
            if tx is not None:
                await tx.merge(thing, data_set)
                # Update local instance with merged data
                for key, value in data_set.items():
                    if hasattr(self, key):
                        setattr(self, key, value)
            else:
                client = await SurrealDBConnectionManager.get_client()
                await client.merge(thing, data_set)
                await self.refresh()

        # Send post_update signal
        await model_signals.post_update.send(
            sender=self.__class__,
            instance=self,
            update_fields=data_set,
            tx=tx,
        )
        return self

    async def delete(self, tx: "BaseTransaction | None" = None) -> None:
        """
        Delete the model instance from the database.

        Signals:
            - pre_delete: Sent before the delete operation.
            - around_delete: Wraps the DB operation (generator-based).
            - post_delete: Sent after the delete operation completes.

        Args:
            tx: Optional transaction to use for this operation.
        """
        record_id = self.get_id()
        if not record_id:
            raise SurrealDbError("Can't delete record without an ID.")

        # Send pre_delete signal
        await model_signals.pre_delete.send(
            sender=self.__class__,
            instance=self,
            tx=tx,
        )

        thing = format_thing(self.get_table_name(), record_id)

        # Wrap the DB operation with around_delete signal
        async with model_signals.around_delete.wrap(
            sender=self.__class__,
            instance=self,
            tx=tx,
        ):
            if tx is not None:
                await tx.delete(thing)
                logger.info(f"Record deleted (in transaction) -> {thing}.")
            else:
                client = await SurrealDBConnectionManager.get_client()
                result = await client.delete(thing)

                if not result.success:
                    raise SurrealDbError(f"Can't delete Record id -> '{record_id}' not found!")

                logger.info(f"Record deleted -> {result.deleted!r}.")

        # Send post_delete signal
        await model_signals.post_delete.send(
            sender=self.__class__,
            instance=self,
            tx=tx,
        )

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

    @classmethod
    async def raw_query(
        cls,
        query: str,
        variables: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Execute a raw SurrealQL query.

        This method provides direct access to SurrealDB for executing
        arbitrary SurrealQL queries when the ORM abstractions are insufficient.

        Args:
            query: The raw SurrealQL query string to execute.
            variables: Optional dictionary of variables to bind in the query.
                Use $variable_name syntax in the query to reference them.

        Returns:
            list[dict[str, Any]]: Raw query results as a list of dictionaries.

        Example:
            # Simple query
            results = await User.raw_query("SELECT * FROM users WHERE age > 21")

            # With variables (safe from injection)
            results = await User.raw_query(
                "SELECT * FROM users WHERE status = $status AND age > $min_age",
                variables={"status": "active", "min_age": 18}
            )

            # Complex graph query
            results = await User.raw_query(
                "SELECT ->follows->users AS following FROM users:alice"
            )

            # Delete with return
            deleted = await User.raw_query(
                "DELETE FROM users WHERE status = 'inactive' RETURN BEFORE"
            )

        Note:
            This method returns raw dictionaries, not model instances.
            Use this for edge cases where the standard QuerySet API is insufficient.
        """
        client = await SurrealDBConnectionManager.get_client()
        result = await client.query(query, variables or {})
        return list(result.all_records) if result.all_records else []

    # ==================== Atomic Array Operations ====================

    @classmethod
    async def atomic_append(
        cls,
        record_id: str,
        field: str,
        value: Any,
    ) -> list[dict[str, Any]]:
        """Atomically append a value to an array field (allows duplicates).

        Uses SurrealDB's ``array::append()`` in an UPDATE statement,
        avoiding read-modify-write conflicts in concurrent environments.

        Args:
            record_id: The record ID (just ID or "table:id" format).
            field: The array field name to append to.
            value: The value to append.

        Returns:
            The updated record(s) as a list of dicts.

        Example:
            await Event.atomic_append(event_id, "processed_by", pod_id)
        """
        if not _SAFE_IDENTIFIER_RE.match(field):
            raise ValueError(f"Invalid field name: {field!r}")
        _, id_part = parse_record_id(str(record_id))
        thing = format_thing(cls.get_table_name(), id_part)

        query = f"UPDATE {thing} SET {field} = array::append({field}, $value);"
        client = await SurrealDBConnectionManager.get_client()
        result = await client.query(query, {"value": value})
        return list(result.all_records) if result.all_records else []

    @classmethod
    async def atomic_remove(
        cls,
        record_id: str,
        field: str,
        value: Any,
    ) -> list[dict[str, Any]]:
        """Atomically remove all occurrences of a value from an array field.

        Uses SurrealDB's ``-=`` operator in an UPDATE statement.

        Args:
            record_id: The record ID (just ID or "table:id" format).
            field: The array field name to remove from.
            value: The value to remove.

        Returns:
            The updated record(s) as a list of dicts.

        Example:
            await Event.atomic_remove(event_id, "tags", "deprecated")
        """
        if not _SAFE_IDENTIFIER_RE.match(field):
            raise ValueError(f"Invalid field name: {field!r}")
        _, id_part = parse_record_id(str(record_id))
        thing = format_thing(cls.get_table_name(), id_part)

        query = f"UPDATE {thing} SET {field} -= $value;"
        client = await SurrealDBConnectionManager.get_client()
        result = await client.query(query, {"value": value})
        return list(result.all_records) if result.all_records else []

    @classmethod
    async def atomic_set_add(
        cls,
        record_id: str,
        field: str,
        value: Any,
    ) -> list[dict[str, Any]]:
        """Atomically add a value to an array field only if not already present.

        Uses SurrealDB's ``+=`` operator which performs set-like addition
        (no duplicates). Ideal for "claim" patterns where each worker marks
        a record as processed.

        Args:
            record_id: The record ID (just ID or "table:id" format).
            field: The array field name.
            value: The value to add (skipped if already present).

        Returns:
            The updated record(s) as a list of dicts.

        Example:
            await Event.atomic_set_add(event_id, "processed_by", pod_id)
        """
        if not _SAFE_IDENTIFIER_RE.match(field):
            raise ValueError(f"Invalid field name: {field!r}")
        _, id_part = parse_record_id(str(record_id))
        thing = format_thing(cls.get_table_name(), id_part)

        query = f"UPDATE {thing} SET {field} += $value;"
        client = await SurrealDBConnectionManager.get_client()
        result = await client.query(query, {"value": value})
        return list(result.all_records) if result.all_records else []

    # ==================== Graph Relation Methods ====================

    async def relate(
        self,
        relation: str,
        to: "BaseSurrealModel",
        reverse: bool = False,
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
            reverse: If True, creates the relation in reverse direction
                (to -> relation -> self instead of self -> relation -> to).
                Useful when the schema defines the edge with a different
                direction than the calling context. Default: False.
            tx: Optional transaction to use for this operation
            **edge_data: Additional data to store on the edge record

        Returns:
            dict: The created edge record

        Example:
            # Normal: game_tables:abc -> created -> users:xyz
            await table.relate("created", creator)

            # Reverse: users:xyz -> created -> game_tables:abc
            await table.relate("created", creator, reverse=True)

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

        from_thing = format_thing(source_table, source_id)
        to_thing = format_thing(target_table, target_id)

        # When reverse=True, swap direction: to -> relation -> self
        if reverse:
            from_thing, to_thing = to_thing, from_thing

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
        to: "BaseSurrealModel | str",
        reverse: bool = False,
        tx: "BaseTransaction | None" = None,
    ) -> None:
        """
        Remove a graph relation (edge) to another record.

        This method deletes the edge record(s) between this record
        and the target record.

        Args:
            relation: Name of the edge table (e.g., "follows", "likes")
            to: Target model instance or string ID to unrelate.
                String IDs can be in any format:
                - Just the ID: "abc123"
                - Full SurrealDB format: "table:abc123"
            reverse: If True, looks for the relation in reverse direction
                (to -> relation -> self instead of self -> relation -> to).
                Must match the direction used when creating the relation.
                Default: False.
            tx: Optional transaction to use for this operation

        Example:
            # Remove relation with model instance
            await alice.remove_relation("follows", bob)

            # Remove reverse relation
            await table.remove_relation("created", creator, reverse=True)

            # Remove relation with string ID
            await table.remove_relation("has_player", "players:abc123")
            await table.remove_relation("has_player", "abc123")

            # In a transaction
            async with User.transaction() as tx:
                await alice.remove_relation("follows", bob, tx=tx)
                await alice.remove_relation("follows", "users:charlie", tx=tx)
        """
        source_id = self.get_id()

        if not source_id:
            raise SurrealDbError("Cannot remove relation from unsaved instance")

        source_table = self.get_table_name()
        source_thing = format_thing(source_table, source_id)

        # Handle both model instances and string IDs
        if isinstance(to, str):
            # String ID - could be "table:id" or just "id"
            target_table, target_id = parse_record_id(to)

            if target_table:
                # Full format: "table:id"
                target_thing = format_thing(target_table, target_id)
            else:
                # Just ID - we don't know the target table, so we query by out ID only
                # Use record::id() to extract the ID part from the record link
                # Use parameterized query to safely pass the ID
                if reverse:
                    query = f"DELETE {relation} WHERE out = {source_thing} AND record::id(in) = $target_id;"
                else:
                    query = f"DELETE {relation} WHERE in = {source_thing} AND record::id(out) = $target_id;"

                if tx is not None:
                    await tx.query(query, {"target_id": target_id})
                    return

                client = await SurrealDBConnectionManager.get_client()
                await client.query(query, {"target_id": target_id})
                return
        else:
            # Model instance
            model_target_id = to.get_id()
            if not model_target_id:
                raise SurrealDbError("Cannot remove relation to unsaved instance")
            target_table = to.get_table_name()
            target_thing = format_thing(target_table, model_target_id)

        # Delete edge where in=source and out=target (or reversed)
        if reverse:
            query = f"DELETE {relation} WHERE in = {target_thing} AND out = {source_thing};"
        else:
            query = f"DELETE {relation} WHERE in = {source_thing} AND out = {target_thing};"

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
            - out: SELECT VALUE out.* FROM follows WHERE in = users:alice;
            - in: SELECT VALUE in.* FROM follows WHERE out = users:alice;
        """
        source_id = self.get_id()
        if not source_id:
            raise SurrealDbError("Cannot query relations from unsaved instance")

        source_table = self.get_table_name()
        source_thing = format_thing(source_table, source_id)

        client = await SurrealDBConnectionManager.get_client()
        records: list[dict[str, Any]] = []

        # Query edge table and fetch related records
        # Use SELECT VALUE field.* to get the full related records directly
        # This is more reliable than FETCH for extracting nested records
        # For outgoing: get 'out' field where 'in' matches source
        # For incoming: get 'in' field where 'out' matches source
        if direction == "out":
            # Get records that this record points TO
            query = f"SELECT VALUE out.* FROM {relation} WHERE in = {source_thing};"
            result = await client.query(query)
            for record in result.all_records or []:
                if isinstance(record, dict):
                    records.append(record)
        elif direction == "in":
            # Get records that point TO this record
            query = f"SELECT VALUE in.* FROM {relation} WHERE out = {source_thing};"
            result = await client.query(query)
            for record in result.all_records or []:
                if isinstance(record, dict):
                    records.append(record)
        else:  # both
            # Get both outgoing and incoming relations
            query_out = f"SELECT VALUE out.* FROM {relation} WHERE in = {source_thing};"
            query_in = f"SELECT VALUE in.* FROM {relation} WHERE out = {source_thing};"
            result_out = await client.query(query_out)
            result_in = await client.query(query_in)
            for record in result_out.all_records or []:
                if isinstance(record, dict):
                    records.append(record)
            for record in result_in.all_records or []:
                if isinstance(record, dict):
                    records.append(record)

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
