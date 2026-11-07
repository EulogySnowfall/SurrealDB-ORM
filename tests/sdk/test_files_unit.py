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
from datetime import UTC, datetime

import cbor2
import pytest
from cbor2 import CBORTag

import surreal_sdk
from surreal_sdk import FileInfo, FileRef
from surreal_sdk.protocol.cbor import TAG_FILE, decode, encode
from surreal_sdk.protocol.rpc import RPCRequest, SurrealJSONEncoder

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
