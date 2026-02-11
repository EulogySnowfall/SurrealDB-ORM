"""
Declarative test fixtures for SurrealDB ORM.

Provides ``SurrealFixture`` base class and ``@fixture`` decorator for
defining reusable test data that is automatically saved to and cleaned
up from the database.

Example::

    from surreal_orm.testing import SurrealFixture, fixture

    @fixture
    class UserFixtures(SurrealFixture):
        alice = User(name="Alice", email="alice@example.com", role="admin")
        bob = User(name="Bob", email="bob@example.com", role="player")

    # In a test
    async with UserFixtures.load() as fixtures:
        assert fixtures.alice.get_id() is not None
        admins = await User.objects().filter(role="admin").exec()
        assert len(admins) == 1
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from surreal_orm.model_base import BaseSurrealModel

logger = logging.getLogger(__name__)


def _is_surreal_model(obj: object) -> bool:
    """Check if *obj* is a SurrealModel instance (duck-type safe).

    Using a duck-type check avoids false negatives caused by dual-import
    paths (e.g. ``src.surreal_orm`` vs ``surreal_orm``), where
    ``isinstance()`` would fail even though the object is functionally
    the same class.
    """
    return hasattr(obj, "save") and hasattr(obj, "model_copy") and hasattr(obj, "get_table_name")


def fixture(cls: type) -> type:
    """
    Decorator that scans a ``SurrealFixture`` subclass for model instances
    and registers them for bulk loading.

    Example::

        @fixture
        class ProductFixtures(SurrealFixture):
            widget = Product(name="Widget", price=9.99)
            gadget = Product(name="Gadget", price=19.99)
    """
    instances: dict[str, BaseSurrealModel] = {}
    for attr_name in list(vars(cls)):
        value = getattr(cls, attr_name)
        if isinstance(value, BaseSurrealModel) or _is_surreal_model(value):
            instances[attr_name] = value
    cls._fixture_instances = instances  # type: ignore[attr-defined]
    return cls


class SurrealFixture:
    """
    Base class for declarative test fixtures.

    Subclasses define model instances as class attributes and decorate
    the class with ``@fixture``. Use ``load()`` as an async context
    manager to save all instances to the database and automatically
    clean up on exit.

    Example::

        @fixture
        class GameFixtures(SurrealFixture):
            table1 = GameTable(name="Table 1", max_players=4)
            player1 = Player(name="Alice", seat=1)

        async with GameFixtures.load() as fixtures:
            # fixtures.table1 is now saved in DB
            assert fixtures.table1.get_id() is not None
    """

    _fixture_instances: dict[str, BaseSurrealModel]

    @classmethod
    @asynccontextmanager
    async def load(cls) -> AsyncIterator[Any]:
        """
        Save all fixture instances to the database and yield self.

        On exit, delete all saved instances (best-effort cleanup).
        """
        instances = getattr(cls, "_fixture_instances", {})
        if not instances:
            raise ValueError(f"{cls.__name__} has no fixture instances. Did you forget the @fixture decorator?")

        # Create a namespace object to hold saved instances
        holder = cls()

        # Clone and save instances — each load() gets fresh copies to avoid
        # leaking state (id, _db_persisted) across multiple load() calls.
        saved: list[BaseSurrealModel] = []
        for attr_name, template in instances.items():
            clone = template.model_copy(deep=True)
            if hasattr(clone, "id"):
                object.__setattr__(clone, "id", None)
            if hasattr(clone, "_db_persisted"):
                object.__setattr__(clone, "_db_persisted", False)
            await clone.save()
            saved.append(clone)
            setattr(holder, attr_name, clone)

        try:
            yield holder
        finally:
            # Cleanup: delete all saved instances (best-effort)
            for instance in reversed(saved):
                try:
                    await instance.delete()
                except Exception:
                    logger.debug(
                        "Fixture cleanup: failed to delete %s",
                        instance,
                        exc_info=True,
                    )


__all__ = ["SurrealFixture", "fixture"]
