"""File pointers (``FileRef``), file metadata (``FileInfo``) and their wire forms.

SurrealDB sends and accepts a file pointer as CBOR tag 55 ``[bucket, key]``;
before ``FileRef`` existed the decoder returned the bare tuple
``('bucket', '/key')``. The server prefixes ``/`` to a key that lacks one, so
the client normalises the same way and two spellings of one key compare
equal. ``..`` and ``.`` segments are refused client-side because the memory
backend stores them as plain keys while the ``file:`` backend refuses them;
the decoder bypasses that check so a key written by other means still reads.
JSON cannot carry a pointer at all, so the JSON encoder refuses it.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Self

import cbor2
import pytest
from cbor2 import CBORTag

import surreal_sdk
from surreal_sdk import FileInfo, FileRef, FilesNotEnabledError, HTTPConnection, WebSocketConnection
from surreal_sdk.connection.base import BaseSurrealConnection
from surreal_sdk.exceptions import QueryError, TableNotFoundError
from surreal_sdk.protocol.cbor import TAG_FILE, decode, encode
from surreal_sdk.protocol.rpc import RPCError, RPCRequest, RPCResponse, SurrealJSONEncoder

E2 = "Invalid bucket name {bucket!r}: expected a non-empty string without ':', '/', whitespace or control characters"
E4 = "Invalid file key {key!r}: it must name a file below the bucket root (no empty path, no '.' or '..' segment, no NUL)"
E5 = "Invalid file pointer {text!r}: expected 'bucket:/key'"
E10 = "FileRef cannot be sent with protocol='json'; use protocol='cbor'"


def _raw(value: object) -> object:
    """Decode without the SDK tag hook, to see the tags actually emitted.

    cbor2 6.x hands a tag's array payload over as a tuple.
    """
    return cbor2.loads(encode(value))


# --------------------------------------------------------------------------- U1


class TestFileRefNormalisation:
    @pytest.mark.parametrize(
        ("key", "expected"),
        [("bin.dat", "/bin.dat"), ("/bin.dat", "/bin.dat"), ("a//b", "/a//b"), ("/a/b/c.txt", "/a/b/c.txt")],
    )
    def test_leading_slash_added_like_the_server(self, key: str, expected: str) -> None:
        assert FileRef("av", key).key == expected

    def test_equal_and_same_hash_after_normalisation(self) -> None:
        a, b = FileRef("av", "x.png"), FileRef("av", "/x.png")
        assert a == b
        assert hash(a) == hash(b)
        assert len({a, b}) == 1

    def test_different_bucket_is_different(self) -> None:
        assert FileRef("av", "/x") != FileRef("docs", "/x")

    def test_frozen(self) -> None:
        ref = FileRef("av", "/x")
        with pytest.raises(AttributeError):
            ref.key = "/y"  # type: ignore[misc]

    def test_str_is_bucket_colon_key(self) -> None:
        assert str(FileRef("av", "x.png")) == "av:/x.png"

    @pytest.mark.parametrize("ref", [FileRef("av", "/x.png"), FileRef("my-b", "/a//b"), FileRef("b", "/d/é ü.txt")])
    def test_parse_round_trip(self, ref: FileRef) -> None:
        assert FileRef.parse(str(ref)) == ref

    @pytest.mark.parametrize("text", ['f"av:/x.png"', "f'av:/x.png'", "av:/x.png"])
    def test_parse_accepts_plain_and_literal_forms(self, text: str) -> None:
        assert FileRef.parse(text) == FileRef("av", "/x.png")

    def test_parse_splits_at_the_first_colon(self) -> None:
        assert FileRef.parse("av:/a:b") == FileRef("av", "/a:b")

    def test_with_key_keeps_the_bucket(self) -> None:
        ref = FileRef("av", "/x.png").with_key("thumb/x.png")
        assert ref == FileRef("av", "/thumb/x.png")

    def test_with_key_validates(self) -> None:
        with pytest.raises(ValueError):
            FileRef("av", "/x").with_key("../y")


# --------------------------------------------------------------------------- U2


class TestFileRefValidation:
    @pytest.mark.parametrize("bucket", ["", "a:b", "a/b", "a b", "a\tb", "a\nb", "a\x00b", "a\x7fb", "a\u200bb"])
    def test_invalid_bucket(self, bucket: str) -> None:
        with pytest.raises(ValueError) as info:
            FileRef(bucket, "/x")
        assert str(info.value) == E2.format(bucket=bucket)

    def test_non_str_bucket_is_type_error(self) -> None:
        with pytest.raises(TypeError) as info:
            FileRef(5, "/x")  # type: ignore[arg-type]
        assert str(info.value) == E2.format(bucket=5)

    @pytest.mark.parametrize("bucket", ["av", "my-b", "Bucket_1", "é"])
    def test_valid_bucket(self, bucket: str) -> None:
        assert FileRef(bucket, "/x").bucket == bucket

    @pytest.mark.parametrize("key", [b"/x", 5, None])
    def test_non_str_key(self, key: object) -> None:
        with pytest.raises(TypeError) as info:
            FileRef("av", key)  # type: ignore[arg-type]
        assert str(info.value) == f"File key must be a str, got {type(key).__name__}"

    @pytest.mark.parametrize("key", ["", "/", "/a/../b", "/./a", "..", "/..", "a/..", "/a/.", "/a\x00b", "../../etc/passwd"])
    def test_invalid_key(self, key: str) -> None:
        with pytest.raises(ValueError) as info:
            FileRef("av", key)
        assert str(info.value) == E4.format(key=key)

    @pytest.mark.parametrize("key", ["/...", "/.hidden", "/a..b", "/a/.b/c"])
    def test_dots_inside_a_name_are_fine(self, key: str) -> None:
        assert FileRef("av", key).key == key

    @pytest.mark.parametrize(
        "text",
        ["", "av", "av:x.png", ":/x", "av:/", "av:/../x", "a b:/x", 'f"av:/x', "f'av:/x\"", 'f""', "users:abc"],
    )
    def test_parse_invalid(self, text: str) -> None:
        with pytest.raises(ValueError) as info:
            FileRef.parse(text)
        assert str(info.value) == E5.format(text=text)

    def test_parse_non_str(self) -> None:
        with pytest.raises(ValueError) as info:
            FileRef.parse(5)  # type: ignore[arg-type]
        assert str(info.value) == E5.format(text=5)


# --------------------------------------------------------------------------- U3


class TestFromWire:
    def test_accepts_dot_dot_without_raising(self) -> None:
        ref = FileRef._from_wire("av", "/../../etc/passwd")
        assert ref.bucket == "av"
        assert ref.key == "/../../etc/passwd"

    def test_no_normalisation_and_equal_to_validated(self) -> None:
        assert FileRef._from_wire("av", "/x") == FileRef("av", "x")

    def test_decoder_reads_an_invalid_key(self) -> None:
        payload = cbor2.dumps(CBORTag(55, ["av", "/../x"]))
        assert decode(payload) == FileRef._from_wire("av", "/../x")


# --------------------------------------------------------------------------- U4


class TestFileRefCbor:
    def test_tag_number(self) -> None:
        assert TAG_FILE == 55

    def test_encode_is_tag_55_bucket_key(self) -> None:
        assert _raw(FileRef("av", "bin.dat")) == CBORTag(55, ("av", "/bin.dat"))

    def test_encode_bytes(self) -> None:
        assert encode(FileRef("av", "/bin.dat")) == cbor2.dumps(CBORTag(55, ["av", "/bin.dat"]))

    def test_decode_tag_55(self) -> None:
        assert decode(cbor2.dumps(CBORTag(55, ["av", "/bin.dat"]))) == FileRef("av", "/bin.dat")

    def test_round_trip(self) -> None:
        ref = FileRef("my-b", "/a//b")
        decoded = decode(encode(ref))
        assert isinstance(decoded, FileRef)
        assert decoded == ref

    @pytest.mark.parametrize("payload", ["x", [1, 2], ["a"], ["a", "/b", "c"], [], None, ["a", 2]])
    def test_malformed_payload_decodes_to_raw_value(self, payload: object) -> None:
        decoded = decode(cbor2.dumps(CBORTag(55, payload)))
        expected = cbor2.loads(cbor2.dumps(CBORTag(55, payload))).value
        assert decoded == expected
        assert not isinstance(decoded, FileRef)

    def test_nested_in_dict_and_list(self) -> None:
        ref = FileRef("av", "/x.png")
        data = {"att": ref, "more": [ref, {"deep": ref}]}
        raw = _raw(data)
        tag = CBORTag(55, ("av", "/x.png"))
        assert raw == {"att": tag, "more": [tag, {"deep": tag}]}
        assert decode(encode(data)) == data

    def test_rpc_request_carries_tag_55(self) -> None:
        ref = FileRef("av", "/x.png")
        request = RPCRequest(method="query", params=["RETURN $_file", {"_file": ref}], id=1)
        raw = cbor2.loads(request.to_cbor())
        assert raw["params"][1]["_file"] == CBORTag(55, ("av", "/x.png"))

    def test_decoded_response_holds_fileref(self) -> None:
        payload = cbor2.dumps({"result": [{"att": CBORTag(55, ["av", "/x.png"])}]})
        assert decode(payload) == {"result": [{"att": FileRef("av", "/x.png")}]}


# --------------------------------------------------------------------------- U5


class TestFileRefJson:
    def test_encoder_refuses(self) -> None:
        with pytest.raises(TypeError) as info:
            json.dumps({"f": FileRef("av", "/x")}, cls=SurrealJSONEncoder)
        assert str(info.value) == E10

    def test_rpc_request_to_json_refuses(self) -> None:
        request = RPCRequest(method="query", params=["RETURN $_file", {"_file": FileRef("av", "/x")}], id=1)
        with pytest.raises(TypeError) as info:
            request.to_json()
        assert str(info.value) == E10


# --------------------------------------------------------------------------- U6


UPDATED = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


class TestFileInfo:
    def test_from_result_with_fileref(self) -> None:
        ref = FileRef("av", "/x.png")
        info = FileInfo.from_result({"file": ref, "size": 12, "updated": UPDATED})
        assert info == FileInfo(file=ref, size=12, updated=UPDATED)
        assert info.bucket == "av"
        assert info.key == "/x.png"

    @pytest.mark.parametrize("pair", [("av", "/x.png"), ["av", "/x.png"]])
    def test_from_result_with_pair(self, pair: object) -> None:
        info = FileInfo.from_result({"file": pair, "size": 1, "updated": UPDATED})
        assert info.file == FileRef("av", "/x.png")

    def test_from_result_pair_keeps_a_key_written_by_other_means(self) -> None:
        info = FileInfo.from_result({"file": ("av", "/../x"), "size": 1, "updated": UPDATED})
        assert info.key == "/../x"

    @pytest.mark.parametrize("text", ['f"av:/x.png"', "av:/x.png"])
    def test_from_result_with_text(self, text: str) -> None:
        info = FileInfo.from_result({"file": text, "size": 1, "updated": UPDATED})
        assert info.file == FileRef("av", "/x.png")

    def test_from_result_ignores_extra_keys(self) -> None:
        info = FileInfo.from_result({"file": FileRef("av", "/x"), "size": 0, "updated": UPDATED, "other": 1})
        assert info.size == 0

    @pytest.mark.parametrize(
        "value",
        [
            {"size": 1, "updated": UPDATED},
            {"file": FileRef("av", "/x"), "updated": UPDATED},
            {"file": FileRef("av", "/x"), "size": 1},
            {"file": FileRef("av", "/x"), "size": "1", "updated": UPDATED},
            {"file": FileRef("av", "/x"), "size": True, "updated": UPDATED},
            {"file": FileRef("av", "/x"), "size": 1, "updated": "2026-11-04T12:00:00Z"},
            {"file": 5, "size": 1, "updated": UPDATED},
            {"file": ("av", 5), "size": 1, "updated": UPDATED},
            {"file": ("av",), "size": 1, "updated": UPDATED},
            {"file": "not a pointer", "size": 1, "updated": UPDATED},
            "not a mapping",
            None,
        ],
    )
    def test_from_result_invalid(self, value: object) -> None:
        with pytest.raises(ValueError):
            FileInfo.from_result(value)  # type: ignore[arg-type]

    def test_frozen(self) -> None:
        info = FileInfo(file=FileRef("av", "/x"), size=1, updated=UPDATED)
        with pytest.raises(AttributeError):
            info.size = 2  # type: ignore[misc]


class TestExports:
    def test_exported_from_surreal_sdk(self) -> None:
        assert "FileRef" in surreal_sdk.__all__
        assert "FileInfo" in surreal_sdk.__all__
        assert surreal_sdk.FileRef is FileRef
        assert surreal_sdk.FileInfo is FileInfo

    @pytest.mark.parametrize("obj", [FileRef, FileInfo])
    def test_experimental_label(self, obj: type) -> None:
        doc = obj.__doc__ or ""
        assert "experimental" in doc.lower()
        assert "files" in doc
        assert "--allow-experimental=files" in doc


# --------------------------------------------------------------------------- U7-U9, U12: connection file methods

E1 = "File storage is not enabled on this SurrealDB server: start it with --allow-experimental=files (server said: {text})"
E8 = "File operations need protocol='cbor': JSON cannot carry bytes or file pointers"

# What the server answers when the capability is off (3.2.6 and 3.3.5).
FILES_NOT_ENABLED_TEXTS = [
    "There was a problem running the file::exists() function. Experimental capability `files` is not enabled",
    "There was a problem with the database: Experimental feature files is not enabled",
    "Parse error: Unexpected token `BUCKET`, expected the experimental files feature to be enabled\n --> [1:8]",
]

FILE_METHODS = [
    "file_put",
    "file_get",
    "file_head",
    "file_exists",
    "file_delete",
    "file_copy",
    "file_rename",
    "file_list",
    "iter_files",
]

REF = FileRef("av", "/a.png")


def _ok(result: object) -> RPCResponse:
    """A successful ``query`` RPC reply holding one ``OK`` statement."""
    return RPCResponse(id=1, result=[{"status": "OK", "result": result, "time": "1ms"}])


def _err(text: str) -> RPCResponse:
    """A successful ``query`` RPC reply holding one ``ERR`` statement."""
    return RPCResponse(id=1, result=[{"status": "ERR", "result": text, "time": "1ms"}])


def _info(key: str, size: int = 3) -> dict[str, object]:
    return {"file": FileRef("av", key), "size": size, "updated": UPDATED}


class FakeConnection(BaseSurrealConnection):
    """A connection whose ``_send_rpc`` records each request and returns queued replies."""

    def __init__(self, *replies: RPCResponse, protocol: str = "cbor") -> None:
        super().__init__("http://fake", "ns", "db")
        self.protocol = protocol  # type: ignore[assignment]
        self.replies = list(replies)
        self.requests: list[RPCRequest] = []

    async def connect(self) -> Self:
        return self

    async def close(self) -> None:
        return None

    def transaction(self) -> Any:
        raise NotImplementedError

    async def _send_rpc(self, request: RPCRequest) -> RPCResponse:
        self.requests.append(request)
        return self.replies.pop(0) if self.replies else _ok(None)

    @property
    def sent(self) -> list[tuple[str, dict[str, Any]]]:
        """The ``(sql, vars)`` of every ``query`` RPC sent."""
        out = []
        for request in self.requests:
            assert request.method == "query"
            sql, variables = request.params
            out.append((sql, variables))
        return out


class TestFileMethodStatements:
    """U7: each method sends exactly one bound statement (C1-C9)."""

    async def test_put_overwrite(self) -> None:
        conn = FakeConnection(_ok(None))
        assert await conn.file_put(REF, b"\x00\xff") is None
        assert conn.sent == [("file::put($_file, $_data);", {"_file": REF, "_data": b"\x00\xff"})]

    async def test_put_if_not_exists(self) -> None:
        conn = FakeConnection(_ok(None))
        await conn.file_put(REF, b"x", overwrite=False)
        assert conn.sent == [("file::put_if_not_exists($_file, $_data);", {"_file": REF, "_data": b"x"})]

    @pytest.mark.parametrize("data", [bytearray(b"abc"), memoryview(b"abc")])
    async def test_put_binds_plain_bytes(self, data: bytearray | memoryview) -> None:
        conn = FakeConnection(_ok(None))
        await conn.file_put(REF, data)
        bound = conn.sent[0][1]["_data"]
        assert type(bound) is bytes
        assert bound == b"abc"

    async def test_get(self) -> None:
        conn = FakeConnection(_ok(b"\x01\x02"))
        assert await conn.file_get(REF) == b"\x01\x02"
        assert conn.sent == [("RETURN file::get($_file);", {"_file": REF})]

    async def test_get_missing_is_none(self) -> None:
        assert await FakeConnection(_ok(None)).file_get(REF) is None

    async def test_head(self) -> None:
        conn = FakeConnection(_ok(_info("/a.png", 7)))
        info = await conn.file_head(REF)
        assert info == FileInfo(file=REF, size=7, updated=UPDATED)
        assert conn.sent == [("RETURN file::head($_file);", {"_file": REF})]

    async def test_head_missing_is_none(self) -> None:
        assert await FakeConnection(_ok(None)).file_head(REF) is None

    @pytest.mark.parametrize("exists", [True, False])
    async def test_exists(self, exists: bool) -> None:
        conn = FakeConnection(_ok(exists))
        assert await conn.file_exists(REF) is exists
        assert conn.sent == [("RETURN file::exists($_file);", {"_file": REF})]

    async def test_delete(self) -> None:
        conn = FakeConnection(_ok(None))
        assert await conn.file_delete(REF) is None
        assert conn.sent == [("file::delete($_file);", {"_file": REF})]

    @pytest.mark.parametrize(
        ("method", "overwrite", "function"),
        [
            ("file_copy", True, "file::copy"),
            ("file_copy", False, "file::copy_if_not_exists"),
            ("file_rename", True, "file::rename"),
            ("file_rename", False, "file::rename_if_not_exists"),
        ],
    )
    async def test_copy_and_rename(self, method: str, overwrite: bool, function: str) -> None:
        conn = FakeConnection(_ok(None))
        target = await getattr(conn, method)(REF, "b.png", overwrite=overwrite)
        assert target == REF.with_key("b.png")
        assert target == FileRef("av", "/b.png")
        assert conn.sent == [(f"{function}($_file, $_key);", {"_file": REF, "_key": "/b.png"})]

    @pytest.mark.parametrize("method", ["file_copy", "file_rename"])
    @pytest.mark.parametrize("key", ["/", "/a/../b", 5])
    async def test_copy_and_rename_validate_the_key(self, method: str, key: object) -> None:
        conn = FakeConnection()
        with pytest.raises((TypeError, ValueError)):
            await getattr(conn, method)(REF, key)
        assert conn.requests == []

    @pytest.mark.parametrize(
        ("kwargs", "opts"),
        [
            ({}, {}),
            ({"prefix": "/p/"}, {"prefix": "/p/"}),
            ({"start": "/p/3"}, {"start": "/p/3"}),
            ({"limit": 2}, {"limit": 2}),
            ({"prefix": "p", "start": "/p/1", "limit": 5}, {"prefix": "p", "start": "/p/1", "limit": 5}),
        ],
    )
    async def test_list(self, kwargs: dict[str, Any], opts: dict[str, Any]) -> None:
        conn = FakeConnection(_ok([_info("/p/1"), _info("/p/2", 4)]))
        infos = await conn.file_list("av", **kwargs)
        assert infos == [
            FileInfo(file=FileRef("av", "/p/1"), size=3, updated=UPDATED),
            FileInfo(file=FileRef("av", "/p/2"), size=4, updated=UPDATED),
        ]
        assert conn.sent == [("RETURN file::list($_bucket, $_opts);", {"_bucket": "av", "_opts": opts})]

    async def test_list_empty(self) -> None:
        assert await FakeConnection(_ok([])).file_list("av") == []

    @pytest.mark.parametrize("name", FILE_METHODS)
    def test_methods_on_every_connection(self, name: str) -> None:
        assert getattr(HTTPConnection, name) is getattr(BaseSurrealConnection, name)
        assert getattr(WebSocketConnection, name) is getattr(BaseSurrealConnection, name)


class TestFileStatementErrors:
    """U8: a rejected file statement raises; the disabled capability is ``FilesNotEnabledError``."""

    @pytest.mark.parametrize(
        ("call", "sql"),
        [
            (lambda c: c.file_put(REF, b"x"), "file::put($_file, $_data);"),
            (lambda c: c.file_get(REF), "RETURN file::get($_file);"),
            (lambda c: c.file_head(REF), "RETURN file::head($_file);"),
            (lambda c: c.file_exists(REF), "RETURN file::exists($_file);"),
            (lambda c: c.file_delete(REF), "file::delete($_file);"),
            (lambda c: c.file_copy(REF, "/b"), "file::copy($_file, $_key);"),
            (lambda c: c.file_rename(REF, "/b"), "file::rename($_file, $_key);"),
            (lambda c: c.file_list("av"), "RETURN file::list($_bucket, $_opts);"),
        ],
    )
    async def test_statement_error_raises_query_error(self, call: Callable[[Any], Any], sql: str) -> None:
        text = "The bucket 'nope' does not exist"
        conn = FakeConnection(_err(text))
        with pytest.raises(QueryError) as excinfo:
            await call(conn)
        assert type(excinfo.value) is QueryError
        assert excinfo.value.message == text
        assert excinfo.value.query == sql

    @pytest.mark.parametrize("text", FILES_NOT_ENABLED_TEXTS)
    async def test_statement_error_files_not_enabled(self, text: str) -> None:
        conn = FakeConnection(_err(text))
        with pytest.raises(FilesNotEnabledError) as excinfo:
            await conn.file_exists(REF)
        assert isinstance(excinfo.value, QueryError)
        assert str(excinfo.value) == E1.format(text=text)
        assert excinfo.value.query == "RETURN file::exists($_file);"

    @pytest.mark.parametrize("text", FILES_NOT_ENABLED_TEXTS)
    def test_is_files_not_enabled(self, text: str) -> None:
        assert FilesNotEnabledError.is_files_not_enabled(text)

    @pytest.mark.parametrize("text", ["The bucket 'nope' does not exist", "experimental capability `files` is not enabled", ""])
    def test_is_files_not_enabled_other_texts(self, text: str) -> None:
        assert not FilesNotEnabledError.is_files_not_enabled(text)

    @pytest.mark.parametrize("factory", [HTTPConnection, WebSocketConnection])
    async def test_rpc_error_files_not_enabled(self, factory: type[BaseSurrealConnection]) -> None:
        """A DEFINE BUCKET on a flagless server is an RPC error (code -32000) on both transports."""
        text = FILES_NOT_ENABLED_TEXTS[2]
        conn = factory("http://localhost:1", "ns", "db")  # type: ignore[call-arg]

        async def send_rpc(request: RPCRequest) -> RPCResponse:
            return RPCResponse(id=1, error=RPCError(code=-32000, message=text))

        conn._send_rpc = send_rpc  # type: ignore[method-assign]
        with pytest.raises(FilesNotEnabledError) as excinfo:
            await conn.query("DEFINE BUCKET b BACKEND 'memory';")
        assert isinstance(excinfo.value, QueryError)
        assert excinfo.value.code == -32000
        assert str(excinfo.value) == E1.format(text=text)

    @pytest.mark.parametrize("factory", [HTTPConnection, WebSocketConnection])
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Parse error: Unexpected token `FOO`", QueryError),
            ("The table 'nope' does not exist", TableNotFoundError),
        ],
    )
    async def test_rpc_error_unrelated(
        self, factory: type[BaseSurrealConnection], text: str, expected: type[QueryError]
    ) -> None:
        conn = factory("http://localhost:1", "ns", "db")  # type: ignore[call-arg]

        async def send_rpc(request: RPCRequest) -> RPCResponse:
            return RPCResponse(id=1, error=RPCError(code=-32000, message=text))

        conn._send_rpc = send_rpc  # type: ignore[method-assign]
        with pytest.raises(QueryError) as excinfo:
            await conn.query("SELECT 1;")
        assert type(excinfo.value) is expected
        assert excinfo.value.message == text
        assert excinfo.value.code == -32000


class TestFileMethodArguments:
    """U9: argument checks at the call, before any request."""

    @pytest.mark.parametrize(
        "call",
        [
            lambda c: c.file_put(REF, b"x"),
            lambda c: c.file_get(REF),
            lambda c: c.file_head(REF),
            lambda c: c.file_exists(REF),
            lambda c: c.file_delete(REF),
            lambda c: c.file_copy(REF, "/b"),
            lambda c: c.file_rename(REF, "/b"),
            lambda c: c.file_list("av"),
        ],
    )
    async def test_json_protocol_refused(self, call: Callable[[Any], Any]) -> None:
        conn = FakeConnection(protocol="json")
        with pytest.raises(ValueError) as excinfo:
            await call(conn)
        assert str(excinfo.value) == E8
        assert conn.requests == []

    def test_iter_files_json_protocol_refused_at_the_call(self) -> None:
        conn = FakeConnection(protocol="json")
        with pytest.raises(ValueError) as excinfo:
            conn.iter_files("av")
        assert str(excinfo.value) == E8

    @pytest.mark.parametrize("data", ["text", 5, None, [1, 2]])
    async def test_put_data_must_be_bytes(self, data: object) -> None:
        conn = FakeConnection()
        with pytest.raises(TypeError) as excinfo:
            await conn.file_put(REF, data)  # type: ignore[arg-type]
        assert str(excinfo.value) == f"File data must be bytes, bytearray or memoryview, got {type(data).__name__}"
        assert conn.requests == []

    @pytest.mark.parametrize(
        "call",
        [
            lambda c, f: c.file_put(f, b"x"),
            lambda c, f: c.file_get(f),
            lambda c, f: c.file_head(f),
            lambda c, f: c.file_exists(f),
            lambda c, f: c.file_delete(f),
            lambda c, f: c.file_copy(f, "/b"),
            lambda c, f: c.file_rename(f, "/b"),
        ],
    )
    async def test_file_must_be_a_file_ref(self, call: Callable[[Any, Any], Any]) -> None:
        conn = FakeConnection()
        with pytest.raises(TypeError) as excinfo:
            await call(conn, "av:/a.png")
        assert str(excinfo.value) == "file must be a FileRef, got str"
        assert conn.requests == []

    @pytest.mark.parametrize(("bucket", "error"), [("", ValueError), ("a:b", ValueError), ("a b", ValueError), (5, TypeError)])
    async def test_list_bucket(self, bucket: object, error: type[Exception]) -> None:
        conn = FakeConnection()
        with pytest.raises(error) as excinfo:
            await conn.file_list(bucket)  # type: ignore[arg-type]
        assert str(excinfo.value) == E2.format(bucket=bucket)
        assert conn.requests == []

    @pytest.mark.parametrize(("bucket", "error"), [("", ValueError), (5, TypeError)])
    def test_iter_files_bucket_at_the_call(self, bucket: object, error: type[Exception]) -> None:
        with pytest.raises(error) as excinfo:
            FakeConnection().iter_files(bucket)  # type: ignore[arg-type]
        assert str(excinfo.value) == E2.format(bucket=bucket)

    @pytest.mark.parametrize("name", ["prefix", "start"])
    @pytest.mark.parametrize("value", [5, b"/p", ["/p"]])
    async def test_list_prefix_and_start_must_be_str(self, name: str, value: object) -> None:
        conn = FakeConnection()
        with pytest.raises(TypeError) as excinfo:
            await conn.file_list("av", **{name: value})
        assert str(excinfo.value) == f"{name} must be a str, got {type(value).__name__}"
        assert conn.requests == []

    @pytest.mark.parametrize("value", [5, b"/p"])
    def test_iter_files_prefix_must_be_str(self, value: object) -> None:
        with pytest.raises(TypeError) as excinfo:
            FakeConnection().iter_files("av", prefix=value)  # type: ignore[arg-type]
        assert str(excinfo.value) == f"prefix must be a str, got {type(value).__name__}"

    @pytest.mark.parametrize(
        ("value", "error", "text"),
        [
            ("5", TypeError, "limit must be an int, got str"),
            (True, TypeError, "limit must be an int, got bool"),
            (2.0, TypeError, "limit must be an int, got float"),
            (0, ValueError, "limit must be >= 1, got 0"),
        ],
    )
    async def test_list_limit(self, value: object, error: type[Exception], text: str) -> None:
        conn = FakeConnection()
        with pytest.raises(error) as excinfo:
            await conn.file_list("av", limit=value)  # type: ignore[arg-type]
        assert str(excinfo.value) == text
        assert conn.requests == []

    @pytest.mark.parametrize(
        ("value", "error", "text"),
        [
            ("5", TypeError, "batch_size must be an int, got str"),
            (False, TypeError, "batch_size must be an int, got bool"),
            (0, ValueError, "batch_size must be >= 1, got 0"),
        ],
    )
    def test_iter_files_batch_size_at_the_call(self, value: object, error: type[Exception], text: str) -> None:
        with pytest.raises(error) as excinfo:
            FakeConnection().iter_files("av", batch_size=value)  # type: ignore[arg-type]
        assert str(excinfo.value) == text


class TestIterFiles:
    """U12: keyset paging, ``start`` chained from the previous batch's last key."""

    async def test_pages_until_a_short_batch(self) -> None:
        conn = FakeConnection(
            _ok([_info("/p/1"), _info("/p/2")]),
            _ok([_info("/p/3"), _info("/p/4")]),
            _ok([_info("/p/5")]),
        )
        keys = [info.key async for info in conn.iter_files("av", prefix="/p/", batch_size=2)]
        assert keys == ["/p/1", "/p/2", "/p/3", "/p/4", "/p/5"]
        assert [variables["_opts"] for _, variables in conn.sent] == [
            {"prefix": "/p/", "limit": 2},
            {"prefix": "/p/", "start": "/p/2", "limit": 2},
            {"prefix": "/p/", "start": "/p/4", "limit": 2},
        ]
        assert {sql for sql, _ in conn.sent} == {"RETURN file::list($_bucket, $_opts);"}

    async def test_full_batch_then_empty_batch(self) -> None:
        conn = FakeConnection(_ok([_info("/a"), _info("/b")]), _ok([]))
        keys = [info.key async for info in conn.iter_files("av", batch_size=2)]
        assert keys == ["/a", "/b"]
        assert [variables["_opts"] for _, variables in conn.sent] == [{"limit": 2}, {"start": "/b", "limit": 2}]

    async def test_empty_bucket(self) -> None:
        conn = FakeConnection(_ok([]))
        assert [info async for info in conn.iter_files("av")] == []
        assert [variables["_opts"] for _, variables in conn.sent] == [{"limit": 1000}]

    async def test_sends_nothing_until_iterated(self) -> None:
        conn = FakeConnection()
        conn.iter_files("av")
        assert conn.requests == []

    async def test_statement_error_raises(self) -> None:
        conn = FakeConnection(_ok([_info("/a")]), _err("boom"))
        stream = conn.iter_files("av", batch_size=1)
        assert (await stream.__anext__()).key == "/a"
        with pytest.raises(QueryError, match="boom"):
            await stream.__anext__()


class TestFileExports:
    def test_files_not_enabled_error_exported(self) -> None:
        assert "FilesNotEnabledError" in surreal_sdk.__all__
        assert surreal_sdk.FilesNotEnabledError is FilesNotEnabledError
        assert issubclass(FilesNotEnabledError, QueryError)

    @pytest.mark.parametrize("name", ["FilesNotEnabledError", *FILE_METHODS])
    def test_experimental_label(self, name: str) -> None:
        obj = FilesNotEnabledError if name == "FilesNotEnabledError" else getattr(BaseSurrealConnection, name)
        doc = obj.__doc__ or ""
        assert "experimental" in doc.lower()
        assert "files" in doc
        assert "--allow-experimental=files" in doc
