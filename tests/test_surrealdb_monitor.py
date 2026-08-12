"""Tests for the SurrealDB version monitors' update decision.

Regression for #155: the monitors decided "a new version is available" with a
plain string inequality::

    if [[ "$CURRENT_VERSION" != "$LATEST_VERSION" ]]; then HAS_UPDATE="true"; fi

"different" is not "newer". SurrealDB shipped 3.2.1-3.2.3 as image tags without
matching GitHub Release entries, so the newest v3.x *release* read ``3.2.0``
while ``.surrealdb-version`` already read ``3.2.3`` — and the monitor opened a PR
*downgrading* the pin. A lexical compare is also wrong across digit widths
(``2.10.0`` sorts below ``2.6.5`` as a string).

The guard is exercised here by extracting the real shell block from the workflow
and running it, so these tests fail if the block is edited into something that
no longer compares versions.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

WORKFLOW_DIR = Path(__file__).resolve().parent.parent / ".github" / "workflows"

MONITORS = ["surrealdb-security.yml", "surrealdb-v2-security.yml"]

_BLOCK_START = "# --- Compare:"
_BLOCK_END = "# Force update overrides the check"


def _compare_block(workflow: str) -> str:
    """Extract the version-comparison shell block from a monitor workflow."""
    text = (WORKFLOW_DIR / workflow).read_text(encoding="utf-8")
    start = text.index(_BLOCK_START)
    end = text.index(_BLOCK_END, start)
    return textwrap.dedent(text[start:end])


def _decide(workflow: str, current: str, latest: str) -> str:
    """Run the extracted block and return the HAS_UPDATE it computes."""
    script = f"""
        set -euo pipefail
        CURRENT_VERSION={current!r}
        LATEST_VERSION={latest!r}
        {_compare_block(workflow)}
        echo "$HAS_UPDATE"
    """
    result = subprocess.run(
        ["bash", "-c", textwrap.dedent(script)],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip().splitlines()[-1]


@pytest.mark.parametrize("workflow", MONITORS)
class TestNoDowngrade:
    """Regression tests for #155."""

    def test_block_is_extractable(self, workflow: str) -> None:
        block = _compare_block(workflow)
        assert "HAS_UPDATE" in block, f"{workflow}: comparison block not found"

    def test_block_compares_versions_not_strings(self, workflow: str) -> None:
        assert "sort -V" in _compare_block(workflow), (
            f"{workflow}: the update decision must compare versions, not strings — "
            "a bare `!=` treats an older release as an update and downgrades the pin (#155)"
        )

    @pytest.mark.parametrize(
        ("current", "latest", "expected"),
        [
            # The exact #155 scenario: GitHub Releases lag the published image tags.
            ("3.2.3", "3.2.0", "false"),
            ("2.6.5", "2.6.4", "false"),
            # Equal — nothing to do.
            ("3.2.3", "3.2.3", "false"),
            # Genuine upgrades still fire.
            ("3.2.0", "3.2.3", "true"),
            ("3.1.5", "3.2.0", "true"),
            # Numeric, not lexical: "2.10.0" > "2.6.5" despite sorting below as a string.
            ("2.6.5", "2.10.0", "true"),
        ],
    )
    def test_update_decision(self, workflow: str, current: str, latest: str, expected: str) -> None:
        assert _decide(workflow, current, latest) == expected, (
            f"{workflow}: current={current} latest={latest} should yield HAS_UPDATE={expected}"
        )
