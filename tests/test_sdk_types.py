"""Unit tests for surreal_sdk.types — SDK response types."""

import pytest

from surreal_sdk.types import (
    AuthResponse,
    DeleteResponse,
    InfoResponse,
    LiveQueryId,
    QueryResponse,
    QueryResult,
    RecordResponse,
    RecordsResponse,
    ResponseStatus,
)


class TestResponseStatus:
    def test_ok_value(self) -> None:
        assert ResponseStatus.OK.value == "OK"

    def test_err_value(self) -> None:
        assert ResponseStatus.ERR.value == "ERR"

    def test_from_string(self) -> None:
        assert ResponseStatus("OK") == ResponseStatus.OK
        assert ResponseStatus("ERR") == ResponseStatus.ERR


class TestQueryResult:
    def test_from_dict_minimal(self) -> None:
        result = QueryResult.from_dict({})
        assert result.status == ResponseStatus.OK
        assert result.result is None
        assert result.time == ""

    def test_from_dict_full(self) -> None:
        result = QueryResult.from_dict({"status": "OK", "result": [{"id": "1"}], "time": "1ms"})
        assert result.is_ok is True
        assert result.time == "1ms"

    def test_from_dict_error(self) -> None:
        result = QueryResult.from_dict({"status": "ERR", "result": "query failed"})
        assert result.is_error is True
        assert result.is_ok is False

    def test_records_with_list(self) -> None:
        result = QueryResult(ResponseStatus.OK, [{"id": "1"}, {"id": "2"}], "1ms")
        assert len(result.records) == 2

    def test_records_with_non_list(self) -> None:
        result = QueryResult(ResponseStatus.OK, "scalar_value", "1ms")
        assert result.records == []

    def test_first_with_records(self) -> None:
        result = QueryResult(ResponseStatus.OK, [{"id": "1"}, {"id": "2"}], "1ms")
        assert result.first == {"id": "1"}

    def test_first_empty(self) -> None:
        result = QueryResult(ResponseStatus.OK, [], "1ms")
        assert result.first is None

    def test_scalar_with_string(self) -> None:
        result = QueryResult(ResponseStatus.OK, "hello", "1ms")
        assert result.scalar == "hello"

    def test_scalar_with_int(self) -> None:
        result = QueryResult(ResponseStatus.OK, 42, "1ms")
        assert result.scalar == 42

    def test_scalar_with_float(self) -> None:
        result = QueryResult(ResponseStatus.OK, 3.14, "1ms")
        assert result.scalar == 3.14

    def test_scalar_with_bool(self) -> None:
        result = QueryResult(ResponseStatus.OK, True, "1ms")
        assert result.scalar is True

    def test_scalar_with_list(self) -> None:
        result = QueryResult(ResponseStatus.OK, [1, 2, 3], "1ms")
        assert result.scalar is None

    def test_scalar_with_none(self) -> None:
        result = QueryResult(ResponseStatus.OK, None, "1ms")
        assert result.scalar is None


