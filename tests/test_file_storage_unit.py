"""Unit tests for file storage (0.39.0) that need no server.

SurrealDB 3.2 and 3.3 keep file storage behind ``--allow-experimental=files``:
without it, ``DEFINE BUCKET`` is a parse error and every ``file::*`` call is a
statement error. The file integration tests probe the capability first and
skip on a flagless server, unless ``SORM_REQUIRE_FILES=1`` asks them to fail
instead. CI, the compose file and the SurrealDB monitors start the server with
the flag and set that variable, so a skip there is impossible.
"""

from __future__ import annotations

import re
import warnings
from pathlib import Path
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import Field, ValidationError
from pydantic.warnings import UnsupportedFieldAttributeWarning

from surreal_orm import BaseSurrealModel, FileField, FileInfo, FileRef, ImageField, Q, SurrealConfigDict
from surreal_orm.fields import get_file_buckets, is_file_field, is_image_field
from tests.files_capability import FILES_SKIP_REASON, files_capability_outcome

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_DIR = ROOT / ".github" / "workflows"
COMPOSE_FILE = ROOT / "devops" / "docker-compose.yml"

FLAG = "--allow-experimental=files"

# The texts the server answers with when the capability is off (3.2.6 and 3.3.5).
FILES_NOT_ENABLED_TEXTS = [
    "There was a problem running the file::exists() function. Experimental capability `files` is not enabled",
    "There was a problem with the database: Experimental feature files is not enabled",
    "Parse error: Unexpected token `BUCKET`, expected the experimental files feature to be enabled\n --> [1:8]",
]


# ---------------------------------------------------------------------------
# U30 — files_capability_outcome()
# ---------------------------------------------------------------------------


class TestFilesCapabilityOutcome:
    """The skip-or-fail decision of the file integration tests' capability probe."""

    @pytest.mark.parametrize("require", [False, True])
    def test_no_error_runs(self, require: bool) -> None:
        assert files_capability_outcome(None, require) == "run"

    @pytest.mark.parametrize("text", FILES_NOT_ENABLED_TEXTS)
    def test_files_not_enabled_skips_when_not_required(self, text: str) -> None:
        assert files_capability_outcome(text, False) == "skip"

    @pytest.mark.parametrize("text", FILES_NOT_ENABLED_TEXTS)
    def test_files_not_enabled_fails_when_required(self, text: str) -> None:
        assert files_capability_outcome(text, True) == "fail"

    @pytest.mark.parametrize("require", [False, True])
    def test_unrelated_error_is_raised_back(self, require: bool) -> None:
        text = "There was a problem with authentication"
        with pytest.raises(RuntimeError, match=re.escape(text)):
            files_capability_outcome(text, require)

    def test_match_is_case_sensitive(self) -> None:
        with pytest.raises(RuntimeError):
            files_capability_outcome("EXPERIMENTAL FEATURE FILES IS NOT ENABLED", False)

    def test_skip_reason_names_the_flag(self) -> None:
        assert FILES_SKIP_REASON == "SurrealDB was started without --allow-experimental=files"


# ---------------------------------------------------------------------------
# The servers the suite runs on carry the flag
# ---------------------------------------------------------------------------


def _load(path: Path) -> Any:
    yaml = pytest.importorskip("yaml", reason="pyyaml required for workflow lint tests")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _steps(workflow: str) -> list[dict[str, Any]]:
    data = _load(WORKFLOW_DIR / workflow)
    steps: list[dict[str, Any]] = []
    for job in data["jobs"].values():
        steps.extend(job.get("steps", []))
    return steps


def _step(workflow: str, name_prefix: str) -> dict[str, Any]:
    matches = [s for s in _steps(workflow) if str(s.get("name", "")).startswith(name_prefix)]
    assert len(matches) == 1, f"{workflow}: expected one step named {name_prefix!r}, found {len(matches)}"
    return matches[0]


class TestServersStartWithFilesCapability:
    """Compose, CI and the 3.x monitors run the suite on a flag-enabled server."""

    def test_compose_surrealdb_service_has_flag(self) -> None:
        command = _load(COMPOSE_FILE)["services"]["surrealdb"]["command"]
        assert command == f"start --log warn --user root --pass root {FLAG} memory"

    def test_ci_integration_step_requires_files(self) -> None:
        step = _step("ci.yml", "Run integration tests")
        assert step["env"]["SORM_REQUIRE_FILES"] == "1"

    def test_ci_starts_surrealdb_from_compose(self) -> None:
        # The flag reaches CI through the compose service, not a docker run line.
        step = _step("ci.yml", "Start SurrealDB")
        assert "docker compose -f devops/docker-compose.yml up -d surrealdb" in step["run"]

    @pytest.mark.parametrize("workflow", ["surrealdb-security.yml", "surrealdb-prerelease-canary.yml"])
    def test_monitor_starts_server_with_flag(self, workflow: str) -> None:
        run = _step(workflow, "Start SurrealDB")["run"]
        assert re.search(rf"start --log error --user root --pass root {re.escape(FLAG)} memory\b", run)

    @pytest.mark.parametrize("workflow", ["surrealdb-security.yml", "surrealdb-prerelease-canary.yml"])
    def test_monitor_integration_step_requires_files(self, workflow: str) -> None:
        step = _step(workflow, "Run integration tests")
        assert step["env"]["SORM_REQUIRE_FILES"] == "1"

    def test_v2_monitor_is_unchanged(self) -> None:
        # SurrealDB 2.x has no files capability: the v2 monitor must not ask for it.
        text = (WORKFLOW_DIR / "surrealdb-v2-security.yml").read_text(encoding="utf-8")
        assert FLAG not in text
        assert "SORM_REQUIRE_FILES" not in text


# ---------------------------------------------------------------------------
# U20 — FileField / ImageField detection helpers
# ---------------------------------------------------------------------------


class TestFileFieldDetection:
    """``is_file_field`` / ``is_image_field`` / ``get_file_buckets`` on every spelling."""

    @pytest.mark.parametrize(
        "field_type",
        [
            FileField["a"],
            FileField["a"] | None,
            Optional[FileField["a"]],  # noqa: F821
        ],
    )
    def test_single_bucket_file_field(self, field_type: Any) -> None:
        assert is_file_field(field_type)
        assert not is_image_field(field_type)
        assert get_file_buckets(field_type) == ("a",)

    @pytest.mark.parametrize(
        "field_type",
        [
            FileField["a", "b"],
            FileField["a", "b"] | None,
            Optional[FileField["a", "b"]],  # noqa: F821
        ],
    )
    def test_two_bucket_file_field(self, field_type: Any) -> None:
        assert is_file_field(field_type)
        assert not is_image_field(field_type)
        assert get_file_buckets(field_type) == ("a", "b")

    @pytest.mark.parametrize(
        "field_type",
        [
            ImageField["a"],
            ImageField["a"] | None,
            Optional[ImageField["a"]],  # noqa: F821
        ],
    )
    def test_image_field(self, field_type: Any) -> None:
        assert is_file_field(field_type)
        assert is_image_field(field_type)
        assert get_file_buckets(field_type) == ("a",)

    @pytest.mark.parametrize("field_type", [FileRef, FileRef | None, str, int | None, Any])
    def test_other_types_are_not_file_fields(self, field_type: Any) -> None:
        assert not is_file_field(field_type)
        assert not is_image_field(field_type)
        assert get_file_buckets(field_type) is None

    def test_file_field_without_bucket(self) -> None:
        with pytest.raises(TypeError, match=re.escape('FileField needs at least one bucket name: FileField["bucket"]')):
            FileField[()]

    def test_image_field_without_bucket(self) -> None:
        with pytest.raises(TypeError, match=re.escape('ImageField needs at least one bucket name: ImageField["bucket"]')):
            ImageField[()]

    @pytest.mark.parametrize("bucket", ["my-b", "", "1a", "a b", "a\n", "a:b"])
    def test_non_identifier_bucket_is_rejected(self, bucket: str) -> None:
        # The names are written into DDL as file<...>, so they must be identifiers.
        with pytest.raises(ValueError, match="bucket name"):
            FileField[bucket]
        with pytest.raises(ValueError, match="bucket name"):
            ImageField["ok", bucket]

    def test_non_string_bucket_is_rejected(self) -> None:
        with pytest.raises((TypeError, ValueError)):
            FileField[1]  # type: ignore[index]


# ---------------------------------------------------------------------------
# U21 — Pydantic validation, serialisation and the default (M30)
# ---------------------------------------------------------------------------


class FSUDoc(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="fsu_doc")

    id: str | None = None
    att: FileField["a"]  # noqa: F821, UP037
    two: FileField["a", "b"]  # noqa: F821, UP037
    pic: ImageField["img"]  # noqa: F821, UP037


# Pydantic warns that the metadata default has no effect inside a union member
# (true: the "= None" is what makes these optional). Recorded here so the run
# stays quiet and TestFileFieldDefault can assert the warning is still there.
with warnings.catch_warnings(record=True) as _UNION_WARNINGS:
    warnings.simplefilter("always")

    class FSUDocUnion(BaseSurrealModel):
        model_config = SurrealConfigDict(table_name="fsu_doc_union")

        id: str | None = None
        att: FileField["a"] | None = None  # noqa: F821, UP037
        opt: Optional[FileField["a"]] = None  # noqa: F821, UP037

    class FSUDocUnionNoDefault(BaseSurrealModel):
        model_config = SurrealConfigDict(table_name="fsu_doc_union_nodefault")

        id: str | None = None
        att: FileField["a"] | None  # noqa: F821, UP037


with warnings.catch_warnings(record=True) as _BARE_WARNINGS:
    warnings.simplefilter("always")

    class FSUDocBare(BaseSurrealModel):
        model_config = SurrealConfigDict(table_name="fsu_doc_bare")

        id: str | None = None
        att: FileField["a"]  # noqa: F821, UP037
        pic: ImageField["img"]  # noqa: F821, UP037


class FSUDocAnyBucket(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="fsu_doc_any")

    id: str | None = None
    att: FileRef | None = None


