"""Unit tests for #470 (v2): values a query attaches to an instance are never saved.

``similar_to()`` attaches ``_knn_distance``, a ``SearchScore`` /
``SearchHighlight`` annotation attaches its alias and ``prefetch_related()``
attaches the relation name (or ``Prefetch(to_attr=...)``). On a model with
``extra="allow"``, those values used to reach the extra fields (directly from
the row, or on the next validated assignment), and ``save()`` / ``update()``
then wrote them as columns. The attached names are tracked outside the model
data, so nothing that validated input supplies can change what a write sends.
These tests need no server.
"""

from __future__ import annotations

import gc
import pickle
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import Field

from surreal_orm import BaseSurrealModel, Prefetch, SearchHighlight, SearchScore, SurrealConfigDict, model_base
from surreal_orm.query_set import QuerySet
from surreal_sdk.protocol.cbor import RecordId


class I470Order(BaseSurrealModel):
    """Orders with the default ``extra`` setting."""

    model_config = SurrealConfigDict(table_name="i470_order")

    id: str | None = None
    status: str = Field(default="")
    title: str = ""


class I470Extra(BaseSurrealModel):
    """Orders whose model keeps unknown keys (``extra="allow"``)."""

    model_config = SurrealConfigDict(table_name="i470_extra", extra="allow")

    id: str | None = None
    status: str = Field(default="")
    title: str = ""


def _row(**extra: Any) -> dict[str, Any]:
    return {"id": RecordId(table="i470_extra", id="o1"), "status": "open", "title": "quantum", **extra}


async def _searched() -> I470Extra:
    """An ``extra="allow"`` instance from a KNN + full-text query, with a real extra field ``other``."""
    rows = [_row(_knn_distance=0.5, rel=1.5, hl="<b>quantum</b>", other=1)]
    qs = (
        I470Extra.objects()
        .search(title="quantum")
        .annotate(rel=SearchScore(0), hl=SearchHighlight("<b>", "</b>", 0))
        .similar_to("title", [1.0], limit=1)
    )
    with patch.object(QuerySet, "_execute_query", new=AsyncMock(return_value=rows)):
        (order,) = await qs.exec()
    assert isinstance(order, I470Extra)
    return order


def _prefetch_client() -> MagicMock:
    client = MagicMock()
    result = MagicMock()
    result.all_records = [{"in": "i470_extra:o1", "id": "post:p1", "title": "t"}]
    client.query = AsyncMock(return_value=result)
    return client


