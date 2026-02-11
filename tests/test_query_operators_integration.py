"""Integration tests for query filter operators against SurrealDB 2.6.

Verifies that every lookup type generates valid SurrealQL that executes
successfully and returns the correct results.
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

import pytest

from src import surreal_orm
from src.surreal_orm import BaseSurrealModel, Q, SurrealConfigDict
from tests.conftest import SURREALDB_URL, SURREALDB_USER, SURREALDB_PASS, SURREALDB_NAMESPACE

SURREALDB_DATABASE = "test_operators"


class Person(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="person")

    id: str | None = None
    name: str = ""
    email: str = ""
    age: int = 0
    role: str = ""
    tags: list[str] = []
    is_verified: bool = False


@pytest.fixture(scope="module", autouse=True)
async def setup(request: pytest.FixtureRequest) -> AsyncGenerator[None, Any]:
    """Set up connection and seed test data."""
    surreal_orm.SurrealDBConnectionManager.set_connection(
        SURREALDB_URL,
        SURREALDB_USER,
        SURREALDB_PASS,
        SURREALDB_NAMESPACE,
        SURREALDB_DATABASE,
    )
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    await client.query("REMOVE TABLE IF EXISTS person;")

    # Seed
    people = [
        Person(
            id="alice",
            name="Alice Smith",
            email="alice@example.com",
            age=30,
            role="admin",
            tags=["python", "rust"],
            is_verified=True,
        ),
        Person(
            id="bob", name="Bob Jones", email="bob@example.com", age=25, role="user", tags=["go", "python"], is_verified=False
        ),
        Person(id="carol", name="Carol Grey", email="carol@example.com", age=40, role="admin", tags=["java"], is_verified=True),
        Person(
            id="dave",
            name="Dave Brown",
            email="dave@test.org",
            age=22,
            role="user",
            tags=["python", "java", "go"],
            is_verified=False,
        ),
    ]
    for p in people:
        await p.save()

    yield

    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    await client.query("REMOVE TABLE IF EXISTS person;")


# ── Comparison operators ─────────────────────────────────────────────


@pytest.mark.integration
async def test_filter_exact() -> None:
    results = await Person.objects().filter(name="Alice Smith").exec()
    assert len(results) == 1
    assert results[0].name == "Alice Smith"


@pytest.mark.integration
async def test_filter_gt_gte_lt_lte() -> None:
    gt = await Person.objects().filter(age__gt=30).exec()
    assert all(p.age > 30 for p in gt)

    gte = await Person.objects().filter(age__gte=30).exec()
    assert all(p.age >= 30 for p in gte)
    assert len(gte) >= len(gt)

    lt = await Person.objects().filter(age__lt=30).exec()
    assert all(p.age < 30 for p in lt)

    lte = await Person.objects().filter(age__lte=30).exec()
    assert all(p.age <= 30 for p in lte)


@pytest.mark.integration
async def test_filter_in() -> None:
    results = await Person.objects().filter(role__in=["admin", "moderator"]).exec()
    assert all(p.role == "admin" for p in results)
    assert len(results) == 2


@pytest.mark.integration
async def test_filter_not_in() -> None:
    results = await Person.objects().filter(role__not_in=["admin"]).exec()
    assert all(p.role != "admin" for p in results)
    assert len(results) == 2


# ── String function lookups ──────────────────────────────────────────


@pytest.mark.integration
async def test_filter_startswith() -> None:
    results = await Person.objects().filter(name__startswith="Al").exec()
    assert len(results) == 1
    assert results[0].name == "Alice Smith"


@pytest.mark.integration
async def test_filter_endswith() -> None:
    results = await Person.objects().filter(name__endswith="Jones").exec()
    assert len(results) == 1
    assert results[0].name == "Bob Jones"


@pytest.mark.integration
async def test_filter_like() -> None:
    """LIKE %pattern% → string::matches(field, regex)."""
    results = await Person.objects().filter(name__like="%Grey%").exec()
    assert len(results) == 1
    assert results[0].name == "Carol Grey"


@pytest.mark.integration
async def test_filter_like_prefix() -> None:
    """LIKE pattern% → starts-with behavior."""
    results = await Person.objects().filter(name__like="Dave%").exec()
    assert len(results) == 1
    assert results[0].name == "Dave Brown"


@pytest.mark.integration
async def test_filter_ilike() -> None:
    """ILIKE (case-insensitive) → string::matches with (?i)."""
    results = await Person.objects().filter(name__ilike="%grey%").exec()
    assert len(results) == 1
    assert results[0].name == "Carol Grey"


@pytest.mark.integration
async def test_filter_icontains() -> None:
    """icontains → string::contains(string::lowercase(...), lowercase(value))."""
    results = await Person.objects().filter(name__icontains="ALICE").exec()
    assert len(results) == 1
    assert results[0].name == "Alice Smith"


@pytest.mark.integration
async def test_filter_regex() -> None:
    """regex → string::matches(field, pattern)."""
    results = await Person.objects().filter(name__regex="Gr(a|e)y").exec()
    assert len(results) == 1
    assert results[0].name == "Carol Grey"


@pytest.mark.integration
async def test_filter_iregex() -> None:
    """iregex → string::matches(field, (?i)pattern)."""
    results = await Person.objects().filter(name__iregex="alice").exec()
    assert len(results) == 1
    assert results[0].name == "Alice Smith"


# ── Array operators ──────────────────────────────────────────────────


@pytest.mark.integration
async def test_filter_contains_array() -> None:
    """CONTAINS checks array membership."""
    results = await Person.objects().filter(tags__contains="rust").exec()
    assert len(results) == 1
    assert results[0].name == "Alice Smith"


@pytest.mark.integration
async def test_filter_not_contains() -> None:
    results = await Person.objects().filter(tags__not_contains="python").exec()
    assert all("python" not in p.tags for p in results)


@pytest.mark.integration
async def test_filter_containsall() -> None:
    results = await Person.objects().filter(tags__containsall=["python", "go"]).exec()
    assert all("python" in p.tags and "go" in p.tags for p in results)


@pytest.mark.integration
async def test_filter_containsany() -> None:
    results = await Person.objects().filter(tags__containsany=["rust", "java"]).exec()
    assert len(results) >= 2  # Alice (rust), Carol (java), Dave (java)


# ── IS NULL ──────────────────────────────────────────────────────────


@pytest.mark.integration
async def test_filter_isnull() -> None:
    # All persons have a name, so isnull=True should return 0
    results = await Person.objects().filter(name__isnull=True).exec()
    assert len(results) == 0

    # isnull=False should return all
    results = await Person.objects().filter(name__isnull=False).exec()
    assert len(results) == 4


# ── Q objects ────────────────────────────────────────────────────────


@pytest.mark.integration
async def test_q_or() -> None:
    results = (
        await Person.objects()
        .filter(
            Q(name__startswith="Al") | Q(name__startswith="Bo"),
        )
        .exec()
    )
    names = {p.name for p in results}
    assert "Alice Smith" in names
    assert "Bob Jones" in names
    assert len(results) == 2


@pytest.mark.integration
async def test_q_and_with_kwargs() -> None:
    results = (
        await Person.objects()
        .filter(
            Q(name__ilike="%ali%") | Q(email__ilike="%ali%"),
            is_verified=True,
        )
        .exec()
    )
    assert len(results) == 1
    assert results[0].name == "Alice Smith"


@pytest.mark.integration
async def test_q_not() -> None:
    results = await Person.objects().filter(~Q(role="admin")).exec()
    assert all(p.role != "admin" for p in results)
    assert len(results) == 2


# ── Combined filters ────────────────────────────────────────────────


@pytest.mark.integration
async def test_combined_filters() -> None:
    """Multiple filter conditions combined with AND."""
    results = (
        await Person.objects()
        .filter(
            role="admin",
            age__gte=30,
            is_verified=True,
        )
        .exec()
    )
    assert len(results) == 2
    assert all(p.role == "admin" and p.age >= 30 and p.is_verified for p in results)


@pytest.mark.integration
async def test_filter_with_order_and_limit() -> None:
    results = (
        await Person.objects()
        .filter(
            role="user",
        )
        .order_by("age")
        .limit(1)
        .exec()
    )
    assert len(results) == 1
    assert results[0].name == "Dave Brown"  # youngest user (22)