class TestFileFieldValidation:
    """Values a file field accepts, refuses and how it dumps them."""

    def test_accepts_file_ref(self) -> None:
        doc = FSUDoc(att=FileRef("a", "/k"))
        assert doc.att == FileRef("a", "/k")

    @pytest.mark.parametrize("text", ["a:/k", 'f"a:/k"', "a:/dir/k"])
    def test_accepts_pointer_text(self, text: str) -> None:
        doc = FSUDoc(att=text)
        assert isinstance(doc.att, FileRef)
        assert doc.att == FileRef.parse(text)

    def test_two_bucket_field_accepts_both(self) -> None:
        assert FSUDoc(two="a:/k").two == FileRef("a", "/k")
        assert FSUDoc(two=FileRef("b", "/k")).two == FileRef("b", "/k")

    def test_other_bucket_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match=re.escape("File pointer x:/k is not in the allowed bucket(s) a")):
            FSUDoc(att=FileRef("x", "/k"))
        with pytest.raises(ValidationError, match=re.escape("File pointer c:/k is not in the allowed bucket(s) a, b")):
            FSUDoc(two="c:/k")

    def test_unparsable_text_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match=re.escape("Invalid file pointer 'users:abc': expected 'bucket:/key'")):
            FSUDoc(att="users:abc")

    @pytest.mark.parametrize("value", [42, b"a:/k", {"bucket": "a", "key": "/k"}, ["a", "/k"]])
    def test_other_types_are_rejected(self, value: Any) -> None:
        with pytest.raises(
            ValidationError, match=re.escape(f"Expected a FileRef or a 'bucket:/key' string, got {type(value).__name__}")
        ):
            FSUDoc(att=value)

    def test_none_is_accepted(self) -> None:
        assert FSUDoc(att=None).att is None

    def test_default_is_none(self) -> None:
        doc = FSUDoc()
        assert doc.att is None
        assert doc.two is None
        assert doc.pic is None

    def test_model_dump_keeps_file_ref(self) -> None:
        # Python mode keeps the FileRef so the CBOR encoder sends tag 55.
        doc = FSUDoc(att="a:/k")
        assert doc.model_dump()["att"] == FileRef("a", "/k")
        assert type(doc.model_dump()["att"]) is FileRef

    def test_model_dump_json_mode_gives_text(self) -> None:
        doc = FSUDoc(att="a:/k")
        dumped = doc.model_dump(mode="json")
        assert dumped["att"] == "a:/k"
        assert dumped["two"] is None
        assert '"att":"a:/k"' in doc.model_dump_json()

    def test_json_round_trip(self) -> None:
        doc = FSUDoc(att="a:/k", pic=FileRef("img", "/p.png"))
        assert FSUDoc.model_validate_json(doc.model_dump_json()) == doc

    def test_validated_assignment(self) -> None:
        doc = FSUDoc()
        doc.att = "a:/k"  # type: ignore[assignment]
        assert doc.att == FileRef("a", "/k")
        with pytest.raises(ValidationError, match=re.escape("is not in the allowed bucket(s) a")):
            doc.att = FileRef("x", "/k")
        with pytest.raises(ValidationError, match="Expected a FileRef"):
            doc.att = 1  # type: ignore[assignment]
        assert doc.att == FileRef("a", "/k")
        doc.att = None
        assert doc.att is None

    def test_image_field_validates_bucket_only(self) -> None:
        # The image signature is checked on upload, not on the pointer.
        assert FSUDoc(pic="img:/x.bin").pic == FileRef("img", "/x.bin")
        with pytest.raises(ValidationError):
            FSUDoc(pic="a:/x.png")

    def test_union_form_validates_the_same(self) -> None:
        doc = FSUDocUnion(att="a:/k", opt=FileRef("a", "/o"))
        assert doc.att == FileRef("a", "/k")
        assert doc.opt == FileRef("a", "/o")
        with pytest.raises(ValidationError, match=re.escape("is not in the allowed bucket(s) a")):
            FSUDocUnion(att="x:/k")
        with pytest.raises(ValidationError, match=re.escape("is not in the allowed bucket(s) a")):
            FSUDocUnion(opt="x:/k")

    def test_bare_file_ref_accepts_any_bucket(self) -> None:
        assert FSUDocAnyBucket(att=FileRef("anything", "/k")).att == FileRef("anything", "/k")
        assert FSUDocAnyBucket(att="other:/k").att == FileRef("other", "/k")
        with pytest.raises(ValidationError, match="Expected a FileRef"):
            FSUDocAnyBucket(att=3)

    def test_bare_file_ref_dumps_like_a_file_field(self) -> None:
        doc = FSUDocAnyBucket(att="other:/k")
        assert type(doc.model_dump()["att"]) is FileRef
        assert doc.model_dump(mode="json")["att"] == "other:/k"
        assert FSUDocAnyBucket.model_fields["att"].is_required() is False

    def test_decoded_pointer_with_dot_segment_is_kept(self) -> None:
        # A pointer the server sent is not re-validated (``..`` on a memory bucket).
        wire = FileRef._from_wire("a", "/../x")
        assert FSUDoc(att=wire).att is wire
        assert FSUDoc.model_validate({"att": wire}).att is wire

    def test_from_db_keeps_file_ref(self) -> None:
        doc = FSUDoc.from_db({"id": "fsu_doc:1", "att": FileRef("a", "/k")})
        assert isinstance(doc, FSUDoc)
        assert doc.att == FileRef("a", "/k")

    def test_file_info_is_exported(self) -> None:
        # FileInfo is re-exported from the SDK next to FileRef.
        from surreal_sdk import FileInfo as SdkFileInfo

        assert FileInfo is SdkFileInfo


class TestFileFieldDefault:
    """M30: the default sits in the field metadata, so the bare form is optional everywhere."""

    @pytest.mark.parametrize("field", ["att", "two", "pic"])
    def test_bare_form_is_not_required(self, field: str) -> None:
        assert FSUDoc.model_fields[field].is_required() is False

    @pytest.mark.parametrize("field", ["att", "two", "pic"])
    def test_bare_form_model_construct_gives_none(self, field: str) -> None:
        # live() builds a DELETE event's instance with model_construct(id=...).
        assert getattr(FSUDoc.model_construct(id="x"), field) is None

    @pytest.mark.parametrize("field", ["att", "two", "pic"])
    def test_bare_form_not_required_in_json_schema(self, field: str) -> None:
        schema = FSUDoc.model_json_schema()
        assert field not in schema.get("required", [])
        assert field in schema["properties"]

    @pytest.mark.parametrize("field", ["att", "opt"])
    def test_union_form_with_default_is_not_required(self, field: str) -> None:
        assert FSUDocUnion.model_fields[field].is_required() is False
        assert getattr(FSUDocUnion.model_construct(id="x"), field) is None
        assert field not in FSUDocUnion.model_json_schema().get("required", [])

    def test_bare_form_emits_no_warning(self) -> None:
        assert [w for w in _BARE_WARNINGS if issubclass(w.category, UnsupportedFieldAttributeWarning)] == []
        assert FSUDocBare.model_fields["att"].is_required() is False

    def test_union_form_warns_that_the_metadata_default_is_ignored(self) -> None:
        # Documented in the FileField docstring; if a Pydantic release stops
        # warning (or starts lifting the default), the docstring must follow.
        assert any(issubclass(w.category, UnsupportedFieldAttributeWarning) for w in _UNION_WARNINGS)

    def test_union_form_without_default_stays_required(self) -> None:
        # Pydantic does not lift metadata (the default included) out of a union
        # member; if a Pydantic release starts doing so, this test says it.
        assert FSUDocUnionNoDefault.model_fields["att"].is_required() is True
        assert "att" in FSUDocUnionNoDefault.model_json_schema().get("required", [])
        with pytest.raises(ValidationError):
            FSUDocUnionNoDefault()
        assert FSUDocUnionNoDefault(att=None).att is None

    def test_json_schema_describes_text(self) -> None:
        prop = FSUDoc.model_json_schema()["properties"]["att"]
        assert {"type": "string"} in prop["anyOf"]
        assert {"type": "null"} in prop["anyOf"]
        assert prop.get("default") is None


# ---------------------------------------------------------------------------
# U31 — "bucket:/key" text coerced in filters and QuerySet writes (M29, §7.5)
# ---------------------------------------------------------------------------


class FSUFiltered(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="fsu_filtered")

    id: str | None = None
    title: str = ""
    att: FileField["fsu_b"]  # noqa: F821, UP037
    aliased: FileField["fsu_b"] = Field(default=None, alias="att_col")  # noqa: F821, UP037
    loose: FileRef | None = None


def _bound(qs: Any) -> Any:
    """The value the first filter binds (``$_f0``) once the WHERE is compiled."""
    qs._compile_where_clause()
    return qs._variables["_f0"]


def _mock_client(records: list[Any] | None = None) -> Any:
    client = AsyncMock()
    client.query = AsyncMock(return_value=MagicMock(all_records=records or []))
    return client


class TestFileColumns:
    def test_file_columns_by_name_and_alias(self) -> None:
        assert FSUFiltered.get_file_columns() == {
            "att": ("fsu_b",),
            "aliased": ("fsu_b",),
            "att_col": ("fsu_b",),
            "loose": None,
        }

    def test_model_without_file_fields(self) -> None:
        class FSUNoFiles(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="fsu_no_files")

            id: str | None = None
            name: str = ""

        assert FSUNoFiles.get_file_columns() == {}


class TestFileFilterCoercion:
    def test_exact_text_binds_file_ref(self) -> None:
        bound = _bound(FSUFiltered.objects().filter(att="fsu_b:/k"))
        assert isinstance(bound, FileRef)
        assert bound == FileRef("fsu_b", "/k")

    def test_alias_column_is_coerced(self) -> None:
        assert _bound(FSUFiltered.objects().filter(att_col="fsu_b:/k")) == FileRef("fsu_b", "/k")

    def test_bare_file_ref_column_is_coerced(self) -> None:
        assert _bound(FSUFiltered.objects().filter(loose="other:/k")) == FileRef("other", "/k")

    def test_in_is_coerced_element_wise(self) -> None:
        bound = _bound(FSUFiltered.objects().filter(att__in=["fsu_b:/a", FileRef("fsu_b", "/b")]))
        assert bound == [FileRef("fsu_b", "/a"), FileRef("fsu_b", "/b")]
        assert all(isinstance(item, FileRef) for item in bound)

    def test_not_in_is_coerced_element_wise(self) -> None:
        bound = _bound(FSUFiltered.objects().filter(att__not_in=("fsu_b:/a",)))
        assert bound == [FileRef("fsu_b", "/a")]

    def test_q_object_is_coerced(self) -> None:
        assert _bound(FSUFiltered.objects().filter(Q(att="fsu_b:/k"))) == FileRef("fsu_b", "/k")

    def test_file_ref_passes_unchanged(self) -> None:
        ref = FileRef("fsu_b", "/k")
        assert _bound(FSUFiltered.objects().filter(att=ref)) is ref

    def test_other_bucket_is_not_checked(self) -> None:
        # A pointer to another bucket simply matches no row.
        assert _bound(FSUFiltered.objects().filter(att="elsewhere:/k")) == FileRef("elsewhere", "/k")

    def test_isnull_is_untouched(self) -> None:
        qs = FSUFiltered.objects().filter(att__isnull=True)
        assert "att IS NULL" in qs._compile_query()

    def test_string_lookup_is_untouched(self) -> None:
        assert _bound(FSUFiltered.objects().filter(att__contains="x")) == "x"

    def test_other_column_is_untouched(self) -> None:
        assert _bound(FSUFiltered.objects().filter(title="fsu_b:/k")) == "fsu_b:/k"

    def test_unparsable_text_raises_before_any_query(self) -> None:
        with pytest.raises(ValueError, match=re.escape("Invalid file pointer 'nope': expected 'bucket:/key'")):
            FSUFiltered.objects().filter(att="nope")._compile_where_clause()

    def test_declared_variable_stays_a_reference(self) -> None:
        qs = FSUFiltered.objects().filter(att="$ptr").variables(ptr=FileRef("fsu_b", "/k"))
        assert "att = $ptr" in qs._compile_query()
        assert qs._variables["ptr"] == FileRef("fsu_b", "/k")


