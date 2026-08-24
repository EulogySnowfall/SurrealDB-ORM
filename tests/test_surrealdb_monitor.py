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

yaml = pytest.importorskip("yaml", reason="pyyaml required for workflow lint tests")

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


@pytest.mark.parametrize("workflow", MONITORS)
class TestNoPrerelease:
    """Regression tests for #163.

    The v3 monitor's release query deliberately included pre-releases, left over
    from the 3.0 alpha migration. Once 3.3.0 entered beta it pinned
    ``3.3.0-beta.3`` as the version the library declares support for and runs CI
    against — and because a pin bump auto-merges into a version bump, it burned
    three PyPI releases (0.32.3-0.32.5) on a beta database.

    ``sort -V`` makes it worse rather than catching it: it ranks
    ``3.3.0-beta.3`` *above* ``3.3.0``, so a beta pin refuses every subsequent
    stable release as a "downgrade" and stays stuck forever.
    """

    @pytest.mark.parametrize(
        ("current", "latest"),
        [
            ("3.2.4", "3.3.0-beta.3"),
            ("3.2.4", "3.3.0-rc.1"),
            ("2.6.5", "2.7.0-beta.1"),
        ],
    )
    def test_prerelease_candidate_is_rejected(self, workflow: str, current: str, latest: str) -> None:
        assert _decide(workflow, current, latest) == "false", (
            f"{workflow}: {latest} is a pre-release and must never be pinned (#163)"
        )

    @pytest.mark.parametrize(
        ("current", "latest"),
        [
            # The state #163 left main in: a beta pin must not strand the repo.
            ("3.3.0-beta.3", "3.2.4"),
            ("3.3.0-beta.3", "3.3.0"),
        ],
    )
    def test_prerelease_pin_is_replaced_by_a_stable_release(self, workflow: str, current: str, latest: str) -> None:
        assert _decide(workflow, current, latest) == "true", (
            f"{workflow}: a stable release must supersede the pre-release pin {current} (#163) — "
            "`sort -V` alone ranks the pre-release higher and would stay stuck"
        )


def test_v3_release_query_excludes_prereleases() -> None:
    """The v3 monitor must filter pre-releases at the source, like the v2 one (#163)."""
    text = (WORKFLOW_DIR / "surrealdb-security.yml").read_text(encoding="utf-8")
    query = next(line for line in text.splitlines() if 'startswith("v3.")' in line)
    assert "select(.prerelease == false)" in query, "the v3 release query must exclude pre-releases (#163)"
    assert "select(.draft == false)" in query, "the v3 release query must exclude drafts"


def test_pinned_version_is_not_a_prerelease() -> None:
    """`.surrealdb-version` must name a stable release (#163)."""
    pin = (WORKFLOW_DIR.parent.parent / ".surrealdb-version").read_text(encoding="utf-8").strip()
    assert "-" not in pin, f"the pinned SurrealDB version {pin!r} is a pre-release"


CANARY = "surrealdb-prerelease-canary.yml"