class TestQueryResponse:
    def test_from_rpc_result_empty_list(self) -> None:
        resp = QueryResponse.from_rpc_result([])
        assert resp.is_empty is True
        assert resp.all_records == []
        assert resp.is_ok is True

    def test_from_rpc_result_with_status(self) -> None:
        resp = QueryResponse.from_rpc_result([{"status": "OK", "result": [{"id": "1"}]}])
        assert resp.is_ok is True
        assert len(resp.all_records) == 1

    def test_from_rpc_result_dict_without_status(self) -> None:
        resp = QueryResponse.from_rpc_result([{"id": "1", "name": "Alice"}])
        assert resp.is_ok is True
        assert resp.first_result is not None

    def test_from_rpc_result_non_dict_items(self) -> None:
        resp = QueryResponse.from_rpc_result([42, "hello"])
        assert len(resp.results) == 2

    def test_from_rpc_result_single_dict_with_status(self) -> None:
        resp = QueryResponse.from_rpc_result({"status": "OK", "result": [{"id": "1"}]})
        assert resp.is_ok is True

    def test_from_rpc_result_single_dict_without_status(self) -> None:
        resp = QueryResponse.from_rpc_result({"id": "1", "name": "Alice"})
        assert resp.first_result is not None

    def test_from_rpc_result_none(self) -> None:
        resp = QueryResponse.from_rpc_result(None)
        assert resp.is_empty is True

    def test_first_result_empty(self) -> None:
        resp = QueryResponse(results=[])
        assert resp.first_result is None

    def test_first_record_with_status(self) -> None:
        resp = QueryResponse.from_rpc_result([{"status": "OK", "result": [{"id": "1"}]}])
        assert resp.first is not None
        assert resp.first["id"] == "1"

    def test_first_record_empty(self) -> None:
        resp = QueryResponse.from_rpc_result([])
        assert resp.first is None

    def test_first_direct_dict_is_not_records(self) -> None:
        # A dict result (not wrapped in list) is a single value, not "records"
        resp = QueryResponse.from_rpc_result([{"id": "1", "name": "Alice"}])
        # The result is stored as a dict, not a list, so records is empty
        assert resp.first is None


class TestRecordResponse:
    def test_from_rpc_result_dict(self) -> None:
        resp = RecordResponse.from_rpc_result({"id": "users:1", "name": "Alice"})
        assert resp.exists is True
        assert resp.id == "users:1"

    def test_from_rpc_result_list(self) -> None:
        resp = RecordResponse.from_rpc_result([{"id": "users:1", "name": "Alice"}])
        assert resp.exists is True

    def test_from_rpc_result_empty_list(self) -> None:
        resp = RecordResponse.from_rpc_result([])
        assert resp.exists is False
        assert resp.id is None

    def test_from_rpc_result_none(self) -> None:
        resp = RecordResponse.from_rpc_result(None)
        assert resp.exists is False

    def test_get_existing_field(self) -> None:
        resp = RecordResponse(record={"id": "1", "name": "Alice"})
        assert resp.get("name") == "Alice"

    def test_get_missing_field_default(self) -> None:
        resp = RecordResponse(record={"id": "1"})
        assert resp.get("missing", "default") == "default"

    def test_get_no_record(self) -> None:
        resp = RecordResponse(record=None)
        assert resp.get("name") is None

    def test_from_rpc_result_list_with_non_dict(self) -> None:
        resp = RecordResponse.from_rpc_result(["string_value"])
        assert resp.exists is False


class TestRecordsResponse:
    def test_from_rpc_result_list(self) -> None:
        resp = RecordsResponse.from_rpc_result([{"id": "1"}, {"id": "2"}])
        assert resp.count == 2
        assert not resp.is_empty

    def test_from_rpc_result_dict(self) -> None:
        resp = RecordsResponse.from_rpc_result({"id": "1"})
        assert resp.count == 1

    def test_from_rpc_result_empty(self) -> None:
        resp = RecordsResponse.from_rpc_result([])
        assert resp.is_empty is True
        assert resp.first is None

    def test_from_rpc_result_mixed_list(self) -> None:
        resp = RecordsResponse.from_rpc_result([{"id": "1"}, "not_a_dict", {"id": "2"}])
        assert resp.count == 2

    def test_iter(self) -> None:
        resp = RecordsResponse(records=[{"id": "1"}, {"id": "2"}])
        items = list(resp)
        assert len(items) == 2

    def test_len(self) -> None:
        resp = RecordsResponse(records=[{"id": "1"}, {"id": "2"}])
        assert len(resp) == 2

    def test_first(self) -> None:
        resp = RecordsResponse(records=[{"id": "1"}, {"id": "2"}])
        assert resp.first == {"id": "1"}

    def test_from_rpc_result_none(self) -> None:
        resp = RecordsResponse.from_rpc_result(None)
        assert resp.is_empty is True


