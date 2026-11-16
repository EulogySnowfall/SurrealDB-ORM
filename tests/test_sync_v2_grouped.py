"""Tests for the automerge workflow's "Sync to v2 / v3" job.

Regression: a *grouped* Dependabot PR (#217 bumped ``tornado`` and ``urllib3``
together) reports its dependency names as one comma-separated string,
``"tornado, urllib3"``. The job looked that whole string up as a single package
name in v2's ``uv.lock``, found nothing, logged "not found in v2 — skipping
sync" and finished green. Every grouped update was silently dropped for the LTS
line, which stayed on six known advisories until a manual audit.

The theme of the fixes, and of these tests: a run that leaves v2 unsynced must
never look like a run that synced it. Each path either does the work, skips for
a stated reason, warns, or fails.

The real shell blocks are extracted from the workflow and executed: the gate
and the presence check directly, the update step against a temporary git
repository (with a bare ``origin`` carrying ``main`` and ``v2``) and fake
``uv`` / ``gh`` binaries on ``PATH``, and the automerge job's metadata step on
the PR body the update step produces.

#579 extends the job to the permanent ``v3`` branch: one matrix job,
``sync-lts``, runs the same steps once per target branch (``TARGET``), so a
Dependabot ``uv`` security update merged on ``main`` reaches ``v3`` exactly as
it reaches ``v2``.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests._release_series import owner_repo, pyproject

yaml = pytest.importorskip("yaml", reason="pyyaml required for workflow lint tests")

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "dependabot-automerge.yml"
DEPENDABOT = ROOT / ".github" / "dependabot.yml"

GITHUB_TITLE_LIMIT = 256

JOB = "sync-lts"
TARGETS = ["v2", "v3"]


def _lock(*packages: tuple[str, str]) -> str:
    entries = "".join(f'\n[[package]]\nname = "{name}"\nversion = "{version}"\n' for name, version in packages)
    return "version = 1\n" + entries


V2_LOCK = _lock(("tornado", "6.5.8"), ("urllib3", "2.7.0"), ("typing-extensions", "4.12.0"), ("cbor2", "5.9.0"))


def _workflow() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return data


def _step(job: str, name_prefix: str) -> dict[str, Any]:
    for step in _workflow()["jobs"][job]["steps"]:
        if step.get("name", "").startswith(name_prefix):
            return step
    raise AssertionError(f"{job} has no step starting with {name_prefix!r}")


def _outputs(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)


def _bash(script: str, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    # GitHub runs `run:` blocks with `bash -e {0}`.
    return subprocess.run(["bash", "-e", "-c", script], cwd=cwd, env=env, capture_output=True, text=True)


# ---------------------------------------------------------------------------
# Which updates are synced at all
# ---------------------------------------------------------------------------


def _run_gate(tmp_path: Path, ecosystem: str, target: str = "v2") -> subprocess.CompletedProcess[str]:
    out = tmp_path / "gate"
    out.write_text("")
    env = {**os.environ, "ECOSYSTEM": ecosystem, "TARGET": target, "GITHUB_OUTPUT": str(out)}
    return _bash(_step(JOB, "Decide whether")["run"], tmp_path, env)


class TestGate:
    @pytest.mark.parametrize("target", TARGETS)
    def test_uv_updates_are_synced(self, tmp_path: Path, target: str) -> None:
        result = _run_gate(tmp_path, "uv", target)
        assert result.returncode == 0, result.stdout + result.stderr
        assert _outputs(tmp_path / "gate")["sync"] == "true"

    @pytest.mark.parametrize("target", TARGETS)
    @pytest.mark.parametrize("ecosystem", ["github_actions", "pip", "docker"])
    def test_other_ecosystems_skip_with_a_stated_reason(self, tmp_path: Path, ecosystem: str, target: str) -> None:
        # Regression (review of #220): the name check rejected `actions/checkout`
        # and turned every GitHub Actions bump red. Those have nothing to sync.
        result = _run_gate(tmp_path, ecosystem, target)
        assert result.returncode == 0, result.stdout + result.stderr
        assert _outputs(tmp_path / "gate")["sync"] == "false"
        assert "::notice::" in result.stdout
        assert target in result.stdout

    def test_missing_ecosystem_fails_loudly(self, tmp_path: Path) -> None:
        result = _run_gate(tmp_path, "")
        assert result.returncode != 0
        assert "::error::" in result.stdout

    def test_every_later_step_is_behind_the_gate(self) -> None:
        steps = _workflow()["jobs"][JOB]["steps"]
        names = [s.get("name", "") for s in steps]
        gate = next(i for i, n in enumerate(names) if n.startswith("Decide whether"))
        for step in steps[gate + 1 :]:
            assert "steps.gate.outputs.sync == 'true'" in step.get("if", ""), step.get("name")

    def test_every_step_after_the_owner_check_is_behind_it(self) -> None:
        # #579: nothing is installed, resolved, pushed or opened for a target
        # whose patch series main owns.
        steps = _workflow()["jobs"][JOB]["steps"]
        names = [s.get("name", "") for s in steps]
        owner = names.index("Defer to main while it owns the patch series")
        assert names.index("Checkout target branch") < owner < names.index("Update dependencies and create PR")
        assert steps[owner]["id"] == "owner"
        for step in steps[owner + 1 :]:
            assert "steps.owner.outputs.sync == 'true'" in step.get("if", ""), step.get("name")


class TestUvMeansSecurity:
    """The gate reads `uv` as "security update". This keeps that true."""

    def test_no_uv_version_update_config_on_main(self) -> None:
        # Dependabot opens *security* updates on the default branch for every
        # ecosystem it detects, but *version* updates only for configured ones.
        # With no `uv` entry, a `uv` PR on main is a security update. Routine
        # version updates reach v2 through its own `target-branch: v2` entry,
        # so syncing them too would race it on the same uv.lock lines and ship
        # a v2 release for a ruff bump. Adding a `uv` entry breaks the gate.
        config: dict[str, Any] = yaml.safe_load(DEPENDABOT.read_text(encoding="utf-8"))
        ecosystems = {u["package-ecosystem"] for u in config["updates"] if u.get("target-branch", "main") == "main"}
        assert "uv" not in ecosystems

    def test_v2_has_its_own_version_updates(self) -> None:
        config: dict[str, Any] = yaml.safe_load(DEPENDABOT.read_text(encoding="utf-8"))
        assert any(u.get("target-branch") == "v2" for u in config["updates"])

    def test_no_uv_version_update_config_on_any_branch(self) -> None:
        # #579: the same reading holds for v3. A `uv` version-update entry with
        # `target-branch: v3` (or v2) would open routine `uv` PRs on that branch
        # and race the security sync on the same uv.lock lines.
        config: dict[str, Any] = yaml.safe_load(DEPENDABOT.read_text(encoding="utf-8"))
        assert all(u["package-ecosystem"] != "uv" for u in config["updates"])

    def test_only_prs_merged_on_main_are_synced(self) -> None:
        # The sync source is always main: a security-sync PR merged on v2 or v3
        # must not fan out again to the other branch.
        cond = " ".join(str(_workflow()["jobs"][JOB]["if"]).split())
        assert "github.event.pull_request.base.ref == 'main'" in cond
        assert "needs.automerge.outputs.merged == 'true'" in cond


class TestMatrix:
    """#579: one job, one run per target branch."""

    def test_the_job_runs_once_per_target(self) -> None:
        job = _workflow()["jobs"][JOB]
        assert job["strategy"]["matrix"]["target"] == TARGETS
        # A failed sync to one branch must not cancel the other.
        assert job["strategy"]["fail-fast"] is False

    def test_the_old_v2_only_job_is_gone(self) -> None:
        assert "sync-v2" not in _workflow()["jobs"]

    def test_the_target_branch_is_checked_out(self) -> None:
        step = _step(JOB, "Checkout target branch")
        assert step["with"]["ref"] == "${{ matrix.target }}"

    def test_every_step_script_reads_the_target_from_env(self) -> None:
        # The branch name reaches the scripts as data, never expanded into them.
        for step in _workflow()["jobs"][JOB]["steps"]:
            run = str(step.get("run", ""))
            assert "matrix." not in run, step.get("name")
            if "TARGET" in run:
                assert step["env"]["TARGET"] == "${{ matrix.target }}", step.get("name")

    def test_no_step_command_names_v2_alone(self) -> None:
        # Comments may explain v2's history; commands and messages use $TARGET.
        for step in _workflow()["jobs"][JOB]["steps"]:
            code = [line for line in str(step.get("run", "")).splitlines() if not line.lstrip().startswith("#")]
            offenders = [line for line in code if re.search(r"\bv2\b", line)]
            assert not offenders, (step.get("name"), offenders)