class TestQueryValuesAreNeverWritten:
    async def test_knn_and_search_values_after_an_assignment(self) -> None:
        order = await _searched()
        assert order._knn_distance == 0.5  # type: ignore[attr-defined]
        assert order.rel == 1.5  # type: ignore[attr-defined]
        assert order.hl == "<b>quantum</b>"  # type: ignore[attr-defined]
        order.status = "done"  # a validated assignment moves non-field __dict__ keys into the extras
        with patch.object(I470Extra, "_execute_save", new=AsyncMock()) as run:
            await order.save()
        assert run.await_args is not None
        assert run.await_args.args[3] == {"status": "done", "other": 1}  # a real extra field is still written

        tx = AsyncMock()
        await order.update(tx=tx)
        tx.merge.assert_awaited_once_with("i470_extra:o1", {"status": "done", "other": 1})

    async def test_values_never_reach_the_extra_fields_from_the_row(self) -> None:
        order = await _searched()
        assert order.__pydantic_extra__ == {"other": 1}

    async def test_save_without_assignment(self) -> None:
        order = await _searched()
        with patch.object(I470Extra, "_execute_save", new=AsyncMock()) as run:
            await order.save()
        assert run.await_args is not None
        assert run.await_args.args[3] == {"other": 1}  # extra fields are always dumped

    async def test_save_in_a_transaction(self) -> None:
        order = await _searched()
        order.title = "physics"
        tx = AsyncMock()
        with patch.object(I470Extra, "_execute_save", new=AsyncMock()) as run:
            await order.save(tx=tx)
        assert run.await_args is not None
        assert run.await_args.args[0] is tx
        assert run.await_args.args[3] == {"title": "physics", "other": 1}

    async def test_bulk_create_saves_through_save(self) -> None:
        order = await _searched()
        order.status = "done"
        with patch.object(I470Extra, "_execute_save", new=AsyncMock()) as run:
            await I470Extra.objects().bulk_create([order])
        assert run.await_args is not None
        assert run.await_args.args[3] == {"status": "done", "other": 1}

    @pytest.mark.parametrize("prefetch", [Prefetch("wrote", to_attr="posts"), "wrote"])
    async def test_prefetched_values(self, prefetch: Prefetch | str) -> None:
        name = prefetch.to_attr if isinstance(prefetch, Prefetch) else prefetch
        qs = I470Extra.objects().prefetch_related(prefetch)
        with (
            patch.object(QuerySet, "_execute_query", new=AsyncMock(return_value=[_row(other=1)])),
            patch(
                "surreal_orm.query_set.SurrealDBConnectionManager.get_client",
                new=AsyncMock(return_value=_prefetch_client()),
            ),
        ):
            (order,) = await qs.exec()
        assert isinstance(order, I470Extra)
        assert getattr(order, name) == [{"id": "post:p1", "title": "t"}]
        order.status = "done"
        with patch.object(I470Extra, "_execute_save", new=AsyncMock()) as run:
            await order.save()
        assert run.await_args is not None
        assert run.await_args.args[3] == {"status": "done", "other": 1}
        tx = AsyncMock()
        await order.update(tx=tx)
        tx.merge.assert_awaited_once_with("i470_extra:o1", {"status": "done", "other": 1})

    async def test_prefetch_without_ids_attaches_an_empty_list(self) -> None:
        orders = [I470Extra(status="open")]
        qs = I470Extra.objects().prefetch_related(Prefetch("wrote", to_attr="posts"))
        with patch(
            "surreal_orm.query_set.SurrealDBConnectionManager.get_client",
            new=AsyncMock(return_value=_prefetch_client()),
        ):
            await qs._execute_prefetch(orders)
        assert orders[0].posts == []  # type: ignore[attr-defined]
        assert orders[0]._attached_names() == {"posts"}


class TestAttachedNamesStorage:
    def test_attached_value_does_not_change_equality(self) -> None:
        loaded = I470Order(id="o1", status="open")
        annotated = I470Order(id="o1", status="open")
        annotated._attach_value("_knn_distance", 0.5)
        annotated._attach_value("rel", 1.5)
        assert annotated == loaded
        assert loaded == annotated

    async def test_searched_instance_equals_the_plain_record(self) -> None:
        order = await _searched()
        plain = I470Extra.from_db(_row(other=1))
        assert order == plain

    def test_model_copy_does_not_share_the_names(self) -> None:
        original = I470Order(id="o1")
        original._attach_value("_knn_distance", 0.5)
        copy = original.model_copy()
        copy._attach_value("score", 1)
        assert original._attached_names() == {"_knn_distance"}
        assert copy._attached_names() == {"_knn_distance", "score"}

    def test_copies_carry_the_names_of_the_values_they_copy(self) -> None:
        original = I470Extra(id="o1")
        original._attach_value("rel", 1)
        for copy in (original.model_copy(), original.model_copy(deep=True)):
            assert copy._attached_names() == {"rel"}
            copy.status = "done"  # the copied value moves into the extra fields
            assert "rel" in copy._write_excluded_names()
        assert original._attached_names() == {"rel"}

    def test_a_model_field_is_set_but_never_recorded(self) -> None:
        order = I470Order(id="o1")
        assert order._attached_names() == set()
        order._attach_value("status", "x")
        assert order.status == "x"
        assert order._attached_names() == set()

    def test_names_are_not_part_of_the_instance_data(self) -> None:
        order = I470Extra(id="o1")
        order._attach_value("rel", 1)
        order.status = "done"
        assert "_sorm_attached" not in order.__dict__
        assert "_sorm_attached" not in (order.__pydantic_extra__ or {})
        assert order.__pydantic_private__ == I470Extra(id="o1").__pydantic_private__

    async def test_pickle_round_trip_carries_the_names(self) -> None:
        loaded = I470Extra.from_db({"id": RecordId(table="i470_extra", id="o1"), "status": "open"})
        assert isinstance(loaded, I470Extra)
        loaded._attach_value("rel", 1.5)
        order = pickle.loads(pickle.dumps(loaded))
        assert order._attached_names() == {"rel"}
        assert order.rel == 1.5
        order.status = "done"  # moves the unpickled `rel` into the extra fields
        with patch.object(I470Extra, "_execute_save", new=AsyncMock()) as run:
            await order.save()
        assert run.await_args is not None
        assert run.await_args.args[3] == {"status": "done"}

    def test_pickle_round_trip_without_attach_registers_nothing(self) -> None:
        order = pickle.loads(pickle.dumps(I470Extra(id="o1", status="open")))
        assert order._attached_names() == set()
        assert id(order) not in model_base._ATTACHED_NAMES

    @pytest.mark.parametrize("names", [["status", "rel", 5, None], "rel", None, 5, {"rel": 1}])
    def test_tampered_pickle_state_keeps_only_non_field_str_names(self, names: Any) -> None:
        order = I470Extra(id="o1")
        order._attach_value("rel", 1)
        state = order.__getstate__()
        state["_sorm_attached_names"] = names
        restored = I470Extra.__new__(I470Extra)
        restored.__setstate__(state)
        expected = {"rel"} if isinstance(names, list) else set()
        assert restored._attached_names() == expected

    def test_registry_entry_dropped_when_the_instance_is_collected(self) -> None:
        order = I470Order(id="o1")
        order._attach_value("rel", 1)
        key = id(order)
        assert key in model_base._ATTACHED_NAMES
        del order
        gc.collect()
        assert key not in model_base._ATTACHED_NAMES


