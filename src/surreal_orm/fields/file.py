"""
File pointer fields for SurrealDB file storage.

Experimental: follows SurrealDB's experimental ``files`` capability. The
server must be started with ``--allow-experimental=files`` (or
``SURREAL_CAPS_ALLOW_EXPERIMENTAL=files``) for the files themselves to be
stored and read; the fields only hold pointers.

A file field holds a :class:`~surreal_sdk.files.FileRef` (``f"bucket:/key"``
in SurrealQL, CBOR tag 55 on the wire) restricted to the buckets named in the
subscript::

    from surreal_orm.fields import FileField, ImageField

    class Document(BaseSurrealModel):
        attachment: FileField["documents"]
        # → DEFINE FIELD attachment ON document TYPE option<file<documents>>;
        scan: FileField["documents", "archive"]
        # → TYPE option<file<documents | archive>>
        avatar: ImageField["avatars"]
        # → TYPE option<file<avatars>>; uploads must be PNG, JPEG, GIF or WebP

The bare form is optional with a default of ``None``. A model may also
annotate ``FileRef | None`` without a marker: any bucket, column type
``option<file>``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any

from pydantic import Field, GetCoreSchemaHandler
from pydantic_core import CoreSchema, PydanticCustomError, core_schema

from surreal_sdk.files import FileRef

from ..utils import validate_identifier
from .relation import _find_marker


def _bucket_not_allowed(ref: FileRef, buckets: tuple[str, ...]) -> str:
    """Text of the error for a pointer outside the field's buckets (E13)."""
    return f"File pointer {ref} is not in the allowed bucket(s) {', '.join(buckets)}"


@dataclass(frozen=True, slots=True)
class _FileMarker:
    """
    Pydantic-compatible marker for a file pointer field.

    Stored in ``Annotated[FileRef | None, _FileMarker(buckets, image), Field(default=None)]``.
    ``buckets`` are the bucket names the field accepts, in the order written
    (the first is where an upload goes by default); ``image`` is ``True`` for
    :class:`ImageField`, whose uploads must carry an image signature.
    """

    buckets: tuple[str, ...]
    image: bool = False

    def _check_bucket(self, value: FileRef | None) -> FileRef | None:
        if value is not None and value.bucket not in self.buckets:
            # Through the context, not the template: a key may hold "{".
            raise PydanticCustomError("file_bucket", "{message}", {"message": _bucket_not_allowed(value, self.buckets)})
        return value

    def __get_pydantic_core_schema__(
        self,
        source_type: Any,
        handler: GetCoreSchemaHandler,
    ) -> CoreSchema:
        """``FileRef | None`` as :class:`FileRef` validates it, then the bucket check (E13)."""
        return core_schema.no_info_after_validator_function(self._check_bucket, handler(source_type))


def _file_annotation(kind: str, buckets: str | tuple[str, ...], *, image: bool) -> Any:
    names = buckets if isinstance(buckets, tuple) else (buckets,)
    if not names:
        raise TypeError(f'{kind} needs at least one bucket name: {kind}["bucket"]')
    for name in names:
        # The names are written into DDL as file<...>.
        validate_identifier(name, "bucket name")
    return Annotated[FileRef | None, _FileMarker(names, image=image), Field(default=None)]


class FileField:
    """
    A field holding a pointer to a file in one of the named buckets.

    Experimental: follows SurrealDB's experimental ``files`` capability; the
    server needs ``--allow-experimental=files``.

    ``FileField["avatars"]`` resolves to
    ``Annotated[FileRef | None, _FileMarker(("avatars",)), Field(default=None)]``
    and maps to the column type ``option<file<avatars>>``;
    ``FileField["a", "b"]`` accepts either bucket (``file<a | b>``). Each
    bucket name must be an identifier (``ValueError`` otherwise), and at least
    one is required (``TypeError``).

    The field accepts a :class:`~surreal_sdk.files.FileRef` or its
    ``"bucket:/key"`` text (read with :meth:`FileRef.parse`), refuses a
    pointer to another bucket, and keeps the ``FileRef`` in a Python-mode
    dump so the CBOR protocol sends it as a file pointer; a JSON-mode dump
    gives the text.

    **Default.** The bare form (``avatar: FileField["avatars"]``) is optional
    and defaults to ``None``: the default sits in the field metadata, so
    ``model_construct()`` (used for live ``DELETE`` events) gives ``None`` too.
    The union forms ``FileField["avatars"] | None`` and
    ``Optional[FileField["avatars"]]`` need an explicit ``= None``, because
    Pydantic does not lift metadata, the default included, out of a union
    member; without it the field is required. Pydantic also emits an
    ``UnsupportedFieldAttributeWarning`` for the union forms (the metadata
    default is ignored there, the ``= None`` applies), so prefer the bare form.

    Example::

        class User(BaseSurrealModel):
            avatar: FileField["avatars"]

        user = User(avatar="avatars:/users/alice.png")
        user.avatar  # FileRef(bucket='avatars', key='/users/alice.png')
    """

    def __class_getitem__(cls, buckets: str | tuple[str, ...]) -> Any:
        """``FileField["bucket"]`` or ``FileField["bucket_a", "bucket_b"]``."""
        return _file_annotation("FileField", buckets, image=False)


class ImageField:
    """
    A :class:`FileField` whose uploads must be images.

    Experimental: follows SurrealDB's experimental ``files`` capability; the
    server needs ``--allow-experimental=files``.

    Same pointer, buckets, column type and default rules as :class:`FileField`
    (the union forms need ``= None``). The image check applies to the data
    given to ``upload_file()``, which must start with a PNG, JPEG, GIF or WebP
    signature; the pointer itself is validated like any file pointer.

    Example::

        class User(BaseSurrealModel):
            photo: ImageField["avatars"]
    """

    def __class_getitem__(cls, buckets: str | tuple[str, ...]) -> Any:
        """``ImageField["bucket"]`` or ``ImageField["bucket_a", "bucket_b"]``."""
        return _file_annotation("ImageField", buckets, image=True)


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------


def _get_file_marker(field_type: Any) -> _FileMarker | None:
    """The file marker of *field_type*, union members included."""
    return _find_marker(field_type, (), _FileMarker)


def is_file_field(field_type: Any) -> bool:
    """
    Whether *field_type* is a :class:`FileField` or :class:`ImageField` (also inside ``| None``).

    Experimental: follows SurrealDB's experimental ``files`` capability; the
    server needs ``--allow-experimental=files`` for the files themselves.
    """
    return _get_file_marker(field_type) is not None


def is_image_field(field_type: Any) -> bool:
    """
    Whether *field_type* is an :class:`ImageField` (also inside ``| None``).

    Experimental: follows SurrealDB's experimental ``files`` capability; the
    server needs ``--allow-experimental=files`` for the files themselves.
    """
    marker = _get_file_marker(field_type)
    return marker is not None and marker.image


def get_file_buckets(field_type: Any) -> tuple[str, ...] | None:
    """
    The bucket names a file field accepts, in the order written.

    Experimental: follows SurrealDB's experimental ``files`` capability; the
    server needs ``--allow-experimental=files`` for the files themselves.

    Returns:
        ``("avatars",)`` for ``FileField["avatars"]``, or ``None`` when
        *field_type* is not a file field (a bare ``FileRef`` included).
    """
    marker = _get_file_marker(field_type)
    return marker.buckets if marker is not None else None


__all__ = [
    "FileField",
    "ImageField",
    "get_file_buckets",
    "is_file_field",
    "is_image_field",
]