# ---------------------------------------------------------------------------
# #579: one patch series has one owner
# ---------------------------------------------------------------------------
#
# While main's pyproject.toml has the same X.Y as v3's, main owns that series
# (it releases 0.39.10, 0.39.11, ... until the 4.x pin). A sync PR on v3 would
# end in a v3 bump computing one of main's numbers, so v3 takes none until
# main leaves the series. v2 (0.21.x) is not concerned.


def _run_owner(
    tmp_path: Path, *, main: str | None, target_version: str, target: str = "v3"
) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    repo = owner_repo(tmp_path, main=main, target_version=target_version, target=target)
    out = tmp_path / "owner"
    out.write_text("")
    env = {**os.environ, "TARGET": target, "GITHUB_OUTPUT": str(out)}
    result = _bash(_step(JOB, "Defer to main")["run"], repo, env)
    return result, _outputs(out)


class TestPatchSeriesOwner:
    @pytest.mark.parametrize(("on_main", "on_v3"), [("0.39.9", "0.39.9"), ("0.39.12", "0.39.9"), ("0.39.0", "0.39.9")])
    def test_v3_is_not_synced_while_main_shares_its_series(self, tmp_path: Path, on_main: str, on_v3: str) -> None:
        result, outputs = _run_owner(tmp_path, main=pyproject(on_main), target_version=pyproject(on_v3))
        assert result.returncode == 0, result.stdout + result.stderr
        assert outputs == {"sync": "false"}
        assert "::notice::v3 follows main until main leaves 0.39.x; no sync" in result.stdout

    @pytest.mark.parametrize("on_main", ["0.40.0", "0.40.3", "0.41.0"])
    def test_v3_is_synced_once_main_leaves_the_series(self, tmp_path: Path, on_main: str) -> None:
        result, outputs = _run_owner(tmp_path, main=pyproject(on_main), target_version=pyproject("0.39.9"))
        assert result.returncode == 0, result.stdout + result.stderr
        assert outputs == {"sync": "true"}
        assert "::notice::" not in result.stdout

    @pytest.mark.parametrize(
        "main",
        [
            pyproject("0.39.9"),
            pyproject("0.21.62"),
            None,  # no main branch at all
            "",  # no pyproject.toml on main
            "[project]\nname = 'x'\n",  # no readable version
        ],
    )
    def test_v2_is_never_affected(self, tmp_path: Path, main: str | None) -> None:
        result, outputs = _run_owner(tmp_path, main=main, target_version=pyproject("0.21.62"), target="v2")
        assert result.returncode == 0, result.stdout + result.stderr
        assert outputs == {"sync": "true"}

    @pytest.mark.parametrize(
        "main",
        [
            None,  # main cannot be fetched
            "[project]\nname = 'x'\n",  # no version line
            pyproject("0.40.0rc1"),
            pyproject("v0.40.0"),
            pyproject("0.40"),
            pyproject("0.40.0\nversion = 0.39.9"),
            # An X bump is the user's decision: the rule is re-read then.
            pyproject("1.0.0"),
        ],
        ids=["no-main", "no-version", "pre-release", "prefixed", "two-parts", "two-lines", "main-1.x"],
    )
    def test_an_unreadable_main_version_fails_closed(self, tmp_path: Path, main: str | None) -> None:
        result, outputs = _run_owner(tmp_path, main=main, target_version=pyproject("0.39.9"))
        assert result.returncode != 0, result.stdout + result.stderr
        assert "::error::" in result.stdout
        assert "sync" not in outputs

    def test_a_missing_main_pyproject_fails_closed(self, tmp_path: Path) -> None:
        result, outputs = _run_owner(tmp_path, main="", target_version=pyproject("0.39.9"))
        assert result.returncode != 0, result.stdout + result.stderr
        assert "::error::" in result.stdout
        assert "sync" not in outputs

    @pytest.mark.parametrize("target_version", ["", "[project]\nname = 'x'\n", pyproject("0.39")])
    def test_an_unreadable_v3_version_fails_closed(self, tmp_path: Path, target_version: str) -> None:
        result, outputs = _run_owner(tmp_path, main=pyproject("0.40.0"), target_version=target_version)
        assert result.returncode != 0, result.stdout + result.stderr
        assert "::error::" in result.stdout
        assert "sync" not in outputs

    def test_main_and_v3_never_compute_the_same_number(self, tmp_path: Path) -> None:
        # A v3 sync PR is what leads to a v3 bump (x.y.z -> x.y.z+1). Whenever
        # the sync is open, main's next bump and v3's are in different series.
        for index, on_main in enumerate(["0.39.9", "0.39.10", "0.39.11", "0.40.0", "0.40.1"]):
            run_dir = tmp_path / str(index)
            run_dir.mkdir()
            result, outputs = _run_owner(run_dir, main=pyproject(on_main), target_version=pyproject("0.39.9"))
            assert result.returncode == 0, result.stdout + result.stderr
            if outputs["sync"] == "true":
                assert on_main.rsplit(".", 1)[0] != "0.39", on_main


