"""Unit tests for the ORM half of v0.37.0 data I/O: ``QuerySet`` JSON Lines export (U11) and import (U12).

No server: the client is a fake whose ``query()`` returns prepared replies, so
the tests check the emitted statements, the bound variables and the lines.

``RecordId`` is imported from ``surreal_sdk.protocol.cbor``, never from
``src.surreal_sdk.protocol.cbor``: with ``PYTHONPATH=.`` the latter is a second,
distinct class, which the JSON Lines codec does not recognise.
"""

from __future__ import annotations

import io
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from src.surreal_orm.debug import QueryLogger, _log_query
from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict
from src.surreal_orm.prefetch import Prefetch
from src.surreal_orm.q import Q
from src.surreal_orm.query_set import QuerySet
from src.surreal_orm.search import SearchScore
from src.surreal_orm.signals import post_save
from src.surreal_orm.subquery import Subquery
from src.surreal_orm.types import TableType
from surreal_sdk.exceptions import DataImportError
from surreal_sdk.jsonl import ImportReport, dumps_record
from surreal_sdk.protocol.cbor import RecordId


class DIOUPost(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="diou_post")

    id: str | None = None
    published: bool = False
    n: int = 0


class DIOUScore(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="diou_score")

    id: str | None = None
    v: int = 0


def _rows(*keys: int) -> list[dict[str, Any]]:
    return [{"id": RecordId("diou_post", k), "published": True, "n": k} for k in keys]


def _ok(*results: Any) -> Any:
    from surreal_sdk.types import QueryResponse, QueryResult, ResponseStatus

    return QueryResponse(results=[QueryResult(status=ResponseStatus.OK, result=r) for r in results])


def _client(*responses: Any, protocol: str = "cbor") -> Any:
    client = AsyncMock()
    client.protocol = protocol
    client.query = AsyncMock(side_effect=list(responses))
    return client


def _patch_client(client: Any) -> Any:
    return patch(
        "src.surreal_orm.query_set.SurrealDBConnectionManager.get_client",
        new_callable=AsyncMock,
        return_value=client,
    )


async def _lines(qs: QuerySet[Any], **kwargs: Any) -> list[str]:
    return [line async for line in qs.iter_jsonl(**kwargs)]


def _calls(client: Any) -> list[tuple[str, dict[str, Any]]]:
    return [(call.args[0], call.args[1]) for call in client.query.await_args_list]


# ---------------------------------------------------------------------------
# U11 — statements, variables, limit
# ---------------------------------------------------------------------------