class TestAuthResponse:
    def test_from_rpc_result_token(self) -> None:
        resp = AuthResponse.from_rpc_result("jwt_token_here")
        assert resp.success is True
        assert resp.token == "jwt_token_here"

    def test_from_rpc_result_none(self) -> None:
        resp = AuthResponse.from_rpc_result(None)
        assert resp.success is True
        assert resp.token is None

    def test_from_rpc_result_other(self) -> None:
        resp = AuthResponse.from_rpc_result(42)
        assert resp.success is False
        assert resp.token is None


class TestInfoResponse:
    def test_from_rpc_result_dict(self) -> None:
        data = {"tables": {"users": "DEFINE TABLE users"}, "ns": {"test": {}}}
        resp = InfoResponse.from_rpc_result(data)
        assert resp.tables == {"users": "DEFINE TABLE users"}

    def test_from_rpc_result_list(self) -> None:
        resp = InfoResponse.from_rpc_result([{"tables": {"users": "..."}}])
        assert resp.tables == {"users": "..."}

    def test_from_rpc_result_empty(self) -> None:
        resp = InfoResponse.from_rpc_result(None)
        assert resp.tables == {}

    def test_namespaces(self) -> None:
        resp = InfoResponse(data={"ns": {"test": {}}})
        assert resp.namespaces == {"test": {}}

    def test_databases(self) -> None:
        resp = InfoResponse(data={"db": {"mydb": {}}})
        assert resp.databases == {"mydb": {}}

    def test_tables_with_tb_key(self) -> None:
        resp = InfoResponse(data={"tb": {"users": "..."}})
        assert resp.tables == {"users": "..."}

    def test_tables_non_dict_value(self) -> None:
        resp = InfoResponse(data={"tables": "not_a_dict"})
        assert resp.tables == {}

    def test_namespaces_non_dict_value(self) -> None:
        resp = InfoResponse(data={"ns": "not_a_dict"})
        assert resp.namespaces == {}

    def test_databases_non_dict_value(self) -> None:
        resp = InfoResponse(data={"db": "not_a_dict"})
        assert resp.databases == {}

    def test_from_rpc_result_list_with_non_dict(self) -> None:
        resp = InfoResponse.from_rpc_result([42])
        assert resp.data == {}


class TestLiveQueryId:
    def test_from_rpc_result_string(self) -> None:
        qid = LiveQueryId.from_rpc_result("abc-123-uuid")
        assert str(qid) == "abc-123-uuid"

    def test_from_rpc_result_list_string(self) -> None:
        qid = LiveQueryId.from_rpc_result(["abc-123"])
        assert str(qid) == "abc-123"

    def test_from_rpc_result_list_dict_with_result(self) -> None:
        qid = LiveQueryId.from_rpc_result([{"result": "uuid-val"}])
        assert str(qid) == "uuid-val"

    def test_from_rpc_result_invalid(self) -> None:
        with pytest.raises(ValueError, match="Cannot parse"):
            LiveQueryId.from_rpc_result(42)

    def test_from_rpc_result_empty_list(self) -> None:
        with pytest.raises(ValueError, match="Cannot parse"):
            LiveQueryId.from_rpc_result([])

    def test_from_rpc_result_list_with_non_string_non_dict(self) -> None:
        with pytest.raises(ValueError, match="Cannot parse"):
            LiveQueryId.from_rpc_result([42])


class TestDeleteResponse:
    def test_from_rpc_result_list(self) -> None:
        resp = DeleteResponse.from_rpc_result([{"id": "users:1"}])
        assert resp.count == 1
        assert resp.success is True

    def test_from_rpc_result_dict(self) -> None:
        resp = DeleteResponse.from_rpc_result({"id": "users:1"})
        assert resp.count == 1
        assert resp.success is True

    def test_from_rpc_result_empty(self) -> None:
        resp = DeleteResponse.from_rpc_result([])
        assert resp.count == 0
        assert resp.success is False

    def test_from_rpc_result_none(self) -> None:
        resp = DeleteResponse.from_rpc_result(None)
        assert resp.count == 0

    def test_from_rpc_result_mixed_list(self) -> None:
        resp = DeleteResponse.from_rpc_result([{"id": "1"}, "not_dict", {"id": "2"}])
        assert resp.count == 2
