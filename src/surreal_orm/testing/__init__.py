"""
Testing utilities for SurrealDB ORM.

Provides declarative test fixtures, model factories with built-in fake
data generation, and helpers for writing integration tests.

Example::

    from surreal_orm.testing import SurrealFixture, fixture, ModelFactory, Faker

    @fixture
    class UserFixtures(SurrealFixture):
        alice = User(name="Alice", email="alice@example.com", role="admin")
        bob = User(name="Bob", email="bob@example.com", role="player")

    class UserFactory(ModelFactory):
        class Meta:
            model = User
        name = Faker("name")
        email = Faker("email")
        age = Faker("random_int", min=18, max=80)
"""

from .factories import Faker, ModelFactory
from .fixtures import SurrealFixture, fixture

__all__ = [
    "Faker",
    "ModelFactory",
    "SurrealFixture",
    "fixture",
]
