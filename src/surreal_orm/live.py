"""
ORM-level Live Streaming for SurrealDB.

Provides typed model change events via Live Queries (WebSocket) and Change Feeds (HTTP).

Usage:
    # Live Models (WebSocket, real-time)
    async with User.objects().filter(role="admin").live() as stream:
        async for event in stream:
            print(event.action, event.instance.name)

    # Change Feed (HTTP, CDC/event streaming)
    async for event in User.objects().changes(since="2026-01-01"):
        print(event.action, event.instance.email)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, Generic, Self, TypeVar

from surreal_sdk.streaming.live_select import LiveAction, LiveChange, LiveSelectStream, ReconnectCallback
from surreal_sdk.streaming.change_feed import ChangeFeedStream
from surreal_sdk.connection.websocket import WebSocketConnection
from surreal_sdk.connection.http import HTTPConnection

if TYPE_CHECKING:
    from .model_base import BaseSurrealModel

logger = logging.getLogger(__name__)

T = TypeVar("T", bound="BaseSurrealModel")


@dataclass
class ModelChangeEvent(Generic[T]):
    """
    Typed model change event from a live query or change feed.

    Attributes:
        action: The type of change (CREATE, UPDATE, DELETE).
        instance: The model instance after the change (for DELETE, may only have id).
        record_id: The affected record ID (e.g., "users:abc123").
        changed_fields: List of changed field names (only in DIFF mode for live queries).
        raw: The raw result dict from the database.
    """

    action: LiveAction
    instance: T
    record_id: str
    changed_fields: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


class LiveModelStream(Generic[T]):
    """
    Async context manager and iterator for ORM-level live query subscriptions.

    Wraps the SDK's ``LiveSelectStream`` and converts raw dict results into
    typed Pydantic model instances via ``Model.from_db()``.

    The WebSocket connection is resolved lazily on ``__aenter__`` via
    ``SurrealDBConnectionManager.get_ws_client()`` when ``connection`` is
    ``None``.

    Usage::

        async with User.objects().filter(role="admin").live() as stream:
            async for event in stream:
                match event.action:
                    case LiveAction.CREATE:
                        print(f"New admin: {event.instance.name}")
                    case LiveAction.UPDATE:
                        print(f"Admin updated: {event.instance}")
                    case LiveAction.DELETE:
                        print(f"Admin removed: {event.record_id}")
    """

    def __init__(
        self,
        model: type[T],
        connection: WebSocketConnection | None,
        table: str,
        where: str | None = None,
        params: dict[str, Any] | None = None,
        diff: bool = False,
        auto_resubscribe: bool = True,
        on_reconnect: ReconnectCallback | None = None,
    ) -> None:
        self._model = model
        self._connection = connection
        self._table = table
        self._where = where
        self._params = params
        self._diff = diff
        self._auto_resubscribe = auto_resubscribe
        self._on_reconnect = on_reconnect
        self._stream: LiveSelectStream | None = None

    @staticmethod
    def _parse_id(record_id: str) -> str:
        """Strip optional table prefix from a record ID (e.g. 'table:abc' -> 'abc')."""
        if isinstance(record_id, str) and ":" in record_id:
            return record_id.split(":", 1)[1]
        return record_id

    def _to_event(self, change: LiveChange) -> ModelChangeEvent[T]:
        """Convert a raw LiveChange into a typed ModelChangeEvent."""
        from . import signals as model_signals

        parsed_id = self._parse_id(change.record_id)

        instance: T
        if change.action == LiveAction.DELETE and not change.result:
            # DELETE may return an empty result; build a minimal instance with just the id
            try:
                instance = self._model.model_validate({"id": parsed_id})
            except Exception:
                instance = self._model.model_construct(id=parsed_id)  # type: ignore[arg-type]
        else:
            result = self._model.from_db(change.result)
            if isinstance(result, list):
                instance = result[0] if result else self._model.model_construct(id=parsed_id)  # type: ignore[arg-type]
            else:
                instance = result  # type: ignore[assignment]

        event: ModelChangeEvent[T] = ModelChangeEvent(
            action=change.action,
            instance=instance,
            record_id=change.record_id,
            changed_fields=change.changed_fields,
            raw=change.result,
        )

        # Fire the post_live_change signal (fire-and-forget, don't block the stream)
        try:
            asyncio.ensure_future(
                model_signals.post_live_change.send(
                    self._model,
                    instance=instance,
                    action=change.action,
                    record_id=change.record_id,
                    changed_fields=change.changed_fields,
                )
            )
        except Exception:
            logger.debug(
                "Failed to schedule post_live_change signal for %s (action=%s, record_id=%s)",
                self._model,
                change.action,
                change.record_id,
                exc_info=True,
            )

        return event

    @property
    def live_id(self) -> str | None:
        """Get the live query UUID."""
        return self._stream.live_id if self._stream else None

    @property
    def is_active(self) -> bool:
        """Check if stream is active."""
        return self._stream.is_active if self._stream else False

    # Async iterator protocol

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> ModelChangeEvent[T]:
        if self._stream is None:
            raise StopAsyncIteration

        change = await self._stream.__anext__()
        return self._to_event(change)

    # Async context manager protocol

    async def __aenter__(self) -> Self:
        # Lazy-resolve WebSocket connection if not provided
        if self._connection is None:
            from .connection_manager import SurrealDBConnectionManager

            self._connection = await SurrealDBConnectionManager.get_ws_client()

        self._stream = LiveSelectStream(
            connection=self._connection,
            table=self._table,
            where=self._where,
            params=self._params,
            diff=self._diff,
            auto_resubscribe=self._auto_resubscribe,
            on_reconnect=self._on_reconnect,
        )
        await self._stream.start()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._stream is not None:
            await self._stream.stop()
            self._stream = None

    async def stop(self) -> None:
        """Manually stop the live stream."""
        if self._stream is not None:
            await self._stream.stop()
            self._stream = None


class ChangeModelStream(Generic[T]):
    """
    Async iterator for ORM-level change feed streaming.

    Wraps the SDK's ``ChangeFeedStream`` and converts raw change records
    into typed ``ModelChangeEvent`` instances.

    The HTTP connection is resolved lazily on first iteration via
    ``SurrealDBConnectionManager.get_client()`` when ``connection`` is
    ``None``.

    Change feeds are HTTP-based (stateless) and ideal for:
    - Microservices event streaming
    - Data replication
    - Audit trails
    - Event sourcing

    Usage::

        async for event in User.objects().changes(since="2026-01-01"):
            await publish_to_queue({
                "type": f"user.{event.action.value.lower()}",
                "data": event.raw,
            })
    """

    def __init__(
        self,
        model: type[T],
        connection: HTTPConnection | None,
        table: str,
        since: str | datetime | None = None,
        poll_interval: float = 0.1,
        batch_size: int = 100,
    ) -> None:
        self._model = model
        self._connection = connection
        self._table = table
        self._since = since
        self._poll_interval = poll_interval
        self._batch_size = batch_size
        self._feed: ChangeFeedStream | None = None
        self._iterator: AsyncIterator[ModelChangeEvent[T]] | None = None

    async def _ensure_feed(self) -> ChangeFeedStream:
        """Lazily create the ChangeFeedStream, resolving connection if needed."""
        if self._feed is not None:
            return self._feed

        if self._connection is None:
            from .connection_manager import SurrealDBConnectionManager

            self._connection = await SurrealDBConnectionManager.get_client()

        self._feed = ChangeFeedStream(
            connection=self._connection,
            table=self._table,
            poll_interval=self._poll_interval,
            batch_size=self._batch_size,
        )
        return self._feed

    @property
    def cursor(self) -> str | None:
        """Current stream cursor (versionstamp or timestamp)."""
        return self._feed.cursor if self._feed else None

    def stop(self) -> None:
        """Stop the change feed stream."""
        if self._feed is not None:
            self._feed.stop()

    def _parse_change(self, change: dict[str, Any]) -> list[ModelChangeEvent[T]]:
        """Parse a single change feed record into ModelChangeEvent(s)."""
        events: list[ModelChangeEvent[T]] = []
        changes_list = change.get("changes", [])

        for item in changes_list:
            if not isinstance(item, dict):
                continue

            # Change feed format: {"define_table": {...}} or {"update": {...}} or {"delete": {...}} or {"create": {...}}
            for action_key in ("create", "update", "delete"):
                if action_key in item:
                    record = item[action_key]
                    if not isinstance(record, dict):
                        continue

                    action = LiveAction(action_key.upper())
                    record_id = str(record.get("id", ""))
                    # Normalize ID: strip table prefix for model construction
                    parsed_id = LiveModelStream._parse_id(record_id) if record_id else ""

                    try:
                        result = self._model.from_db(record)
                        if isinstance(result, list):
                            instance = result[0] if result else self._model.model_construct(id=parsed_id)  # type: ignore[arg-type]
                        else:
                            instance = result  # type: ignore[assignment]
                    except Exception:
                        instance = self._model.model_construct(id=parsed_id)  # type: ignore[arg-type]

                    events.append(
                        ModelChangeEvent(
                            action=action,
                            instance=instance,
                            record_id=record_id,
                            raw=record,
                        )
                    )

        return events

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> ModelChangeEvent[T]:
        # Lazy-initialize the underlying async generator
        if self._iterator is None:
            self._iterator = self._stream_events()

        return await self._iterator.__anext__()  # type: ignore[no-any-return, union-attr]

    async def _stream_events(self) -> AsyncIterator[ModelChangeEvent[T]]:
        """Internal generator that yields ModelChangeEvent from the change feed."""
        feed = await self._ensure_feed()
        async for change in feed.stream(since=self._since):
            events = self._parse_change(change)
            for event in events:
                yield event


__all__ = [
    "ModelChangeEvent",
    "LiveModelStream",
    "ChangeModelStream",
]
