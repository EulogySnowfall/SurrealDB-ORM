from typing import Any, Self, cast
from pydantic import BaseModel, ConfigDict, model_validator
from .connection_manager import SurrealDBConnectionManager

import logging


class SurrealDbError(Exception):
    """Error from SurrealDB operations."""

    pass


logger = logging.getLogger(__name__)


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

    Attributes:
        primary_key (str | None): The primary key field name for the model.
    """

    primary_key: str | None
    " The primary key field name for the model. "


class BaseSurrealModel(BaseModel):
    """
    Base class for models interacting with SurrealDB.
    """

    @classmethod
    def get_table_name(cls) -> str:
        """
        Get the table name for the model.
        """
        return cls.__name__

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

    async def save(self) -> Self:
        """
        Save the model instance to the database.
        """
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

    async def update(self) -> Any:
        """
        Update the model instance to the database.
        """
        client = await SurrealDBConnectionManager.get_client()

        data = self.model_dump(exclude={"id"})
        id = self.get_id()
        if id is not None:
            thing = f"{self.__class__.__name__}:{id}"
            result = await client.update(thing, data)
            return result.records
        raise SurrealDbError("Can't update data, no id found.")

    @classmethod
    def get(cls, item: str) -> str:
        """
        Get the table name for the model.
        """
        return f"{cls.__name__}:{item}"

    async def merge(self, **data: Any) -> Any:
        """
        Update the model instance to the database.
        """

        client = await SurrealDBConnectionManager.get_client()
        data_set = {key: value for key, value in data.items()}

        id = self.get_id()
        if id:
            thing = f"{self.get_table_name()}:{id}"

            await client.merge(thing, data_set)
            await self.refresh()
            return

        raise SurrealDbError(f"No Id for the data to merge: {data}")

    async def delete(self) -> None:
        """
        Delete the model instance from the database.
        """

        client = await SurrealDBConnectionManager.get_client()

        id = self.get_id()

        thing = f"{self.get_table_name()}:{id}"

        result = await client.delete(thing)

        if not result.success:
            raise SurrealDbError(f"Can't delete Record id -> '{id}' not found!")

        logger.info(f"Record deleted -> {result.deleted!r}.")
        del self

    @model_validator(mode="after")
    def check_config(self) -> Self:
        """
        Check the model configuration.
        """

        if not self.get_index_primary_key() and not hasattr(self, "id"):
            raise SurrealDbError(  # pragma: no cover
                "Can't create model, the model need either 'id' field or primirary_key in 'model_config'."
            )

        return self

    @classmethod
    def objects(cls) -> Any:
        """
        Return a QuerySet for the model class.
        """
        from .query_set import QuerySet

        return QuerySet(cls)

    class DoesNotExist(Exception):
        """
        Exception raised when a model instance does not exist.
        """

        pass