# ---------------------------------------------------------------------------
# Which packages exist on v2
# ---------------------------------------------------------------------------


def _run_check(tmp_path: Path, dep_names: str, target: str = "v2") -> subprocess.CompletedProcess[str]:
    (tmp_path / "uv.lock").write_text(V2_LOCK)
    out = tmp_path / "out"
    out.write_text("")
    env = {**os.environ, "DEP_NAME": dep_names, "TARGET": target, "GITHUB_OUTPUT": str(out)}
    return _bash(_step(JOB, "Check which")["run"], tmp_path, env)


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

    @pytest.mark.parametrize("target", TARGETS)
    def test_only_the_names_present_in_the_target_are_synced(self, tmp_path: Path, target: str) -> None:
        # A main-only dependency is a legitimate skip, not an error.
        result = _run_check(tmp_path, "tornado, some-main-only-pkg", target)
        assert result.returncode == 0, result.stderr
        assert _outputs(tmp_path / "out")["packages"].split() == ["tornado"]
        assert "some-main-only-pkg" in result.stdout
        assert f"not in {target}'s uv.lock" in result.stdout

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


# ---------------------------------------------------------------------------
# The update itself
# ---------------------------------------------------------------------------

FAKE_UV = """\
#!/usr/bin/env python3
# Fake `uv lock --upgrade-package A --upgrade-package B ...`.
# FAKE_UV_UPGRADES = {"pkg": "new"} bumps every entry of pkg;
#                    {"pkg": ["old", "new"]} bumps only the entry at `old`.
# Packages it is not told about stay put, like a v2 constraint holding them.
import json, os, re, sys
args = sys.argv[1:]
assert args[0] == "lock", args
names = [args[i + 1] for i, a in enumerate(args) if a == "--upgrade-package"]
plan = json.loads(os.environ.get("FAKE_UV_UPGRADES", "{}"))
text = open("uv.lock").read()
for name in names:
    spec = plan.get(name)
    if spec is None:
        continue
    old, new = (spec if isinstance(spec, list) else (None, spec))
    pat = re.compile(r'(name = "%s"\\nversion = ")([^"]*)(")' % re.escape(name))
    text = pat.sub(lambda m: m.group(1) + (new if old in (None, m.group(2)) else m.group(2)) + m.group(3), text)
open("uv.lock", "w").write(text)
"""

