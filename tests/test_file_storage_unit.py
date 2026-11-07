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

import pytest
from pydantic import ValidationError
from pydantic.warnings import UnsupportedFieldAttributeWarning

from surreal_orm import BaseSurrealModel, FileField, FileInfo, FileRef, ImageField, SurrealConfigDict
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
