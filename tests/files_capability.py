"""Skip-or-fail decision for the file storage integration tests.

Not a test module. SurrealDB 3.2 and 3.3 keep file storage behind
``--allow-experimental=files``. The integration tests probe the capability
once per module; without it they skip, unless ``SORM_REQUIRE_FILES=1`` (CI,
the SurrealDB monitors, the team's test runner) asks them to fail, so a
missing flag can never turn the file tests into a green skip there.

The decision is a pure function of the probe's error text, unit-tested
without a server (``tests/test_file_storage_unit.py``).
"""

from __future__ import annotations

import os
from typing import Literal

from surreal_sdk import FilesNotEnabledError

FILES_SKIP_REASON = "SurrealDB was started without --allow-experimental=files"


def files_required() -> bool:
    """Whether ``SORM_REQUIRE_FILES=1`` asks a missing capability to fail the tests."""
    return os.getenv("SORM_REQUIRE_FILES", "") == "1"


def files_capability_outcome(error_text: str | None, require: bool) -> Literal["run", "skip", "fail"]:
    """Decide what the file tests do after the capability probe.

    Args:
        error_text: The probe's error message, or ``None`` when it succeeded.
        require: ``True`` when a missing capability must fail the tests
            (``SORM_REQUIRE_FILES=1``) instead of skipping them.

    Returns:
        ``"run"`` when the probe succeeded; ``"skip"`` or ``"fail"`` (per
        ``require``) when the server said file storage is not enabled.

    Raises:
        RuntimeError: The probe failed for another reason. That is a real
            failure (authentication, connection, a server change), never a skip.
    """
    if error_text is None:
        return "run"
    if not FilesNotEnabledError.is_files_not_enabled(error_text):
        raise RuntimeError(f"File storage capability probe failed: {error_text}")
    return "fail" if require else "skip"