class TestFileWriteCoercion:
    @pytest.mark.asyncio
    async def test_bulk_update_binds_file_ref(self) -> None:
        client = _mock_client([{}])
        with patch(
            "surreal_orm.query_set.SurrealDBConnectionManager",
            new=MagicMock(get_client=AsyncMock(return_value=client)),
        ):
            await FSUFiltered.objects().filter(title="t").bulk_update({"att": "fsu_b:/k", "att_col": "fsu_b:/c"})

        _query, variables = client.query.call_args[0]
        assert variables["_bu0"] == FileRef("fsu_b", "/k")
        assert variables["_bu1"] == FileRef("fsu_b", "/c")

    @pytest.mark.asyncio
    async def test_bulk_update_unparsable_text_raises_before_any_query(self) -> None:
        client = _mock_client([{}])
        with patch(
            "surreal_orm.query_set.SurrealDBConnectionManager",
            new=MagicMock(get_client=AsyncMock(return_value=client)),
        ):
            with pytest.raises(ValueError, match="Invalid file pointer"):
                await FSUFiltered.objects().filter(title="t").bulk_update({"att": "nope"})
        client.query.assert_not_called()

    @pytest.mark.asyncio
    async def test_upsert_defaults_bind_file_ref(self) -> None:
        client = _mock_client([{"id": "fsu_filtered:1"}])
        with patch(
            "surreal_orm.query_set.SurrealDBConnectionManager",
            new=MagicMock(get_client=AsyncMock(return_value=client)),
        ):
            await FSUFiltered.objects().upsert(defaults={"title": "t", "att": "fsu_b:/k"}, id="fsu_filtered:1")

        _query, variables = client.query.call_args[0]
        assert variables["_up_att"] == FileRef("fsu_b", "/k")
        assert variables["_up_title"] == "t"

    @pytest.mark.asyncio
    async def test_upsert_on_conflict_binds_file_ref(self) -> None:
        client = _mock_client([{"id": "fsu_filtered:1"}])
        with patch(
            "surreal_orm.query_set.SurrealDBConnectionManager",
            new=MagicMock(get_client=AsyncMock(return_value=client)),
        ):
            await FSUFiltered.objects().upsert(
                defaults={"att": "fsu_b:/k"},
                id="fsu_filtered:1",
                on_conflict={"att": "fsu_b:/c"},
            )

        _query, variables = client.query.call_args[0]
        assert variables["_up_att"] == FileRef("fsu_b", "/k")
        assert variables["_oc_att"] == FileRef("fsu_b", "/c")

    @pytest.mark.asyncio
    async def test_bulk_upsert_on_conflict_binds_file_ref(self) -> None:
        client = _mock_client([])
        docs = [FSUFiltered(id="1", att="fsu_b:/k")]
        with patch(
            "surreal_orm.query_set.SurrealDBConnectionManager",
            new=MagicMock(get_client=AsyncMock(return_value=client)),
        ):
            await FSUFiltered.objects().bulk_upsert(docs, on_conflict={"att": "fsu_b:/c"})

        _query, variables = client.query.call_args[0]
        assert variables["_bu0_att"] == FileRef("fsu_b", "/k")
        assert variables["_oc0_att"] == FileRef("fsu_b", "/c")


# merge(**data) skips Pydantic validation, so "bucket:/key" text given there
# reached the wire as a string: a typed column rejected it and no file filter
# matched it on a SCHEMALESS table (review of the 0.39.0 feature PR).


def _merge_patch(client: Any) -> Any:
    return patch(
        "surreal_orm.model_base.SurrealDBConnectionManager",
        new=MagicMock(get_client=AsyncMock(return_value=client)),
    )


def _filtered_merge_client() -> Any:
    client = AsyncMock()
    client.merge = AsyncMock(return_value=MagicMock(is_empty=False, records=[{"id": "fsu_filtered:1"}]))
    client.query = AsyncMock(return_value=MagicMock(is_empty=False, all_records=[{"id": "fsu_filtered:1"}]))
    return client


class TestFileMergeCoercion:
    @pytest.mark.asyncio
    async def test_merge_sends_file_ref_for_text(self) -> None:
        doc = FSUFiltered(id="d1")
        client = _filtered_merge_client()
        with _merge_patch(client):
            await doc.merge(refresh=False, att="fsu_b:/k", title="t")

        thing, data = client.merge.call_args[0]
        assert thing == "fsu_filtered:d1"
        assert data == {"att": FileRef("fsu_b", "/k"), "title": "t"}
        assert isinstance(data["att"], FileRef)

    @pytest.mark.asyncio
    async def test_merge_sends_file_ref_under_alias(self) -> None:
        doc = FSUFiltered(id="d1")
        client = _filtered_merge_client()
        with _merge_patch(client):
            await doc.merge(refresh=False, att_col="fsu_b:/c")

        _thing, data = client.merge.call_args[0]
        assert data == {"att_col": FileRef("fsu_b", "/c")}
        assert isinstance(data["att_col"], FileRef)

    @pytest.mark.asyncio
    async def test_merge_sends_file_ref_for_bare_annotation(self) -> None:
        doc = FSUFiltered(id="d1")
        client = _filtered_merge_client()
        with _merge_patch(client):
            await doc.merge(refresh=False, loose="other:/x")

        _thing, data = client.merge.call_args[0]
        assert isinstance(data["loose"], FileRef)
        assert data["loose"] == FileRef("other", "/x")

    @pytest.mark.asyncio
    async def test_merge_keeps_file_ref_and_none(self) -> None:
        doc = FSUFiltered(id="d1")
        client = _filtered_merge_client()
        ref = FileRef("fsu_b", "/k")
        with _merge_patch(client):
            await doc.merge(refresh=False, att=ref, att_col=None)

        _thing, data = client.merge.call_args[0]
        assert data["att"] is ref
        assert data["att_col"] is None

    @pytest.mark.asyncio
    async def test_merge_unparsable_text_raises_before_any_request(self) -> None:
        doc = FSUFiltered(id="d1")
        client = _filtered_merge_client()
        with _merge_patch(client):
            with pytest.raises(ValueError, match="Invalid file pointer"):
                await doc.merge(att="nope")
        client.merge.assert_not_called()
        client.query.assert_not_called()

    @pytest.mark.asyncio
    async def test_merge_in_transaction_sends_file_ref(self) -> None:
        doc = FSUFiltered(id="d1")
        tx = AsyncMock()
        tx.merge = AsyncMock(return_value=MagicMock(is_empty=False, records=[{"id": "fsu_filtered:1"}]))
        await doc.merge(tx=tx, att="fsu_b:/k")

        _thing, data = tx.merge.call_args[0]
        assert isinstance(data["att"], FileRef)
        assert data["att"] == FileRef("fsu_b", "/k")
        assert doc.att == FileRef("fsu_b", "/k")

    @pytest.mark.asyncio
    async def test_merge_with_surreal_func_binds_file_ref(self) -> None:
        from surreal_orm import SurrealFunc

        doc = FSUFiltered(id="d1")
        client = _filtered_merge_client()
        with _merge_patch(client):
            await doc.merge(refresh=False, att="fsu_b:/k", title=SurrealFunc("'t'"))

        _query, variables = client.query.call_args[0]
        bound = [value for value in variables.values() if isinstance(value, FileRef)]
        assert bound == [FileRef("fsu_b", "/k")]

    @pytest.mark.asyncio
    async def test_merge_signals_keep_the_callers_values(self) -> None:
        from surreal_orm import pre_update

        doc = FSUFiltered(id="d1")
        client = _filtered_merge_client()
        seen: list[dict[str, Any]] = []

        async def on_pre(sender: Any, instance: Any, update_fields: dict[str, Any], **kwargs: Any) -> None:
            seen.append(dict(update_fields))

        pre_update.connect(FSUFiltered)(on_pre)
        try:
            with _merge_patch(client):
                await doc.merge(refresh=False, att="fsu_b:/k")
        finally:
            pre_update.disconnect(on_pre, sender=FSUFiltered)

        assert seen == [{"att": "fsu_b:/k"}]


# ---------------------------------------------------------------------------
# U22 — ModelIntrospector maps file columns to file<bucket> (AC10)
# ---------------------------------------------------------------------------


class FSUColumns(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="fsu_columns")

    id: str | None = None
    att: FileField["a"]  # noqa: F821, UP037
    two: FileField["zz", "aa"]  # noqa: F821, UP037
    pic: ImageField["img"]  # noqa: F821, UP037
    loose: FileRef | None = None
    required: FileRef
    title: str = ""


def _columns_state(model: type[BaseSurrealModel]) -> Any:
    from surreal_orm.migrations.introspector import ModelIntrospector

    return ModelIntrospector([model]).introspect().tables[model.get_table_name()]


class TestFileColumnIntrospection:
    def test_bare_file_field_is_an_optional_file_column(self) -> None:
        """The generic union path would keep nullable=False and map the Annotated to any."""
        field = _columns_state(FSUColumns).fields["att"]
        assert field.field_type == "file<a>"
        assert field.nullable is True

    def test_bare_file_field_renders_option_file(self) -> None:
        from surreal_orm.migrations.operations import AddField

        field = _columns_state(FSUColumns).fields["att"]
        sql = AddField.from_field_state("fsu_columns", field).forwards()
        assert "TYPE option<file<a>>" in sql

    @pytest.mark.parametrize("name", ["att", "opt"])
    def test_union_spellings_give_the_same_state(self, name: str) -> None:
        bare = _columns_state(FSUColumns).fields["att"]
        union = _columns_state(FSUDocUnion).fields[name]
        assert (union.field_type, union.nullable) == (bare.field_type, bare.nullable)
        assert union.default is None

    def test_buckets_keep_the_declared_order(self) -> None:
        field = _columns_state(FSUColumns).fields["two"]
        assert field.field_type == "file<zz | aa>"
        assert field.nullable is True

    def test_image_field_maps_like_a_file_field(self) -> None:
        field = _columns_state(FSUColumns).fields["pic"]
        assert (field.field_type, field.nullable) == ("file<img>", True)

    def test_bare_file_ref_is_an_untyped_file_column(self) -> None:
        from surreal_orm.migrations.operations import AddField

        loose = _columns_state(FSUColumns).fields["loose"]
        assert (loose.field_type, loose.nullable) == ("file", True)
        assert "TYPE option<file>" in AddField.from_field_state("fsu_columns", loose).forwards()
        required = _columns_state(FSUColumns).fields["required"]
        assert (required.field_type, required.nullable) == ("file", False)

    def test_field_info_metadata_alone_is_enough(self) -> None:
        """When get_type_hints fails the introspector reads FieldInfo; the marker is in its metadata."""
        from surreal_orm.migrations.introspector import ModelIntrospector

        info = FSUColumns.model_fields["att"]
        field = ModelIntrospector([])._introspect_field("att", info.annotation, info, FSUColumns)
        assert field is not None
        assert (field.field_type, field.nullable) == ("file<a>", True)

    @pytest.mark.parametrize(
        ("name", "server_text"),
        [
            ("att", "DEFINE FIELD att ON fsu_columns TYPE none | file<a> PERMISSIONS FULL"),
            ("two", "DEFINE FIELD two ON fsu_columns TYPE none | file<zz | aa> PERMISSIONS FULL"),
            ("loose", "DEFINE FIELD loose ON fsu_columns TYPE none | file PERMISSIONS FULL"),
            ("required", "DEFINE FIELD required ON fsu_columns TYPE file PERMISSIONS FULL"),
        ],
    )
    def test_server_definition_compares_equal(self, name: str, server_text: str) -> None:
        """What INFO FOR TABLE reports for the column (M15) is the model's state: no diff."""
        from surreal_orm.migrations.define_parser import parse_define_field

        assert parse_define_field(server_text) == _columns_state(FSUColumns).fields[name]

    def test_unchanged_model_diffs_to_nothing(self) -> None:
        from surreal_orm.migrations.define_parser import parse_define_field
        from surreal_orm.migrations.operations import AddField, AlterField, DropField
        from surreal_orm.migrations.state import SchemaState, TableState

        target = SchemaState(tables={"fsu_columns": _columns_state(FSUColumns)})
        texts = {
            "att": "DEFINE FIELD att ON fsu_columns TYPE none | file<a> PERMISSIONS FULL",
            "two": "DEFINE FIELD two ON fsu_columns TYPE none | file<zz | aa> PERMISSIONS FULL",
            "pic": "DEFINE FIELD pic ON fsu_columns TYPE none | file<img> PERMISSIONS FULL",
            "loose": "DEFINE FIELD loose ON fsu_columns TYPE none | file PERMISSIONS FULL",
            "required": "DEFINE FIELD required ON fsu_columns TYPE file PERMISSIONS FULL",
            "title": "DEFINE FIELD title ON fsu_columns TYPE string DEFAULT '' PERMISSIONS FULL",
        }
        current_table = TableState(name="fsu_columns", fields={name: parse_define_field(text) for name, text in texts.items()})
        current = SchemaState(tables={"fsu_columns": current_table})
        field_ops = [op for op in current.diff(target) if isinstance(op, (AddField, AlterField, DropField))]
        assert field_ops == []