FAKE_GH = """\
#!/usr/bin/env python3
# `gh api .../pulls?...` prints the open-PR count from FAKE_GH_OPEN_PRS; every call is logged as JSON.
import json, os, sys
with open(os.environ["FAKE_GH_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1:2] == ["api"]:
    print(os.environ.get("FAKE_GH_OPEN_PRS", "0"))
"""


def _write_exe(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


class Sandbox:
    """A checkout of the target branch whose `origin` also has `main`, plus fake uv/gh."""

    def __init__(self, tmp_path: Path, v2_lock: str = V2_LOCK, main_lock: str | None = None, target: str = "v2") -> None:
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.tmp = tmp_path
        self.target = target
        self.repo = tmp_path / "repo"
        self.origin = tmp_path / "origin.git"
        self.gh_log = tmp_path / "gh.log"
        self.gh_log.write_text("")
        subprocess.run(["git", "init", "-q", "--bare", str(self.origin)], check=True)

        seed = tmp_path / "seed"
        subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
        self._commit(seed, main_lock if main_lock is not None else v2_lock, "main")
        subprocess.run(["git", "-C", str(seed), "push", "-q", str(self.origin), "main"], check=True)
        subprocess.run(["git", "-C", str(seed), "checkout", "-q", "--orphan", target], check=True)
        self._commit(seed, v2_lock, target)
        subprocess.run(["git", "-C", str(seed), "push", "-q", str(self.origin), target], check=True)
        subprocess.run(["git", "clone", "-q", "-b", target, str(self.origin), str(self.repo)], check=True)

        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        _write_exe(self.bin / "uv", FAKE_UV)
        _write_exe(self.bin / "gh", FAKE_GH)

    @staticmethod
    def _commit(repo: Path, lock: str, msg: str) -> None:
        (repo / "uv.lock").write_text(lock)
        git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run([*git, "add", "uv.lock"], check=True)
        subprocess.run([*git, "commit", "-q", "-m", msg], check=True)

    def run(self, packages: str, upgrades: dict[str, Any], open_prs: int = 0) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "PACKAGES": packages,
            "TARGET": self.target,
            "GH_TOKEN": "x",
            "GITHUB_REPOSITORY": "owner/repo",
            "UPDATE_TYPE": "version-update:semver-patch",
            "SOURCE_PR": "217",
            "FAKE_UV_UPGRADES": json.dumps(upgrades),
            "FAKE_GH_OPEN_PRS": str(open_prs),
            "FAKE_GH_LOG": str(self.gh_log),
            "GITHUB_STEP_SUMMARY": str(self.tmp / "summary.md"),
        }
        return _bash(_step(JOB, "Update dependencies")["run"], self.repo, env)

    def gh_calls(self) -> list[list[str]]:
        return [json.loads(line) for line in self.gh_log.read_text().splitlines() if line]

    def pr_create(self) -> list[str] | None:
        calls = [c for c in self.gh_calls() if c[:2] == ["pr", "create"]]
        assert len(calls) <= 1, calls
        return calls[0] if calls else None

    def arg(self, flag: str) -> str:
        call = self.pr_create()
        assert call is not None, "no PR was created"
        return call[call.index(flag) + 1]

    def remote_branches(self) -> list[str]:
        out = subprocess.run(
            ["git", "-C", str(self.origin), "branch", "--format=%(refname:short)"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        return out.split()

    def sync_branch(self) -> str:
        (branch,) = [b for b in self.remote_branches() if b.startswith(f"chore/sync-{self.target}-")]
        return branch

    def remote_lock(self, branch: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.origin), "show", f"{branch}:uv.lock"], capture_output=True, text=True, check=True
        ).stdout


