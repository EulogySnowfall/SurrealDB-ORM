"""
Integration tests for Issue #55: Parameter binding fails for large nested dicts.

These tests verify that large/deeply nested dicts survive round-trips through
both the SDK (direct RPC) and ORM (save/raw_query) paths against a real
SurrealDB v2.6 instance.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import Any

import pytest

from src import surreal_orm
from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "test_issue_55"


def _make_large_game_state(num_players: int = 4, hand_size: int = 15) -> dict[str, Any]:
    """Create a realistic game state dict (~20-80KB JSON)."""
    return {
        "phase": "play",
        "round_number": 3,
        "current_player_index": 0,
        "deck": [
            {"suit": s, "rank": r, "id": f"{s}_{r}"}
            for s in ["hearts", "diamonds", "clubs", "spades"]
            for r in ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]
        ],
        "discard_pile": [{"suit": "hearts", "rank": "7", "played_by": 0}],
        "players": [
            {
                "seat": i,
                "name": f"Player_{i}",
                "hand": [
                    {"suit": ["hearts", "diamonds", "clubs", "spades"][j % 4], "rank": str(j + 2)} for j in range(hand_size)
                ],
                "melds": [
                    {"type": "set", "cards": [{"suit": "hearts", "rank": "7"}] * 4},
                    {"type": "run", "cards": [{"suit": "clubs", "rank": str(k)} for k in range(3, 10)]},
                    {"type": "set", "cards": [{"suit": "spades", "rank": "A"}] * 3},
                ],
                "score": 1500 + i * 200,
                "is_ready": True,
                "metadata": {"join_time": "2026-02-15T10:00:00Z", "avatar": f"avatar_{i}.png"},
            }
            for i in range(num_players)
        ],
        "team_scores": [3200, 2800],
        "settings": {
            "max_rounds": 10,
            "time_limit": 60,
            "allow_undo": False,
            "scoring": {"base_points": 100, "bonus_multiplier": 1.5},
        },
    }


# ── ORM model for testing ──────────────────────────────────────────────


class Issue55Model(BaseSurrealModel):
    """Schemaless model with dict field for testing nested dict saves."""

    model_config = SurrealConfigDict(table_name="issue55_test")

    id: str | None = None
    name: str = "test"
    game_state: dict[str, Any] | None = None


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture(scope="module", autouse=True)
async def setup_and_clean() -> AsyncGenerator[None, Any]:
    """Initialize connection and clean test tables."""
    surreal_orm.SurrealDBConnectionManager.set_connection(
        SURREALDB_URL,
        SURREALDB_USER,
        SURREALDB_PASS,
        SURREALDB_NAMESPACE,
        SURREALDB_DATABASE,
    )
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    for table in ["issue55_test", "issue55_sdk"]:
        try:
            await client.query(f"REMOVE TABLE IF EXISTS {table};")
        except Exception:
            pass

    yield

    try:
        client = await surreal_orm.SurrealDBConnectionManager.get_client()
        for table in ["issue55_test", "issue55_sdk"]:
            try:
                await client.query(f"REMOVE TABLE IF EXISTS {table};")
            except Exception:
                pass
    except Exception:
        pass


# ── SDK-level tests ─────────────────────────────────────────────────────


@pytest.mark.integration
async def test_sdk_upsert_large_nested_dict() -> None:
    """SDK upsert (RPC) with large nested dict should preserve all data."""
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    state = _make_large_game_state()

    await client.upsert("issue55_sdk:upsert1", {"game_state": state, "name": "upsert_test"})

    resp = await client.select("issue55_sdk:upsert1")
    record = resp.first
    assert record is not None, "Record not found after upsert"

    gs = record.get("game_state", {})
    assert gs.get("phase") == "play", f"Expected phase='play', got {gs.get('phase')!r}"
    assert len(gs.get("players", [])) == 4, f"Expected 4 players, got {len(gs.get('players', []))}"
    assert len(gs.get("deck", [])) == 52, f"Expected 52 cards, got {len(gs.get('deck', []))}"
    assert gs.get("players", [{}])[0].get("melds", [{}])[0].get("cards", [{}])[0].get("suit") == "hearts"


@pytest.mark.integration
async def test_sdk_query_large_dict_variable() -> None:
    """SDK query() with large nested dict as $variable should work."""
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    state = _make_large_game_state()

    # Use query with parameter binding
    await client.query(
        "UPSERT issue55_sdk:query1 SET game_state = $state, name = 'query_test';",
        {"state": state},
    )

    resp = await client.select("issue55_sdk:query1")
    record = resp.first
    assert record is not None, "Record not found after query UPSERT"

    gs = record.get("game_state", {})
    assert gs.get("phase") == "play", f"Expected phase='play', got {gs.get('phase')!r}"
    assert len(gs.get("players", [])) == 4, f"Expected 4 players, got {len(gs.get('players', []))}"
    assert len(gs.get("deck", [])) == 52, f"Expected 52 cards, got {len(gs.get('deck', []))}"


@pytest.mark.integration
async def test_sdk_merge_large_nested_dict() -> None:
    """SDK merge (RPC) with large nested dict should preserve all data."""
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    state = _make_large_game_state()

    # Create record first
    await client.create("issue55_sdk:merge1", {"name": "merge_test"})
    # Merge with large nested dict
    await client.merge("issue55_sdk:merge1", {"game_state": state})

    resp = await client.select("issue55_sdk:merge1")
    record = resp.first
    assert record is not None, "Record not found after merge"

    gs = record.get("game_state", {})
    assert gs.get("phase") == "play", f"Expected phase='play', got {gs.get('phase')!r}"
    assert len(gs.get("players", [])) == 4, f"Expected 4 players, got {len(gs.get('players', []))}"


# ── ORM-level tests ─────────────────────────────────────────────────────


@pytest.mark.integration
async def test_orm_save_large_nested_dict() -> None:
    """ORM save() with large nested dict should preserve all data."""
    state = _make_large_game_state()
    model = Issue55Model(id="save1", name="save_test", game_state=state)
    await model.save()

    # Read back via SDK to verify
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    resp = await client.select("issue55_test:save1")
    record = resp.first
    assert record is not None, "Record not found after save"

    gs = record.get("game_state", {})
    assert gs.get("phase") == "play", f"Expected phase='play', got {gs.get('phase')!r}"
    assert len(gs.get("players", [])) == 4, f"Expected 4 players, got {len(gs.get('players', []))}"
    assert len(gs.get("deck", [])) == 52, f"Expected 52 cards, got {len(gs.get('deck', []))}"


@pytest.mark.integration
async def test_orm_save_merge_large_nested_dict() -> None:
    """ORM save() for already-persisted record with updated large dict."""
    state = _make_large_game_state()
    model = Issue55Model(id="merge1", name="initial")
    await model.save()

    # Update with large nested dict (triggers merge path)
    model.game_state = state
    await model.save()

    # Read back
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    resp = await client.select("issue55_test:merge1")
    record = resp.first
    assert record is not None, "Record not found after save-merge"

    gs = record.get("game_state", {})
    assert gs.get("phase") == "play", f"Expected phase='play', got {gs.get('phase')!r}"
    assert len(gs.get("players", [])) == 4, f"Expected 4 players, got {len(gs.get('players', []))}"


@pytest.mark.integration
async def test_orm_raw_query_large_dict_variable() -> None:
    """ORM raw_query() with large nested dict variable (inline_dicts=True)."""
    state = _make_large_game_state()

    await Issue55Model.raw_query(
        "UPSERT issue55_test:raw1 SET game_state = $state, name = 'raw_test';",
        variables={"state": state},
        inline_dicts=True,
    )

    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    resp = await client.select("issue55_test:raw1")
    record = resp.first
    assert record is not None, "Record not found after raw_query"

    gs = record.get("game_state", {})
    assert gs.get("phase") == "play", f"Expected phase='play', got {gs.get('phase')!r}"
    assert len(gs.get("players", [])) == 4, f"Expected 4 players, got {len(gs.get('players', []))}"
    assert len(gs.get("deck", [])) == 52, f"Expected 52 cards, got {len(gs.get('deck', []))}"


@pytest.mark.integration
async def test_orm_raw_query_inline_preserves_simple_vars() -> None:
    """inline_dicts=True should not affect simple (non-dict) variables."""
    await Issue55Model.raw_query(
        "UPSERT issue55_test:simple1 SET name = $name;",
        variables={"name": "simple_test"},
        inline_dicts=True,
    )

    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    resp = await client.select("issue55_test:simple1")
    record = resp.first
    assert record is not None
    assert record["name"] == "simple_test"


@pytest.mark.integration
async def test_data_integrity_deep_nesting() -> None:
    """Verify deeply nested values survive the round-trip."""
    state = _make_large_game_state()

    model = Issue55Model(id="deep1", name="deep_test", game_state=state)
    await model.save()

    # Verify specific deep values
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    resp = await client.select("issue55_test:deep1")
    record = resp.first
    assert record is not None

    gs = record["game_state"]

    # 3 levels deep: game_state -> players[0] -> hand[0] -> suit
    assert gs["players"][0]["hand"][0]["suit"] == "hearts"

    # 3 levels deep: game_state -> players[0] -> melds[0] -> cards[0] -> suit
    assert gs["players"][0]["melds"][0]["cards"][0]["suit"] == "hearts"

    # 3 levels deep: game_state -> settings -> scoring -> base_points
    assert gs["settings"]["scoring"]["base_points"] == 100

    # Verify JSON size is substantial
    json_size = len(json.dumps(gs))
    assert json_size > 5000, f"Expected >5KB, got {json_size}"
