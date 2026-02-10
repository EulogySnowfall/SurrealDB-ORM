"""
Model Signals for SurrealDB ORM.

Provides Django-style signals for model lifecycle events.
Signals allow you to execute code when models are saved, deleted, or updated.

Usage:
    from surreal_orm.signals import post_save, pre_delete

    @post_save.connect(Player)
    async def on_player_saved(sender, instance, created, **kwargs):
        if instance.is_ready:
            await ws_manager.broadcast({"type": "player_ready", "id": instance.id})

    @pre_delete.connect(Player)
    async def on_player_deleting(sender, instance, **kwargs):
        await cleanup_player_resources(instance)
"""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncGenerator, AsyncIterator, Awaitable, Callable, TypeVar

if TYPE_CHECKING:
    from .model_base import BaseSurrealModel

logger = logging.getLogger(__name__)

T = TypeVar("T", bound="BaseSurrealModel")

# Type alias for signal handlers
SignalHandler = Callable[..., Awaitable[None]]

# Type alias for around signal handlers (async generators that yield once)
AroundHandler = Callable[..., AsyncGenerator[None, None]]


@dataclass
class Signal:
    """
    Django-style signal dispatcher for async handlers.

    Signals allow decoupled applications to get notified when certain
    actions occur elsewhere in the application.

    Attributes:
        name: Human-readable name for the signal (for debugging)

    Example:
        # Create a custom signal
        order_completed = Signal("order_completed")

        # Connect a handler
        @order_completed.connect(Order)
        async def handle_order(sender, instance, **kwargs):
            await send_confirmation_email(instance)

        # Send the signal (done automatically by ORM for built-in signals)
        await order_completed.send(Order, instance=order)
    """

    name: str
    _handlers: dict[type | None, list[SignalHandler]] = field(default_factory=dict, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def connect(
        self,
        sender: type[T] | SignalHandler | None = None,
    ) -> Callable[[SignalHandler], SignalHandler] | SignalHandler:
        """
        Connect a receiver function to this signal.

        Can be used as a decorator with or without sender:

            # With sender - only fires for Player model
            @post_save.connect(Player)
            async def on_player_save(sender, instance, created, **kwargs):
                ...

            # Without sender - fires for all models
            @post_save.connect()
            async def on_any_save(sender, instance, created, **kwargs):
                ...

        Or as a method call:

            post_save.connect(handler, sender=Player)

        Args:
            sender: Optional model class to filter signals.
                    If None, handler receives signals from all models.
                    Can also be the handler function itself (for @signal.connect() usage).

        Returns:
            Decorator function if sender is a class or None,
            or the handler itself if sender is a function.
        """

        def decorator(func: SignalHandler) -> SignalHandler:
            # Determine the actual sender (None means all senders)
            actual_sender: type | None = None
            if sender is not None and isinstance(sender, type):
                actual_sender = sender

            with self._lock:
                if actual_sender not in self._handlers:
                    self._handlers[actual_sender] = []

                # Avoid duplicate handlers
                if func not in self._handlers[actual_sender]:
                    self._handlers[actual_sender].append(func)

            return func

        # Check if sender is actually the handler function (decorator without parens)
        if sender is not None and callable(sender) and not isinstance(sender, type):
            # @signal.connect used without parentheses - sender is the function
            handler: SignalHandler = sender  # type: ignore
            with self._lock:
                if None not in self._handlers:
                    self._handlers[None] = []
                if handler not in self._handlers[None]:
                    self._handlers[None].append(handler)
            return handler

        return decorator

    def disconnect(
        self,
        receiver: SignalHandler,
        sender: type | None = None,
    ) -> bool:
        """
        Disconnect a receiver from this signal.

        Args:
            receiver: The handler function to disconnect.
            sender: The sender class the handler was connected to.
                    Use None if it was connected to all senders.

        Returns:
            True if the handler was found and removed, False otherwise.
        """
        with self._lock:
            if sender in self._handlers:
                try:
                    self._handlers[sender].remove(receiver)
                    return True
                except ValueError:
                    pass
        return False

    def disconnect_all(self, sender: type | None = None) -> int:
        """
        Disconnect all receivers for a sender.

        Args:
            sender: The sender class to disconnect handlers for.
                    If None, disconnects handlers registered for all senders.

        Returns:
            Number of handlers disconnected.
        """
        with self._lock:
            if sender in self._handlers:
                count = len(self._handlers[sender])
                self._handlers[sender] = []
                return count
        return 0

    def clear(self) -> None:
        """Disconnect all handlers from this signal."""
        with self._lock:
            self._handlers.clear()

    async def send(
        self,
        sender: type[T],
        **kwargs: Any,
    ) -> list[tuple[SignalHandler, Any]]:
        """
        Send signal to all connected receivers.

        Handlers are executed concurrently. If a handler raises an exception,
        it is logged but does not prevent other handlers from executing.

        Args:
            sender: The model class sending the signal.
            **kwargs: Additional arguments passed to handlers.
                      Common kwargs include:
                      - instance: The model instance
                      - created: bool (for save signals)
                      - tx: Transaction context

        Returns:
            List of (handler, result_or_exception) tuples.
        """
        handlers: list[SignalHandler] = []

        # Get a copy of handlers while holding the lock to ensure thread-safety
        with self._lock:
            # Get handlers registered for this specific sender
            if sender in self._handlers:
                handlers.extend(self._handlers[sender])

            # Get handlers registered for any sender (None key)
            if None in self._handlers:
                handlers.extend(self._handlers[None])

        if not handlers:
            return []

        # Execute all handlers concurrently
        results: list[tuple[SignalHandler, Any]] = []

        async def run_handler(handler: SignalHandler) -> tuple[SignalHandler, Any]:
            try:
                await handler(sender=sender, **kwargs)
                return (handler, None)
            except Exception as e:
                logger.exception(
                    f"Signal handler {handler.__name__} raised an exception for signal {self.name} on {sender.__name__}: {e}"
                )
                return (handler, e)

        tasks = [run_handler(h) for h in handlers]
        results = await asyncio.gather(*tasks)

        return list(results)

    async def send_robust(
        self,
        sender: type[T],
        **kwargs: Any,
    ) -> list[tuple[SignalHandler, Any]]:
        """
        Send signal to all receivers, catching exceptions.

        Same as send(), but explicitly designed to handle errors gracefully.
        This is the default behavior - both methods catch exceptions.

        Args:
            sender: The model class sending the signal.
            **kwargs: Additional arguments passed to handlers.

        Returns:
            List of (handler, result_or_exception) tuples.
        """
        return await self.send(sender, **kwargs)

    @property
    def receivers(self) -> dict[type | None, list[SignalHandler]]:
        """Get a copy of all registered handlers."""
        with self._lock:
            return {k: list(v) for k, v in self._handlers.items()}

    def has_receivers(self, sender: type | None = None) -> bool:
        """
        Check if there are any receivers for the given sender.

        Args:
            sender: The sender class to check, or None to check for any receivers.

        Returns:
            True if there are receivers registered.
        """
        with self._lock:
            if sender is None:
                return bool(self._handlers)

            # Check specific sender and global handlers
            return bool(self._handlers.get(sender)) or bool(self._handlers.get(None))


# =============================================================================
# Around Signal - Generator-based middleware pattern
# =============================================================================


@dataclass
class AroundSignal:
    """
    Generator-based signal for wrapping operations with before/after logic.

    Unlike regular signals (pre_*/post_*), AroundSignal handlers are async
    generators that yield once. Code before the yield runs before the operation,
    and code after the yield runs after the operation completes.

    This pattern allows:
    - Shared state between before/after logic (local variables)
    - Guaranteed cleanup with try/finally
    - Timing and metrics collection
    - Transaction-like wrapping

    Attributes:
        name: Human-readable name for the signal (for debugging)

    Example:
        @around_save.connect(Player)
        async def audit_player_save(sender, instance, **kwargs):
            # === BEFORE save ===
            start_time = time.time()
            old_status = instance._db_persisted

            yield  # <-- The save() operation happens here

            # === AFTER save ===
            duration = time.time() - start_time
            await log_audit(
                action="save",
                model=sender.__name__,
                id=instance.id,
                duration=duration,
                was_create=not old_status,
            )

        # With try/finally for guaranteed cleanup
        @around_delete.connect(Player)
        async def with_lock(sender, instance, **kwargs):
            lock = await acquire_lock(instance.id)
            try:
                yield
            finally:
                await release_lock(lock)
    """

    name: str
    _handlers: dict[type | None, list[AroundHandler]] = field(default_factory=dict, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def connect(
        self,
        sender: type[T] | AroundHandler | None = None,
    ) -> Callable[[AroundHandler], AroundHandler] | AroundHandler:
        """
        Connect an async generator handler to this signal.

        Can be used as a decorator with or without sender:

            # With sender - only fires for Player model
            @around_save.connect(Player)
            async def on_player_save(sender, instance, **kwargs):
                # before
                yield
                # after

            # Without sender - fires for all models
            @around_save.connect()
            async def on_any_save(sender, instance, **kwargs):
                yield

        Args:
            sender: Optional model class to filter signals.
                    If None, handler receives signals from all models.

        Returns:
            Decorator function.
        """

        def decorator(func: AroundHandler) -> AroundHandler:
            actual_sender: type | None = None
            if sender is not None and isinstance(sender, type):
                actual_sender = sender

            with self._lock:
                if actual_sender not in self._handlers:
                    self._handlers[actual_sender] = []

                if func not in self._handlers[actual_sender]:
                    self._handlers[actual_sender].append(func)

            return func

        # Check if sender is actually the handler function (decorator without parens)
        if sender is not None and callable(sender) and not isinstance(sender, type):
            handler: AroundHandler = sender  # type: ignore
            with self._lock:
                if None not in self._handlers:
                    self._handlers[None] = []
                if handler not in self._handlers[None]:
                    self._handlers[None].append(handler)
            return handler

        return decorator

    def disconnect(
        self,
        receiver: AroundHandler,
        sender: type | None = None,
    ) -> bool:
        """Disconnect a receiver from this signal."""
        with self._lock:
            if sender in self._handlers:
                try:
                    self._handlers[sender].remove(receiver)
                    return True
                except ValueError:
                    pass
        return False

    def disconnect_all(self, sender: type | None = None) -> int:
        """Disconnect all receivers for a sender."""
        with self._lock:
            if sender in self._handlers:
                count = len(self._handlers[sender])
                self._handlers[sender] = []
                return count
        return 0

    def clear(self) -> None:
        """Disconnect all handlers from this signal."""
        with self._lock:
            self._handlers.clear()

    @asynccontextmanager
    async def wrap(
        self,
        sender: type[T],
        **kwargs: Any,
    ) -> AsyncIterator[None]:
        """
        Context manager that wraps an operation with all connected handlers.

        Usage in model_base.py:
            async with around_save.wrap(self.__class__, instance=self, created=created):
                # The actual save operation happens here
                await client.upsert(thing, data)

        Args:
            sender: The model class.
            **kwargs: Arguments passed to handlers.

        Yields:
            Control back to caller to perform the wrapped operation.
        """
        handlers: list[AroundHandler] = []

        with self._lock:
            if sender in self._handlers:
                handlers.extend(self._handlers[sender])
            if None in self._handlers:
                handlers.extend(self._handlers[None])

        if not handlers:
            # No handlers - just yield control
            yield
            return

        # Track generators with their handlers for proper cleanup and error reporting
        # Each tuple is (generator, handler_function)
        active_generators: list[tuple[AsyncGenerator[None, None], AroundHandler]] = []

        for handler in handlers:
            gen: AsyncGenerator[None, None] | None = None
            try:
                gen = handler(sender=sender, **kwargs)
                await gen.__anext__()  # Run code before yield
                active_generators.append((gen, handler))
            except StopAsyncIteration:
                # Handler didn't yield - close the generator to prevent resource leaks
                if gen is not None:
                    await gen.aclose()
            except Exception as e:
                # Clean up the generator that failed before yielding
                if gen is not None:
                    await gen.aclose()
                logger.exception(
                    f"Around handler {handler.__name__} raised an exception "
                    f"(before phase) for signal {self.name} on {sender.__name__}: {e}"
                )

        try:
            # Yield control to perform the actual operation
            yield
        finally:
            # Run "after" code for all handlers (in reverse order for proper cleanup)
            for gen, handler in reversed(active_generators):
                try:
                    await gen.__anext__()
                except StopAsyncIteration:
                    # Normal - generator finished
                    pass
                except Exception as e:
                    logger.exception(
                        f"Around handler {handler.__name__} raised an exception "
                        f"(after phase) for signal {self.name} on {sender.__name__}: {e}"
                    )
                finally:
                    # Always close the generator to prevent resource leaks
                    await gen.aclose()

    @property
    def receivers(self) -> dict[type | None, list[AroundHandler]]:
        """Get a copy of all registered handlers."""
        with self._lock:
            return {k: list(v) for k, v in self._handlers.items()}

    def has_receivers(self, sender: type | None = None) -> bool:
        """Check if there are any receivers for the given sender."""
        with self._lock:
            if sender is None:
                return bool(self._handlers)
            return bool(self._handlers.get(sender)) or bool(self._handlers.get(None))


# =============================================================================
# Pre-defined Signals
# =============================================================================

pre_save = Signal("pre_save")
"""
Sent before a model's save() method is called.

Arguments sent with this signal:
    sender: The model class.
    instance: The model instance being saved.
    created: Boolean; True if a new record is being created.
    tx: The transaction context, if save() was called with tx parameter.

Example:
    @pre_save.connect(User)
    async def validate_user(sender, instance, created, **kwargs):
        if created and not instance.email:
            raise ValueError("Email is required for new users")
"""

post_save = Signal("post_save")
"""
Sent after a model's save() method is called.

Arguments sent with this signal:
    sender: The model class.
    instance: The model instance that was saved.
    created: Boolean; True if a new record was created.
    tx: The transaction context, if save() was called with tx parameter.

Example:
    @post_save.connect(Order)
    async def send_order_notification(sender, instance, created, **kwargs):
        if created:
            await notify_customer(instance.customer_id, "Order placed!")
"""

pre_delete = Signal("pre_delete")
"""
Sent before a model's delete() method is called.

Arguments sent with this signal:
    sender: The model class.
    instance: The model instance being deleted.
    tx: The transaction context, if delete() was called with tx parameter.

Example:
    @pre_delete.connect(User)
    async def archive_user_data(sender, instance, **kwargs):
        await archive_to_cold_storage(instance)
"""

post_delete = Signal("post_delete")
"""
Sent after a model's delete() method is called.

Arguments sent with this signal:
    sender: The model class.
    instance: The model instance that was deleted.
    tx: The transaction context, if delete() was called with tx parameter.

Example:
    @post_delete.connect(File)
    async def cleanup_file_storage(sender, instance, **kwargs):
        await storage.delete(instance.path)
"""

pre_update = Signal("pre_update")
"""
Sent before a model's merge() or update() method is called.

Arguments sent with this signal:
    sender: The model class.
    instance: The model instance being updated.
    update_fields: Dictionary of fields being updated.
    tx: The transaction context, if method was called with tx parameter.

Example:
    @pre_update.connect(Product)
    async def log_price_change(sender, instance, update_fields, **kwargs):
        if "price" in update_fields:
            await log_audit("price_change", instance.id, update_fields["price"])
"""

post_update = Signal("post_update")
"""
Sent after a model's merge() or update() method is called.

Arguments sent with this signal:
    sender: The model class.
    instance: The model instance that was updated.
    update_fields: Dictionary of fields that were updated.
    tx: The transaction context, if method was called with tx parameter.

Example:
    @post_update.connect(Player)
    async def broadcast_player_update(sender, instance, update_fields, **kwargs):
        await ws_manager.broadcast({
            "type": "player_updated",
            "player_id": instance.id,
            "fields": list(update_fields.keys()),
        })
"""

# =============================================================================
# Pre-defined Around Signals
# =============================================================================

around_save = AroundSignal("around_save")
"""
Wraps a model's save() method with before/after logic.

The handler is an async generator that yields once. Code before yield
runs before the save, code after yield runs after the save completes.

Arguments passed to handler:
    sender: The model class.
    instance: The model instance being saved.
    created: Boolean; True if a new record is being created.
    tx: The transaction context, if save() was called with tx parameter.

Example:
    @around_save.connect(Player)
    async def time_save(sender, instance, created, **kwargs):
        start = time.time()
        yield  # save happens here
        duration = time.time() - start
        logger.info(f"Saved {instance.id} in {duration:.3f}s")

    @around_save.connect(Order)
    async def save_with_lock(sender, instance, **kwargs):
        async with acquire_lock(f"order:{instance.id}"):
            yield  # save happens while lock is held
"""

around_delete = AroundSignal("around_delete")
"""
Wraps a model's delete() method with before/after logic.

Arguments passed to handler:
    sender: The model class.
    instance: The model instance being deleted.
    tx: The transaction context, if delete() was called with tx parameter.

Example:
    @around_delete.connect(File)
    async def delete_with_backup(sender, instance, **kwargs):
        # Before delete - create backup
        backup = await create_backup(instance)
        try:
            yield  # delete happens here
        except Exception:
            # Restore if delete failed
            await restore_backup(backup)
            raise
        finally:
            # Cleanup backup reference
            await cleanup_backup_metadata(backup)
"""

around_update = AroundSignal("around_update")
"""
Wraps a model's update() or merge() method with before/after logic.

Arguments passed to handler:
    sender: The model class.
    instance: The model instance being updated.
    update_fields: Dictionary of fields being updated.
    tx: The transaction context, if method was called with tx parameter.

Example:
    @around_update.connect(Player)
    async def track_field_changes(sender, instance, update_fields, **kwargs):
        # Capture state before update
        old_values = {f: getattr(instance, f, None) for f in update_fields}

        yield  # update happens here

        # Compare after update
        for field, old_val in old_values.items():
            new_val = update_fields.get(field)
            if old_val != new_val:
                await log_change(instance.id, field, old_val, new_val)
"""

# =============================================================================
# Live Change Signal
# =============================================================================

post_live_change = Signal("post_live_change")
"""
Sent when a live query event is received from the database.

Unlike post_save/post_update/post_delete which fire from local CRUD operations,
this signal fires for *external* changes detected via Live Queries. This allows
applications to react to changes made by other clients or services.

Arguments sent with this signal:
    sender: The model class.
    instance: The model instance constructed from the change data.
    action: LiveAction (CREATE, UPDATE, or DELETE).
    record_id: The affected record ID string (e.g., "users:abc123").
    changed_fields: List of changed field names (only in DIFF mode).

Example:
    @post_live_change.connect(Player)
    async def on_player_live_change(sender, instance, action, **kwargs):
        from surreal_sdk.streaming.live_select import LiveAction

        if action == LiveAction.CREATE:
            await ws_manager.broadcast({"type": "player_joined", "name": instance.name})
        elif action == LiveAction.UPDATE:
            await ws_manager.broadcast({"type": "player_updated", "id": instance.id})
        elif action == LiveAction.DELETE:
            await ws_manager.broadcast({"type": "player_left", "id": kwargs["record_id"]})
"""


__all__ = [
    # Regular signals
    "Signal",
    "SignalHandler",
    "pre_save",
    "post_save",
    "pre_delete",
    "post_delete",
    "pre_update",
    "post_update",
    # Live change signal
    "post_live_change",
    # Around signals (generator-based)
    "AroundSignal",
    "AroundHandler",
    "around_save",
    "around_delete",
    "around_update",
]