GROUP = {"tornado": "6.5.9", "urllib3": "2.8.0"}


class TestUpdateStep:
    def test_grouped_sync_upgrades_every_package_in_one_pr(self, tmp_path: Path) -> None:
        box = Sandbox(tmp_path)
        result = box.run("tornado urllib3", GROUP)
        assert result.returncode == 0, result.stdout + result.stderr
        assert box.arg("--title") == "chore(deps): bump tornado, urllib3 (security sync from main)"
        lock = box.remote_lock(box.sync_branch())
        assert 'version = "6.5.9"' in lock and 'version = "2.8.0"' in lock

    @pytest.mark.parametrize("target", TARGETS)
    def test_pr_is_opened_against_the_target_with_the_sync_label(self, tmp_path: Path, target: str) -> None:
        box = Sandbox(tmp_path, target=target)
        result = box.run("tornado urllib3", GROUP)
        assert result.returncode == 0, result.stdout + result.stderr
        assert box.arg("--base") == target
        assert "security-sync" in box.arg("--label")
        assert f"applied to {target}'s own lock" in box.arg("--body")
        assert box.arg("--title") == "chore(deps): bump tornado, urllib3 (security sync from main)"

    def test_the_open_pr_dedupe_looks_at_the_target_branch(self, tmp_path: Path) -> None:
        box = Sandbox(tmp_path, target="v3")
        assert box.run("tornado urllib3", GROUP).returncode == 0
        (query,) = [c[1] for c in box.gh_calls() if c[:1] == ["api"]]
        assert "&base=v3&" in query and ":chore/sync-v3-" in query, query

    def test_the_same_update_gets_one_branch_per_target(self, tmp_path: Path) -> None:
        on_v2 = Sandbox(tmp_path / "a", target="v2")
        on_v3 = Sandbox(tmp_path / "b", target="v3")
        assert on_v2.run("tornado urllib3", GROUP).returncode == 0
        assert on_v3.run("tornado urllib3", GROUP).returncode == 0
        assert on_v2.sync_branch().removeprefix("chore/sync-v2-") == on_v3.sync_branch().removeprefix("chore/sync-v3-")

    def test_body_carries_the_versions_and_a_machine_readable_package_list(self, tmp_path: Path) -> None:
        box = Sandbox(tmp_path)
        result = box.run("tornado urllib3", GROUP)
        assert result.returncode == 0, result.stdout + result.stderr
        body = box.arg("--body")
        assert "<!-- security-sync-packages: tornado urllib3 -->" in body
        assert "6.5.8 → 6.5.9" in body and "2.7.0 → 2.8.0" in body
        assert "#217" in body

    def test_long_groups_keep_title_and_branch_bounded(self, tmp_path: Path) -> None:
        # Review of #220: `python-patch` matches `*`, so a group has no size bound;
        # an unbounded title is rejected by GitHub *after* the push.
        names = [f"some-rather-long-package-name-number-{i:02d}" for i in range(12)]
        box = Sandbox(tmp_path, v2_lock=_lock(*[(n, "1.0.0") for n in names]))
        result = box.run(" ".join(names), {n: "1.0.1" for n in names})
        assert result.returncode == 0, result.stdout + result.stderr
        title = box.arg("--title")
        assert len(title) <= GITHUB_TITLE_LIMIT
        assert "and 9 more" in title
        assert len(box.sync_branch()) <= 40
        for name in names:  # nothing is lost: the body lists every package
            assert name in box.arg("--body")

    def test_branch_name_is_deterministic_for_the_same_update(self, tmp_path: Path) -> None:
        first = Sandbox(tmp_path / "a")
        second = Sandbox(tmp_path / "b")
        assert first.run("tornado urllib3", GROUP).returncode == 0
        assert second.run("tornado urllib3", GROUP).returncode == 0
        assert first.sync_branch() == second.sync_branch()

    def test_an_open_sync_pr_is_not_duplicated(self, tmp_path: Path) -> None:
        box = Sandbox(tmp_path)
        result = box.run("tornado urllib3", GROUP, open_prs=1)
        assert result.returncode == 0, result.stdout + result.stderr
        assert box.pr_create() is None
        assert "::notice::" in result.stdout

    def test_a_leftover_branch_without_an_open_pr_is_recreated(self, tmp_path: Path) -> None:
        # Review of #220: the old guard skipped on "branch exists". A push that
        # succeeded before `gh pr create` failed, or a closed PR, then made every
        # re-run exit green with v2 still unsynced.
        box = Sandbox(tmp_path)
        assert box.run("tornado urllib3", GROUP).returncode == 0
        box.gh_log.write_text("")
        subprocess.run(["git", "-C", str(box.repo), "checkout", "-q", box.target], check=True)
        subprocess.run(["git", "-C", str(box.repo), "reset", "-q", "--hard", f"origin/{box.target}"], check=True)
        result = box.run("tornado urllib3", GROUP, open_prs=0)
        assert result.returncode == 0, result.stdout + result.stderr
        assert box.pr_create() is not None

    def test_a_package_v2_cannot_upgrade_is_a_warning_not_a_quiet_success(self, tmp_path: Path) -> None:
        # Review of #220: `cbor2 <6` on v2 makes `uv lock --upgrade-package cbor2`
        # a no-op, which read as "v2 already up to date". A fix that cannot reach
        # v2 must be visible.
        box = Sandbox(tmp_path, main_lock=_lock(("cbor2", "6.1.2")))
        result = box.run("cbor2", {})
        assert result.returncode == 0, result.stdout + result.stderr
        assert "::warning::" in result.stdout
        assert "cbor2" in result.stdout and "6.1.2" in result.stdout
        assert box.pr_create() is None

    def test_held_back_package_is_reported_even_when_the_rest_syncs(self, tmp_path: Path) -> None:
        box = Sandbox(tmp_path, main_lock=_lock(("tornado", "6.5.9"), ("cbor2", "6.1.2")))
        result = box.run("tornado cbor2", {"tornado": "6.5.9"})
        assert result.returncode == 0, result.stdout + result.stderr
        assert "::warning::" in result.stdout and "cbor2" in result.stdout
        assert box.arg("--title") == "chore(deps): bump tornado (security sync from main)"
        assert "cbor2" in box.arg("--body")

    def test_already_up_to_date_is_a_plain_skip(self, tmp_path: Path) -> None:
        box = Sandbox(tmp_path, v2_lock=_lock(("tornado", "6.5.9")), main_lock=_lock(("tornado", "6.5.9")))
        result = box.run("tornado", {})
        assert result.returncode == 0, result.stdout + result.stderr
        assert "::warning::" not in result.stdout
        assert box.pr_create() is None

    def test_a_package_with_several_lock_entries_is_seen_moving(self, tmp_path: Path) -> None:
        # Review of #220: forked resolutions give one name several [[package]]
        # entries; reading only the first missed an upgrade of the second.
        v2_lock = _lock(("anyio", "3.7.1"), ("anyio", "4.13.0"))
        main_lock = _lock(("anyio", "3.7.1"), ("anyio", "4.14.2"))
        box = Sandbox(tmp_path, v2_lock=v2_lock, main_lock=main_lock)
        result = box.run("anyio", {"anyio": ["4.13.0", "4.14.2"]})
        assert result.returncode == 0, result.stdout + result.stderr
        assert box.pr_create() is not None
        assert "4.13.0 → 4.14.2" in box.arg("--body")


