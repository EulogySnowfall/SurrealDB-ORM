"""The ``CI Success`` check must run on a pull request that only touches docs.

``CI Success`` is a required status check on ``v2``, but ``ci.yml`` filters its
``pull_request`` trigger by path. A PR whose paths all fall outside the filter
never starts the workflow, so the check stays "Expected" and the PR cannot
merge (#252, PR #516). These tests read the filter from the workflow file and
check that docs-only changes are covered.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CI_WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"


def _pull_request_paths() -> list[str]:
    """Return the ``on.pull_request.paths`` globs of ``ci.yml``.

    The file is read as text (no YAML dependency on this branch): the list is
    the ``- '...'`` items indented under ``paths:`` inside ``pull_request:``.
    """
    lines = CI_WORKFLOW.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "pull_request:")
    paths_at = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "paths:")
    indent = len(lines[paths_at]) - len(lines[paths_at].lstrip())
    globs: list[str] = []
    for line in lines[paths_at + 1 :]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if len(line) - len(line.lstrip()) <= indent:
            break
        match = re.fullmatch(r"-\s*['\"]?([^'\"]+)['\"]?", stripped)
        assert match, f"unexpected line in pull_request.paths: {line!r}"
        globs.append(match.group(1))
    return globs


def _glob_to_regex(glob: str) -> re.Pattern[str]:
    """Translate a GitHub Actions path glob (``**``, ``*``) to a regex."""
    out = ""
    i = 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif glob.startswith("**", i):
            out += ".*"
            i += 2
        elif glob[i] == "*":
            out += "[^/]*"
            i += 1
        else:
            out += re.escape(glob[i])
            i += 1
    return re.compile(out + r"\Z")


def _triggers(path: str) -> bool:
    return any(_glob_to_regex(glob).match(path) for glob in _pull_request_paths())


def test_code_paths_still_trigger() -> None:
    assert _triggers("src/surreal_orm/query_set.py")
    assert _triggers("pyproject.toml")


@pytest.mark.parametrize(
    "path",
    ["README.md", "CLAUDE.md", "CHANGELOG", "SECURITY.md", "docs/roadmap.md"],
)
def test_docs_only_change_triggers_ci(path: str) -> None:
    assert _triggers(path), f"a PR touching only {path} would never report CI Success"
