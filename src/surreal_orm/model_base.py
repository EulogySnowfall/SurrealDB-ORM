from typing import Any, Self
from pydantic import BaseModel, ConfigDict, model_validator
from .connection_manager import SurrealDBConnectionManager
from surrealdb import RecordID, SurrealDbError

import logging


logger = logging.getLogger(__name__)


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

    def get_id(self) -> None | str | RecordID:
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
    def from_db(cls, record: dict | list) -> Self | list[Self]:
        """
        Create an instance from a SurrealDB record.
        """
        if isinstance(record, list):
            return [cls.from_db(rs) for rs in record]  # type: ignore

        return cls(**record)

    @model_validator(mode="before")
    @classmethod
    def set_data(cls, data: Any) -> Any:
        """
        Set the ID of the model instance.
        """
        if isinstance(data, dict):  # pragma: no cover
            if "id" in data and isinstance(data["id"], RecordID):
                data["id"] = str(data["id"]).split(":")[1]
            return data

    async def refresh(self) -> None:
        """
        Refresh the model instance from the database.
        """
        if not self.get_id():
            raise SurrealDbError("Can't refresh data, not recorded yet.")  # pragma: no cover

        client = await SurrealDBConnectionManager.get_client()
        record = await client.select(f"{self.get_table_name()}:{self.get_id()}")

        if record is None:
            raise SurrealDbError("Can't refresh data, no record found.")  # pragma: no cover

        self.from_db(record)
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
        record = await client.create(table, data)  # pragma: no cover

        if isinstance(record, list):
            raise SurrealDbError("Can't save data, multiple records returned.")  # pragma: no cover

        if record is None:
            raise SurrealDbError("Can't save data, no record returned.")  # pragma: no cover

        obj = self.from_db(record)
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
            test = await client.update(thing, data)
            return test
        raise SurrealDbError("Can't update data, no id found.")

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

        deleted = await client.delete(thing)

        if not deleted:
            raise SurrealDbError(f"Can't delete Record id -> '{id}' not found!")

        logger.info(f"Record deleted -> {deleted}.")
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
