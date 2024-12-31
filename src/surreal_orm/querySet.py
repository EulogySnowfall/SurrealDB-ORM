from .constants import LOOKUP_OPERATORS
from .connection_manager import SurrealDBConnectionManager
from surrealdb import Table
from .modelBase import BaseSurrealModel
from typing import Self


class QuerySet:
    """
    A class used to build and execute queries on a SurrealDB table.
    """

    def __init__(self, model: BaseSurrealModel):
        """
        Initialize the QuerySet with a model.

        :param model: The model class associated with the table.
        """
        self.model = model
        self._filters = []
        self.select_item = []
        self._limit = None
        self._offset = None
        self._order_by = None
        self._model_table = getattr(model, "_table_name", model.__name__)
        self._variables = None

    def select(self, *fields) -> Self:
        """
        Specify the fields to retrieve in the query.

        :param fields: The fields to select.
        :return: The QuerySet instance.
        """
        # On stocke la liste des champs à récupérer
        self.select_item = list(fields)
        return self

    def variables(self, **kwargs) -> Self:
        """
        Set variables for the query.

        :param kwargs: The variables to set.
        :return: The QuerySet instance.
        """
        self._variables = {key: value for key, value in kwargs.items()}
        return self

    def filter(self, **kwargs) -> Self:
        """
        Add filter conditions to the query.

        :param kwargs: The filter conditions.
        :return: The QuerySet instance.
        """
        for key, value in kwargs.items():
            field_name, lookup = self._parse_lookup(key)
            self._filters.append((field_name, lookup, value))
        return self

    def _parse_lookup(self, key):
        """
        Parse the lookup type from the filter key.

        :param key: The filter key.
        :return: A tuple of field name and lookup type.
        """
        if "__" in key:
            field_name, lookup_name = key.split("__", 1)
        else:
            field_name, lookup_name = key, "exact"
        return field_name, lookup_name

    def limit(self, value) -> Self:
        """
        Set a limit on the number of results.

        :param value: The limit value.
        :return: The QuerySet instance.
        """
        self._limit = value
        return self

    def offset(self, value) -> Self:
        """
        Set an offset for the results.

        :param value: The offset value.
        :return: The QuerySet instance.
        """
        self._offset = value
        return self

    def order_by(self, field_name) -> Self:
        """
        Set the field to order the results by.

        :param field_name: The field name to order by.
        :return: The QuerySet instance.
        """
        self._order_by = field_name
        return self

    def _compile_query(self):
        """
        Compile the query into a SQL string.

        :return: The compiled SQL query string.
        """
        where_clauses = []
        for field_name, lookup_name, value in self._filters:
            op = LOOKUP_OPERATORS.get(lookup_name, "=")
            if lookup_name == "in":
                where_clauses.append(f"{field_name} {op} {repr(value)}")
            else:
                where_clauses.append(f"{field_name} {op} {repr(value)}")

        # Construction de la clause SELECT
        if self.select_item:
            fields = ", ".join(self.select_item)
            query = f"SELECT {fields} FROM {self._model_table}"
        else:
            query = f"SELECT * FROM {self._model_table}"

        # WHERE
        if where_clauses:
            query += " WHERE " + " AND ".join(where_clauses)
        # ORDER BY
        if self._order_by:
            query += f" ORDER BY {self._order_by}"
        # LIMIT
        if self._limit is not None:
            query += f" LIMIT {self._limit}"
        # OFFSET / START
        if self._offset is not None:
            query += f" START {self._offset}"

        query += ";"
        return query

    async def exec(self) -> list[BaseSurrealModel]:
        """
        Execute the compiled query and return the results.

        :return: A list of model instances.
        """
        query = self._compile_query()
        results = await self._execute_query(query)
        return [self.model.from_db(r) for r in results[0]["result"]]

    async def fletch_table(self) -> list[BaseSurrealModel]:
        """
        Fetch all records from the table.

        :return: A list of model instances.
        """
        client = await SurrealDBConnectionManager().get_client()
        results = await client.select(Table(self._model_table))
        return [self.model.from_db(r) for r in results]

    async def _execute_query(self, query: str) -> list[dict]:
        """
        Execute the query on the SurrealDB client.

        :param query: The SQL query string.
        :return: The query results.
        """
        client = await SurrealDBConnectionManager().get_client()
        return await self._run_query_on_client(client, query)

    async def _run_query_on_client(self, client, query: str) -> list[dict]:
        """
        Run the query on the provided client.

        :param client: The SurrealDB client.
        :param query: The SQL query string.
        :return: The query results.
        """
        return await client.query(query, self._variables)

    async def delete_table(self) -> bool:
        """
        Delete the table from the database.
        """
        try:
            client = await SurrealDBConnectionManager().get_client()
            result = await client.delete(Table(self._model_table))
            print(result)
            return True
        except Exception as e:
            print(f"Error deleting table: {e}")
            return False
