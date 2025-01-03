from typing import Any, Type, Self
from pydantic import BaseModel, create_model, ConfigDict
from .connection_manager import SurrealDBConnectionManager
from surrealdb import RecordID, SurrealDbError

import warnings
import logging

warnings.filterwarnings("ignore", message="fields may not start with an underscore", category=RuntimeWarning)

logger = logging.getLogger(__name__)


class BaseSurrealModel(BaseModel):
    """
    Base class for models interacting with SurrealDB.
    """

    __pydantic_model_cache__: Type[BaseModel] | None = None

    def __init__(self, **data: Any):
        model_cls = self._init_model()
        instance = model_cls(**data)
        object.__setattr__(self, "_data", instance.model_dump())
        object.__setattr__(self, "_table_name", self.__class__.__name__)

    def __getattr__(self, item: str) -> Any:
        """
        If the item is a field in _data, return it,
        otherwise, let the normal mechanism raise AttributeError.
        """
        _data = object.__getattribute__(self, "_data")
        if item in _data:
            return _data[item]
        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{item}'.")

    def __str__(self) -> str:
        return f"{self._data}"

    def __setattr__(self, key: str, value: Any) -> None:
        """
        If we want to allow updates, reinstantiate a Pydantic model
        with the new value.
        """
        if key in ("_data",):  # and other internal attributes
            object.__setattr__(self, key, value)
        else:
            # Update the dict, validate via Pydantic, etc.
            current_data = dict(object.__getattribute__(self, "_data"))
            current_data[key] = value
            instance = self._init_model()(**current_data)
            object.__setattr__(self, "_data", instance.model_dump())

    @classmethod
    def from_db(cls, record: dict | list) -> Any:
        """
        Create an instance from a SurrealDB record.
        """
        if isinstance(record, list):
            return [cls.from_db(rs) for rs in record]

        record = cls.__set_data(record)

        return cls(**record)

    def to_db_dict(self) -> dict[str, Any]:
        """
        Return a dictionary ready to be inserted into the database.
        """
        data_set = {key: value for key, value in self._data.items() if not key.startswith("_") and key != "id"}
        return data_set

    def show_config(self) -> ConfigDict:
        # Accès depuis une méthode d'instance
        return type(self).model_config

    def get_id(self) -> str | RecordID | None:
        if "id" in self._data:
            return self._data["id"]

        config = self.show_config()
        pk_field = config.get("primary_key", "id")
        return self._data.get(pk_field, None)

    @staticmethod
    def __set_data(data: Any) -> dict:
        """
        Set the model instance data.
        """
        if isinstance(data, dict):  # pragma: no cover
            if "id" in data and isinstance(data["id"], RecordID):  # pragma: no cover
                data["id"] = str(data["id"]).split(":")[1]
            return data

        raise TypeError("Data must be a dictionary.")  # pragma: no cover

    async def refresh(self) -> None:
        """
        Refresh the model instance from the database.
        """
        client = await SurrealDBConnectionManager.get_client()
        record = None

        id = self.get_id()
        record = await client.select(f"{self._table_name}:{id}")

        self._data = self.__set_data(record)

    async def save(self) -> Self:
        """
        Save the model instance to the database.
        """
        client = await SurrealDBConnectionManager.get_client()

        data = self.to_db_dict()
        id = self.get_id()
        if id:
            thing = f"{self._table_name}:{id}"
            await client.create(thing, data)
            return self
        # Auto-generate the ID
        record = await client.create(self._table_name, data)  # pragma: no cover
        if isinstance(record, dict):  # pragma: no cover
            self._data = self.__set_data(record)

        return self

    async def update(self) -> Any:
        """
        Update the model instance to the database.
        """
        client = await SurrealDBConnectionManager.get_client()

        data = self.to_db_dict()
        id = self.get_id()
        if id:
            thing = f"{self._table_name}:{id}"
            return await client.update(thing, data)

        raise SurrealDbError("Can't update data, no id found.")

    async def merge(self, **data: Any) -> Any:
        """
        Update the model instance to the database.
        """

        client = await SurrealDBConnectionManager.get_client()
        data_set = {key: value for key, value in data.items()}

        id = self.get_id()
        if id:
            thing = f"{self._table_name}:{id}"

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

        thing = f"{self._table_name}:{id}"

        deleted = await client.delete(thing)

        if not deleted:
            raise SurrealDbError(f"Can't delete Record id -> '{id}' not found!")

        logger.info(f"Record deleted -> {deleted}.")
        self._data = {}
        del self

    @classmethod
    def _init_model(cls) -> Any:
        """
        Generate a real Pydantic model only once (per subclass)
        from the fields annotated in the class inheriting from BaseSurrealModel.
        """
        if cls.__pydantic_model_cache__ is not None:
            return cls.__pydantic_model_cache__

        # Retrieve the annotations declared in the class (e.g., ModelTest)
        hints: dict[str, Any] = {}
        config_dict = None
        for base in reversed(cls.__mro__):  # To capture all annotations
            hints.update(getattr(base, "__annotations__", {}))
            # Optionally, check if the class has 'model_config' to inject it
            if hasattr(base, "model_config"):
                config_dict = getattr(base, "model_config")

        # Create the Pydantic model (dynamically)
        fields = {}
        for field_name, field_type in hints.items():
            # Read the object already defined in the class (if Field(...))
            default_val = getattr(cls, field_name, ...)
            fields[field_name] = (field_type, default_val)

        # Create model
        if config_dict:
            pyd_model = create_model(  # type: ignore
                f"{cls.__name__}PydModel",
                __config__=config_dict,
                **fields,
            )
        else:
            pyd_model = create_model(  # type: ignore
                f"{cls.__name__}PydModel",
                __base__=BaseModel,
                **fields,
            )

        cls.__pydantic_model_cache__ = pyd_model
        return pyd_model

    @classmethod
    def objects(cls) -> Any:
        """
        Return a QuerySet for the model class.
        """
        from .query_set import QuerySet

        return QuerySet(cls)