class TestIterJsonlStatements:
    async def test_no_filter_is_c1_then_c2(self) -> None:
        client = _client(_ok(_rows(1, 2)), _ok(_rows(3)))
        with _patch_client(client):
            lines = await _lines(DIOUPost.objects(), batch_size=2)
        assert lines == [dumps_record(r) for r in _rows(1, 2, 3)]
        (sql1, vars1), (sql2, vars2) = _calls(client)
        assert sql1 == "SELECT * FROM diou_post ORDER BY id LIMIT 2;"
        assert vars1 == {}
        assert sql2 == "SELECT * FROM diou_post WHERE id > $_x_after ORDER BY id LIMIT 2;"
        assert vars2 == {"_x_after": RecordId("diou_post", 2)}

    async def test_keyword_filters_and_q_are_c1o_c2o(self) -> None:
        client = _client(_ok(_rows(1, 2)), _ok([]))
        qs = DIOUPost.objects().filter(published=True).filter(Q(n__gt=0) | Q(n__lt=-5))
        with _patch_client(client):
            lines = await _lines(qs, batch_size=2)
        assert len(lines) == 2
        (sql1, vars1), (sql2, vars2) = _calls(client)
        match = re.fullmatch(r"SELECT \* FROM diou_post WHERE published = \$_f0 AND (.+) ORDER BY id LIMIT 2;", sql1)
        assert match is not None, sql1
        q_part = match.group(1)
        assert "n > $_f1" in q_part and "n < $_f2" in q_part and " OR " in q_part
        assert sql2 == f"SELECT * FROM diou_post WHERE published = $_f0 AND {q_part} AND id > $_x_after ORDER BY id LIMIT 2;"
        assert vars1 == {"_f0": True, "_f1": 0, "_f2": -5}
        assert vars2 == {"_f0": True, "_f1": 0, "_f2": -5, "_x_after": RecordId("diou_post", 2)}

    async def test_declared_variables_are_merged(self) -> None:
        client = _client(_ok(_rows(1)))
        qs = DIOUPost.objects().filter(n="$wanted").variables(wanted=1, other="x")
        with _patch_client(client):
            await _lines(qs, batch_size=5)
        ((sql, variables),) = _calls(client)
        assert sql == "SELECT * FROM diou_post WHERE n = $wanted ORDER BY id LIMIT 5;"
        assert variables == {"wanted": 1, "other": "x"}

    async def test_limit_caps_the_total(self) -> None:
        client = _client(_ok(_rows(1, 2)), _ok(_rows(3)))
        with _patch_client(client):
            lines = await _lines(DIOUPost.objects().limit(3), batch_size=2)
        assert len(lines) == 3
        assert [sql for sql, _ in _calls(client)] == [
            "SELECT * FROM diou_post ORDER BY id LIMIT 2;",
            "SELECT * FROM diou_post WHERE id > $_x_after ORDER BY id LIMIT 1;",
        ]

    async def test_limit_zero_sends_nothing(self) -> None:
        client = _client()
        with _patch_client(client):
            assert await _lines(DIOUPost.objects().limit(0)) == []
        client.query.assert_not_awaited()

    async def test_queryset_variables_are_not_mutated(self) -> None:
        client = _client(_ok(_rows(1, 2)), _ok([]))
        qs = DIOUPost.objects().filter(published=True).variables(keep=1)
        before = dict(qs._variables)
        with _patch_client(client):
            await _lines(qs, batch_size=2)
        assert qs._variables == before

    async def test_typed_false_writes_plain_json(self) -> None:
        client = _client(_ok(_rows(1)))
        with _patch_client(client):
            lines = await _lines(DIOUPost.objects(), typed=False)
        assert lines == [dumps_record(_rows(1)[0], typed=False)]


class TestIterJsonlSubquery:
    async def test_subquery_is_hoisted_into_a_let_prelude_on_every_batch(self) -> None:
        inner = DIOUScore.objects().filter(v__gt=10).order_by("-v").limit(4).select("id")
        qs = DIOUPost.objects().filter(id__in=Subquery(inner))
        client = _client(_ok(None, _rows(1, 2)), _ok(None, _rows(3)))
        with _patch_client(client):
            lines = await _lines(qs, batch_size=2)
        assert len(lines) == 3
        calls = _calls(client)
        assert len(calls) == 2
        let = "LET $_sq0 = (SELECT VALUE id FROM diou_score WHERE v > $_f0 ORDER BY v DESC LIMIT 4);"
        assert calls[0][0] == f"{let} SELECT * FROM diou_post WHERE id IN $_sq0 ORDER BY id LIMIT 2;"
        assert calls[1][0] == f"{let} SELECT * FROM diou_post WHERE id IN $_sq0 AND id > $_x_after ORDER BY id LIMIT 2;"
        for sql, variables in calls:
            select = sql.removeprefix(let)
            assert "id IN $_sq0" in select
            assert "(SELECT" not in select  # never the inline sub-SELECT of #147
            assert variables["_f0"] == 10
        assert calls[1][1]["_x_after"] == RecordId("diou_post", 2)


# ---------------------------------------------------------------------------
# U11 — refusals
# ---------------------------------------------------------------------------

REFUSED: list[tuple[str, Callable[[QuerySet[Any]], QuerySet[Any]]]] = [
    ("order_by", lambda qs: qs.order_by("n")),
    ("offset", lambda qs: qs.offset(1)),
    ("select", lambda qs: qs.select("n")),
    ("values", lambda qs: qs.values("n")),
    ("annotate", lambda qs: qs.annotate(score=SearchScore(0))),
    ("fetch", lambda qs: qs.fetch("n")),
    ("select_related", lambda qs: qs.select_related("n")),
    ("prefetch_related", lambda qs: qs.prefetch_related(Prefetch("n"))),
    ("cache", lambda qs: qs.cache(ttl=10)),
    ("similar_to", lambda qs: qs.similar_to("emb", [0.1, 0.2], limit=3)),
    ("search", lambda qs: qs.search(title="x")),
    ("nearby", lambda qs: qs.nearby("loc", (1.0, 2.0), max_distance=10)),
    ("traverse", lambda qs: qs.traverse("->x->y")),
    ("reachable_from", lambda qs: qs.reachable_from(DIOUPost(id="a"), "diou_edge", max_depth=2)),
]


