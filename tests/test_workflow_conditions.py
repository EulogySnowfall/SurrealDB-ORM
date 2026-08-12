"""Lint tests for GitHub Actions job/step conditions — v0.31.14.

Guards the class of bug fixed in #146: a job whose ``if:`` expression tests a
dependency's *failure* but contains no status-check function.  GitHub
implicitly ANDs ``success()`` into any ``if:`` that lacks one, so such a job is
skipped exactly when it was meant to run.

See https://docs.github.com/en/actions/reference/workflows-and-actions/expressions#status-check-functions
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

yaml = pytest.importorskip("yaml", reason="pyyaml required for workflow lint tests")

WORKFLOW_DIR = Path(__file__).resolve().parent.parent / ".github" / "workflows"

# A status-check function anywhere in the condition disables GitHub's implicit
# `success() &&` prefix.
_STATUS_FN = re.compile(r"\b(always|failure|cancelled|success)\s*\(")

# References to a non-success outcome of a dependency, e.g.
#   needs.test-new-version.result == 'failure'
_NON_SUCCESS_REF = re.compile(r"result\s*[=!]=\s*['\"]?(failure|cancelled|skipped)")


def _workflow_files() -> list[Path]:
    return sorted(WORKFLOW_DIR.glob("*.yml")) + sorted(WORKFLOW_DIR.glob("*.yaml"))


def _conditions() -> list[tuple[str, str, str]]:
    """Yield ``(workflow, location, condition)`` for every ``if:`` in every workflow."""
    found: list[tuple[str, str, str]] = []
    for path in _workflow_files():
        data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for job_id, job in (data.get("jobs") or {}).items():
            if not isinstance(job, dict):
                continue
            if job.get("if") is not None:
                found.append((path.name, job_id, " ".join(str(job["if"]).split())))
            for i, step in enumerate(job.get("steps") or []):
                if isinstance(step, dict) and step.get("if") is not None:
                    label = step.get("name") or f"step[{i}]"
                    found.append((path.name, f"{job_id}::{label}", " ".join(str(step["if"]).split())))
    return found


class TestWorkflowsAreParseable:
    """Sanity checks on the workflow directory itself."""

    def test_workflow_dir_exists(self) -> None:
        assert WORKFLOW_DIR.is_dir(), f"{WORKFLOW_DIR} not found"

    def test_workflows_are_valid_yaml(self) -> None:
        files = _workflow_files()
        assert files, "no workflow files discovered"
        for path in files:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            assert isinstance(data, dict), f"{path.name} did not parse to a mapping"
            assert data.get("jobs"), f"{path.name} defines no jobs"

    def test_some_conditions_were_collected(self) -> None:
        """Guard against the collector silently matching nothing."""
        assert _conditions(), "no `if:` conditions discovered — collector is broken"


class TestFailureConditionsHaveStatusFunction:
    """Regression tests for #146."""

    def test_no_failure_condition_relies_on_implicit_success(self) -> None:
        """A condition testing a dependency's failure must call always()/failure().

        Without one, GitHub prefixes the expression with ``success() &&``, so the
        job is skipped precisely when the dependency failed — the condition is
        unreachable.
        """
        offenders = [
            f"{wf}::{loc} -> {cond!r}"
            for wf, loc, cond in _conditions()
            if _NON_SUCCESS_REF.search(cond) and not _STATUS_FN.search(cond)
        ]
        assert not offenders, (
            "These conditions test for failure but have no status-check function, "
            "so GitHub implicitly ANDs `success()` in and they can never run:\n  " + "\n  ".join(offenders)
        )

    @pytest.mark.parametrize(
        "workflow",
        ["surrealdb-security.yml", "surrealdb-v2-security.yml"],
    )
    def test_monitor_failure_issue_job_can_run(self, workflow: str) -> None:
        """The SurrealDB monitors' `create-failure-issue` job must be reachable."""
        path = WORKFLOW_DIR / workflow
        data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
        job = data["jobs"]["create-failure-issue"]
        cond = " ".join(str(job["if"]).split())

        assert "always()" in cond, f"{workflow}: create-failure-issue would be skipped on dependency failure"
        # always() must not have weakened the guard.
        assert "needs.test-new-version.result == 'failure'" in cond
        assert "needs.check-release.outputs.has-update == 'true'" in cond

    def test_status_function_regex_matches_known_forms(self) -> None:
        for cond in ("always()", "always ()", "failure()", "!cancelled()", "success() && x"):
            assert _STATUS_FN.search(cond), cond
        for cond in ("needs.a.result == 'failure'", "github.event_name == 'push'"):
            assert not _STATUS_FN.search(cond), cond

    def test_non_success_ref_regex_matches_known_forms(self) -> None:
        for cond in (
            "needs.a.result == 'failure'",
            'needs.a.result == "cancelled"',
            "needs.a.result != 'skipped'",
        ):
            assert _NON_SUCCESS_REF.search(cond), cond
        for cond in ("needs.a.result == 'success'", "needs.a.outputs.x == 'true'"):
            assert not _NON_SUCCESS_REF.search(cond), cond
