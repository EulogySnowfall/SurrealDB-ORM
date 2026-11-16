"""The ``v3`` branch's own push and tag workflows (#579).

GitHub runs a push workflow from the pushed commit's own copy of the file, and
a tag workflow from the tagged commit's copy. So ``v3`` (SurrealDB 3.x, bug and
advisory fixes only) needs its own ``tag-release.yml``, ``publish.yml`` and
``ci.yml`` triggers: a push to ``v3`` must tag and run CI, a ``v0.39.Z`` tag on
a ``v3`` commit must publish, and neither may claim the repository's "Latest"
release, which belongs to ``main``.

The publish guard is a shell step: the tests run it in a git sandbox whose
``origin`` is a local bare repository.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

yaml = pytest.importorskip("yaml", reason="pyyaml required for workflow tests")

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"
_IDENTITY = ["-c", "user.name=t", "-c", "user.email=t@t"]


def _load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    return data


def _triggers(data: dict[str, Any]) -> dict[str, Any]:
    # PyYAML reads the bare key `on` as the boolean True.
    on: dict[str, Any] = data.get("on") or data[True]
    return on


def _step(job: dict[str, Any], uses_prefix: str) -> dict[str, Any]:
    matches = [s for s in job["steps"] if str(s.get("uses", "")).startswith(uses_prefix)]
    assert len(matches) == 1, matches
    return matches[0]


class TestTagRelease:
    def test_runs_on_pushes_to_v3_only(self) -> None:
        assert _triggers(_load("tag-release.yml"))["push"]["branches"] == ["v3"]

    def test_its_release_never_claims_latest(self) -> None:
        job = _load("tag-release.yml")["jobs"]["tag-release"]
        release = _step(job, "softprops/action-gh-release")
        assert release["with"]["make_latest"] is False


class TestCi:
    def test_runs_on_pushes_to_v3(self) -> None:
        on = _triggers(_load("ci.yml"))
        assert on["push"]["branches"] == ["v3"]
        assert on["pull_request"]["branches"] == ["*"]

    def test_ci_success_still_gates_every_job(self) -> None:
        jobs = _load("ci.yml")["jobs"]
        success = next(job for job in jobs.values() if job.get("name") == "CI Success")
        assert set(success["needs"]) == {"branch-guard", "lint", "test-unit", "test-integration", "coverage"}


class TestPublishTriggers:
    def test_triggers_on_v0_39_tags_only(self) -> None:
        on = _triggers(_load("publish.yml"))
        assert on == {"push": {"tags": ["v0.39.*"]}}

    def test_build_and_release_wait_for_the_v3_check(self) -> None:
        jobs = _load("publish.yml")["jobs"]
        assert "check-main" not in jobs
        assert jobs["build"]["needs"] == "check-v3"
        assert jobs["build"]["if"] == "needs.check-v3.outputs.on-v3 == 'true'"
        assert jobs["publish-pypi"]["needs"] == "build"
        assert jobs["github-release"]["needs"] == ["check-v3", "build"]
        assert jobs["github-release"]["if"] == "needs.check-v3.outputs.on-v3 == 'true'"

    def test_its_release_never_claims_latest(self) -> None:
        release = _step(_load("publish.yml")["jobs"]["github-release"], "softprops/action-gh-release")
        assert release["with"]["make_latest"] is False


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *_IDENTITY, *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _sandbox(tmp_path: Path) -> tuple[Path, str, str]:
    """A clone whose ``origin`` has ``v3`` and a side branch off it.

    Returns ``(clone, sha on v3, sha only on the side branch)``.
    """
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "v3", str(seed)], check=True)
    (seed / "README").write_text("v3\n")
    _git(seed, "add", "README")
    _git(seed, "commit", "-q", "-m", "v3")
    on_v3 = _git(seed, "rev-parse", "HEAD")
    _git(seed, "checkout", "-q", "-b", "side")
    (seed / "README").write_text("side\n")
    _git(seed, "commit", "-q", "-am", "side")
    off_v3 = _git(seed, "rev-parse", "HEAD")
    _git(seed, "push", "-q", str(origin), "v3", "side")
    clone = tmp_path / "repo"
    subprocess.run(["git", "clone", "-q", "-b", "side", str(origin), str(clone)], check=True, capture_output=True)
    return clone, on_v3, off_v3


def _run_check(tmp_path: Path, *, tag: str, sha: str, repo: Path) -> tuple[int, dict[str, str], str]:
    step = next(s for s in _load("publish.yml")["jobs"]["check-v3"]["steps"] if s.get("id") == "check")
    assert set(step["env"]) == {"TAG", "TAGGED_SHA"}
    assert "${{" not in step["run"]
    script = tmp_path / "check.sh"
    script.write_text(step["run"], encoding="utf-8")
    outputs = tmp_path / "outputs"
    outputs.write_text("")
    env = {**os.environ, "TAG": tag, "TAGGED_SHA": sha, "GITHUB_OUTPUT": str(outputs)}
    # GitHub runs a `run:` block with no `shell:` as `bash -e {0}`.
    result = subprocess.run(["bash", "-e", str(script)], cwd=repo, env=env, capture_output=True, text=True)
    values = dict(line.split("=", 1) for line in outputs.read_text().splitlines() if "=" in line)
    return result.returncode, values, result.stdout + result.stderr


class TestPublishGuard:
    @pytest.mark.parametrize("tag", ["v0.39.10", "v0.39.0", "v0.39.123"])
    def test_a_v0_39_tag_on_v3_publishes(self, tmp_path: Path, tag: str) -> None:
        repo, on_v3, _ = _sandbox(tmp_path)
        code, outputs, log = _run_check(tmp_path, tag=tag, sha=on_v3, repo=repo)
        assert code == 0, log
        assert outputs == {"on-v3": "true"}, log

    def test_a_v0_39_tag_off_v3_is_refused(self, tmp_path: Path) -> None:
        repo, _, off_v3 = _sandbox(tmp_path)
        code, outputs, log = _run_check(tmp_path, tag="v0.39.10", sha=off_v3, repo=repo)
        assert code != 0, log
        assert outputs.get("on-v3") != "true", log

    @pytest.mark.parametrize(
        "tag",
        ["v0.40.0", "v1.0.0", "v0.21.63", "v0.39", "v0.39.x", "v0.39.10-rc.1", "v0.39.10a", "v0.391.0", "0.39.10"],
    )
    def test_any_other_tag_is_refused(self, tmp_path: Path, tag: str) -> None:
        repo, on_v3, _ = _sandbox(tmp_path)
        code, outputs, log = _run_check(tmp_path, tag=tag, sha=on_v3, repo=repo)
        assert code != 0, log
        assert outputs == {}, log


@pytest.mark.parametrize("name", ["surrealdb-security.yml", "surrealdb-v2-security.yml"])
def test_monitor_copies_have_no_release_notes_output(name: str) -> None:
    """``v3``'s copies of the monitors drop the output no job reads, as on ``main``."""
    text = (WORKFLOWS / name).read_text(encoding="utf-8")
    assert "release-notes" not in text
    outputs = _load(name)["jobs"]["check-release"]["outputs"]
    assert "release-notes" not in outputs
