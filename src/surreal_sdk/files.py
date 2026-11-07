"""
File pointers and file metadata for SurrealDB file storage.

SurrealDB stores files in *buckets* (``DEFINE BUCKET``) and refers to a file
with a pointer, written ``f"bucket:/key"`` in SurrealQL and sent as CBOR
tag 55 ``[bucket, key]``. :class:`FileRef` is that pointer on the Python
side and :class:`FileInfo` the metadata ``file::head()`` / ``file::list()``
return for a file.

Experimental: follows SurrealDB's experimental ``files`` capability.
The server must be started with ``--allow-experimental=files`` (or
``SURREAL_CAPS_ALLOW_EXPERIMENTAL=files``); the official image does not
enable it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

__all__ = ["FileInfo", "FileRef"]

#: How a SurrealQL file literal opens (``f"…"`` / ``f'…'``).
_LITERAL_PREFIXES = ('f"', "f'")


def _bucket_error(bucket: object) -> str:
    return f"Invalid bucket name {bucket!r}: expected a non-empty string without ':', '/', whitespace or control characters"


def _key_error(key: str) -> str:
    return (
        f"Invalid file key {key!r}: it must name a file below the bucket root (no empty path, no '.' or '..' segment, no NUL)"
    )


def _pointer_error(text: object) -> str:
    return f"Invalid file pointer {text!r}: expected 'bucket:/key'"


def _check_bucket(bucket: object) -> str:
    """Return *bucket* if it is a valid bucket name, else raise (``TypeError`` / ``ValueError``)."""
    if not isinstance(bucket, str):
        raise TypeError(_bucket_error(bucket))
    if not bucket or any(ch in ":/" or ch.isspace() or not ch.isprintable() for ch in bucket):
        raise ValueError(_bucket_error(bucket))
    return bucket


def _normalise_key(key: object) -> str:
    """Return *key* with a leading ``/`` (as the server writes it), or raise.

    The ``.`` / ``..`` / NUL rule is the same for every backend: the ``memory``
    backend stores ``/../x`` as a plain key while ``file:`` refuses it, so a
    key accepted here keeps its meaning when a bucket moves to another backend.
    """
    if not isinstance(key, str):
        raise TypeError(f"File key must be a str, got {type(key).__name__}")
    normalised = key if key.startswith("/") else "/" + key
    if normalised == "/" or "\x00" in normalised or any(part in (".", "..") for part in normalised.split("/")):
        raise ValueError(_key_error(key))
    return normalised


@dataclass(frozen=True, slots=True)
class FileRef:
    """A SurrealDB file pointer (``f"bucket:/key"``), sent as CBOR tag 55.

    Experimental: follows SurrealDB's experimental ``files`` capability.
    Using a pointer in a file function needs a server started with
    ``--allow-experimental=files``; binding one needs the CBOR protocol
    (the JSON encoder refuses a ``FileRef``).

    ``bucket`` is a non-empty name without ``:``, ``/``, whitespace or control
    characters; it is always bound, so it need not be an identifier
    (``my-b`` is fine). ``key`` gets a leading ``/`` if it has none, as the
    server does (``FileRef("av", "x.png").key == "/x.png"``); it must not be
    ``/`` alone, contain NUL or have a ``.`` / ``..`` segment. Two spellings
    of one key compare (and hash) equal.

    Raises:
        TypeError: ``bucket`` or ``key`` is not a ``str``.
        ValueError: ``bucket`` or ``key`` is invalid.
    """

    bucket: str
    key: str

    def __post_init__(self) -> None:
        _check_bucket(self.bucket)
        object.__setattr__(self, "key", _normalise_key(self.key))

    @classmethod
    def parse(cls, text: str) -> FileRef:
        """Read ``"bucket:/key"`` (or the literal ``f"bucket:/key"``) into a ``FileRef``.

        The text is split at the first ``:``; the key part must start with
        ``/``, which is how ``str(FileRef)`` and SurrealDB write it, so a
        record id such as ``"users:abc"`` is never read as a pointer. Quotes
        inside a literal are not unescaped. This is the one reader of the
        text form in the codebase.

        Raises:
            ValueError: the text is not a valid pointer (the cause, when the
                bucket or key is rejected, is chained).
        """
        if not isinstance(text, str):
            raise ValueError(_pointer_error(text))
        body = text
        if body[:2] in _LITERAL_PREFIXES:
            if len(body) < 3 or body[-1] != body[1]:
                raise ValueError(_pointer_error(text))
            body = body[2:-1]
        bucket, sep, key = body.partition(":")
        if not sep or not key.startswith("/"):
            raise ValueError(_pointer_error(text))
        try:
            return cls(bucket, key)
        except (TypeError, ValueError) as exc:
            raise ValueError(_pointer_error(text)) from exc

    def with_key(self, key: str) -> FileRef:
        """Return a pointer to *key* in the same bucket (validated like the constructor)."""
        return FileRef(self.bucket, key)

    def __str__(self) -> str:
        """Return ``"bucket:/key"``; ``FileRef.parse(str(ref)) == ref``."""
        return f"{self.bucket}:{self.key}"

    @classmethod
    def _from_wire(cls, bucket: str, key: str) -> FileRef:
        """Build a pointer the server sent, without validation; never raises.

        A key written by other means (``/../x`` on a ``memory`` bucket) must
        still decode, or one such value would fail the whole response.
        """
        ref = object.__new__(cls)
        object.__setattr__(ref, "bucket", bucket)
        object.__setattr__(ref, "key", key)
        return ref


@dataclass(frozen=True, slots=True)
class FileInfo:
    """Metadata of a stored file, as ``file::head()`` and ``file::list()`` return it.

    Experimental: follows SurrealDB's experimental ``files`` capability.
    The server must be started with ``--allow-experimental=files``.

    Attributes:
        file: The file's pointer.
        size: Its size in bytes.
        updated: When it was last written.
    """

    file: FileRef
    size: int
    updated: datetime

    @property
    def bucket(self) -> str:
        """The file's bucket."""
        return self.file.bucket

    @property
    def key(self) -> str:
        """The file's key (with its leading ``/``)."""
        return self.file.key

    @classmethod
    def from_result(cls, value: Mapping[str, Any]) -> FileInfo:
        """Build from one ``file::head()`` / ``file::list()`` entry (``{file, size, updated}``).

        ``file`` may be a ``FileRef`` (CBOR), a ``(bucket, key)`` pair of
        strings, or the pointer text (read with :meth:`FileRef.parse`). Other
        keys are ignored.

        Raises:
            ValueError: *value* is not a mapping, a key is missing, ``file``
                cannot be read, ``size`` is not an ``int`` or ``updated`` is not
                a ``datetime``.
        """
        if not isinstance(value, Mapping):
            raise ValueError(f"Invalid file metadata {value!r}: expected a mapping with file, size and updated")
        missing = [name for name in ("file", "size", "updated") if name not in value]
        if missing:
            raise ValueError(f"Invalid file metadata {value!r}: missing {', '.join(missing)}")
        raw_file, size, updated = value["file"], value["size"], value["updated"]
        file: FileRef
        if isinstance(raw_file, FileRef):
            file = raw_file
        elif isinstance(raw_file, (list, tuple)) and len(raw_file) == 2 and all(isinstance(part, str) for part in raw_file):
            file = FileRef._from_wire(raw_file[0], raw_file[1])
        elif isinstance(raw_file, str):
            file = FileRef.parse(raw_file)
        else:
            raise ValueError(f"Invalid file metadata {value!r}: file is not a file pointer")
        if not isinstance(size, int) or isinstance(size, bool):
            raise ValueError(f"Invalid file metadata {value!r}: size must be an int")
        if not isinstance(updated, datetime):
            raise ValueError(f"Invalid file metadata {value!r}: updated must be a datetime")
        return cls(file=file, size=size, updated=updated)
