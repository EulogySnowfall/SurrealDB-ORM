"""Tests for the automerge workflow's "Sync to V2" job.

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

yaml = pytest.importorskip("yaml", reason="pyyaml required for workflow lint tests")

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "dependabot-automerge.yml"
DEPENDABOT = ROOT / ".github" / "dependabot.yml"

GITHUB_TITLE_LIMIT = 256


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


def _run_gate(tmp_path: Path, ecosystem: str) -> subprocess.CompletedProcess[str]:
    out = tmp_path / "gate"
    out.write_text("")
    env = {**os.environ, "ECOSYSTEM": ecosystem, "GITHUB_OUTPUT": str(out)}
    return _bash(_step("sync-v2", "Decide whether")["run"], tmp_path, env)


class TestGate:
    def test_uv_updates_are_synced(self, tmp_path: Path) -> None:
        result = _run_gate(tmp_path, "uv")
        assert result.returncode == 0, result.stdout + result.stderr
        assert _outputs(tmp_path / "gate")["sync"] == "true"

    @pytest.mark.parametrize("ecosystem", ["github_actions", "pip", "docker"])
    def test_other_ecosystems_skip_with_a_stated_reason(self, tmp_path: Path, ecosystem: str) -> None:
        # Regression (review of #220): the name check rejected `actions/checkout`
        # and turned every GitHub Actions bump red. Those have nothing to sync.
        result = _run_gate(tmp_path, ecosystem)
        assert result.returncode == 0, result.stdout + result.stderr
        assert _outputs(tmp_path / "gate")["sync"] == "false"
        assert "::notice::" in result.stdout

    def test_missing_ecosystem_fails_loudly(self, tmp_path: Path) -> None:
        result = _run_gate(tmp_path, "")
        assert result.returncode != 0
        assert "::error::" in result.stdout

    def test_every_later_step_is_behind_the_gate(self) -> None:
        steps = _workflow()["jobs"]["sync-v2"]["steps"]
        names = [s.get("name", "") for s in steps]
        gate = next(i for i, n in enumerate(names) if n.startswith("Decide whether"))
        for step in steps[gate + 1 :]:
            assert "steps.gate.outputs.sync == 'true'" in step.get("if", ""), step.get("name")


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


# ---------------------------------------------------------------------------
# Which packages exist on v2
# ---------------------------------------------------------------------------


def _run_check(tmp_path: Path, dep_names: str) -> subprocess.CompletedProcess[str]:
    (tmp_path / "uv.lock").write_text(V2_LOCK)
    out = tmp_path / "out"
    out.write_text("")
    env = {**os.environ, "DEP_NAME": dep_names, "GITHUB_OUTPUT": str(out)}
    return _bash(_step("sync-v2", "Check which")["run"], tmp_path, env)


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
    """A v2 checkout whose `origin` also has `main`, plus fake uv/gh."""

    def __init__(self, tmp_path: Path, v2_lock: str = V2_LOCK, main_lock: str | None = None) -> None:
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.tmp = tmp_path
        self.repo = tmp_path / "repo"
        self.origin = tmp_path / "origin.git"
        self.gh_log = tmp_path / "gh.log"
        self.gh_log.write_text("")
        subprocess.run(["git", "init", "-q", "--bare", str(self.origin)], check=True)

        seed = tmp_path / "seed"
        subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
        self._commit(seed, main_lock if main_lock is not None else v2_lock, "main")
        subprocess.run(["git", "-C", str(seed), "push", "-q", str(self.origin), "main"], check=True)
        subprocess.run(["git", "-C", str(seed), "checkout", "-q", "--orphan", "v2"], check=True)
        self._commit(seed, v2_lock, "v2")
        subprocess.run(["git", "-C", str(seed), "push", "-q", str(self.origin), "v2"], check=True)
        subprocess.run(["git", "clone", "-q", "-b", "v2", str(self.origin), str(self.repo)], check=True)

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
            "GH_TOKEN": "x",
            "GITHUB_REPOSITORY": "owner/repo",
            "UPDATE_TYPE": "version-update:semver-patch",
            "SOURCE_PR": "217",
            "FAKE_UV_UPGRADES": json.dumps(upgrades),
            "FAKE_GH_OPEN_PRS": str(open_prs),
            "FAKE_GH_LOG": str(self.gh_log),
            "GITHUB_STEP_SUMMARY": str(self.tmp / "summary.md"),
        }
        return _bash(_step("sync-v2", "Update dependencies")["run"], self.repo, env)

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
        (branch,) = [b for b in self.remote_branches() if b.startswith("chore/sync-v2-")]
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

    def test_pr_is_opened_against_v2_with_the_sync_label(self, tmp_path: Path) -> None:
        box = Sandbox(tmp_path)
        result = box.run("tornado urllib3", GROUP)
        assert result.returncode == 0, result.stdout + result.stderr
        assert box.arg("--base") == "v2"
        assert "security-sync" in box.arg("--label")

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
        subprocess.run(["git", "-C", str(box.repo), "checkout", "-q", "v2"], check=True)
        subprocess.run(["git", "-C", str(box.repo), "reset", "-q", "--hard", "origin/v2"], check=True)
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
    def test_package_list_round_trips_through_the_body_marker(self, tmp_path: Path) -> None:
        box = Sandbox(tmp_path)
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