class TestPrereleaseCanary:
    """The canary tests pre-releases; it must never adopt one.

    Testing a beta and *pinning* a beta are different acts. `.surrealdb-version`
    is the version the library declares support for, and a bump to it cascades
    into a published PyPI release — that is what burned 0.32.3-0.32.5 (#163).
    The canary exists to give early warning on an upcoming SurrealDB line
    without touching either.
    """

    def _text(self) -> str:
        return (WORKFLOW_DIR / CANARY).read_text(encoding="utf-8")

    def test_canary_exists(self) -> None:
        assert (WORKFLOW_DIR / CANARY).is_file(), f"{CANARY} not found"

    def test_it_cannot_write_to_the_repository(self) -> None:
        """`contents: read` is the structural guarantee, not the step bodies."""
        data = yaml.safe_load(self._text())
        assert data["permissions"]["contents"] == "read", (
            "the canary must not be able to push — testing a pre-release is not adopting it (#163)"
        )
        assert "pull-requests" not in data["permissions"], "the canary must not open PRs (#163)"

    def test_it_never_touches_the_pin_or_the_compose_file(self) -> None:
        text = self._text()
        for forbidden in ("> .surrealdb-version", ">> .surrealdb-version", "sed -i"):
            assert forbidden not in text, f"the canary must never rewrite the pin ({forbidden!r} found)"
        assert "gh pr create" not in text, "the canary must never open a PR"

    def test_it_only_considers_pre_releases(self) -> None:
        text = self._text()
        query = next(line for line in text.splitlines() if 'startswith("v3.")' in line)
        assert "select(.prerelease == true)" in query, "the canary is for pre-releases only"
        assert "select(.draft == false)" in query, "drafts are not testable releases"

    def test_pytest_steps_disable_coverage(self) -> None:
        """A coverage gate must never be able to masquerade as a broken database.

        ``--cov-fail-under=30`` lives in pyproject addopts and a partial selection
        never reaches it — the integration selection alone measures ~28%. The
        canary's first live run failed exactly this way: ``435 passed`` followed
        by a coverage error, which the workflow would have reported as SurrealDB
        3.3.0-beta.3 breaking the suite.
        """
        for line in self._text().splitlines():
            if "uv run pytest" in line:
                assert "--no-cov" in line, f"canary pytest step must disable coverage: {line.strip()!r}"

    def test_the_reporting_job_can_reach_the_api_without_a_checkout(self) -> None:
        """`gh` resolves the repo from git; that job has no checkout (#175 follow-up)."""
        data = yaml.safe_load(self._text())
        step = next(s for s in data["jobs"]["report-failure"]["steps"] if "gh issue" in str(s.get("run", "")))
        assert "GH_REPO" in step["env"], "without GH_REPO, gh dies with 'not a git repository' before filing anything"

    def test_a_stable_candidate_is_refused(self) -> None:
        """The inverse mistake: the monitor owns stable releases, not this job."""
        assert 'if [[ "$VERSION" != *-* ]]; then' in self._text(), (
            "the canary must refuse a stable candidate — otherwise a manual dispatch would silently duplicate the monitor's job"
        )


_CANARY_START = "# --- Decide:"
_CANARY_END = 'echo "version=$VERSION"'


def _canary_decide(version: str, pin: str) -> str:
    """Run the canary's own selection block and return the FOUND it computes."""
    text = (WORKFLOW_DIR / CANARY).read_text(encoding="utf-8")
    start = text.index(_CANARY_START)
    end = text.index(_CANARY_END, start)
    block = textwrap.dedent(text[start:end])
    script = f"""
        set -euo pipefail
        VERSION={version!r}
        PIN={pin!r}
        {block}
        echo "$FOUND"
    """
    result = subprocess.run(
        ["bash", "-c", textwrap.dedent(script)],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": "/usr/bin:/bin", "GITHUB_OUTPUT": "/dev/null"},
    )
    return result.stdout.strip().splitlines()[-1]


@pytest.mark.parametrize(
    ("version", "pin", "expected"),
    [
        # The case that motivated the canary: a beta of the next minor line.
        ("3.3.0-beta.3", "3.2.4", "true"),
        ("3.3.0-rc.1", "3.2.4", "true"),
        # Numeric, not lexical — "3.10.0" is ahead of "3.9.0" despite sorting below.
        ("3.10.0-beta.1", "3.9.0", "true"),
        # A pre-release of the pinned version teaches nothing.
        ("3.2.4-beta.1", "3.2.4", "false"),
        # Behind the pin.
        ("3.1.0-beta.1", "3.2.4", "false"),
        # Stable releases belong to the monitor, not here.
        ("3.3.0", "3.2.4", "false"),
        # No open pre-release.
        ("", "3.2.4", "false"),
    ],
)
def test_canary_selection(version: str, pin: str, expected: str) -> None:
    assert _canary_decide(version, pin) == expected, f"canary: version={version!r} pin={pin!r} should yield found={expected}"
