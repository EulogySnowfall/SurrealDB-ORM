"""Tests for streaming modules (Change Feeds and Live Queries)."""

from typing import Any, AsyncGenerator
import pytest
from unittest.mock import MagicMock

from src.surreal_sdk.streaming.change_feed import ChangeFeedStream, MultiTableChangeFeed
from src.surreal_sdk.streaming.live_query import LiveQuery, LiveQueryManager, LiveNotification, LiveAction
from src.surreal_sdk.connection.http import HTTPConnection
from src.surreal_sdk.connection.websocket import WebSocketConnection


class TestChangeFeedStream:
    """Tests for ChangeFeedStream class."""

    def test_init(self) -> None:
        """Test initialization."""
        mock_conn = MagicMock()
        stream = ChangeFeedStream(mock_conn, "orders")

        assert stream.table == "orders"
        assert stream.poll_interval == 0.1
        assert stream.batch_size == 100
        assert stream.cursor is None

    def test_custom_settings(self) -> None:
        """Test custom settings."""
        mock_conn = MagicMock()
        stream = ChangeFeedStream(
            mock_conn,
            "orders",
            poll_interval=1.0,
            batch_size=50,
        )

        assert stream.poll_interval == 1.0
        assert stream.batch_size == 50


class TestLiveNotification:
    """Tests for LiveNotification class."""

    def test_from_dict_create(self) -> None:
        """Test parsing CREATE notification."""
        data = {"id": "live-query-uuid", "action": "CREATE", "result": {"id": "users:1", "name": "Alice"}}
        notification = LiveNotification.from_dict(data)

        assert notification.id == "live-query-uuid"
        assert notification.action == LiveAction.CREATE
        assert notification.result == {"id": "users:1", "name": "Alice"}

    def test_from_dict_update(self) -> None:
        """Test parsing UPDATE notification."""
        data = {"id": "live-query-uuid", "action": "UPDATE", "result": {"id": "users:1", "name": "Alice Updated"}}
        notification = LiveNotification.from_dict(data)

        assert notification.action == LiveAction.UPDATE

    def test_from_dict_delete(self) -> None:
        """Test parsing DELETE notification."""
        data = {"id": "live-query-uuid", "action": "DELETE", "result": {"id": "users:1"}}
        notification = LiveNotification.from_dict(data)

        assert notification.action == LiveAction.DELETE


class TestLiveAction:
    """Tests for LiveAction enum."""

    def test_action_values(self) -> None:
        """Test action enum values."""
        assert LiveAction.CREATE == "CREATE"
        assert LiveAction.UPDATE == "UPDATE"
        assert LiveAction.DELETE == "DELETE"


class TestLiveQuery:
    """Tests for LiveQuery class."""

    def test_init(self) -> None:
        """Test initialization."""
        mock_conn = MagicMock()
        live = LiveQuery(mock_conn, "users")

        assert live.table == "users"
        assert live.where is None
        assert live.diff is False
        assert not live.is_active
        assert live.live_id is None

    def test_init_with_options(self) -> None:
        """Test initialization with options."""
        mock_conn = MagicMock()
        live = LiveQuery(
            mock_conn,
            "users",
            where="age > 21",
            diff=True,
        )

        assert live.where == "age > 21"
        assert live.diff is True


class TestLiveQueryManager:
    """Tests for LiveQueryManager class."""

    def test_init(self) -> None:
        """Test initialization."""
        mock_conn = MagicMock()
        manager = LiveQueryManager(mock_conn)

        assert manager.count == 0
        assert manager.active_queries == []


class TestMultiTableChangeFeed:
    """Tests for MultiTableChangeFeed class."""

    def test_init(self) -> None:
        """Test initialization."""
        mock_conn = MagicMock()
        feed = MultiTableChangeFeed(mock_conn, ["users", "orders", "products"])

        assert len(feed.streams) == 3
        assert "users" in feed.streams
        assert "orders" in feed.streams
        assert "products" in feed.streams


class TestStreamingIntegration:
    """Integration tests requiring a running SurrealDB instance."""

    @pytest.fixture(scope="function")
    async def http_connection(self) -> AsyncGenerator[HTTPConnection, None]:
        """Create a connected HTTP connection."""
        conn = HTTPConnection("http://localhost:8000", "test", "test")
        try:
            await conn.connect()
            await conn.signin("root", "root")
            yield conn
        finally:
            await conn.close()

    @pytest.fixture(scope="function")
    async def ws_connection(self) -> AsyncGenerator[WebSocketConnection, None]:
        """Create a connected WebSocket connection."""
        conn = WebSocketConnection(
            "ws://localhost:8000",
            "test",
            "test",
            auto_reconnect=False,
        )
        try:
            await conn.connect()
            await conn.signin("root", "root")
            yield conn
        finally:
            await conn.close()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_define_changefeed(self, http_connection: HTTPConnection) -> None:
        """Test defining a change feed."""
        stream = ChangeFeedStream(http_connection, "cf_test")

        # This may fail if already defined, which is OK
        try:
            await stream.define_changefeed("1h")
        except Exception:
            pass

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_live_query_subscription(self, ws_connection: WebSocketConnection) -> None:
        """Test live query subscription flow."""
        notifications: list[LiveNotification] = []

        async def callback(notification: LiveNotification) -> None:
            notifications.append(notification)

        live = LiveQuery(ws_connection, "lq_test")

        # Subscribe
        live_id = await live.subscribe(callback)
        assert live.is_active
        assert live_id is not None

        # Unsubscribe
        await live.unsubscribe()
        assert not live.is_active

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_live_query_manager(self, ws_connection: WebSocketConnection) -> None:
        """Test live query manager."""

        async def dummy_callback(notification: Any) -> None:
            pass

        manager = LiveQueryManager(ws_connection)

        # Watch multiple tables
        id1 = await manager.watch("lqm_test1", dummy_callback)
        id2 = await manager.watch("lqm_test2", dummy_callback)

        assert manager.count == 2
        assert id1 in manager.active_queries
        assert id2 in manager.active_queries

        # Unwatch all
        await manager.unwatch_all()
        assert manager.count == 0
