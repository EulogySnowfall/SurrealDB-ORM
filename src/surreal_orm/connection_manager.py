from typing import Any, Literal
import logging

from surreal_sdk import HTTPConnection
from surreal_sdk.exceptions import SurrealDBError
from surreal_sdk.transaction import HTTPTransaction

logger = logging.getLogger(__name__)


class SurrealDbConnectionError(Exception):
    """Connection error for SurrealDB."""

    pass


class SurrealDBConnectionManager:
    __url: str | None = None
    __user: str | None = None
    __password: str | None = None
    __namespace: str | None = None
    __database: str | None = None
    __protocol: Literal["json", "cbor"] = "cbor"
    __client: HTTPConnection | None = None

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await SurrealDBConnectionManager.close_connection()

    async def __aenter__(self) -> HTTPConnection:
        return await SurrealDBConnectionManager.get_client()

    @classmethod
    def set_connection(
        cls,
        url: str,
        user: str,
        password: str,
        namespace: str,
        database: str,
        *,
        username: str | None = None,
        protocol: Literal["json", "cbor"] = "cbor",
    ) -> None:
        """
        Set the connection kwargs for the SurrealDB instance.

        :param url: The URL of the SurrealDB instance.
        :param user: The username for authentication.
        :param password: The password for authentication.
        :param namespace: The namespace to use.
        :param database: The database to use.
        :param username: Keyword-only alias for 'user' (overrides 'user' if provided).
        :param protocol: Serialization protocol ("json" or "cbor"). Defaults to "cbor"
                         which properly handles string values that might be misinterpreted
                         as record links (e.g., data URLs like "data:image/png;base64,...").
        """
        # Allow 'username' keyword to override 'user' for API flexibility
        actual_user = username if username is not None else user

        cls.__url = url
        cls.__user = actual_user
        cls.__password = password
        cls.__namespace = namespace
        cls.__database = database
        cls.__protocol = protocol

    @classmethod
    async def unset_connection(cls) -> None:
        """
        Unset the connection kwargs and close any active connection.

        This is an async method that properly closes the connection before
        clearing the settings. Use unset_connection_sync() if you need a
        synchronous version (e.g., in atexit handlers or non-async contexts).
        """
        cls.__url = None
        cls.__user = None
        cls.__password = None
        cls.__namespace = None
        cls.__database = None
        await cls.close_connection()

    @classmethod
    def unset_connection_sync(cls) -> None:
        """
        Synchronously unset the connection kwargs without closing the connection.

        This method clears all connection settings but does NOT close the active
        connection (since close() is async). Use this in contexts where you cannot
        use async/await, such as:
        - atexit handlers
        - __del__ methods
        - synchronous cleanup code

        For proper cleanup that closes the connection, use the async unset_connection().

        Note: The underlying connection object will be garbage collected, but the
        WebSocket/HTTP session may not be cleanly closed. If possible, prefer
        calling unset_connection() in an async context.
        """
        cls.__url = None
        cls.__user = None
        cls.__password = None
        cls.__namespace = None
        cls.__database = None
        cls.__protocol = "cbor"
        cls.__client = None

    @classmethod
    def is_connection_set(cls) -> bool:
        """
        Check if the connection kwargs are set.

        :return: True if the connection kwargs are set, False otherwise.
        """
        return all([cls.__url, cls.__user, cls.__password, cls.__namespace, cls.__database])

    @classmethod
    async def get_client(cls) -> HTTPConnection:
        """
        Connect to the SurrealDB instance using the custom SDK.

        :return: The HTTPConnection instance.
        """

        if cls.__client is not None and cls.__client.is_connected:
            return cls.__client

        if not cls.is_connection_set():
            raise ValueError("Connection not been set.")

        # Establish connection
        try:
            url = cls.get_connection_string()
            assert url is not None  # Already validated by is_connection_set()
            assert cls.__namespace is not None
            assert cls.__database is not None
            assert cls.__user is not None
            assert cls.__password is not None

            _client = HTTPConnection(
                url,
                cls.__namespace,
                cls.__database,
                protocol=cls.__protocol,
            )
            await _client.connect()
            await _client.signin(cls.__user, cls.__password)

            cls.__client = _client
            return cls.__client
        except SurrealDBError as e:
            logger.warning(f"Can't get connection: {e}")
            if cls.__client is not None:  # pragma: no cover
                await cls.__client.close()
                cls.__client = None
            raise SurrealDbConnectionError(f"Can't connect to the database: {e}")
        except Exception as e:
            logger.warning(f"Can't get connection: {e}")
            if cls.__client is not None:  # pragma: no cover
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

        try:
            await cls.__client.close()
        except NotImplementedError:
            # close() is not implemented for HTTP connections in surrealdb SDK 1.0.8
            pass
        cls.__client = None

    @classmethod
    async def reconnect(cls) -> HTTPConnection | None:
        """
        Reconnect to the SurrealDB instance.
        """
        await cls.close_connection()
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

    @classmethod
    async def transaction(cls) -> HTTPTransaction:
        """
        Create a transaction context manager for atomic operations.

        Usage:
            async with SurrealDBConnectionManager.transaction() as tx:
                user = User(name="Alice")
                await user.save(tx=tx)
                order = Order(user_id=user.id)
                await order.save(tx=tx)
                # Auto-commit on success, auto-rollback on exception

        :return: HTTPTransaction context manager
        """
        client = await cls.get_client()
        return client.transaction()
