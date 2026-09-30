"""
Every GitHub Release created from the v2 LTS branch must opt out of "Latest".

``softprops/action-gh-release`` defaults to ``make_latest: true``. #113 set
``make_latest: false`` in ``publish.yml`` only, but ``tag-release.yml`` creates
the release first — so v0.21.7 and v0.21.8 each took the repo's "Latest" label
from the 3.x ``main`` line, and ``publish.yml``'s later update of the same
release did not take it back.
"""

import re
from pathlib import Path

import pytest

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"
RELEASE_ACTION = "uses: softprops/action-gh-release"


def _release_steps() -> list[tuple[str, str]]:
    """Return (workflow file, step text) for every step using the release action."""
    steps = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        # Split on step boundaries; a step starts with "- name:" at any indent.
        for step in re.split(r"\n\s*- name:", path.read_text()):
            if RELEASE_ACTION in step:
                steps.append((path.name, step))
    return steps


def test_release_steps_exist() -> None:
    names = {name for name, _ in _release_steps()}
    assert {"tag-release.yml", "publish.yml"} <= names


@pytest.mark.parametrize(("workflow", "step"), _release_steps(), ids=[name for name, _ in _release_steps()])
def test_release_step_never_claims_latest(workflow: str, step: str) -> None:
    assert re.search(r"^\s*make_latest:\s*false\s*$", step, re.MULTILINE), (
        f"{workflow}: a release step without `make_latest: false` marks a v2 LTS release as the repo's Latest"
    )