_SOURCES = ["init", "model_validate", "from_db"]


def _extra_instance(source: str) -> I470Extra:
    """An ``extra="allow"`` instance built from input whose reserved key names real fields."""
    payload: dict[str, Any] = {
        "id": "o1",
        "status": "done",
        "title": "t",
        "_sorm_attached": ["status", "title", "id"],
    }
    if source == "init":
        return I470Extra(**payload)
    if source == "model_validate":
        return I470Extra.model_validate(payload)
    loaded = I470Extra.from_db({**payload, "id": RecordId(table="i470_extra", id="o1")})
    assert isinstance(loaded, I470Extra)
    loaded.status = "done"  # validated assignments, as after a load and an edit
    loaded.title = "t"
    return loaded


class TestReservedKeyNamesNothing:
    @pytest.mark.parametrize("bad", [["status"], "status", 5, None, [5, "title"], {"status": 1}])
    def test_input_key_names_nothing(self, bad: Any) -> None:
        order = I470Extra(id="o1", status="done", _sorm_attached=bad)
        assert order._attached_names() == set()
        assert order._write_excluded_names() == {"_sorm_attached"}

    @pytest.mark.parametrize("source", _SOURCES)
    async def test_save_writes_every_real_field(self, source: str) -> None:
        order = _extra_instance(source)
        with patch.object(I470Extra, "_execute_save", new=AsyncMock()) as run:
            await order.save()
        assert run.await_args is not None
        assert run.await_args.args[3] == {"status": "done", "title": "t"}

    @pytest.mark.parametrize("source", _SOURCES)
    async def test_update_writes_every_real_field(self, source: str) -> None:
        order = _extra_instance(source)
        tx = AsyncMock()
        await order.update(tx=tx)
        tx.merge.assert_awaited_once_with("i470_extra:o1", {"status": "done", "title": "t"})

    @pytest.mark.parametrize("source", _SOURCES)
    async def test_reserved_key_does_not_exclude_attached_names_either(self, source: str) -> None:
        order = _extra_instance(source)
        order._attach_value("rel", 1.5)
        order.status = "done"
        with patch.object(I470Extra, "_execute_save", new=AsyncMock()) as run:
            await order.save()
        assert run.await_args is not None
        assert run.await_args.args[3] == {"status": "done", "title": "t"}