class TestIterJsonlRefusals:
    @pytest.mark.parametrize(("clause", "build"), REFUSED, ids=[c for c, _ in REFUSED])
    @pytest.mark.parametrize("method", ["iter_jsonl", "export_jsonl"])
    async def test_e9_names_the_refused_clause_before_any_io(
        self, method: str, clause: str, build: Callable[[QuerySet[Any]], QuerySet[Any]], tmp_path: Path
    ) -> None:
        qs = build(DIOUPost.objects().filter(published=True).limit(5))
        expected = f"{method}() reads the table in id order and cannot be combined with {clause}()"
        path = tmp_path / "out.jsonl"
        with _patch_client(_client()) as get_client:
            with pytest.raises(ValueError) as excinfo:
                if method == "iter_jsonl":
                    qs.iter_jsonl()
                else:
                    await qs.export_jsonl(path)
            get_client.assert_not_awaited()
        assert str(excinfo.value) == expected
        assert not path.exists()

    def test_e9_names_the_first_clause_in_spec_order(self) -> None:
        qs = DIOUPost.objects().traverse("->x->y").cache(ttl=1).order_by("n")
        with pytest.raises(ValueError, match=r"cannot be combined with order_by\(\)$"):
            qs.iter_jsonl()

    @pytest.mark.parametrize("method", ["iter_jsonl", "export_jsonl"])
    async def test_e14_reserved_variable_name(self, method: str) -> None:
        qs = DIOUPost.objects().variables(_x_after=1)
        with _patch_client(_client()) as get_client:
            with pytest.raises(ValueError) as excinfo:
                if method == "iter_jsonl":
                    qs.iter_jsonl()
                else:
                    await qs.export_jsonl(io.StringIO())
            get_client.assert_not_awaited()
        assert str(excinfo.value) == "Variable names starting with '_x_' are reserved for data export/import: '_x_after'"

    @pytest.mark.parametrize("value", [0, -1])
    def test_e1_batch_size_minimum(self, value: int) -> None:
        with pytest.raises(ValueError, match=rf"^batch_size must be >= 1, got {value}$"):
            DIOUPost.objects().iter_jsonl(batch_size=value)

    def test_e1_batch_size_type(self) -> None:
        with pytest.raises(TypeError, match=r"^batch_size must be an int, got bool$"):
            DIOUPost.objects().iter_jsonl(batch_size=True)

    @pytest.mark.parametrize("method", ["iter_jsonl", "export_jsonl"])
    async def test_e4_json_protocol_client(self, method: str) -> None:
        client = _client(_ok(_rows(1)), protocol="json")
        expected = (
            f"{method}() needs the CBOR protocol: under JSON, record ids, datetimes and decimals "
            "arrive as strings and would not round-trip"
        )
        with _patch_client(client), pytest.raises(ValueError) as excinfo:
            if method == "iter_jsonl":
                await _lines(DIOUPost.objects())
            else:
                await DIOUPost.objects().export_jsonl(io.StringIO())
        assert str(excinfo.value) == expected
        client.query.assert_not_awaited()


# ---------------------------------------------------------------------------
# U11 — logging, export
# ---------------------------------------------------------------------------


class TestJsonlLoggingAndExport:
    async def test_one_query_log_per_batch(self) -> None:
        client = _client(_ok(_rows(1, 2)), _ok(_rows(3)))
        async with QueryLogger() as log:
            with _patch_client(client):
                await _lines(DIOUPost.objects().filter(published=True), batch_size=2)
        assert [q.sql for q in log.queries] == [sql for sql, _ in _calls(client)]
        assert log.queries[0].variables == {"_f0": True}
        assert log.queries[1].variables == {"_f0": True, "_x_after": RecordId("diou_post", 2)}

    async def test_export_to_a_stream_returns_the_count(self) -> None:
        client = _client(_ok(_rows(1, 2)), _ok(_rows(3)))
        sink = io.StringIO()
        with _patch_client(client):
            count = await DIOUPost.objects().export_jsonl(sink, batch_size=2)
        assert count == 3
        assert sink.getvalue() == "".join(dumps_record(r) + "\n" for r in _rows(1, 2, 3))
        assert not sink.closed

    async def test_export_to_a_path(self, tmp_path: Path) -> None:
        client = _client(_ok(_rows(1)))
        path = tmp_path / "posts.jsonl"
        with _patch_client(client):
            count = await DIOUPost.objects().export_jsonl(path)
        assert count == 1
        assert path.read_text(encoding="utf-8") == dumps_record(_rows(1)[0]) + "\n"

    async def test_connection_is_the_models(self) -> None:
        client = _client(_ok([]))
        with _patch_client(client) as get_client:
            await _lines(DIOUPost.objects())
        get_client.assert_awaited_once_with(DIOUPost.get_connection_name())


