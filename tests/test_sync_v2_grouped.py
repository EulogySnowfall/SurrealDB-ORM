"""Tests for the automerge workflow's "Sync to V2" job.

Regression: a *grouped* Dependabot PR (#217 bumped ``tornado`` and ``urllib3``
together) reports its dependency names as one comma-separated string,
``"tornado, urllib3"``. The job looked that whole string up as a single package
name in v2's ``uv.lock``, found nothing, logged "not found in v2 — skipping
sync" and finished green. Every grouped update was silently dropped for the LTS
line, which stayed on six known advisories until a manual audit.

The real shell blocks are extracted from the workflow and executed here: the
presence check directly, the update step against a temporary git repository
with fake ``uv`` and ``gh`` binaries on ``PATH``.
"""

from __future__ import annotations

import os
import stat
import subprocess
import textwrap
from pathlib import Path
from typing import Any

import pytest

yaml = pytest.importorskip("yaml", reason="pyyaml required for workflow lint tests")

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "dependabot-automerge.yml"

V2_LOCK = textwrap.dedent(
    """\
    version = 1

    [[package]]
    name = "tornado"
    version = "6.5.8"

    [[package]]
    name = "urllib3"
    version = "2.7.0"

    [[package]]
    name = "typing-extensions"
    version = "4.12.0"
    """
)

# What `uv lock --upgrade-package ...` resolves to in these tests.
UPGRADES = {"tornado": "6.5.9", "urllib3": "2.8.0", "typing-extensions": "4.13.0"}


def _step(name_prefix: str) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for step in data["jobs"]["sync-v2"]["steps"]:
        if step.get("name", "").startswith(name_prefix):
            return step
    raise AssertionError(f"sync-v2 has no step starting with {name_prefix!r}")


def _outputs(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)


def _run_check(tmp_path: Path, dep_names: str) -> subprocess.CompletedProcess[str]:
    (tmp_path / "uv.lock").write_text(V2_LOCK)
    out = tmp_path / "out"
    out.write_text("")
    env = {**os.environ, "DEP_NAME": dep_names, "GITHUB_OUTPUT": str(out)}
    return subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", _step("Check which").get("run", "")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )


class TestCheckStep:
    def test_grouped_names_are_split_and_each_one_checked(self, tmp_path: Path) -> None:
        result = _run_check(tmp_path, "tornado, urllib3")
        assert result.returncode == 0, result.stderr
        outputs = _outputs(tmp_path / "out")
        assert outputs["exists"] == "true"
        assert outputs["packages"].split() == ["tornado", "urllib3"]

    def test_single_name_still_works(self, tmp_path: Path) -> None:
        result = _run_check(tmp_path, "tornado")
        assert result.returncode == 0, result.stderr
        assert _outputs(tmp_path / "out")["packages"].split() == ["tornado"]

    def test_only_the_names_present_in_v2_are_synced(self, tmp_path: Path) -> None:
        # A main-only dependency is a legitimate skip, not an error.
        result = _run_check(tmp_path, "tornado, some-main-only-pkg")
        assert result.returncode == 0, result.stderr
        assert _outputs(tmp_path / "out")["packages"].split() == ["tornado"]
        assert "some-main-only-pkg" in result.stdout

    def test_no_name_in_v2_is_a_quiet_skip(self, tmp_path: Path) -> None:
        result = _run_check(tmp_path, "some-main-only-pkg")
        assert result.returncode == 0, result.stderr
        assert _outputs(tmp_path / "out")["exists"] == "false"

    def test_names_are_normalised_like_the_lock_file(self, tmp_path: Path) -> None:
        # uv.lock stores PEP 503 names; Dependabot may report `Typing_Extensions`.
        result = _run_check(tmp_path, "Typing_Extensions")
        assert result.returncode == 0, result.stderr
        assert _outputs(tmp_path / "out")["packages"].split() == ["typing-extensions"]

    @pytest.mark.parametrize("bad", ["", "   ", "tornado; rm -rf /", "tornado from 1 to 2"])
    def test_unparseable_input_fails_loudly(self, tmp_path: Path, bad: str) -> None:
        # The original bug was a format the job did not understand, reported green.
        # Anything it cannot read as package names must turn the job red instead.
        result = _run_check(tmp_path, bad)
        assert result.returncode != 0
        assert "::error::" in result.stdout


