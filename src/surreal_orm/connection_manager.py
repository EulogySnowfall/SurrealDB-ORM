from typing import Any
from surrealdb import AsyncSurrealDB
from surrealdb.errors import SurrealDbConnectionError
import logging

logger = logging.getLogger(__name__)


class SurrealDBConnectionManager:
    __url: str | None = None
    __user: str | None = None
    __password: str | None = None
    __namespace: str | None = None
    __database: str | None = None
    __client: AsyncSurrealDB | None = None

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await SurrealDBConnectionManager.close_connection()

    async def __aenter__(self) -> AsyncSurrealDB:
        return await SurrealDBConnectionManager.get_client()

    @classmethod
    def set_connection(cls, url: str, user: str, password: str, namespace: str, database: str) -> None:
        """
        Set the connection kwargs for the SurrealDB instance.

        :param kwargs: The connection kwargs for the SurrealDB instance.
        """
        cls.__url = url
        cls.__user = user
        cls.__password = password
        cls.__namespace = namespace
        cls.__database = database

    @classmethod
    async def unset_connection(cls) -> None:
        """
        Set the connection kwargs for the SurrealDB instance.

        :param kwargs: The connection kwargs for the SurrealDB instance.
        """
        cls.__url = None
        cls.__user = None
        cls.__password = None
        cls.__namespace = None
        cls.__database = None
        await cls.close_connection()

    @classmethod
    def is_connection_set(cls) -> bool:
        """
        Check if the connection kwargs are set.

        :return: True if the connection kwargs are set, False otherwise.
        """
        return all([cls.__url, cls.__user, cls.__password, cls.__namespace, cls.__database])

    @classmethod
    async def get_client(cls) -> AsyncSurrealDB:
        """
        Connect to the SurrealDB instance.

        :return: The SurrealDB instance.
        """

        if cls.__client is not None:
            return cls.__client

        if not cls.is_connection_set():
            raise ValueError("Connection not been set.")

        # Établir la connexion
        try:
            _client = AsyncSurrealDB(cls.get_connection_string())
            await _client.connect()  # type: ignore
            await _client.use(cls.__namespace, cls.__database)  # type: ignore
            await _client.sign_in(cls.__user, cls.__password)  # type: ignore

            cls.__client = _client
            return cls.__client
        except Exception as e:
            logger.warning(f"Can't get connection: {e}")
            if isinstance(cls.__client, AsyncSurrealDB):  # pragma: no cover
                await cls.__client.close()
                cls.__client = None
            raise SurrealDbConnectionError("Can't connect to the database.")

    @classmethod
    async def close_connection(cls) -> None:
        """
        Close the connection to the SurrealDB instance.
        """
        # Fermer la connexion

        if cls.__client is None:
            return

        await cls.__client.close()
        cls.__client = None

    @classmethod
    async def reconnect(cls) -> AsyncSurrealDB | None:
        """
        Reconnect to the SurrealDB instance.
        """
        # Fermer la connexion
        await cls.close_connection()
        # Établir la connexion
        return await cls.get_client()

    @classmethod
    async def validate_connection(cls) -> bool:
        """
        Validate the connection to the SurrealDB instance.

        :return: True if the connection is valid, False otherwise.
        """
        # Valider la connexion
        try:
            await cls.reconnect()
            return True
        except SurrealDbConnectionError:
            return False

    @classmethod
    def get_connection_string(cls) -> str | None:
        """
        Get the connection string for the SurrealDB instance.

        :return: The connection string for the SurrealDB instance.
        """
        return cls.__url

    @classmethod
    def get_connection_kwargs(cls) -> dict[str, str | None]:
        """
        Get the connection kwargs for the SurrealDB instance.

        :return: The connection kwargs for the SurrealDB instance.
        """
        return {
            "url": cls.__url,
            "user": cls.__user,
            "namespace": cls.__namespace,
            "database": cls.__database,
        }

    @classmethod
    async def set_url(cls, url: str, reconnect: bool = False) -> bool:
        """
        Set the URL for the SurrealDB instance.

        :param url: The URL of the SurrealDB instance.
        """

        if not cls.is_connection_set():
            raise ValueError("You can't change the URL when the others setting are not already set.")

        cls.__url = url

        if reconnect:
            if not await cls.validate_connection():  # pragma: no cover
                cls.__url = None
                return False

        return True

    @classmethod
    async def set_user(cls, user: str, reconnect: bool = False) -> bool:
        """
        Set the username for authentication.

        :param user: The username for authentication.
        """

        if not cls.is_connection_set():
            raise ValueError("You can't change the User when the others setting are not already set.")

        cls.__user = user

        if reconnect:
            if not await cls.validate_connection():  # pragma: no cover
                cls.__user = None
                return False

        return True

    @classmethod
    async def set_password(cls, password: str, reconnect: bool = False) -> bool:
        """
        Set the password for authentication.

        :param password: The password for authentication.
        """

        if not cls.is_connection_set():
            raise ValueError("You can't change the password when the others setting are not already set.")

        cls.__password = password

        if reconnect:
            if not await cls.validate_connection():  # pragma: no cover
                cls.__password = None
                return False

        return True

    @classmethod
    async def set_namespace(cls, namespace: str, reconnect: bool = False) -> bool:
        """
        Set the namespace to use.

        :param namespace: The namespace to use.
        """

        if not cls.is_connection_set():
            raise ValueError("You can't change the namespace when the others setting are not already set.")

        cls.__namespace = namespace

        if reconnect:
            if not await cls.validate_connection():  # pragma: no cover
                cls.__namespace = None
                return False

        return True

    @classmethod
    async def set_database(cls, database: str, reconnect: bool = False) -> bool:
        """
        Set the database to use.

        :param database: The database to use.
        """
        if not cls.is_connection_set():
            raise ValueError("You can't change the database when the others setting are not already set.")

        cls.__database = database

        if reconnect:
            if not await cls.validate_connection():  # pragma: no cover
                cls.__database = None
                return False

        return True

    @classmethod
    def get_url(cls) -> str | None:
        """
        Get the URL of the SurrealDB instance.

        :return: The URL of the SurrealDB instance.
        """
        return cls.__url

    @classmethod
    def get_user(cls) -> str | None:
        """
        Get the username for authentication.

        :return: The username for authentication.
        """
        return cls.__user

    @classmethod
    def get_namespace(cls) -> str | None:
        """
        Get the namespace to use.

        :return: The namespace to use.
        """
        return cls.__namespace

    @classmethod
    def get_database(cls) -> str | None:
        """
        Get the database to use.

        :return: The database to use.
        """
        return cls.__database

    @classmethod
    def is_password_set(cls) -> bool:
        """
        Get the database to use.

        :return: The database to use.
        """
        return cls.__password is not None

    @classmethod
    def is_connected(cls) -> bool:
        """
        Check if the connection to the SurrealDB instance is established.

        :return: True if the connection is established, False otherwise.
        """

        return cls.__client is not None
