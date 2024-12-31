from surrealdb import AsyncSurrealDB


class SurrealDBConnectionManager:
    """
    A singleton class to manage connections to a SurrealDB instance.
    """
    _instance = None

    def __new__(cls, *args, **kwargs):
        """
        Create a new instance if one does not exist, otherwise return the existing instance.
        """
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, url=None, user=None, password=None, namespace=None, database=None):
        """
        Initialize the connection manager with connection parameters.

        :param url: The URL of the SurrealDB instance.
        :param user: The username for authentication.
        :param password: The password for authentication.
        :param namespace: The namespace to use.
        :param database: The database to use.
        """
        if not hasattr(self, "_initialized"):
            self.url = url
            self.user = user
            self.password = password
            self.namespace = namespace
            self.database = database
            self._client = None
            self._initialized = True

    async def get_client(self) -> AsyncSurrealDB:
        """
        Get the SurrealDB client, creating it if necessary.

        :return: The SurrealDB client.
        """
        if self._client is None:
            await self._create_client()
        return self._client

    async def _create_client(self):
        """
        Create and initialize the SurrealDB client.
        """
        # Établir la connexion
        self._client = AsyncSurrealDB(self.url)
        await self._client.connect()
        await self._client.use(self.namespace, self.database)
        await self._client.sign_in(self.user, self.password)
        return self._client

    async def close(self):
        """
        Close the SurrealDB client connection.
        """
        if self._client:
            await self._client.close()
            self._client = None
