"""Issue #473: the v2 line's import-time notice announces the end of maintenance.

``import surreal_orm`` on the ``v2`` line (0.21.x, SurrealDB 2.x) emits a
``DeprecationWarning``. Its text used to say the branch would be retired once
SurrealDB-ORM-lite reached v0.40.0; the line now stops being maintained when
SurrealDB-ORM 0.39.0 is released, and SurrealDB 2.x users are pointed to
SurrealDB-ORM-lite. The category and the moment the warning fires do not change.

The import runs in a fresh interpreter: reloading ``surreal_orm`` in the test
process would re-create its classes under the other tests.
"""

import json
import subprocess
import sys

_PROBE = """
import json
import warnings

with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    import surreal_orm  # noqa: F401

print(json.dumps([
    {"category": w.category.__name__, "message": str(w.message)}
    for w in caught
    if "SurrealDB-ORM" in str(w.message)
]))
"""


def _import_notices() -> list[dict[str, str]]:
    """Import ``surreal_orm`` in a fresh interpreter and return its notices."""
    completed = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    notices: list[dict[str, str]] = json.loads(completed.stdout.strip().splitlines()[-1])
    return notices


def test_import_emits_exactly_one_deprecation_notice() -> None:
    notices = _import_notices()
    assert len(notices) == 1
    assert notices[0]["category"] == "DeprecationWarning"


def test_notice_says_the_line_is_no_longer_maintained_after_0_39_0() -> None:
    message = _import_notices()[0]["message"]
    assert "no longer maintained" in message
    assert "0.39.0" in message


def test_notice_points_to_surrealdb_orm_lite() -> None:
    message = _import_notices()[0]["message"]
    assert "SurrealDB-ORM-lite" in message
    assert "https://github.com/EulogySnowfall/SurrealDB-ORM-lite" in message


def test_notice_drops_the_v0_40_0_retirement_threshold() -> None:
    message = _import_notices()[0]["message"]
    assert "0.40.0" not in message
