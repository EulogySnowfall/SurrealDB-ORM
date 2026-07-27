"""
Unit tests for the CREATE vs UPDATE permission-denial asymmetry bug.

When SurrealDB denies a write because of row-level ``PERMISSIONS``, it returns
an empty result (zero records affected) rather than an error. Historically:

- A denied **create** raised ``SurrealDbError`` (the create path inspects the
  returned record and raises when nothing comes back).
- A denied **update / merge** was a *silent no-op* — it returned ``self`` with
  no error and no indication that 0 rows were affected.

These tests pin down the fixed behavior: the merge / persisted-save paths must
also raise ``SurrealDbError`` when no record is affected, mirroring the create
guard. The empty ``RecordsResponse`` returned by ``client.merge()`` simulates a
permission-denied (or missing-record) write at the SDK boundary.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.surreal_orm.model_base import (
    BaseSurrealModel,
    SurrealConfigDict,
    SurrealDbError,
)
from src.surreal_orm.surreal_function import SurrealFunc
from src.surreal_sdk.types import QueryResponse, QueryResult, RecordResponse, RecordsResponse, ResponseStatus


class Note(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="note")
    id: str | None = None
    title: str
    owner: str


def _denied_merge_client() -> AsyncMock:
    """A mock client whose merge() returns an empty result (write denied)."""
    client = AsyncMock()
    # Permission-denied / missing row → SurrealDB returns no affected records.
    client.merge = AsyncMock(return_value=RecordsResponse(records=[], raw=[]))
    return client


def _allowed_merge_client() -> AsyncMock:
    """A mock client whose merge() returns the updated record (write allowed)."""
    client = AsyncMock()
    client.merge = AsyncMock(
        return_value=RecordsResponse(
            records=[{"id": "note:mine", "title": "updated", "owner": "me"}],
            raw=[{"id": "note:mine", "title": "updated", "owner": "me"}],
        )
    )
    return client


def _patch_client(client: AsyncMock):
    return patch(
        "src.surreal_orm.model_base.SurrealDBConnectionManager.get_client",
        new_callable=AsyncMock,
        return_value=client,
    )


# ── merge(): denied write must raise (was a silent no-op) ────────────────


class TestMergeDeniedRaises:
    async def test_merge_denied_raises(self) -> None:
        """A denied merge (0 records affected) must raise, not silently no-op.

        ``refresh`` is patched out so the *only* possible source of an exception
        is the merge guard itself, not an incidental failure inside refresh().
        """
        note = Note(id="not_mine", title="original", owner="someone_else")
        note._db_persisted = True

        with _patch_client(_denied_merge_client()):
            with patch.object(Note, "refresh", new_callable=AsyncMock):
                with pytest.raises(SurrealDbError):
                    await note.merge(title="hacked")

    async def test_merge_denied_with_refresh_false_raises(self) -> None:
        """Even fire-and-forget merges (refresh=False) must surface denial."""
        note = Note(id="not_mine", title="original", owner="someone_else")
        note._db_persisted = True

        with _patch_client(_denied_merge_client()):
            with pytest.raises(SurrealDbError):
                await note.merge(title="hacked", refresh=False)

    async def test_merge_allowed_returns_self(self) -> None:
        """The happy path is unaffected: an allowed merge returns ``self``."""
        note = Note(id="mine", title="original", owner="me")
        note._db_persisted = True

        with _patch_client(_allowed_merge_client()):
            with patch.object(Note, "refresh", new_callable=AsyncMock):
                result = await note.merge(title="updated")

        assert result is note


# ── save() on a persisted instance must raise when denied ────────────────


class TestPersistedSaveDeniedRaises:
    async def test_persisted_save_denied_raises(self) -> None:
        """save() on a persisted instance is a merge; a denied merge must raise."""
        note = Note(id="not_mine", title="original", owner="someone_else")
        note._db_persisted = True
        note.title = "hacked again"

        with _patch_client(_denied_merge_client()):
            with pytest.raises(SurrealDbError):
                await note.save()

    async def test_persisted_save_allowed_succeeds(self) -> None:
        """The happy path is unaffected: an allowed persisted save returns self."""
        note = Note(id="mine", title="original", owner="me")
        note._db_persisted = True
        note.title = "updated"

        with _patch_client(_allowed_merge_client()):
            result = await note.save()

        assert result is note


# ── Consistency: create and update both raise on denial ──────────────────


class TestCreateUpdateConsistency:
    async def test_create_denied_raises(self) -> None:
        """Baseline: a denied create raises (the behavior update should match)."""
        note = Note(title="x", owner="someone_else")
        note._db_persisted = False

        client = AsyncMock()
        # create() returns an empty RecordResponse when the write is denied.
        client.create = AsyncMock(return_value=RecordResponse(record=None, raw=[]))

        with _patch_client(client):
            with pytest.raises(SurrealDbError):
                await note.save()


# ── Helpers for the SurrealFunc (raw-query) and transaction paths ────────


def _empty_query_response() -> QueryResponse:
    """A QueryResponse with zero affected records (write denied / missing row)."""
    return QueryResponse(results=[QueryResult(status=ResponseStatus.OK, result=[])], raw=[])


def _nonempty_query_response() -> QueryResponse:
    """A QueryResponse carrying one updated record (write allowed)."""
    return QueryResponse(
        results=[QueryResult(status=ResponseStatus.OK, result=[{"id": "note:mine", "title": "updated"}])],
        raw=[{"id": "note:mine", "title": "updated"}],
    )


def _query_client(response: QueryResponse) -> AsyncMock:
    """A mock client whose query() returns *response* (the SurrealFunc path)."""
    client = AsyncMock()
    client.query = AsyncMock(return_value=response)
    return client


def _fake_tx(
    *,
    defers_results: bool,
    merge_result: RecordsResponse | None = None,
    query_result: QueryResponse | None = None,
) -> MagicMock:
    """Build a fake transaction.

    ``defers_results=True`` mimics an HTTP transaction (statements are
    buffered and an *empty placeholder* is returned immediately — the real
    result only exists after commit). ``defers_results=False`` mimics a
    WebSocket transaction (each statement executes immediately, real result).
    """
    tx = MagicMock()
    tx.defers_results = defers_results
    tx.merge = AsyncMock(return_value=merge_result if merge_result is not None else RecordsResponse(records=[], raw=[]))
    tx.query = AsyncMock(return_value=query_result if query_result is not None else _empty_query_response())
    return tx


# ── SurrealFunc (raw UPDATE query) path: denial must raise ───────────────


class TestMergeSurrealFuncDeniedRaises:
    """merge() with a SurrealFunc value runs a raw UPDATE via client.query()."""

    async def test_surrealfunc_merge_denied_raises(self) -> None:
        note = Note(id="not_mine", title="original", owner="someone_else")
        note._db_persisted = True

        with _patch_client(_query_client(_empty_query_response())):
            with pytest.raises(SurrealDbError):
                await note.merge(last_ping=SurrealFunc("time::now()"), refresh=False)

    async def test_surrealfunc_merge_allowed_succeeds(self) -> None:
        note = Note(id="mine", title="original", owner="me")
        note._db_persisted = True

        with _patch_client(_query_client(_nonempty_query_response())):
            result = await note.merge(last_ping=SurrealFunc("time::now()"), refresh=False)

        assert result is note

    async def test_surrealfunc_save_denied_raises(self) -> None:
        """save(server_values=SurrealFunc) on a persisted instance: denied must raise."""
        note = Note(id="not_mine", title="original", owner="someone_else")
        note._db_persisted = True

        with _patch_client(_query_client(_empty_query_response())):
            with pytest.raises(SurrealDbError):
                await note.save(server_values={"last_ping": SurrealFunc("time::now()")})


# ── Transaction paths: WebSocket-style (immediate) denial must raise ─────


class TestTransactionDeniedRaises:
    """A WS-style transaction returns the real result, so denial is detectable."""

    async def test_tx_merge_denied_raises(self) -> None:
        note = Note(id="not_mine", title="original", owner="someone_else")
        note._db_persisted = True

        tx = _fake_tx(defers_results=False)
        with pytest.raises(SurrealDbError):
            await note.merge(title="hacked", tx=tx)

    async def test_tx_persisted_save_denied_raises(self) -> None:
        note = Note(id="not_mine", title="original", owner="someone_else")
        note._db_persisted = True
        note.title = "hacked"

        tx = _fake_tx(defers_results=False)
        with pytest.raises(SurrealDbError):
            await note.save(tx=tx)

    async def test_tx_surrealfunc_merge_denied_raises(self) -> None:
        note = Note(id="not_mine", title="original", owner="someone_else")
        note._db_persisted = True

        tx = _fake_tx(defers_results=False)
        with pytest.raises(SurrealDbError):
            await note.merge(last_ping=SurrealFunc("time::now()"), tx=tx, refresh=False)

    async def test_tx_merge_allowed_succeeds(self) -> None:
        note = Note(id="mine", title="original", owner="me")
        note._db_persisted = True

        tx = _fake_tx(
            defers_results=False,
            merge_result=RecordsResponse(records=[{"id": "note:mine", "title": "ok"}], raw=[]),
        )
        result = await note.merge(title="ok", tx=tx)
        assert result is note


# ── HTTP transactions buffer results: an empty placeholder is NOT denial ──


class TestDeferredTransactionDoesNotFalselyRaise:
    """HTTP transactions return empty placeholders before commit.

    The guard must NOT treat that placeholder as a denied write, or every
    transactional merge/save would wrongly raise.
    """

    async def test_deferred_tx_merge_does_not_raise(self) -> None:
        note = Note(id="any", title="original", owner="me")
        note._db_persisted = True

        tx = _fake_tx(defers_results=True)  # empty placeholder, but deferred
        result = await note.merge(title="queued", tx=tx)
        assert result is note

    async def test_deferred_tx_persisted_save_does_not_raise(self) -> None:
        note = Note(id="any", title="original", owner="me")
        note._db_persisted = True
        note.title = "queued"

        tx = _fake_tx(defers_results=True)
        result = await note.save(tx=tx)
        assert result is note

    async def test_deferred_tx_surrealfunc_merge_does_not_raise(self) -> None:
        note = Note(id="any", title="original", owner="me")
        note._db_persisted = True

        tx = _fake_tx(defers_results=True)
        result = await note.merge(last_ping=SurrealFunc("time::now()"), tx=tx, refresh=False)
        assert result is note