# ---------------------------------------------------------------------------
# The sync PR, once it lands on v2
# ---------------------------------------------------------------------------


def _parse_sync_pr(body: str, tmp_path: Path) -> dict[str, str]:
    """Run the automerge job's metadata step on a security-sync PR body."""
    out = tmp_path / "parsed"
    out.write_text("")
    env = {**os.environ, "PR_BODY": body, "GITHUB_OUTPUT": str(out)}
    result = _bash(_step("automerge", "Extract metadata")["run"], tmp_path, env)
    assert result.returncode == 0, result.stdout + result.stderr
    return _outputs(out)


class TestSyncPrMetadata:
    @pytest.mark.parametrize("target", TARGETS)
    def test_package_list_round_trips_through_the_body_marker(self, tmp_path: Path, target: str) -> None:
        box = Sandbox(tmp_path, target=target)
        assert box.run("tornado urllib3", GROUP).returncode == 0
        parsed = _parse_sync_pr(box.arg("--body"), tmp_path)
        assert parsed["dependency-names"] == "tornado, urllib3"

    def test_the_title_is_not_parsed(self) -> None:
        # Review of #220: the greedy `bump (.*) from` title parse broke once (it
        # ran to the "from" in "(security sync from main)"). The list now comes
        # from a structured marker; the title is free text.
        step = _step("automerge", "Extract metadata")
        assert "title" not in json.dumps(step.get("env", {})).lower()
        assert not re.search(r"\bsed\b", step["run"])

    def test_a_body_without_the_marker_does_not_invent_names(self, tmp_path: Path) -> None:
        parsed = _parse_sync_pr("hand-written security-sync PR", tmp_path)
        assert parsed["dependency-names"] == "(see PR)"

    def test_a_tampered_marker_is_not_passed_on(self, tmp_path: Path) -> None:
        parsed = _parse_sync_pr("<!-- security-sync-packages: tornado $(id) -->", tmp_path)
        assert parsed["dependency-names"] == "(see PR)"

    def test_unused_version_outputs_are_gone(self) -> None:
        # Review of #220: previous/new versions were no longer read anywhere.
        outputs = _workflow()["jobs"]["automerge"]["outputs"]
        assert "previous-version-dep" not in outputs and "new-version-dep" not in outputs