def _write_exe(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _run_update(tmp_path: Path, packages: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "uv.lock").write_text(V2_LOCK)
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", "-b", "v2", str(repo)], check=True)
    subprocess.run([*git, "add", "uv.lock"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], check=True)
    # A bare "origin" so `git ls-remote` and `git push` work offline.
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    subprocess.run([*git, "remote", "add", "origin", str(origin)], check=True)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    upgrades = " ".join(f"[{k}]={v}" for k, v in UPGRADES.items())
    # Fake uv: bump the version line under each --upgrade-package name.
    _write_exe(
        bin_dir / "uv",
        textwrap.dedent(
            f"""\
            declare -A NEW=({upgrades})
            [[ "$1" == lock ]] || exit 2
            shift
            while [[ $# -gt 0 ]]; do
              [[ "$1" == --upgrade-package ]] || exit 2
              pkg="$2"; shift 2
              sed -i "/^name = \\"$pkg\\"$/{{n;s/^version = .*/version = \\"${{NEW[$pkg]}}\\"/}}" uv.lock
            done
            """
        ),
    )
    gh_log = tmp_path / "gh.log"
    _write_exe(bin_dir / "gh", f'printf "%s\\n" "$@" > {gh_log}\n')

    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "PACKAGES": packages,
        "GH_TOKEN": "x",
        "UPDATE_TYPE": "version-update:semver-patch",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", _step("Update dependencies").get("run", "")],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )
    return result, gh_log


def _gh_title(gh_log: Path) -> str:
    args = gh_log.read_text().splitlines()
    return args[args.index("--title") + 1]


def _parse_like_the_automerge_job(title: str, tmp_path: Path) -> tuple[str, str, str]:
    """Run the automerge job's own title parser on a security-sync PR title."""
    data: dict[str, Any] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    step = next(s for s in data["jobs"]["automerge"]["steps"] if s.get("id") == "sync-metadata")
    out = tmp_path / "parsed"
    out.write_text("")
    subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        env={**os.environ, "PR_TITLE": title, "GITHUB_OUTPUT": str(out)},
        check=True,
    )
    parsed = _outputs(out)
    return parsed["dependency-names"], parsed["previous-version"], parsed["new-version"]


class TestUpdateStep:
    def test_grouped_sync_upgrades_every_package_in_one_pr(self, tmp_path: Path) -> None:
        result, gh_log = _run_update(tmp_path, "tornado urllib3")
        assert result.returncode == 0, result.stdout + result.stderr
        title = _gh_title(gh_log)
        assert title == ("chore(deps): bump tornado, urllib3 from 6.5.8, 2.7.0 to 6.5.9, 2.8.0 (security sync from main)")
        lock = subprocess.run(
            ["git", "-C", str(tmp_path / "repo"), "show", "HEAD:uv.lock"], capture_output=True, text=True, check=True
        ).stdout
        assert 'version = "6.5.9"' in lock and 'version = "2.8.0"' in lock

    def test_single_package_title_is_unchanged_in_shape(self, tmp_path: Path) -> None:
        result, gh_log = _run_update(tmp_path, "tornado")
        assert result.returncode == 0, result.stdout + result.stderr
        assert _gh_title(gh_log) == "chore(deps): bump tornado from 6.5.8 to 6.5.9 (security sync from main)"

    def test_title_round_trips_through_the_automerge_parser(self, tmp_path: Path) -> None:
        # The sync PR lands on v2 and is processed again by this same workflow,
        # which reads the dependency and versions back out of its title.
        result, gh_log = _run_update(tmp_path, "tornado urllib3")
        assert result.returncode == 0, result.stdout + result.stderr
        assert _parse_like_the_automerge_job(_gh_title(gh_log), tmp_path) == (
            "tornado, urllib3",
            "6.5.8, 2.7.0",
            "6.5.9, 2.8.0",
        )

    def test_single_package_title_round_trips_too(self, tmp_path: Path) -> None:
        # Pre-existing defect: the greedy `bump (.*) from` ran to the "from" in
        # "(security sync from main)", so even single-package syncs reported
        # "anyio from 4.13.0 to 4.14.2 (security sync" as the package name.
        result, gh_log = _run_update(tmp_path, "tornado")
        assert result.returncode == 0, result.stdout + result.stderr
        assert _parse_like_the_automerge_job(_gh_title(gh_log), tmp_path) == ("tornado", "6.5.8", "6.5.9")

    def test_pr_is_opened_against_v2_with_the_sync_label(self, tmp_path: Path) -> None:
        result, gh_log = _run_update(tmp_path, "tornado urllib3")
        assert result.returncode == 0, result.stdout + result.stderr
        args = gh_log.read_text().splitlines()
        assert args[args.index("--base") + 1] == "v2"
        assert "security-sync" in args[args.index("--label") + 1]