# ---------------------------------------------------------------------------
# U12 — QuerySet.import_jsonl()
# ---------------------------------------------------------------------------


class DIOUEdge(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="diou_edge", table_type=TableType.RELATION)

    id: str | None = None


def _import_client(*, report: ImportReport | None = None, error: BaseException | None = None) -> Any:
    """A CBOR client whose ``import_jsonl()`` logs two batches through ``_on_query``, then returns or raises."""
    client = _client()

    async def fake_import(table: str, source: Any, **kwargs: Any) -> ImportReport:
        on_query = kwargs["_on_query"]
        on_query("INSERT INTO diou_post $_x_rows;", {"_x_rows": {"rows": 2, "bytes": 40}}, 1.0)
        on_query("INSERT INTO diou_post $_x_rows;", {"_x_rows": {"rows": 1, "bytes": 20}}, 1.0)
        if error is not None:
            raise error
        assert report is not None
        return report

    client.import_jsonl = AsyncMock(side_effect=fake_import)
    return client


E10 = "import_jsonl() writes the model's table; call it on Model.objects() without filter(), order_by() or other read clauses"

IMPORT_REFUSED: list[tuple[str, Callable[[QuerySet[Any]], QuerySet[Any]]]] = [
    ("filter", lambda qs: qs.filter(published=True)),
    ("q", lambda qs: qs.filter(Q(n=1) | Q(n=2))),
    ("order_by", lambda qs: qs.order_by("n")),
    ("limit", lambda qs: qs.limit(3)),
    ("traverse", lambda qs: qs.traverse("->x->y")),
    ("cache", lambda qs: qs.cache(ttl=10)),
]


