"""
Integration tests for v0.9.0: Live Models + Change Feed ORM Integration.

These tests require a running SurrealDB instance (port 8001).
They test actual WebSocket live queries and change feed streaming
against a real database.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncGenerator

import pytest
from pydantic import Field

from src.surreal_orm import (
    BaseSurrealModel,
    SurrealConfigDict,
    SurrealDBConnectionManager,
    LiveAction,
    ModelChangeEvent,
    post_live_change,
)
from src.surreal_orm.types import TableType


# ==================== Test Config ====================

SURREALDB_URL = "http://localhost:8001"
SURREALDB_USER = "root"
SURREALDB_PASS = "root"
SURREALDB_NAMESPACE = "test"
SURREALDB_DATABASE = "test_live_integration"


# ==================== Test Models ====================


class LiveTestPlayer(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="live_test_players")

    id: str | None = None
    name: str = Field(default="")
    role: str = Field(default="player")
    score: int = Field(default=0)


class StreamTestEvent(BaseSurrealModel):
    model_config = SurrealConfigDict(
        table_name="stream_test_events",
        table_type=TableType.STREAM,
    )

    id: str | None = None
    event_type: str = Field(default="")
    payload: str = Field(default="")


# ==================== Fixtures ====================


@pytest.fixture(scope="module", autouse=True)
async def setup_live_integration() -> AsyncGenerator[None, Any]:
    """Set up connection and clean tables for live integration tests."""
    SurrealDBConnectionManager.set_connection(
        SURREALDB_URL,
        SURREALDB_USER,
        SURREALDB_PASS,
        SURREALDB_NAMESPACE,
        SURREALDB_DATABASE,
    )

    client = await SurrealDBConnectionManager.get_client()
    try:
        for table in ["live_test_players", "stream_test_events"]:
            try:
                await client.query(f"REMOVE TABLE IF EXISTS {table};")
            except Exception:
                pass
    except Exception:
        pass

    yield

    # Teardown
    try:
        client = await SurrealDBConnectionManager.get_client()
        for table in ["live_test_players", "stream_test_events"]:
            try:
                await client.query(f"REMOVE TABLE IF EXISTS {table};")
            except Exception:
                pass
    except Exception:
        pass

    # Close WebSocket and HTTP connections
    await SurrealDBConnectionManager.close_connection()


# ==================== WebSocket Connection Tests ====================


@pytest.mark.integration
async def test_get_ws_client_returns_connection() -> None:
    """get_ws_client() returns a connected WebSocketConnection."""
    from src.surreal_sdk.connection.websocket import WebSocketConnection

    ws = await SurrealDBConnectionManager.get_ws_client()
    assert isinstance(ws, WebSocketConnection)
    assert ws.is_connected


@pytest.mark.integration
async def test_get_ws_client_reuses_connection() -> None:
    """get_ws_client() returns the same connection on subsequent calls."""
    ws1 = await SurrealDBConnectionManager.get_ws_client()
    ws2 = await SurrealDBConnectionManager.get_ws_client()
    assert ws1 is ws2


# ==================== LiveModelStream Integration ====================


@pytest.mark.integration
async def test_live_stream_receives_create() -> None:
    """LiveModelStream receives CREATE events when records are inserted."""
    events_received: list[ModelChangeEvent[LiveTestPlayer]] = []

    async def collect_events() -> None:
        async with LiveTestPlayer.objects().live() as stream:
            async for event in stream:
                events_received.append(event)
                if len(events_received) >= 1:
                    break

    # Start the live stream in a task
    task = asyncio.create_task(collect_events())

    # Give the live query time to subscribe
    await asyncio.sleep(0.5)

    # Create a record
    player = LiveTestPlayer(name="LiveAlice", role="admin", score=100)
    await player.save()

    # Wait for the event (with timeout)
    try:
        await asyncio.wait_for(task, timeout=5.0)
    except asyncio.TimeoutError:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert len(events_received) >= 1
    event = events_received[0]
    assert event.action == LiveAction.CREATE
    assert isinstance(event.instance, LiveTestPlayer)
    assert event.instance.name == "LiveAlice"
    assert event.instance.role == "admin"
    assert event.instance.score == 100

    # Cleanup
    await LiveTestPlayer.objects().delete_table()


@pytest.mark.integration
async def test_live_stream_receives_update() -> None:
    """LiveModelStream receives UPDATE events when records are modified."""
    # Pre-create the record
    player = LiveTestPlayer(id="upd1", name="OriginalName", role="player", score=0)
    await player.save()
    await asyncio.sleep(0.2)

    events_received: list[ModelChangeEvent[LiveTestPlayer]] = []

    async def collect_events() -> None:
        async with LiveTestPlayer.objects().live() as stream:
            async for event in stream:
                events_received.append(event)
                if event.action == LiveAction.UPDATE:
                    break

    task = asyncio.create_task(collect_events())
    await asyncio.sleep(0.5)

    # Update the record
    await player.merge(name="UpdatedName", score=50)

    try:
        await asyncio.wait_for(task, timeout=5.0)
    except asyncio.TimeoutError:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    update_events = [e for e in events_received if e.action == LiveAction.UPDATE]
    assert len(update_events) >= 1
    assert update_events[0].instance.name == "UpdatedName"
    assert update_events[0].instance.score == 50

    # Cleanup
    await LiveTestPlayer.objects().delete_table()


@pytest.mark.integration
async def test_live_stream_receives_delete() -> None:
    """LiveModelStream receives DELETE events when records are removed."""
    # Pre-create the record
    player = LiveTestPlayer(id="del1", name="ToDelete", role="player")
    await player.save()
    await asyncio.sleep(0.2)

    events_received: list[ModelChangeEvent[LiveTestPlayer]] = []

    async def collect_events() -> None:
        async with LiveTestPlayer.objects().live() as stream:
            async for event in stream:
                events_received.append(event)
                if event.action == LiveAction.DELETE:
                    break

    task = asyncio.create_task(collect_events())
    await asyncio.sleep(0.5)

    # Delete the record
    await player.delete()

    try:
        await asyncio.wait_for(task, timeout=5.0)
    except asyncio.TimeoutError:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    delete_events = [e for e in events_received if e.action == LiveAction.DELETE]
    assert len(delete_events) >= 1
    assert "del1" in delete_events[0].record_id

    # Cleanup
    await LiveTestPlayer.objects().delete_table()


@pytest.mark.integration
async def test_live_stream_with_filter() -> None:
    """LiveModelStream respects QuerySet filters in the WHERE clause."""
    events_received: list[ModelChangeEvent[LiveTestPlayer]] = []

    async def collect_events() -> None:
        # Only subscribe to admin role changes
        async with LiveTestPlayer.objects().filter(role="admin").live() as stream:
            async for event in stream:
                events_received.append(event)
                if len(events_received) >= 1:
                    break

    task = asyncio.create_task(collect_events())
    await asyncio.sleep(0.5)

    # Create a non-matching record (should NOT trigger)
    non_admin = LiveTestPlayer(name="RegularUser", role="player")
    await non_admin.save()

    # Short wait to ensure the non-matching event would have arrived
    await asyncio.sleep(0.3)

    # Create a matching record (should trigger)
    admin = LiveTestPlayer(name="AdminUser", role="admin")
    await admin.save()

    try:
        await asyncio.wait_for(task, timeout=5.0)
    except asyncio.TimeoutError:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # Should only receive the admin event
    assert len(events_received) >= 1
    assert events_received[0].instance.role == "admin"
    assert events_received[0].instance.name == "AdminUser"

    # Cleanup
    await LiveTestPlayer.objects().delete_table()


@pytest.mark.integration
async def test_live_stream_context_manager() -> None:
    """LiveModelStream properly cleans up on context manager exit."""
    stream = LiveTestPlayer.objects().live()

    async with stream as s:
        assert s.is_active
        assert s.live_id is not None

    # After exit, stream should be inactive
    assert stream.is_active is False


@pytest.mark.integration
async def test_live_stream_manual_stop() -> None:
    """LiveModelStream.stop() properly stops the stream."""
    stream = LiveTestPlayer.objects().live()
    await stream.__aenter__()

    assert stream.is_active
    await stream.stop()
    assert stream.is_active is False


# ==================== post_live_change Signal Integration ====================


@pytest.mark.integration
async def test_post_live_change_signal_fires() -> None:
    """post_live_change signal fires when live events are received."""
    signal_calls: list[dict[str, Any]] = []

    @post_live_change.connect(LiveTestPlayer)
    async def on_change(sender: type, **kwargs: Any) -> None:
        signal_calls.append(kwargs)

    try:

        async def collect_one_event() -> None:
            async with LiveTestPlayer.objects().live() as stream:
                async for _ in stream:
                    break  # Just need one event

        task = asyncio.create_task(collect_one_event())
        await asyncio.sleep(0.5)

        player = LiveTestPlayer(name="SignalTest", role="tester")
        await player.save()

        try:
            await asyncio.wait_for(task, timeout=5.0)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Give the fire-and-forget signal a moment to complete
        await asyncio.sleep(0.3)

        assert len(signal_calls) >= 1
        assert "instance" in signal_calls[0]
        assert "action" in signal_calls[0]
        assert "record_id" in signal_calls[0]
    finally:
        post_live_change.disconnect(on_change, LiveTestPlayer)
        await LiveTestPlayer.objects().delete_table()


# ==================== ConnectionManager Cleanup ====================


@pytest.mark.integration
async def test_close_connection_closes_ws() -> None:
    """close_connection() also closes the WebSocket client."""
    # Ensure WS is connected
    ws = await SurrealDBConnectionManager.get_ws_client()
    assert ws.is_connected

    await SurrealDBConnectionManager.close_connection()

    # Re-establish connections for other tests
    SurrealDBConnectionManager.set_connection(
        SURREALDB_URL,
        SURREALDB_USER,
        SURREALDB_PASS,
        SURREALDB_NAMESPACE,
        SURREALDB_DATABASE,
    )