# ---------------------------------------------------------------------------
# U23 — inspectdb: file<bucket> columns become FileField / FileRef (AC10)
# ---------------------------------------------------------------------------


def _generated_state() -> Any:
    from surreal_orm.migrations.state import FieldState, SchemaState, TableState

    fields = {
        "att": FieldState(name="att", field_type="file<x>", nullable=True),
        "two": FieldState(name="two", field_type="file<zz | aa>", nullable=True),
        "loose": FieldState(name="loose", field_type="file", nullable=True),
        "required": FieldState(name="required", field_type="file", nullable=False),
    }
    return SchemaState(tables={"fsu_gen_docs": TableState(name="fsu_gen_docs", fields=fields)})


def _import_generated(source: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> Any:
    """Write *source* to a module of its own and import it (Pydantic resolves annotations there)."""
    import importlib.util
    import sys

    path = tmp_path / f"{name}.py"
    path.write_text(source)
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


class TestFileColumnCodeGeneration:
    def _source(self, state: Any = None) -> str:
        from surreal_orm.migrations.model_generator import ModelCodeGenerator

        return ModelCodeGenerator().generate(state if state is not None else _generated_state())

    def test_annotations(self) -> None:
        source = self._source()
        assert '    att: FileField["x"]\n' in source
        assert '    two: FileField["zz", "aa"]\n' in source
        assert "    loose: FileRef | None = None\n" in source
        assert "    required: FileRef\n" in source

    def test_imports(self) -> None:
        source = self._source()
        assert "from surreal_orm.fields import FileField" in source
        assert "from surreal_orm import FileRef" in source

    def test_generated_source_re_introspects_equal(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from surreal_orm.migrations.introspector import ModelIntrospector

        module = _import_generated(self._source(), tmp_path, monkeypatch, "fsu_generated_models")
        table = ModelIntrospector([module.FsuGenDoc]).introspect().tables["fsu_gen_docs"]
        assert table.fields == _generated_state().tables["fsu_gen_docs"].fields

    @pytest.mark.parametrize("field_type", ["file<`my-b`>", "file<a | `my-b`>"])
    def test_non_identifier_bucket_falls_back_to_file_ref(
        self, field_type: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A FileField subscript must be an identifier: the generated module would not import otherwise."""
        from surreal_orm.migrations.state import FieldState, SchemaState, TableState

        field = FieldState(name="att", field_type=field_type, nullable=True)
        state = SchemaState(tables={"fsu_odd_docs": TableState(name="fsu_odd_docs", fields={"att": field})})
        source = self._source(state)
        assert f"    att: FileRef | None = None  # {field_type!r}: bucket name is not an identifier\n" in source
        assert "FileField" not in source

        module = _import_generated(source, tmp_path, monkeypatch, f"fsu_odd_models_{len(field_type)}")
        doc = module.FsuOddDoc(att="my-b:/k")
        assert doc.att == FileRef("my-b", "/k")

    @pytest.mark.parametrize(
        "field_type",
        [
            "file<`a\rimport os #`>",
            "file<`a\x0cimport os #`>",
            "file<`a\x00import os #`>",
            "file<`a\r\x0c\x00import os #`>",
        ],
    )
    def test_server_type_text_stays_inside_the_comment(self, field_type: str) -> None:
        """The fallback comment is the only place server text reaches: it must not end the line."""
        import ast

        from surreal_orm.migrations.state import FieldState, SchemaState, TableState

        field = FieldState(name="att", field_type=field_type, nullable=True)
        state = SchemaState(tables={"fsu_ctl_docs": TableState(name="fsu_ctl_docs", fields={"att": field})})
        source = self._source(state)
        assert not any(ch in source for ch in "\r\x0c\x00")
        assert f"    att: FileRef | None = None  # {field_type!r}: bucket name is not an identifier\n" in source

        module = ast.parse(source)
        assert ast.get_docstring(module) is not None
        body = module.body[1:]
        assert all(isinstance(node, (ast.ImportFrom, ast.ClassDef)) for node in body)
        assert all(node.module != "os" for node in body if isinstance(node, ast.ImportFrom))
        (cls,) = [node for node in body if isinstance(node, ast.ClassDef)]
        assert cls.name == "FsuCtlDoc"
        targets: list[str] = []
        for node in cls.body:
            assert isinstance(node, (ast.Assign, ast.AnnAssign))
            targets.append(ast.unparse(node.targets[0] if isinstance(node, ast.Assign) else node.target))
        assert targets == ["model_config", "id", "att"]
        assert isinstance(cls.body[-1], ast.AnnAssign)
        assert ast.unparse(cls.body[-1]) == "att: FileRef | None = None"


class TestFileFieldLiveEvents:
    """A DELETE without the record, or a DIFF event, builds an id-only instance (M30)."""

    @pytest.mark.parametrize(("action", "diff"), [("DELETE", False), ("UPDATE", True)])
    def test_id_only_instance_reads_none(self, action: str, diff: bool) -> None:
        from surreal_orm.live import LiveModelStream
        from surreal_sdk.streaming.live_select import LiveAction, LiveChange

        stream = LiveModelStream(model=FSUDoc, connection=None, table="fsu_doc", diff=diff)
        change = LiveChange(id="live-uuid", action=LiveAction(action), record_id="fsu_doc:x", result={})
        event = stream._to_event(change)
        assert event.instance.id == "x"
        assert event.instance.att is None
        assert event.instance.pic is None


# ---------------------------------------------------------------------------
# U24 — DefineBucket / AlterBucket / RemoveBucket (AC8, C11-C13)
# ---------------------------------------------------------------------------


class TestBucketOperations:
    def test_define_bucket_spec_example(self) -> None:
        from surreal_orm.migrations.operations import DefineBucket

        op = DefineBucket("avatars", backend="memory", readonly=True, permissions="$auth.id != NONE", comment="it's")
        assert op.forwards() == (
            "DEFINE BUCKET avatars READONLY BACKEND 'memory' PERMISSIONS WHERE $auth.id != NONE COMMENT 'it\\'s';"
        )
        assert op.backwards() == "REMOVE BUCKET avatars;"
        assert op.reversible is True
        assert op.describe() == "Define bucket avatars"

    def test_define_bucket_without_backend_uses_the_global_bucket(self) -> None:
        from surreal_orm.migrations.operations import DefineBucket

        assert DefineBucket("x").forwards() == "DEFINE BUCKET x;"

    def test_clause_order_is_the_servers(self) -> None:
        from surreal_orm.migrations.operations import DefineBucket

        op = DefineBucket("b", comment="c", permissions="NONE", backend="memory", readonly=True)
        assert op.forwards() == "DEFINE BUCKET b READONLY BACKEND 'memory' PERMISSIONS NONE COMMENT 'c';"

    @pytest.mark.parametrize(("given", "rendered"), [("FULL", "FULL"), ("full", "FULL"), (" None ", "NONE")])
    def test_keyword_permissions(self, given: str, rendered: str) -> None:
        from surreal_orm.migrations.operations import DefineBucket

        assert DefineBucket("b", permissions=given).forwards() == f"DEFINE BUCKET b PERMISSIONS {rendered};"

    def test_backend_and_comment_go_through_surql_string(self) -> None:
        from surreal_orm.migrations.operations import DefineBucket

        op = DefineBucket("b", backend="file:C:\\data's", comment="a\\b 'c'")
        assert op.forwards() == "DEFINE BUCKET b BACKEND 'file:C:\\\\data\\'s' COMMENT 'a\\\\b \\'c\\'';"

    @pytest.mark.parametrize(
        ("name", "rendered"), [("my-b", "`my-b`"), ("7b", "`7b`"), ("a`b\\c", "`a\\`b\\\\c`"), ("ok_1", "ok_1")]
    )
    def test_name_rendered_bare_or_backticked(self, name: str, rendered: str) -> None:
        from surreal_orm.migrations.operations import AlterBucket, DefineBucket, RemoveBucket

        assert DefineBucket(name).forwards() == f"DEFINE BUCKET {rendered};"
        assert DefineBucket(name).backwards() == f"REMOVE BUCKET {rendered};"
        assert RemoveBucket(name).forwards() == f"REMOVE BUCKET {rendered};"
        assert AlterBucket(name).forwards() == f"DEFINE BUCKET OVERWRITE {rendered};"

    def test_remove_bucket(self) -> None:
        from surreal_orm.migrations.operations import RemoveBucket

        op = RemoveBucket("my-b")
        assert op.forwards() == "REMOVE BUCKET `my-b`;"
        assert op.backwards() == ""
        assert op.reversible is False
        assert op.describe() == "Remove bucket my-b"

    def test_alter_bucket_without_previous_is_irreversible(self) -> None:
        from surreal_orm.migrations.operations import AlterBucket, split_destructive

        op = AlterBucket("x", backend="memory")
        assert op.forwards() == "DEFINE BUCKET OVERWRITE x BACKEND 'memory';"
        assert op.reversible is False
        assert op.backwards() == ""
        assert op.describe() == "Alter bucket x"
        assert split_destructive([op]) == ([], [op])

    def test_alter_bucket_from_states_is_reversible(self) -> None:
        from surreal_orm.migrations.operations import AlterBucket
        from surreal_orm.migrations.state import BucketState

        current = BucketState(name="b", backend="memory", readonly=True, permissions="NONE", comment="old")
        target = BucketState(name="b", backend="file:/srv", permissions="$action = 'get'")
        op = AlterBucket.from_bucket_states(current, target)
        assert op.previous_permissions == "NONE"
        assert op.reversible is True
        assert op.forwards() == "DEFINE BUCKET OVERWRITE b BACKEND 'file:/srv' PERMISSIONS WHERE $action = 'get';"
        assert op.backwards() == "DEFINE BUCKET OVERWRITE b READONLY BACKEND 'memory' PERMISSIONS NONE COMMENT 'old';"

    def test_alter_bucket_from_states_with_global_previous_backend(self) -> None:
        """``None`` is a valid backend (the global bucket), so it cannot be the reversibility signal."""
        from surreal_orm.migrations.operations import AlterBucket
        from surreal_orm.migrations.state import BucketState

        op = AlterBucket.from_bucket_states(BucketState(name="b"), BucketState(name="b", backend="memory"))
        assert op.previous_backend is None
        assert op.previous_permissions == "FULL"
        assert op.reversible is True
        assert op.backwards() == "DEFINE BUCKET OVERWRITE b PERMISSIONS FULL;"

    def test_define_bucket_from_state(self) -> None:
        from surreal_orm.migrations.operations import DefineBucket
        from surreal_orm.migrations.state import BucketState

        state = BucketState(name="b", backend="memory", readonly=True, permissions="NONE", comment="c")
        op = DefineBucket.from_bucket_state(state)
        assert op == DefineBucket("b", backend="memory", readonly=True, permissions="NONE", comment="c")

    @pytest.mark.parametrize("name", ["", "a\nb", "a\x00b", "tab\there", "\x7f", 3, None])
    def test_invalid_name(self, name: Any) -> None:
        from surreal_orm.migrations.operations import AlterBucket, DefineBucket, RemoveBucket

        for cls in (DefineBucket, AlterBucket, RemoveBucket):
            with pytest.raises(ValueError, match=re.escape(f"Invalid bucket name {name!r}")):
                cls(name)

    @pytest.mark.parametrize("permissions", ["WHERE $auth.id != NONE", "  where true", "WHERE"])
    def test_permissions_with_where_rejected(self, permissions: str) -> None:
        from surreal_orm.migrations.operations import AlterBucket, DefineBucket

        message = f"Bucket permissions take the expression without WHERE, or 'FULL' / 'NONE': {permissions!r}"
        with pytest.raises(ValueError, match=re.escape(message)):
            DefineBucket("b", permissions=permissions)
        with pytest.raises(ValueError, match=re.escape(message)):
            AlterBucket("b", permissions=permissions)
        with pytest.raises(ValueError, match=re.escape(message)):
            AlterBucket("b", previous_permissions=permissions)

    def test_expression_starting_with_where_like_word_is_kept(self) -> None:
        from surreal_orm.migrations.operations import DefineBucket

        op = DefineBucket("b", permissions="whereabouts = true")
        assert op.forwards() == "DEFINE BUCKET b PERMISSIONS WHERE whereabouts = true;"

    @pytest.mark.parametrize("name", ["DefineBucket", "AlterBucket", "RemoveBucket", "BucketState"])
    def test_experimental_label(self, name: str) -> None:
        import surreal_orm.migrations as migrations

        doc = getattr(migrations, name).__doc__
        assert doc is not None
        assert "experimental" in doc.lower()
        assert "files" in doc

    def test_define_bucket_docstring_warns_about_full_first(self) -> None:
        from surreal_orm.migrations.operations import DefineBucket

        doc = DefineBucket.__doc__ or ""
        assert "FULL" in doc and "$action" in doc
        first_warning = doc.lower().index("warning")
        assert "full" in doc[first_warning : first_warning + 300].lower()

    def test_exports(self) -> None:
        import surreal_orm
        import surreal_orm.migrations as migrations
        from surreal_orm.migrations import operations, state

        for name in ("DefineBucket", "AlterBucket", "RemoveBucket"):
            assert getattr(surreal_orm, name) is getattr(operations, name)
            assert getattr(migrations, name) is getattr(operations, name)
            assert name in surreal_orm.__all__ and name in migrations.__all__
        assert migrations.BucketState is state.BucketState
        assert "BucketState" in migrations.__all__


class TestBucketState:
    @pytest.mark.parametrize(("left", "right"), [(None, "FULL"), ("full", "FULL"), ("none", " NONE ")])
    def test_permissions_normalised_for_equality(self, left: Any, right: str) -> None:
        from surreal_orm.migrations.state import BucketState

        assert BucketState(name="b", permissions=left) == BucketState(name="b", permissions=right)

    def test_defaults(self) -> None:
        from surreal_orm.migrations.state import BucketState

        state = BucketState(name="b")
        assert (state.backend, state.readonly, state.permissions, state.comment) == (None, False, "FULL", None)

    @pytest.mark.parametrize(
        "changes",
        [
            {"backend": "memory"},
            {"readonly": True},
            {"permissions": "NONE"},
            {"permissions": "$action = 'get'"},
            {"comment": "c"},
            {"name": "other"},
        ],
    )
    def test_every_field_compared(self, changes: dict[str, Any]) -> None:
        from dataclasses import replace

        from surreal_orm.migrations.state import BucketState

        assert BucketState(name="b") != replace(BucketState(name="b"), **changes)

    def test_expression_compared_as_text(self) -> None:
        from surreal_orm.migrations.state import BucketState

        assert BucketState(name="b", permissions="$a = 1") != BucketState(name="b", permissions="$a=1")
        assert BucketState(name="b", permissions="$a = 1") != "b"


# ---------------------------------------------------------------------------
# U33 — a generated migration file with every bucket operation loads (AC8)
# ---------------------------------------------------------------------------


class TestBucketMigrationFile:
    def test_generated_file_executes_and_round_trips(self, tmp_path: Path) -> None:
        import runpy

        from surreal_orm.migrations.generator import MigrationGenerator
        from surreal_orm.migrations.operations import AlterBucket, DefineBucket, RemoveBucket
        from surreal_orm.migrations.state import BucketState

        operations = [
            DefineBucket("fsu_a"),
            DefineBucket("fsu_b", backend="memory", permissions="NONE"),
            AlterBucket.from_bucket_states(
                BucketState(name="fsu_c"),
                BucketState(name="fsu_c", backend="memory", readonly=True, permissions="$action = 'get'", comment="it's"),
            ),
            RemoveBucket("my-b"),
        ]
        path = MigrationGenerator(tmp_path).generate(name="buckets", operations=operations)
        namespace = runpy.run_path(str(path))
        migration = namespace["migration"]
        assert migration.operations == operations
        assert [op.reversible for op in migration.operations] == [True, True, True, False]
        assert migration.operations[2].backwards() == "DEFINE BUCKET OVERWRITE fsu_c PERMISSIONS FULL;"


# ---------------------------------------------------------------------------
# U25 — parse_define_bucket() reads every form INFO FOR DB reports (AC9)
# ---------------------------------------------------------------------------

# What ``INFO FOR DB`` reports under ``buckets``, measured on 3.3.5 and 3.2.6:
# clauses in canonical order, ``PERMISSIONS FULL`` when none was given, a
# comment holding ``'`` in double quotes, the expression re-rendered.
INFO_BUCKETS: dict[str, str] = {
    "fsu-a`b": "DEFINE BUCKET `fsu-a\\`b` READONLY BACKEND 'memory' PERMISSIONS FULL",
    "fsu_p1": (
        "DEFINE BUCKET fsu_p1 BACKEND 'memory' PERMISSIONS WHERE $action INSIDE ['get', 'head'] "
        "OR $auth.role = 'admin' COMMENT \"it's\""
    ),
    "fsu_p3": "DEFINE BUCKET fsu_p3 BACKEND 'memory' PERMISSIONS WHERE $file.comment = 'COMMENT x' COMMENT 'c'",
    "fsu_p5": "DEFINE BUCKET fsu_p5 BACKEND 'memory' PERMISSIONS NONE COMMENT 'back\\\\slash'",
}


def _info_bucket_states() -> dict[str, Any]:
    from surreal_orm.migrations.state import BucketState

    return {
        "fsu-a`b": BucketState(name="fsu-a`b", backend="memory", readonly=True),
        "fsu_p1": BucketState(
            name="fsu_p1",
            backend="memory",
            permissions="$action INSIDE ['get', 'head'] OR $auth.role = 'admin'",
            comment="it's",
        ),
        "fsu_p3": BucketState(name="fsu_p3", backend="memory", permissions="$file.comment = 'COMMENT x'", comment="c"),
        "fsu_p5": BucketState(name="fsu_p5", backend="memory", permissions="NONE", comment="back\\slash"),
    }


class TestParseDefineBucket:
    @pytest.mark.parametrize("name", list(INFO_BUCKETS))
    def test_reads_what_info_for_db_reports(self, name: str) -> None:
        from surreal_orm.migrations.define_parser import parse_define_bucket

        state = parse_define_bucket(INFO_BUCKETS[name])
        expected = _info_bucket_states()[name]
        assert state == expected
        # Exact fields, not only the normalised equality
        assert (state.name, state.backend, state.readonly, state.permissions, state.comment) == (
            expected.name,
            expected.backend,
            expected.readonly,
            expected.permissions,
            expected.comment,
        )

    @pytest.mark.parametrize(
        ("statement", "fields"),
        [
            ("DEFINE BUCKET b", ("b", None, False, "FULL", None)),
            ("DEFINE BUCKET b;", ("b", None, False, "FULL", None)),
            ("define bucket b backend 'memory' permissions none", ("b", "memory", False, "NONE", None)),
            ("DEFINE BUCKET IF NOT EXISTS b BACKEND 'memory'", ("b", "memory", False, "FULL", None)),
            ("DEFINE BUCKET OVERWRITE b READONLY", ("b", None, True, "FULL", None)),
            ("DEFINE BUCKET b BACKEND 'memory' READONLY", ("b", "memory", True, "FULL", None)),
            ("DEFINE BUCKET b COMMENT 'c' BACKEND \"file:/tmp\"", ("b", "file:/tmp", False, "FULL", "c")),
            ("DEFINE BUCKET b PERMISSIONS WHERE $action = 'get'", ("b", None, False, "$action = 'get'", None)),
            (
                "DEFINE BUCKET b PERMISSIONS WHERE $file.key = \"COMMENT 'x'\" COMMENT 'y'",
                ("b", None, False, "$file.key = \"COMMENT 'x'\"", "y"),
            ),
            ("DEFINE BUCKET `a\\\\b` BACKEND 'memory'", ("a\\b", "memory", False, "FULL", None)),
        ],
    )
    def test_other_forms(self, statement: str, fields: tuple[Any, ...]) -> None:
        from surreal_orm.migrations.define_parser import parse_define_bucket

        state = parse_define_bucket(statement)
        assert (state.name, state.backend, state.readonly, state.permissions, state.comment) == fields

    @pytest.mark.parametrize(
        "state_fields",
        [
            {"name": "b"},
            {"name": "b", "backend": "memory", "readonly": True, "permissions": "NONE", "comment": "it's"},
            {"name": "my-b", "backend": "file:/tmp", "permissions": "$action INSIDE ['get', 'head']"},
            {"name": "a`b\\c", "comment": "back\\slash \"q\" 'a'"},
            {"name": "b", "permissions": "$file.comment = 'COMMENT x'", "comment": "COMMENT"},
        ],
    )
    def test_round_trip_through_the_operations(self, state_fields: dict[str, Any]) -> None:
        from surreal_orm.migrations.define_parser import parse_define_bucket
        from surreal_orm.migrations.operations import AlterBucket, DefineBucket
        from surreal_orm.migrations.state import BucketState

        state = BucketState(**state_fields)
        assert parse_define_bucket(DefineBucket.from_bucket_state(state).forwards()) == state
        altered = AlterBucket.from_bucket_states(BucketState(name=state.name), state)
        assert parse_define_bucket(altered.forwards()) == state
        assert parse_define_bucket(altered.backwards()) == BucketState(name=state.name)

    @pytest.mark.parametrize(
        "statement",
        [
            "DEFINE TABLE b",
            "DEFINE BUCKET",
            "DEFINE BUCKET b BACKEND memory",
            "DEFINE BUCKET b PERMISSIONS",
            "DEFINE BUCKET b PERMISSIONS SOME",
            "DEFINE BUCKET b PERMISSIONS WHERE",
            "DEFINE BUCKET b COMMENT",
            "DEFINE BUCKET b SOMETHING 'x'",
            "DEFINE BUCKET b BACKEND 'memory' trailing",
        ],
    )
    def test_unknown_text_raises(self, statement: str) -> None:
        from surreal_orm.migrations.define_parser import parse_define_bucket

        with pytest.raises(ValueError) as excinfo:
            parse_define_bucket(statement)
        assert str(excinfo.value) == f"Not a DEFINE BUCKET statement: {statement!r}"

    def test_exported_next_to_the_other_parsers(self) -> None:
        from surreal_orm import migrations
        from surreal_orm.migrations import define_parser

        assert migrations.parse_define_bucket is define_parser.parse_define_bucket
        assert "parse_define_bucket" in migrations.__all__


# ---------------------------------------------------------------------------
# U26 — SchemaState.diff() handles buckets only when the target describes them (AC9)
# ---------------------------------------------------------------------------


def _bucket_kinds(operations: list[Any]) -> list[tuple[str, str]]:
    from surreal_orm.migrations.operations import AlterBucket, DefineBucket, RemoveBucket

    return [(type(op).__name__, op.name) for op in operations if isinstance(op, (DefineBucket, AlterBucket, RemoveBucket))]


class TestBucketDiff:
    def _state(self, **buckets: Any) -> Any:
        from surreal_orm.migrations.state import SchemaState

        return SchemaState(buckets=dict(buckets))

    def test_add_change_remove(self) -> None:
        from surreal_orm.migrations.operations import AlterBucket, DefineBucket, RemoveBucket, split_destructive
        from surreal_orm.migrations.state import BucketState

        current = self._state(
            kept=BucketState(name="kept", backend="memory"),
            changed=BucketState(name="changed", backend="memory", comment="old"),
            gone=BucketState(name="gone", backend="memory"),
        )
        target = self._state(
            kept=BucketState(name="kept", backend="memory", permissions=None),  # type: ignore[arg-type]
            changed=BucketState(name="changed", backend="memory", permissions="NONE"),
            new=BucketState(name="new", readonly=True),
        )
        operations = current.diff(target)
        assert _bucket_kinds(operations) == [
            ("AlterBucket", "changed"),
            ("DefineBucket", "new"),
            ("RemoveBucket", "gone"),
        ]
        alter = operations[0]
        assert isinstance(alter, AlterBucket)
        assert (alter.permissions, alter.comment, alter.previous_permissions, alter.previous_comment) == (
            "NONE",
            None,
            "FULL",
            "old",
        )
        assert alter.reversible
        assert operations[1] == DefineBucket("new", readonly=True, permissions="FULL")
        keep, destructive = split_destructive(operations)
        assert _bucket_kinds(keep) == [("AlterBucket", "changed"), ("DefineBucket", "new")]
        assert _bucket_kinds(destructive) == [("RemoveBucket", "gone")]
        assert isinstance(destructive[0], RemoveBucket)

    def test_defines_before_tables_removals_last(self) -> None:
        from surreal_orm.migrations.operations import CreateTable, DefineAnalyzer, RemoveAnalyzer, RemoveBucket
        from surreal_orm.migrations.state import AnalyzerState, BucketState, TableState

        current = self._state(old=BucketState(name="old"))
        current.analyzers["az_old"] = AnalyzerState(name="az_old", tokenizers=["blank"], filters=[])
        target = self._state(new=BucketState(name="new"))
        target.analyzers["az_new"] = AnalyzerState(name="az_new", tokenizers=["blank"], filters=[])
        target.tables["fsu_t"] = TableState(name="fsu_t")
        operations = current.diff(target)
        assert _bucket_kinds(operations[:1]) == [("DefineBucket", "new")]
        assert any(isinstance(op, DefineAnalyzer) for op in operations)
        assert any(isinstance(op, CreateTable) for op in operations)
        assert any(isinstance(op, RemoveAnalyzer) for op in operations)
        assert isinstance(operations[-1], RemoveBucket)

    def test_empty_dict_removes_every_bucket(self) -> None:
        from surreal_orm.migrations.state import BucketState

        current = self._state(a=BucketState(name="a"), b=BucketState(name="b"))
        assert _bucket_kinds(current.diff(self._state())) == [("RemoveBucket", "a"), ("RemoveBucket", "b")]

    def test_none_permissions_equal_full(self) -> None:
        from surreal_orm.migrations.state import BucketState

        current = self._state(a=BucketState(name="a", permissions="FULL"))
        target = self._state(a=BucketState(name="a", permissions=None))  # type: ignore[arg-type]
        assert current.diff(target) == []

    def test_self_none_read_as_empty(self) -> None:
        from surreal_orm.migrations.state import BucketState, SchemaState

        assert _bucket_kinds(SchemaState().diff(self._state(a=BucketState(name="a")))) == [("DefineBucket", "a")]

    def test_target_none_yields_no_bucket_operation(self) -> None:
        from surreal_orm.migrations.state import BucketState, SchemaState

        current = self._state(a=BucketState(name="a"), b=BucketState(name="b"))
        assert SchemaState().buckets is None
        assert current.diff(SchemaState()) == []

    @pytest.mark.asyncio
    async def test_model_built_target_never_touches_buckets(self) -> None:
        """The ``makemigrations --drop-missing`` scenario: models never declare buckets."""
        from surreal_orm.migrations.introspector import ModelIntrospector
        from surreal_orm.migrations.operations import split_destructive

        current = await _fake_db_introspection({"fsu_one": INFO_BUCKETS["fsu_p1"], "fsu_two": INFO_BUCKETS["fsu_p5"]})
        assert set(current.buckets) == {"fsu_p1", "fsu_p5"}
        target = ModelIntrospector([FSUColumns]).introspect()
        assert target.buckets is None
        keep, destructive = split_destructive(current.diff(target))
        assert keep  # the table and its columns
        assert _bucket_kinds(keep) == []
        assert _bucket_kinds(destructive) == []

    def test_clone_keeps_none_and_copies_a_dict(self) -> None:
        from surreal_orm.migrations.state import BucketState, SchemaState

        assert SchemaState().clone().buckets is None
        state = self._state(a=BucketState(name="a"))
        copy = state.clone()
        assert copy.buckets == state.buckets
        assert copy.buckets is not state.buckets
        assert copy.buckets is not None and state.buckets is not None
        assert copy.buckets["a"] is not state.buckets["a"]


# ---------------------------------------------------------------------------
# U27 — DatabaseIntrospector reads the buckets of INFO FOR DB (AC9)
# ---------------------------------------------------------------------------


async def _fake_db_introspection(buckets: Any, *, with_key: bool = True) -> Any:
    from surreal_orm.migrations.db_introspector import DatabaseIntrospector

    info: dict[str, Any] = {"tables": {}, "analyzers": {}}
    if with_key:
        info["buckets"] = buckets
    result = MagicMock()
    result.is_ok = True
    result.result = info
    response = MagicMock()
    response.results = [result]
    conn = AsyncMock()
    conn.query.return_value = response
    return await DatabaseIntrospector(connection=conn).introspect()


class TestDatabaseIntrospectorBuckets:
    @pytest.mark.asyncio
    async def test_reads_every_bucket(self) -> None:
        state = await _fake_db_introspection(INFO_BUCKETS)
        assert state.buckets == _info_bucket_states()

    @pytest.mark.asyncio
    async def test_keyed_by_the_parsed_name(self) -> None:
        state = await _fake_db_introspection({"whatever": "DEFINE BUCKET `my-b` BACKEND 'memory' PERMISSIONS FULL"})
        assert list(state.buckets) == ["my-b"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("buckets", [{}, "not a dict"])
    async def test_always_a_dict(self, buckets: Any) -> None:
        assert (await _fake_db_introspection(buckets)).buckets == {}
        assert (await _fake_db_introspection(buckets, with_key=False)).buckets == {}

    @pytest.mark.asyncio
    async def test_empty_database_gives_an_empty_dict(self) -> None:
        from surreal_orm.migrations.db_introspector import DatabaseIntrospector

        response = MagicMock()
        response.results = []
        conn = AsyncMock()
        conn.query.return_value = response
        assert (await DatabaseIntrospector(connection=conn).introspect()).buckets == {}

    @pytest.mark.asyncio
    async def test_unreadable_definition_skipped(self, caplog: pytest.LogCaptureFixture) -> None:
        state = await _fake_db_introspection({"bad": "DEFINE BUCKET bad SOMETHING", "ok": "DEFINE BUCKET ok BACKEND 'memory'"})
        assert list(state.buckets) == ["ok"]
        assert any("bucket" in record.getMessage() for record in caplog.records)

    def test_model_introspector_leaves_none(self) -> None:
        from surreal_orm.migrations.introspector import ModelIntrospector

        assert ModelIntrospector([FSUColumns]).introspect().buckets is None


# ---------------------------------------------------------------------------
# U29 — merge() goes through _merge_columns() (§7.4, AC7)
# ---------------------------------------------------------------------------


class FSUMerge(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="fsu_merge")

    id: str | None = None
    title: str = ""


def _merge_client() -> Any:
    client = AsyncMock()
    client.merge = AsyncMock(return_value=MagicMock(is_empty=False, records=[{"id": "fsu_merge:m1"}]))
    return client


class TestMergeColumns:
    @pytest.mark.asyncio
    async def test_merge_delegates_with_its_keywords(self) -> None:
        doc = FSUMerge(id="m1")
        extra = {"p": 1}
        with patch.object(FSUMerge, "_merge_columns", new=AsyncMock(return_value=doc)) as helper:
            assert await doc.merge(refresh=False, extra_vars=extra, title="t") is doc

        helper.assert_awaited_once_with({"title": "t"}, tx=None, refresh=False, extra_vars=extra)

    @pytest.mark.asyncio
    async def test_merge_defaults_are_forwarded(self) -> None:
        doc = FSUMerge(id="m1")
        with patch.object(FSUMerge, "_merge_columns", new=AsyncMock(return_value=doc)) as helper:
            await doc.merge(title="t")

        helper.assert_awaited_once_with({"title": "t"}, tx=None, refresh=True, extra_vars=None)

    @pytest.mark.asyncio
    async def test_columns_named_like_merge_keywords_reach_the_wire(self) -> None:
        # A file helper writes one column whose name may be "refresh", "tx" or
        # "extra_vars": through _merge_columns() it is data, never a keyword.
        doc = FSUMerge(id="m1")
        client = _merge_client()
        columns = {"refresh": "a", "tx": "b", "extra_vars": "c"}
        with (
            patch(
                "surreal_orm.model_base.SurrealDBConnectionManager",
                new=MagicMock(get_client=AsyncMock(return_value=client)),
            ),
            patch.object(FSUMerge, "refresh", new=AsyncMock()) as refresh,
        ):
            assert await doc._merge_columns(columns, refresh=False) is doc

        thing, data = client.merge.call_args[0]
        assert thing == "fsu_merge:m1"
        assert data == {"refresh": "a", "tx": "b", "extra_vars": "c"}
        refresh.assert_not_awaited()
        assert columns == {"refresh": "a", "tx": "b", "extra_vars": "c"}

    @pytest.mark.asyncio
    async def test_refresh_true_refreshes(self) -> None:
        doc = FSUMerge(id="m1")
        client = _merge_client()
        with (
            patch(
                "surreal_orm.model_base.SurrealDBConnectionManager",
                new=MagicMock(get_client=AsyncMock(return_value=client)),
            ),
            patch.object(FSUMerge, "refresh", new=AsyncMock()) as refresh,
        ):
            await doc._merge_columns({"title": "t"})

        refresh.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_update_signals_carry_the_columns(self) -> None:
        from surreal_orm import post_update, pre_update

        doc = FSUMerge(id="m1")
        client = _merge_client()
        seen: list[tuple[str, dict[str, Any]]] = []

        async def on_pre(sender: Any, instance: Any, update_fields: dict[str, Any], **kwargs: Any) -> None:
            seen.append(("pre", dict(update_fields)))

        async def on_post(sender: Any, instance: Any, update_fields: dict[str, Any], **kwargs: Any) -> None:
            seen.append(("post", dict(update_fields)))

        pre_update.connect(FSUMerge)(on_pre)
        post_update.connect(FSUMerge)(on_post)
        try:
            with patch(
                "surreal_orm.model_base.SurrealDBConnectionManager",
                new=MagicMock(get_client=AsyncMock(return_value=client)),
            ):
                await doc._merge_columns({"refresh": "a"}, refresh=False)
        finally:
            pre_update.disconnect(on_pre, sender=FSUMerge)
            post_update.disconnect(on_post, sender=FSUMerge)

        assert seen == [("pre", {"refresh": "a"}), ("post", {"refresh": "a"})]


# ---------------------------------------------------------------------------
# U28 — model file helpers (§3.5, P2, P7, P10, P14, P16)
# ---------------------------------------------------------------------------


class FSUHelpers(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="fsu_helpers")

    id: str | None = None
    title: str = ""
    doc: FileField["fsu_a", "fsu_b"]  # noqa: F821, UP037
    photo: ImageField["fsu_img"]  # noqa: F821, UP037
    aliased: FileField["fsu_a"] = Field(default=None, alias="doc_col")  # noqa: F821, UP037


class FSUHelpersView(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="fsu_helpers_view", view_query="SELECT * FROM fsu_helpers")

    id: str | None = None
    doc: FileField["fsu_a"]  # noqa: F821, UP037


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 8
GIF87 = b"GIF87a" + b"\x00" * 8
GIF89 = b"GIF89a" + b"\x00" * 8
WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 "


def _file_client(kind: type[Any], limit: int | None = None) -> Any:
    """A real, unconnected connection whose file methods and merge are mocks."""
    from surreal_sdk import HTTPConnection

    url = "http://localhost:8000" if kind is HTTPConnection else "ws://localhost:8000"
    client = kind(url, "ns", "db")
    if limit is not None:
        client.max_request_size = limit
    client.file_put = AsyncMock(return_value=None)
    client.file_exists = AsyncMock(return_value=False)
    client.file_get = AsyncMock(return_value=b"content")
    client.file_head = AsyncMock(return_value=None)
    client.file_delete = AsyncMock(return_value=None)
    client.merge = AsyncMock(return_value=MagicMock(is_empty=False, records=[{"id": "fsu_helpers:h1"}]))
    return client


class _Clients:
    """The manager patched with one HTTP and one WebSocket fake client."""

    def __init__(self, http_limit: int | None = None) -> None:
        from surreal_sdk import HTTPConnection, WebSocketConnection

        self.http = _file_client(HTTPConnection, http_limit)
        self.ws = _file_client(WebSocketConnection)
        self.manager = MagicMock(
            get_client=AsyncMock(return_value=self.http),
            get_ws_client=AsyncMock(return_value=self.ws),
            # No using() override: get_connection_name() falls back to the model's.
            get_active_connection_name=MagicMock(return_value=None),
        )
        self._patch = patch("surreal_orm.model_base.SurrealDBConnectionManager", new=self.manager)

    def __enter__(self) -> _Clients:
        self._patch.__enter__()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._patch.__exit__(*exc)


class _UpdateSignals:
    """Record the update signals FSUHelpers sends while active."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, dict[str, Any]]] = []

    async def _pre(self, sender: Any, instance: Any, update_fields: dict[str, Any], **kwargs: Any) -> None:
        self.seen.append(("pre", dict(update_fields)))

    async def _post(self, sender: Any, instance: Any, update_fields: dict[str, Any], **kwargs: Any) -> None:
        self.seen.append(("post", dict(update_fields)))

    def __enter__(self) -> _UpdateSignals:
        from surreal_orm import post_update, pre_update

        pre_update.connect(FSUHelpers)(self._pre)
        post_update.connect(FSUHelpers)(self._post)
        return self

    def __exit__(self, *exc: Any) -> None:
        from surreal_orm import post_update, pre_update

        pre_update.disconnect(self._pre, sender=FSUHelpers)
        post_update.disconnect(self._post, sender=FSUHelpers)


HELPER_CALLS: list[tuple[str, tuple[Any, ...]]] = [
    ("upload_file", (b"x",)),
    ("download_file", ()),
    ("delete_file", ()),
    ("file_info", ()),
]


class TestFileHelperChecks:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(("method", "args"), HELPER_CALLS, ids=[m for m, _ in HELPER_CALLS])
    @pytest.mark.parametrize("field", ["title", "nope", "doc_col"])
    async def test_not_a_file_field(self, method: str, args: tuple[Any, ...], field: str) -> None:
        # E15; the alias is a column name, not the Python field name the helpers take.
        doc = FSUHelpers(id="h1")
        with _Clients() as clients:
            with pytest.raises(ValueError) as info:
                await getattr(doc, method)(field, *args)
        assert str(info.value) == f"FSUHelpers.{field} is not a FileField or ImageField"
        clients.manager.get_client.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_upload_without_id(self) -> None:
        with _Clients() as clients:
            with pytest.raises(ValueError) as info:
                await FSUHelpers().upload_file("doc", b"x")
        assert str(info.value) == "FSUHelpers instance has no id: save it before upload_file()"
        clients.http.file_put.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_delete_without_id(self) -> None:
        doc = FSUHelpers(doc="fsu_a:/x")
        with _Clients() as clients:
            with pytest.raises(ValueError) as info:
                await doc.delete_file("doc")
        assert str(info.value) == "FSUHelpers instance has no id: save it before delete_file()"
        clients.http.file_delete.assert_not_awaited()
        assert doc.doc == FileRef("fsu_a", "/x")

    @pytest.mark.asyncio
    async def test_reads_need_no_id(self) -> None:
        doc = FSUHelpers(doc="fsu_a:/x")
        with _Clients() as clients:
            assert await doc.download_file("doc") == b"content"
            assert await doc.file_info("doc") is None
        clients.http.file_get.assert_awaited_once_with(FileRef("fsu_a", "/x"))
        clients.http.file_head.assert_awaited_once_with(FileRef("fsu_a", "/x"))

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", ["upload_file", "delete_file"])
    async def test_view_refuses_writes(self, method: str) -> None:
        doc = FSUHelpersView(id="v1", doc="fsu_a:/x")
        args: tuple[Any, ...] = (b"x",) if method == "upload_file" else ()
        with _Clients() as clients:
            with pytest.raises(TypeError, match="Cannot modify materialized view 'fsu_helpers_view'"):
                await getattr(doc, method)("doc", *args)
        clients.manager.get_client.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_upload_data_must_be_bytes(self) -> None:
        with _Clients() as clients:
            with pytest.raises(TypeError) as info:
                await FSUHelpers(id="h1").upload_file("photo", "text")  # type: ignore[arg-type]
        assert str(info.value) == "File data must be bytes, bytearray or memoryview, got str"
        clients.manager.get_client.assert_not_awaited()


class TestUploadFile:
    @pytest.mark.asyncio
    async def test_default_key_first_bucket(self) -> None:
        doc = FSUHelpers(id="h1")
        with _Clients() as clients, _UpdateSignals() as signals:
            ref = await doc.upload_file("doc", b"hello")

        assert ref == FileRef("fsu_a", "/fsu_helpers/h1/doc")
        clients.http.file_put.assert_awaited_once_with(ref, b"hello", overwrite=True)
        clients.http.file_exists.assert_not_awaited()
        clients.manager.get_ws_client.assert_not_awaited()
        thing, data = clients.http.merge.call_args[0]
        assert (thing, data) == ("fsu_helpers:h1", {"doc": ref})
        assert doc.doc == ref
        assert signals.seen == [("pre", {"doc": ref}), ("post", {"doc": ref})]

    @pytest.mark.asyncio
    async def test_merge_is_column_keyed_without_refresh(self) -> None:
        doc = FSUHelpers(id="h1")
        with _Clients(), patch.object(FSUHelpers, "_merge_columns", new=AsyncMock(return_value=doc)) as merge:
            ref = await doc.upload_file("aliased", b"hello")

        assert ref == FileRef("fsu_a", "/fsu_helpers/h1/doc_col")
        merge.assert_awaited_once_with({"doc_col": ref}, refresh=False)
        assert doc.aliased == ref

    @pytest.mark.asyncio
    async def test_explicit_key_and_bucket(self) -> None:
        doc = FSUHelpers(id="h1")
        with _Clients() as clients:
            ref = await doc.upload_file("doc", bytearray(b"hi"), key="reports/q1.pdf", bucket="fsu_b")

        assert ref == FileRef("fsu_b", "/reports/q1.pdf")
        clients.http.file_put.assert_awaited_once_with(ref, bytearray(b"hi"), overwrite=True)
        assert doc.doc == ref

    @pytest.mark.asyncio
    async def test_bucket_not_allowed(self) -> None:
        doc = FSUHelpers(id="h1")
        with _Clients() as clients:
            with pytest.raises(ValueError) as info:
                await doc.upload_file("doc", b"x", bucket="fsu_img")
        assert str(info.value) == "File pointer fsu_img:/fsu_helpers/h1/doc is not in the allowed bucket(s) fsu_a, fsu_b"
        clients.manager.get_client.assert_not_awaited()
        assert doc.doc is None

    @pytest.mark.asyncio
    async def test_id_with_a_dot_dot_segment_is_refused(self) -> None:
        # The id lands in the default key; FileRef refuses a ".." segment, so
        # a crafted id can never point the upload at another record's file.
        with _Clients() as clients:
            with pytest.raises(ValueError, match=r"Invalid file key '/fsu_helpers/\.\./doc'"):
                await FSUHelpers(id="..").upload_file("doc", b"x")
        clients.manager.get_client.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("data", [PNG, JPEG, GIF87, GIF89, WEBP], ids=["png", "jpeg", "gif87a", "gif89a", "webp"])
    async def test_image_signatures_accepted(self, data: bytes) -> None:
        doc = FSUHelpers(id="h1")
        with _Clients() as clients:
            ref = await doc.upload_file("photo", memoryview(data))
        assert ref == FileRef("fsu_img", "/fsu_helpers/h1/photo")
        clients.http.file_put.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "data",
        [b"plain text", b"RIFF\x10\x00\x00\x00WAVEfmt ", b"\x89PNG", b""],
        ids=["text", "riff-wave", "short-png", "empty"],
    )
    async def test_image_signature_rejected(self, data: bytes) -> None:
        doc = FSUHelpers(id="h1")
        with _Clients() as clients:
            with pytest.raises(ValueError) as info:
                await doc.upload_file("photo", data)
        assert str(info.value) == "FSUHelpers.photo accepts PNG, JPEG, GIF or WebP data only"
        clients.manager.get_client.assert_not_awaited()
        assert doc.photo is None

    @pytest.mark.asyncio
    async def test_upload_within_the_http_limit_uses_http(self) -> None:
        # Estimate = data + bucket + key + 512: 100 + 5 + 19 + 512 = 636.
        doc = FSUHelpers(id="h1")
        with _Clients(http_limit=636) as clients:
            ref = await doc.upload_file("doc", b"x" * 100)
        clients.http.file_put.assert_awaited_once_with(ref, b"x" * 100, overwrite=True)
        clients.manager.get_ws_client.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_upload_over_the_http_limit_uses_websocket(self) -> None:
        doc = FSUHelpers(id="h1")
        with _Clients(http_limit=635) as clients:
            ref = await doc.upload_file("doc", b"x" * 100)
        clients.manager.get_client.assert_awaited_with("default")
        clients.manager.get_ws_client.assert_awaited_once_with("default")
        clients.ws.file_put.assert_awaited_once_with(ref, b"x" * 100, overwrite=True)
        clients.http.file_put.assert_not_awaited()
        # The record update goes through the model's usual (HTTP) client.
        clients.http.merge.assert_awaited_once()
        assert doc.doc == ref

    @pytest.mark.asyncio
    async def test_websocket_uses_the_model_connection_name(self) -> None:
        class FSUHelpersRouted(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="fsu_helpers_routed", connection="files_conn")

            id: str | None = None
            doc: FileField["fsu_a"]  # noqa: F821, UP037

        with _Clients(http_limit=1) as clients:
            await FSUHelpersRouted(id="r1").upload_file("doc", b"x")
        clients.manager.get_ws_client.assert_awaited_once_with("files_conn")

    @pytest.mark.asyncio
    async def test_failed_record_update_keeps_the_file(self) -> None:
        # File writes are not transactional: the helper does not delete what
        # it wrote, since the key may be the one the record already points to.
        from surreal_orm.model_base import SurrealDbError

        old = FileRef("fsu_a", "/old")
        doc = FSUHelpers(id="h1", doc=old)
        with _Clients() as clients:
            clients.http.merge.return_value = MagicMock(is_empty=True)
            with pytest.raises(SurrealDbError, match="no record affected"):
                await doc.upload_file("doc", b"x")
        clients.http.file_put.assert_awaited_once()
        clients.http.file_delete.assert_not_awaited()
        assert doc.doc == old

    @pytest.mark.asyncio
    async def test_logger_redacts_the_data(self) -> None:
        from surreal_orm.debug import QueryLogger

        doc = FSUHelpers(id="h1")
        with _Clients():
            async with QueryLogger() as logger:
                ref = await doc.upload_file("doc", b"x" * 1000)

        assert [(q.sql, q.variables) for q in logger.queries] == [
            ("file::put($_file, $_data);", {"_file": ref, "_data": "<1000 bytes>"}),
            ("MERGE fsu_helpers:h1", {"doc": ref}),
        ]


class TestUploadFileOverwriteFalse:
    @pytest.mark.asyncio
    async def test_existing_file_raises_before_any_write(self) -> None:
        old = FileRef("fsu_a", "/old")
        doc = FSUHelpers(id="h1", doc=old)
        with _Clients() as clients, _UpdateSignals() as signals:
            clients.http.file_exists.return_value = True
            with patch.object(FSUHelpers, "_merge_columns", new=AsyncMock()) as merge:
                with pytest.raises(FileExistsError) as info:
                    await doc.upload_file("doc", b"x", overwrite=False)

        assert str(info.value) == "fsu_a:/fsu_helpers/h1/doc already exists: pass overwrite=True or another key="
        clients.http.file_exists.assert_awaited_once_with(FileRef("fsu_a", "/fsu_helpers/h1/doc"))
        clients.http.file_put.assert_not_awaited()
        merge.assert_not_awaited()
        assert signals.seen == []
        assert doc.doc == old

    @pytest.mark.asyncio
    async def test_missing_file_is_put_if_not_exists_then_merged(self) -> None:
        from surreal_orm.debug import QueryLogger

        doc = FSUHelpers(id="h1")
        with _Clients() as clients:
            order = MagicMock()
            order.attach_mock(clients.http.file_exists, "file_exists")
            order.attach_mock(clients.http.file_put, "file_put")
            order.attach_mock(clients.http.merge, "merge")
            async with QueryLogger() as logger:
                ref = await doc.upload_file("doc", b"abc", overwrite=False)

        assert [c[0] for c in order.mock_calls] == ["file_exists", "file_put", "merge"]
        clients.http.file_put.assert_awaited_once_with(ref, b"abc", overwrite=False)
        assert [(q.sql, q.variables) for q in logger.queries] == [
            ("RETURN file::exists($_file);", {"_file": ref}),
            ("file::put_if_not_exists($_file, $_data);", {"_file": ref, "_data": "<3 bytes>"}),
            ("MERGE fsu_helpers:h1", {"doc": ref}),
        ]
        assert doc.doc == ref

    @pytest.mark.asyncio
    async def test_exists_check_runs_on_the_websocket_client_too(self) -> None:
        doc = FSUHelpers(id="h1")
        with _Clients(http_limit=1) as clients:
            clients.ws.file_exists.return_value = True
            with pytest.raises(FileExistsError):
                await doc.upload_file("doc", b"x", overwrite=False)
        clients.ws.file_exists.assert_awaited_once()
        clients.http.file_exists.assert_not_awaited()
        clients.ws.file_put.assert_not_awaited()


class TestDownloadAndInfo:
    @pytest.mark.asyncio
    async def test_download_without_a_pointer(self) -> None:
        with _Clients() as clients:
            with pytest.raises(ValueError) as info:
                await FSUHelpers(id="h1").download_file("doc")
        assert str(info.value) == "FSUHelpers.doc has no file"
        clients.manager.get_client.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_download_of_a_missing_file(self) -> None:
        with _Clients() as clients:
            clients.http.file_get.return_value = None
            with pytest.raises(FileNotFoundError) as info:
                await FSUHelpers(id="h1", doc="fsu_b:/gone.txt").download_file("doc")
        assert str(info.value) == "fsu_b:/gone.txt does not exist"

    @pytest.mark.asyncio
    async def test_download_returns_the_bytes_and_logs(self) -> None:
        from surreal_orm.debug import QueryLogger

        ref = FileRef("fsu_a", "/a.txt")
        with _Clients() as clients:
            async with QueryLogger() as logger:
                assert await FSUHelpers(id="h1", aliased=ref).download_file("aliased") == b"content"
        clients.http.file_get.assert_awaited_once_with(ref)
        assert [(q.sql, q.variables) for q in logger.queries] == [("RETURN file::get($_file);", {"_file": ref})]

    @pytest.mark.asyncio
    async def test_file_info_without_a_pointer(self) -> None:
        with _Clients() as clients:
            assert await FSUHelpers(id="h1").file_info("doc") is None
        clients.manager.get_client.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_file_info_returns_the_head(self) -> None:
        from datetime import UTC, datetime

        from surreal_orm.debug import QueryLogger

        ref = FileRef("fsu_a", "/a.txt")
        head = FileInfo(file=ref, size=7, updated=datetime(2026, 11, 7, tzinfo=UTC))
        with _Clients() as clients:
            clients.http.file_head.return_value = head
            async with QueryLogger() as logger:
                assert await FSUHelpers(id="h1", doc=ref).file_info("doc") == head
        assert [(q.sql, q.variables) for q in logger.queries] == [("RETURN file::head($_file);", {"_file": ref})]


class TestDeleteFile:
    @pytest.mark.asyncio
    async def test_no_pointer_is_a_no_op(self) -> None:
        with _Clients() as clients, _UpdateSignals() as signals:
            await FSUHelpers().delete_file("doc")
        clients.manager.get_client.assert_not_awaited()
        assert signals.seen == []

    @pytest.mark.asyncio
    async def test_file_first_then_field(self) -> None:
        from surreal_orm.debug import QueryLogger

        ref = FileRef("fsu_a", "/x")
        doc = FSUHelpers(id="h1", aliased=ref)
        with _Clients() as clients, _UpdateSignals() as signals:
            order = MagicMock()
            order.attach_mock(clients.http.file_delete, "file_delete")
            order.attach_mock(clients.http.merge, "merge")
            async with QueryLogger() as logger:
                await doc.delete_file("aliased")

        assert [c[0] for c in order.mock_calls] == ["file_delete", "merge"]
        clients.http.file_delete.assert_awaited_once_with(ref)
        assert clients.http.merge.call_args[0] == ("fsu_helpers:h1", {"doc_col": None})
        assert signals.seen == [("pre", {"doc_col": None}), ("post", {"doc_col": None})]
        assert [q.sql for q in logger.queries] == ["file::delete($_file);", "MERGE fsu_helpers:h1"]
        assert doc.aliased is None


# ---------------------------------------------------------------------------
# U34 — experimental label on the public API (P1, AC15)
# ---------------------------------------------------------------------------


def _experimental_objects() -> list[tuple[str, Any]]:
    from surreal_orm.migrations.define_parser import parse_define_bucket
    from surreal_orm.migrations.operations import AlterBucket, DefineBucket, RemoveBucket
    from surreal_orm.migrations.state import BucketState
    from surreal_sdk import FilesNotEnabledError, HTTPConnection, PayloadTooLargeError, WebSocketConnection
    from surreal_sdk.connection.base import BaseSurrealConnection

    objects: list[tuple[str, Any]] = [
        ("FileRef", FileRef),
        ("FileInfo", FileInfo),
        ("FilesNotEnabledError", FilesNotEnabledError),
        ("PayloadTooLargeError", PayloadTooLargeError),
        ("HTTPConnection.__init__ (max_request_size)", HTTPConnection.__init__),
        ("WebSocketConnection.__init__ (max_request_size)", WebSocketConnection.__init__),
        ("FileField", FileField),
        ("ImageField", ImageField),
        ("is_file_field", is_file_field),
        ("is_image_field", is_image_field),
        ("get_file_buckets", get_file_buckets),
        ("DefineBucket", DefineBucket),
        ("AlterBucket", AlterBucket),
        ("RemoveBucket", RemoveBucket),
        ("BucketState", BucketState),
        ("parse_define_bucket", parse_define_bucket),
    ]
    for name in (
        "file_put",
        "file_get",
        "file_head",
        "file_exists",
        "file_delete",
        "file_copy",
        "file_rename",
        "file_list",
        "iter_files",
    ):
        objects.append((f"BaseSurrealConnection.{name}", getattr(BaseSurrealConnection, name)))
    for name in ("upload_file", "download_file", "delete_file", "file_info"):
        objects.append((f"BaseSurrealModel.{name}", getattr(BaseSurrealModel, name, None)))
    return objects


EXPERIMENTAL_OBJECTS = _experimental_objects()


class TestExperimentalLabel:
    @pytest.mark.parametrize("obj", [obj for _, obj in EXPERIMENTAL_OBJECTS], ids=[n for n, _ in EXPERIMENTAL_OBJECTS])
    def test_docstring_says_experimental_files(self, obj: Any) -> None:
        doc = getattr(obj, "__doc__", None) or ""
        assert "experimental" in doc.lower()
        assert "files" in doc
        # L1: the flag the server needs.
        assert "--allow-experimental=files" in doc

    def test_define_bucket_warns_about_full_first(self) -> None:
        # §9 / P3: the FULL default is the first warning of DefineBucket.
        from surreal_orm.migrations.operations import DefineBucket

        doc = DefineBucket.__doc__ or ""
        first_warning = doc.split("Warning:", 1)[1].strip()
        assert first_warning.startswith("Without ``permissions`` the bucket is ``FULL``")