class TestImportJsonl:
    @pytest.mark.parametrize(("clause", "build"), IMPORT_REFUSED, ids=[c for c, _ in IMPORT_REFUSED])
    async def test_e10_refuses_a_read_clause_before_any_io(
        self, clause: str, build: Callable[[QuerySet[Any]], QuerySet[Any]]
    ) -> None:
        client = _import_client(report=ImportReport(table="diou_post", lines=0, rows=0, batches=0))
        with _patch_client(client) as get_client, patch("src.surreal_orm.cache.QueryCache.invalidate") as invalidate:
            with pytest.raises(ValueError) as excinfo:
                await build(DIOUPost.objects()).import_jsonl(["{}"])
            get_client.assert_not_awaited()
        assert str(excinfo.value) == E10
        client.import_jsonl.assert_not_awaited()
        invalidate.assert_not_called()

    async def test_writes_the_models_table_with_auto_relation(self) -> None:
        report = ImportReport(table="diou_post", lines=3, rows=3, batches=2)
        client = _import_client(report=report)
        source = ['{"id": 1}', '{"id": 2}', '{"id": 3}']
        with _patch_client(client) as get_client:
            result = await DIOUPost.objects().import_jsonl(
                source, batch_size=2, max_batch_bytes=100, on_conflict="ignore", raw=True
            )
        assert result is report
        get_client.assert_awaited_once_with(DIOUPost.get_connection_name())
        call = client.import_jsonl.await_args
        assert call.args == ("diou_post", source)
        kwargs = dict(call.kwargs)
        assert kwargs.pop("_on_query") is _log_query
        assert kwargs == {
            "batch_size": 2,
            "max_batch_bytes": 100,
            "on_conflict": "ignore",
            "raw": True,
            "relation": None,
        }

    async def test_relation_model_forces_edges(self) -> None:
        client = _import_client(report=ImportReport(table="diou_edge", lines=0, rows=0, batches=0))
        with _patch_client(client):
            await DIOUEdge.objects().import_jsonl([])
        call = client.import_jsonl.await_args
        assert call.args[0] == "diou_edge"
        assert call.kwargs["relation"] is True

    async def test_defaults_are_the_sdks(self) -> None:
        client = _import_client(report=ImportReport(table="diou_post", lines=0, rows=0, batches=0))
        with _patch_client(client):
            await DIOUPost.objects().import_jsonl([])
        kwargs = client.import_jsonl.await_args.kwargs
        assert (kwargs["batch_size"], kwargs["max_batch_bytes"]) == (1000, 1_048_576)
        assert (kwargs["on_conflict"], kwargs["raw"]) == ("error", False)

    async def test_cache_is_invalidated_on_success(self) -> None:
        client = _import_client(report=ImportReport(table="diou_post", lines=0, rows=0, batches=0))
        with _patch_client(client), patch("src.surreal_orm.cache.QueryCache.invalidate") as invalidate:
            await DIOUPost.objects().import_jsonl([])
        invalidate.assert_called_once_with(DIOUPost)

    async def test_cache_is_invalidated_when_a_batch_is_rejected(self) -> None:
        report = ImportReport(table="diou_post", lines=2, rows=2, batches=1)
        error = DataImportError("Batch at lines 3-3 was rejected by diou_post: boom", errors=["boom"], report=report)
        client = _import_client(error=error)
        with _patch_client(client), patch("src.surreal_orm.cache.QueryCache.invalidate") as invalidate:
            with pytest.raises(DataImportError) as excinfo:
                await DIOUPost.objects().import_jsonl(['{"id": 1}'])
        assert excinfo.value is error
        assert excinfo.value.report == report
        invalidate.assert_called_once_with(DIOUPost)

    async def test_no_signal_is_sent(self) -> None:
        spy = AsyncMock()
        post_save.connect(DIOUPost)(spy)
        try:
            client = _import_client(report=ImportReport(table="diou_post", lines=3, rows=3, batches=2))
            with _patch_client(client):
                await DIOUPost.objects().import_jsonl(['{"id": 1}', '{"id": 2}', '{"id": 3}'])
        finally:
            post_save.disconnect(spy, sender=DIOUPost)
        spy.assert_not_called()
        spy.assert_not_awaited()

    async def test_batches_are_logged(self) -> None:
        client = _import_client(report=ImportReport(table="diou_post", lines=3, rows=3, batches=2))
        async with QueryLogger() as log:
            with _patch_client(client):
                await DIOUPost.objects().import_jsonl([])
        assert [q.sql for q in log.queries] == ["INSERT INTO diou_post $_x_rows;"] * 2
        assert [q.variables for q in log.queries] == [
            {"_x_rows": {"rows": 2, "bytes": 40}},
            {"_x_rows": {"rows": 1, "bytes": 20}},
        ]

    async def test_graph_guard_message_is_unchanged(self) -> None:
        with pytest.raises(ValueError, match=r"^paths_from\(\) runs on a bare QuerySet"):
            DIOUPost.objects().filter(n=1)._reject_read_clauses("paths_from")


class TestDataIoExports:
    """§3.7: the ORM re-exports the SDK's data I/O names (same objects, like ``LiveAction``)."""

    def test_orm_reexports_the_sdk_objects(self) -> None:
        import surreal_orm
        import surreal_sdk
        from src import surreal_orm as src_orm

        for name in ("ImportReport", "DataFormatError", "DataImportError"):
            assert name in surreal_orm.__all__
            assert getattr(surreal_orm, name) is getattr(surreal_sdk, name)
            assert getattr(src_orm, name) is getattr(surreal_sdk, name)

    def test_sdk_exports_the_codec_module(self) -> None:
        import surreal_sdk
        from surreal_sdk import jsonl

        assert surreal_sdk.jsonl is jsonl
        for name in ("ImportReport", "DataFormatError", "DataImportError", "jsonl"):
            assert name in surreal_sdk.__all__
        for name in ("JSONL_TAGS", "encode_value", "decode_value", "dumps_record", "loads_record"):
            assert callable(getattr(jsonl, name)) or name == "JSONL_TAGS"
        assert surreal_sdk.DataImportError is DataImportError
        assert surreal_sdk.ImportReport is ImportReport
