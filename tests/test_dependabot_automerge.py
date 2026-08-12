"""Lint tests for the Dependabot auto-merge workflow.

Guards two regressions in the release chain:

* **#153** — ``gh pr merge --auto`` is rejected by GitHub when the PR's *base*
  branch has no protection ("Protected branch rules not configured for this
  branch"). ``v2`` was unprotected, so every version-bump PR based on it stalled
  and the LTS line stopped publishing. Every merge call must therefore go
  through the fallback helper, never bare ``--auto``.
* **#118** — the auto-merge job is itself reported as the "Auto-merge & Tag"
  check on the PR, so ``gh pr checks --watch`` waits on its own job until the 6h
  timeout. No watch-all gate may come back into this workflow.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

yaml = pytest.importorskip("yaml", reason="pyyaml required for workflow lint tests")

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "dependabot-automerge.yml"

HELPER = "merge-when-green.sh"

# `gh pr merge --auto ...` written directly in a step, i.e. not delegated to the
# helper that falls back to an explicit CI gate.
_BARE_AUTO_MERGE = re.compile(r"gh\s+pr\s+merge\b[^\n]*--auto")


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _steps() -> list[tuple[str, dict[str, Any]]]:
    """Yield ``(job_id, step)`` for every step of every job."""
    data: dict[str, Any] = yaml.safe_load(_workflow_text())
    return [(job_id, step) for job_id, job in data["jobs"].items() for step in (job.get("steps") or [])]


class TestWorkflowShape:
    def test_workflow_exists_and_parses(self) -> None:
        assert WORKFLOW.is_file(), f"{WORKFLOW} not found"
        data = yaml.safe_load(_workflow_text())
        assert data["jobs"]["automerge"]["steps"], "automerge job defines no steps"


class TestMergeGate:
    """Regression tests for #153."""

    def test_helper_is_defined_before_any_use(self) -> None:
        text = _workflow_text()
        definition = text.find(f'cat > "$RUNNER_TEMP/{HELPER}"')
        assert definition != -1, f"{HELPER} is never written to $RUNNER_TEMP"
        first_use = text.find(f'"$RUNNER_TEMP/{HELPER}"', definition + 1)
        assert first_use != -1, f"{HELPER} is defined but never invoked"

    def test_every_merge_goes_through_the_helper(self) -> None:
        """No step may call ``gh pr merge --auto`` outside the helper body.

        A bare ``--auto`` hard-fails on an unprotected base branch, which is what
        left PR #152 open and blocked the whole v2 release chain.
        """
        offenders = [
            f"{job_id}::{step.get('name') or 'unnamed step'}"
            for job_id, step in _steps()
            if HELPER not in str(step.get("run", "")) and _BARE_AUTO_MERGE.search(str(step.get("run", "")))
        ]
        assert not offenders, (
            "These steps call `gh pr merge --auto` directly; on a base branch without "
            f"protection GitHub rejects it and the step fails. Use {HELPER} instead:\n  " + "\n  ".join(offenders)
        )

    def test_helper_falls_back_to_an_explicit_check_gate(self) -> None:
        text = _workflow_text()
        assert "CI Success" in text, "helper has no required-check name to gate on"
        assert "gh pr merge --squash" in text, "helper never performs the fallback merge"

    def test_bump_pr_creation_ends_with_the_helper(self) -> None:
        step = next(s for _, s in _steps() if s.get("name") == "Create version bump PR")
        assert HELPER in str(step["run"]), "the version-bump PR is created but never gated for merge"


class TestNoSelfWatchDeadlock:
    """Regression test for #118."""

    def test_no_watch_all_checks_gate(self) -> None:
        # Comments legitimately mention the banned flag to explain why it is banned.
        code = "\n".join(line for line in _workflow_text().splitlines() if not line.lstrip().startswith("#"))
        assert "--watch" not in code, (
            "`gh pr checks --watch` waits on the Auto-merge & Tag check produced by "
            "this very job — a self-referential deadlock (#118)"
        )
