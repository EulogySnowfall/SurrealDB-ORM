from typing import Any, Self
from .connection_manager import SurrealDBConnectionManager
from .utils import remove_quotes_for_variables
from surreal_sdk import HTTPConnection

import logging

logger = logging.getLogger(__name__)


class SurrealQL:
    def __init__(self) -> None:
        self._variables: dict[str, Any] = {}
        self.related_models: list[Any] = []
        self._query: str = ""

    """
    Class for interacting with SurrealDB using SQL-like commands.
    """

    def select(self, fields: str | list[str] | None) -> Self:
        if fields is None:
            self._query += "SELECT "
        elif isinstance(fields, list):
            fields = ", ".join(fields)
            self._query += f"SELECT {fields}"
        else:
            self._query += f"SELECT {fields}"

        return self

    def related(self, record_table: str) -> Self:
        """
        Placeholder method for defining related models.
        """
        self._query += f"RELATE {record_table}"

        return self

    def to_related(self, table_or_record_table: str) -> Self:
        """
        Placeholder method for defining the target table for a query.
        """
        self._query += f"->{table_or_record_table}"

        return self

    def from_related(self, table_or_record_table: str) -> Self:
        """
        Placeholder method for defining the source table for a query.
        """
        self._query += f"<-{table_or_record_table}"

        return self

    def from_tables(self, tables: str | list[str]) -> Self:
        """
        Placeholder method for defining the source table for a query.
        """
        if isinstance(tables, list):
            tables = ", ".join(tables)
        self._query += f"FROM {tables}"

        return self

    async def _execute_query(self, query: str) -> list[Any]:
        """
        Execute the given SQL query using the SurrealDB client.

        This internal method handles the execution of the compiled SQL query and returns the raw results
        from the database.

        Args:
            query (str): The SQL query string to execute.

        Returns:
            list[Any]: A list of query result objects.

        Raises:
            SurrealDbError: If there is an issue executing the query.

        Example:
            ```python
            results = await self._execute_query("SELECT * FROM users;")
            ```
        """
        client = await SurrealDBConnectionManager.get_client()
        return await self._run_query_on_client(client, query)

    async def _run_query_on_client(self, client: HTTPConnection, query: str) -> list[Any]:
        """
        Run the SQL query on the provided SurrealDB client.

        This internal method sends the query to the SurrealDB client along with any predefined variables
        and returns the raw query responses.

        Args:
            client (HTTPConnection): The active SurrealDB client instance.
            query (str): The SQL query string to execute.

        Returns:
            list[Any]: A list of query result objects.

        Raises:
            SurrealDbError: If there is an issue executing the query.

        Example:
            ```python
            results = await self._run_query_on_client(client, "SELECT * FROM users;")
            ```
        """
        result = await client.query(remove_quotes_for_variables(query), self._variables)
        return result.all_records
