from typing import Any
from .connection_manager import SurrealDBConnectionManager


class BaseSurrealModel:
    """
    Base class for models interacting with SurrealDB.
    """

    def __init__(self, **data):
        pyd_model = self._pydantic_model()
        instance = pyd_model(**data)
        self._data = instance.model_dump()
        self._table_name = self._data.get("_table_name", self.__class__.__name__)

    def __getattr__(self, item):
        """
        Access fields like self.name, self.age, etc.
        """
        return self._data.get(item)

    def __setattr__(self, key, value):
        """
        Set attributes for the model. If the attribute is in the allowed list, set it directly.
        Otherwise, update the Pydantic model instance and set the attribute.
        """

        if key in ("_data", "_pydantic_model", ...):
            super().__setattr__(key, value)
        else:
            pyd_cls = self._pydantic_model()
            instance = pyd_cls(**{**self._data, key: value})
            self._data = instance.model_dump()

    @classmethod
    def from_db(cls, record: dict):
        """
        Create an instance from a SurrealDB record.
        """
        return cls(**record)

    def to_db_dict(self):
        """
        Return a dictionary ready to be inserted into the database.
        """
        data_set = {
            key: value for key, value in self._data.items() if not key.startswith("_")
        }
        return data_set

    def get_id(self) -> str | None:
        """
        Get the ID of the model instance.
        """
        id = None
        if self._data.get("id"):
            id = self.id

        return id

    async def save(self) -> Any:
        """
        Save the model instance to the database.
        """
        client = await SurrealDBConnectionManager().get_client()
        data = self.to_db_dict()
        id = self.get_id()
        if id:
            thing = f"{self._table_name}:{id}"
            return await client.create(thing, data)
        else:
            return await client.create(self._table_name, data)

    @classmethod
    def _pydantic_model(cls):
        """
        To be overridden in subclasses to return the desired Pydantic model.
        """
        raise NotImplementedError

    @classmethod
    def objects(cls):
        """
        Return a QuerySet for the model class.
        """
        from .querySet import QuerySet

        return QuerySet(cls)
