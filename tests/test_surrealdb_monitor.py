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

import json
import os
import re
import stat
import subprocess
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests._release_series import JobRun, bump_repo, owner_repo, pyproject, remote_refs, run_job

yaml = pytest.importorskip("yaml", reason="pyyaml required for workflow lint tests")

WORKFLOW_DIR = Path(__file__).resolve().parent.parent / ".github" / "workflows"

MONITORS = ["surrealdb-security.yml", "surrealdb-v2-security.yml", "surrealdb-v3-security.yml"]

_BLOCK_START = "# --- Compare:"
# The v3 branch monitor has no `force_update` input, so its block ends at the
# security lookup instead.
_BLOCK_ENDS = ("# Force update overrides the check", "# --- Check if security patch")


def _compare_block(workflow: str) -> str:
    """Extract the version-comparison shell block from a monitor workflow."""
    text = (WORKFLOW_DIR / workflow).read_text(encoding="utf-8")
    start = text.index(_BLOCK_START)
    end = min(text.find(marker, start) for marker in _BLOCK_ENDS if text.find(marker, start) != -1)
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


# ---------------------------------------------------------------------------
# #234: which release the monitors *select*
# ---------------------------------------------------------------------------
#
# The no-downgrade guard above is only as good as the candidate it is handed.
# The monitors took the first matching release in API order
# (`[.[] | select(...) | .tag_name][0]`), but the releases API lists by tag
# creation date, not by version: SurrealDB published the v3.2.6 backport after
# v3.3.4, so the 3.x monitor picked 3.2.6, the guard rightly refused it as a
# downgrade, and the run went green with "nothing to do" while the pin sat at
# 3.3.0. The v2 monitor has the same query, and its `releases/latest` fallback
# (3.x only) was the newest release of *any* line — a 4.0.0 would be proposed.
#
# These tests run each monitor's whole "Compare versions" step, as GitHub would
# (`bash -e`, inputs expanded), against a fake `gh` serving a fixture of
# releases in API order.

FAKE_RELEASES_GH = r'''#!/usr/bin/env python3
"""Fake `gh api` over a fixture of GitHub releases listed in API order.

GH_STUB_RELEASES   JSON list of releases (tag_name, prerelease, draft, name, body)
GH_STUB_PAGE_SIZE  releases per page (default 30, the API's)
GH_STUB_FAIL       "1" makes every call fail, like an outage or a rate limit
GH_STUB_LOG        each call is appended as one JSON line

The list endpoint accepts `?per_page=N`, but page boundaries always come from
GH_STUB_PAGE_SIZE, so a test can put a release on a later page. A missing
release answers as gh does: the 404 body on stdout, unfiltered by `--jq`, and
exit status 1.

`--jq` is evaluated for the few filter shapes the workflows use; anything else
is refused (exit 3) rather than guessed, so a filter edited into a shape this
stub does not understand fails the tests instead of being misread. As with gh,
`--paginate` applies the filter to every page, and without it only the first
page is served.
"""
import json, os, re, sys

BASE = "repos/surrealdb/surrealdb/releases"
NOT_FOUND = (
    '{"message":"Not Found","documentation_url":'
    '"https://docs.github.com/rest/releases/releases#get-a-release-by-tag-name","status":"404"}'
)


def refuse(msg):
    sys.stderr.write("gh stub: " + msg + "\n")
    sys.exit(3)


def not_found():
    print(NOT_FOUND)
    sys.stderr.write("gh: Not Found (HTTP 404)\n")
    return 1


def emit(value):
    print("null" if value is None else value)


def split_pipes(expr):
    parts, depth, cur = [], 0, ""
    for ch in expr:
        depth += {"(": 1, ")": -1}.get(ch, 0)
        if ch == "|" and depth == 0:
            parts.append(cur.strip())
            cur = ""
        else:
            cur += ch
    parts.append(cur.strip())
    return parts


def select(rows, stage):
    m = re.fullmatch(r'select\(\.tag_name \| startswith\("([^"]*)"\)\)', stage)
    if m:
        return [r for r in rows if r["tag_name"].startswith(m.group(1))]
    m = re.fullmatch(r'select\(\.tag_name == "([^"]*)"\)', stage)
    if m:
        return [r for r in rows if r["tag_name"] == m.group(1)]
    m = re.fullmatch(r"select\(\.(prerelease|draft) == (true|false)\)", stage)
    if m:
        return [r for r in rows if r[m.group(1)] is (m.group(2) == "true")]
    refuse("unsupported jq stage: " + stage)


def run_list(jq, page):
    jq = jq.strip()
    first = re.fullmatch(r"\[(.*)\]\[0\](?:\.(\w+))?", jq)
    stages = split_pipes(first.group(1) if first else jq)
    if stages[0] != ".[]":
        refuse("unsupported jq filter: " + jq)
    project = stages[-1] == ".tag_name"
    rows = page
    for stage in stages[1:-1] if project else stages[1:]:
        rows = select(rows, stage)
    if first:
        head = rows[0] if rows else None
        field = first.group(2) or ("tag_name" if project else None)
        if field is None:
            refuse("unsupported jq filter: " + jq)
        emit(None if head is None else head.get(field))
    elif project:
        for row in rows:
            print(row["tag_name"])
    else:
        refuse("unsupported jq filter: " + jq)


def run_object(jq, obj):
    m = re.fullmatch(r"\.(\w+)", (jq or "").strip())
    if not m:
        refuse("unsupported jq filter on one release: %r" % jq)
    emit(obj.get(m.group(1)))


def main(argv):
    if os.environ.get("GH_STUB_LOG"):
        with open(os.environ["GH_STUB_LOG"], "a", encoding="utf-8") as log:
            log.write(json.dumps(argv) + "\n")
    if not argv or argv[0] != "api":
        refuse("unsupported command: %r" % argv)
    endpoint, jq, paginate = None, None, False
    args = iter(argv[1:])
    for arg in args:
        if arg == "--paginate":
            paginate = True
        elif arg in ("--jq", "-q"):
            jq = next(args)
        elif arg.startswith("-") or endpoint is not None:
            refuse("unsupported argument: %r" % arg)
        else:
            endpoint = arg
    if os.environ.get("GH_STUB_FAIL") == "1":
        sys.stderr.write("gh: HTTP 503 Service Unavailable\n")
        return 1
    with open(os.environ["GH_STUB_RELEASES"], encoding="utf-8") as fh:
        releases = json.load(fh)
    endpoint, _, query = (endpoint or "").partition("?")
    if query and not re.fullmatch(r"per_page=[0-9]+", query):
        refuse("unsupported query: %r" % query)
    if endpoint == BASE:
        size = int(os.environ.get("GH_STUB_PAGE_SIZE", "30"))
        pages = [releases[i : i + size] for i in range(0, len(releases), size)] or [[]]
        for page in pages if paginate else pages[:1]:
            run_list(jq, page)
        return 0
    if endpoint == BASE + "/latest":
        stable = [r for r in releases if not r["prerelease"] and not r["draft"]]
        if not stable:
            return not_found()
        run_object(jq, stable[0])
        return 0
    if endpoint.startswith(BASE + "/tags/"):
        tag = endpoint[len(BASE + "/tags/") :]
        found = [r for r in releases if r["tag_name"] == tag and not r["draft"]]
        if not found:
            return not_found()
        run_object(jq, found[0])
        return 0
    refuse("unsupported endpoint: %r" % endpoint)


sys.exit(main(sys.argv[1:]))
'''


def _rel(tag: str, *, prerelease: bool = False, draft: bool = False, name: str = "", body: str = "") -> dict[str, object]:
    return {"tag_name": tag, "prerelease": prerelease, "draft": draft, "name": name or f"Release {tag}", "body": body}


# `gh api repos/surrealdb/surrealdb/releases` as it answered on 2026-10-27
# (first page, API order): the v3.2.6 backport is listed above v3.3.4.
REAL_API_ORDER: list[dict[str, object]] = [
    _rel("v3.2.6"),
    _rel("v3.3.4"),
    _rel("v3.3.3"),
    _rel("v3.3.2"),
    _rel("v3.3.1"),
    _rel("v3.2.5"),
    _rel("v3.3.0"),
    _rel("v2.7.0"),
    _rel("v3.3.0-beta.4", prerelease=True),
    _rel("v3.3.0-beta.3", prerelease=True),
    _rel("v3.3.0-beta.2", prerelease=True),
    _rel("v3.3.0-beta.1", prerelease=True),
    _rel("v3.1.6"),
    _rel("v3.2.4"),
    _rel("v3.2.3"),
    _rel("v3.2.2"),
    _rel("v3.2.1"),
    _rel("v3.2.0"),
    _rel("v3.2.0-beta.3", prerelease=True),
    _rel("v3.2.0-beta.2", prerelease=True),
    _rel("v3.2.0-beta.1", prerelease=True),
    _rel("v3.1.5"),
    _rel("v3.1.4"),
    _rel("v3.1.3"),
    _rel("v3.1.2"),
    _rel("v3.1.1"),
    _rel("v3.1.0"),
    _rel("v3.1.0-beta.3", prerelease=True),
    _rel("v3.1.0-beta.2", prerelease=True),
    _rel("v3.1.0-beta.1", prerelease=True),
]

V3 = "surrealdb-security.yml"
V2 = "surrealdb-v2-security.yml"
V3_BRANCH = "surrealdb-v3-security.yml"
RELEASES = "repos/surrealdb/surrealdb/releases"

_EXPR = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")


_CHECK_OUTPUTS = "needs.check-release.outputs."


def _expand(text: str, inputs: dict[str, str], needs: dict[str, str] | None = None) -> str:
    """Expand the `${{ }}` expressions GitHub substitutes before bash runs.

    A dispatch input that was not given (or a scheduled run, which has none)
    expands to an empty string, and so does a `check-release` output missing
    from `needs`. Anything other than an input, a secret or a `check-release`
    output is refused, so the tests never run a script that differs from the
    real one.
    """

    def sub(match: re.Match[str]) -> str:
        expr = match.group(1)
        if expr.startswith("inputs."):
            return inputs.get(expr.removeprefix("inputs."), "")
        if expr.startswith("secrets."):
            return "stub-token"
        if expr.startswith(_CHECK_OUTPUTS):
            return (needs or {}).get(expr.removeprefix(_CHECK_OUTPUTS), "")
        raise AssertionError(f"unexpected expression in a monitored step: ${{{{ {expr} }}}}")

    return _EXPR.sub(sub, text)


def _parse_outputs(text: str) -> dict[str, str]:
    """Read a `$GITHUB_OUTPUT` file, including `key<<DELIM` blocks."""
    outputs: dict[str, str] = {}
    lines = iter(text.splitlines())
    for line in lines:
        key, sep, value = line.partition("=")
        if sep and "<<" not in key:
            outputs[key] = value
        elif "<<" in line:
            key, delim = line.split("<<", 1)
            body: list[str] = []
            for inner in lines:
                if inner == delim:
                    break
                body.append(inner)
            outputs[key] = "\n".join(body)
    return outputs


@dataclass
class StepRun:
    returncode: int
    outputs: dict[str, str]
    log: str
    calls: list[list[str]]

    @property
    def latest(self) -> str | None:
        return self.outputs.get("latest-version")


def _run_step(
    tmp_path: Path,
    workflow: str,
    job: str,
    step_name: str,
    *,
    releases: list[dict[str, object]],
    pin: str,
    inputs: dict[str, str] | None = None,
    page_size: int = 30,
    fail: bool = False,
    needs: dict[str, str] | None = None,
) -> StepRun:
    """Run one workflow step's `run:` block against the fake `gh`."""
    data = yaml.safe_load((WORKFLOW_DIR / workflow).read_text(encoding="utf-8"))
    step = next(s for s in data["jobs"][job]["steps"] if s.get("name") == step_name)
    inputs = inputs or {}

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(FAKE_RELEASES_GH, encoding="utf-8")
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    fixture = tmp_path / "releases.json"
    fixture.write_text(json.dumps(releases), encoding="utf-8")
    (tmp_path / ".surrealdb-version").write_text(pin + "\n", encoding="utf-8")
    output = tmp_path / "github_output"
    output.write_text("", encoding="utf-8")
    gh_log = tmp_path / "gh.log"
    gh_log.write_text("", encoding="utf-8")

    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GITHUB_OUTPUT": str(output),
        "GH_STUB_RELEASES": str(fixture),
        "GH_STUB_PAGE_SIZE": str(page_size),
        "GH_STUB_FAIL": "1" if fail else "0",
        "GH_STUB_LOG": str(gh_log),
    }
    env.update({key: _expand(str(value), inputs, needs) for key, value in (step.get("env") or {}).items()})
    script = tmp_path / "step.sh"
    script.write_text(_expand(step["run"], inputs, needs), encoding="utf-8")
    # GitHub runs a `run:` block with no `shell:` as `bash -e {0}`.
    result = subprocess.run(["bash", "-e", str(script)], cwd=tmp_path, env=env, capture_output=True, text=True)
    return StepRun(
        returncode=result.returncode,
        outputs=_parse_outputs(output.read_text(encoding="utf-8")),
        log=result.stdout + result.stderr,
        calls=[json.loads(line) for line in gh_log.read_text(encoding="utf-8").splitlines() if line],
    )


def _monitor(tmp_path: Path, workflow: str, **kwargs: Any) -> StepRun:
    return _run_step(tmp_path, workflow, "check-release", "Compare versions", **kwargs)


class TestNewestReleaseIsSelected:
    """Regression tests for #234: newest by version, not first in API order."""

    def test_v3_picks_the_newest_stable_not_the_first_listed(self, tmp_path: Path) -> None:
        run = _monitor(tmp_path, V3, releases=REAL_API_ORDER, pin="3.3.0")
        assert run.returncode == 0, run.log
        assert run.latest == "3.3.4", f"v3.2.6 is listed first but 3.3.4 is newer (#234)\n{run.log}"
        assert run.outputs["has-update"] == "true", run.log

    def test_v3_on_the_newest_stable_is_up_to_date(self, tmp_path: Path) -> None:
        run = _monitor(tmp_path, V3, releases=REAL_API_ORDER, pin="3.3.4")
        assert run.returncode == 0, run.log
        assert run.latest == "3.3.4", run.log
        assert run.outputs["has-update"] == "false", run.log

    def test_v2_picks_the_newest_stable_not_the_first_listed(self, tmp_path: Path) -> None:
        releases = [*REAL_API_ORDER[:7], _rel("v2.6.6"), *REAL_API_ORDER[7:]]
        assert [r["tag_name"] for r in releases].index("v2.6.6") < [r["tag_name"] for r in releases].index("v2.7.0")
        run = _monitor(tmp_path, V2, releases=releases, pin="2.6.5")
        assert run.returncode == 0, run.log
        assert run.latest == "2.7.0", f"v2.6.6 is listed first but 2.7.0 is newer (#234)\n{run.log}"
        assert run.outputs["has-update"] == "true", run.log

    @pytest.mark.parametrize(
        ("workflow", "releases", "pin", "expected"),
        [
            # Page size 2: the newest release of the line is only on page 3.
            (V3, [_rel("v3.2.7"), _rel("v3.2.6"), _rel("v2.7.1"), _rel("v2.7.0"), _rel("v3.3.5")], "3.3.4", "3.3.5"),
            (V2, [_rel("v2.6.7"), _rel("v2.6.6"), _rel("v3.3.4"), _rel("v3.3.3"), _rel("v2.7.1")], "2.7.0", "2.7.1"),
        ],
    )
    def test_every_page_is_read(
        self, tmp_path: Path, workflow: str, releases: list[dict[str, object]], pin: str, expected: str
    ) -> None:
        run = _monitor(tmp_path, workflow, releases=releases, pin=pin, page_size=2)
        assert run.returncode == 0, run.log
        assert run.latest == expected, f"{workflow}: the newest release was on a later page\n{run.log}"
        assert run.outputs["has-update"] == "true", run.log

    @pytest.mark.parametrize(
        ("workflow", "releases", "pin", "expected"),
        [
            (
                V3,
                [
                    _rel("v3.4.0", draft=True),
                    # Mislabelled: a pre-release tag without the pre-release flag.
                    # `sort -V` ranks it above 3.3.4, so it must be dropped first.
                    _rel("v3.4.0-rc.1"),
                    _rel("v3.4.0-beta.2", prerelease=True),
                    _rel("v4.0.0"),
                    _rel("v2.7.1"),
                    *REAL_API_ORDER,
                ],
                "3.3.0",
                "3.3.4",
            ),
            (
                V2,
                [
                    _rel("v2.8.0", draft=True),
                    _rel("v2.8.0-rc.1"),
                    _rel("v2.8.0-beta.1", prerelease=True),
                    _rel("v3.3.5"),
                    *REAL_API_ORDER,
                ],
                "2.6.5",
                "2.7.0",
            ),
        ],
    )
    def test_never_a_draft_a_prerelease_or_another_major(
        self, tmp_path: Path, workflow: str, releases: list[dict[str, object]], pin: str, expected: str
    ) -> None:
        run = _monitor(tmp_path, workflow, releases=releases, pin=pin)
        assert run.returncode == 0, run.log
        assert run.latest == expected, run.log

    @pytest.mark.parametrize(
        ("workflow", "releases", "pin"),
        [
            # No stable 3.x release at all, and the newest release overall is 4.0.0.
            (V3, [_rel("v4.0.0"), _rel("v3.4.0-beta.1", prerelease=True), _rel("v2.7.0")], "3.3.4"),
            (V2, [_rel("v4.0.0"), _rel("v3.3.4"), _rel("v2.8.0-beta.1", prerelease=True)], "2.7.0"),
        ],
    )
    def test_no_release_of_the_line_is_a_no_op_not_a_fallback(
        self, tmp_path: Path, workflow: str, releases: list[dict[str, object]], pin: str
    ) -> None:
        run = _monitor(tmp_path, workflow, releases=releases, pin=pin)
        assert run.returncode == 0, run.log
        assert run.outputs.get("has-update") == "false", (
            f"{workflow}: with no stable release of its own line the monitor must do nothing — "
            f"`releases/latest` is the newest release of any line, here 4.0.0\n{run.log}"
        )
        assert run.latest in (None, ""), run.log
        assert not any(arg.partition("?")[0].endswith("/releases/latest") for call in run.calls for arg in call), run.calls

    @pytest.mark.parametrize("workflow", [V3, V2, V3_BRANCH])
    def test_an_unreachable_api_fails_the_run(self, tmp_path: Path, workflow: str) -> None:
        """An outage must not read as "nothing to do": the run goes red."""
        run = _monitor(tmp_path, workflow, releases=REAL_API_ORDER, pin="2.6.5" if workflow == V2 else "3.3.0", fail=True)
        assert run.returncode != 0, f"{workflow}: an API failure ended green\n{run.log}"
        assert run.outputs.get("has-update") != "true", run.log

    @pytest.mark.parametrize("workflow", [V3, V2, V3_BRANCH])
    def test_the_list_is_read_100_releases_per_page(self, tmp_path: Path, workflow: str) -> None:
        """The API's default page is 30 releases: six requests per run in 2026, and growing."""
        run = _monitor(tmp_path, workflow, releases=REAL_API_ORDER, pin="2.7.0" if workflow == V2 else "3.3.4")
        assert run.returncode == 0, run.log
        list_calls = [call for call in run.calls if any(arg.partition("?")[0] == RELEASES for arg in call)]
        assert list_calls, run.calls
        assert all(f"{RELEASES}?per_page=100" in call for call in list_calls), list_calls


class TestSecurityDetectionFollowsTheChosenRelease:
    """The notes and title are looked up by tag, not on the first page (#234).

    The old lookup scanned the first page of the list for the chosen tag. A
    release outside it read as "no notes", so a security release would have
    been pinned without the version bump that ships it.
    """

    @pytest.mark.parametrize(
        ("workflow", "tag", "pin", "notes", "title"),
        [
            (V3, "v3.3.4", "3.3.0", "Fixes CVE-2026-12345 in the HTTP layer.", ""),
            (V3, "v3.3.4", "3.3.0", "Bug fixes.", "Security hotfix 3.3.4"),
            (V2, "v2.7.0", "2.6.5", "Patches a privilege escalation vulnerability.", ""),
        ],
    )
    def test_security_release_beyond_the_first_page(
        self, tmp_path: Path, workflow: str, tag: str, pin: str, notes: str, title: str
    ) -> None:
        releases = [{**r, "name": title or r["name"], "body": notes} if r["tag_name"] == tag else r for r in REAL_API_ORDER]
        # One release per page: the chosen one is never on the first page.
        run = _monitor(tmp_path, workflow, releases=releases, pin=pin, page_size=1)
        assert run.returncode == 0, run.log
        assert run.latest == tag.removeprefix("v"), run.log
        assert run.outputs["is-security-patch"] == "true", f"{workflow}: security release not detected\n{run.log}"

    @pytest.mark.parametrize(("workflow", "version", "pin"), [(V3, "3.3.5", "3.3.4"), (V2, "2.7.1", "2.7.0")])
    def test_a_dispatched_version_without_a_release_entry(self, tmp_path: Path, workflow: str, version: str, pin: str) -> None:
        """Image tags can exist without a GitHub Release (#155): the lookup must not abort the run."""
        run = _monitor(tmp_path, workflow, releases=REAL_API_ORDER, pin=pin, inputs={"surrealdb_version": version})
        assert run.returncode == 0, run.log
        assert run.latest == version, run.log
        assert run.outputs["has-update"] == "true", run.log
        assert run.outputs["is-security-patch"] == "false", run.log


def test_v3_dispatched_version_of_another_major_is_refused(tmp_path: Path) -> None:
    """The 3.x monitor never proposes another major, even on a manual dispatch."""
    run = _monitor(tmp_path, V3, releases=REAL_API_ORDER, pin="3.3.4", inputs={"surrealdb_version": "4.0.0"})
    assert run.returncode != 0, f"a dispatched 4.0.0 was accepted\n{run.log}"
    assert run.outputs.get("has-update") != "true", run.log


@pytest.mark.parametrize(
    ("workflow", "version", "pin"),
    [
        (V3, "3.3", "3.3.4"),
        (V3, "3.3.5 ", "3.3.4"),
        (V3, "3.3.x", "3.3.4"),
        (V3, "3.3.5\n3.3.6", "3.3.4"),
        (V2, "2.7", "2.7.0"),
        (V2, "2.7.1 ", "2.7.0"),
        (V2, "2.x", "2.7.0"),
    ],
)
def test_a_dispatched_version_must_be_a_whole_version_of_the_line(
    tmp_path: Path, workflow: str, version: str, pin: str
) -> None:
    """The dispatched value becomes the pin, the image tag and the PR title.

    Only the text before the first dot was checked, so `3.3` (a floating image
    tag) or a value with trailing text was accepted as a candidate.
    """
    run = _monitor(tmp_path, workflow, releases=REAL_API_ORDER, pin=pin, inputs={"surrealdb_version": version})
    assert run.returncode != 0, f"{workflow}: dispatched {version!r} was accepted\n{run.log}"
    assert "has-update" not in run.outputs, run.log
    assert "latest-version" not in run.outputs, run.log


@pytest.mark.parametrize("workflow", [*MONITORS, CANARY])
def test_inputs_reach_the_scripts_through_env_only(workflow: str) -> None:
    """A `${{ inputs.* }}` expanded into a `run:` block becomes part of the script text.

    Every input goes through the step's `env:` instead, as the canary has done
    from the start, and the script reads it as a variable.
    """
    data = yaml.safe_load((WORKFLOW_DIR / workflow).read_text(encoding="utf-8"))
    for job_name, job in data["jobs"].items():
        for step in job.get("steps", []):
            script = step.get("run") or ""
            assert not re.search(r"\$\{\{\s*(github\.event\.)?inputs\.", script), (
                f"{workflow}: job {job_name!r}, step {step.get('name')!r} expands an input into its script"
            )


def _canary(tmp_path: Path, **kwargs: Any) -> StepRun:
    return _run_step(tmp_path, CANARY, "check-prerelease", "Resolve pre-release", **kwargs)


class TestCanaryPicksTheNewestPrerelease:
    """The canary had the same `[0]` selection, among pre-releases (#234)."""

    @pytest.mark.parametrize(
        ("releases", "expected"),
        [
            # A backport pre-release on an older line, created last, listed first.
            (
                [
                    _rel("v3.2.7-beta.1", prerelease=True),
                    _rel("v3.4.0-beta.2", prerelease=True),
                    _rel("v3.4.0-beta.1", prerelease=True),
                    *REAL_API_ORDER,
                ],
                "3.4.0-beta.2",
            ),
            # Numeric, not lexical.
            (
                [_rel("v3.4.0-beta.9", prerelease=True), _rel("v3.4.0-beta.10", prerelease=True), *REAL_API_ORDER],
                "3.4.0-beta.10",
            ),
        ],
    )
    def test_newest_prerelease_by_version(self, tmp_path: Path, releases: list[dict[str, object]], expected: str) -> None:
        run = _canary(tmp_path, releases=releases, pin="3.3.4")
        assert run.returncode == 0, run.log
        assert run.outputs["version"] == expected, run.log
        assert run.outputs["found"] == "true", run.log

    def test_every_page_is_read(self, tmp_path: Path) -> None:
        releases = [_rel("v3.3.4"), _rel("v3.3.3"), _rel("v3.4.0-beta.1", prerelease=True)]
        run = _canary(tmp_path, releases=releases, pin="3.3.4", page_size=1)
        assert run.returncode == 0, run.log
        assert run.outputs["version"] == "3.4.0-beta.1", run.log

    def test_no_prerelease_ahead_of_the_pin(self, tmp_path: Path) -> None:
        run = _canary(tmp_path, releases=REAL_API_ORDER, pin="3.3.4")
        assert run.returncode == 0, run.log
        assert run.outputs["found"] == "false", run.log

    def test_an_unreachable_api_fails_the_run(self, tmp_path: Path) -> None:
        """An outage must not read as "No open v3.x pre-release": the run goes red."""
        run = _canary(tmp_path, releases=REAL_API_ORDER, pin="3.3.4", fail=True)
        assert run.returncode != 0, f"an API failure ended green\n{run.log}"
        assert run.outputs.get("found") != "true", run.log

    def test_the_list_is_read_100_releases_per_page(self, tmp_path: Path) -> None:
        run = _canary(tmp_path, releases=REAL_API_ORDER, pin="3.3.4")
        assert run.returncode == 0, run.log
        assert run.calls, run.log
        assert all(f"{RELEASES}?per_page=100" in call for call in run.calls), run.calls


# ---------------------------------------------------------------------------
# #579: the monitor of the permanent `v3` branch
# ---------------------------------------------------------------------------
#
# `v3` carries the 3.x line once `main` moves to SurrealDB 4.x. It takes bug and
# security fixes only, so its monitor is stricter than the others: it proposes
# a pin PR for a newer stable release of the pinned 3.3 line *and only when that
# release is a security release*. Never another minor, never 4.x, never a
# downgrade, never a routine release. Like the v2 monitor it lives on `main`
# (schedules only run from the default branch) and works on `v3`.

_CVE = "Fixes CVE-2026-40001 in the HTTP layer."


def _v3_branch(tmp_path: Path, **kwargs: Any) -> StepRun:
    return _monitor(tmp_path, V3_BRANCH, **kwargs)


def _v3_branch_workflow() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((WORKFLOW_DIR / V3_BRANCH).read_text(encoding="utf-8"))
    return data


class TestV3BranchMonitorSelection:
    def test_a_newer_3_3_security_release_is_proposed(self, tmp_path: Path) -> None:
        releases = [_rel("v3.3.6", body=_CVE), *REAL_API_ORDER]
        run = _v3_branch(tmp_path, releases=releases, pin="3.3.5")
        assert run.returncode == 0, run.log
        assert run.latest == "3.3.6", run.log
        assert run.outputs["is-security-patch"] == "true", run.log
        assert run.outputs["has-update"] == "true", run.log

    def test_a_security_title_is_enough(self, tmp_path: Path) -> None:
        releases = [_rel("v3.3.6", name="Security hotfix 3.3.6", body="Bug fixes."), *REAL_API_ORDER]
        run = _v3_branch(tmp_path, releases=releases, pin="3.3.5")
        assert run.returncode == 0, run.log
        assert run.outputs["has-update"] == "true", run.log

    def test_a_newer_routine_release_is_not_proposed(self, tmp_path: Path) -> None:
        """The v3 branch takes security releases only: a bug-fix release leaves the pin alone."""
        releases = [_rel("v3.3.6", body="Bug fixes and performance improvements."), *REAL_API_ORDER]
        run = _v3_branch(tmp_path, releases=releases, pin="3.3.5")
        assert run.returncode == 0, run.log
        assert run.outputs["is-security-patch"] == "false", run.log
        assert run.outputs["has-update"] == "false", f"a routine release was proposed for v3\n{run.log}"

    @pytest.mark.parametrize("tag", ["v3.4.0", "v3.10.0", "v4.0.0", "v4.0.1"])
    def test_never_another_minor_or_major_even_a_security_one(self, tmp_path: Path, tag: str) -> None:
        releases = [_rel(tag, body=_CVE), *REAL_API_ORDER]
        run = _v3_branch(tmp_path, releases=releases, pin="3.3.5")
        assert run.returncode == 0, run.log
        assert run.outputs.get("has-update") == "false", f"{tag} was proposed for the 3.3 line\n{run.log}"
        assert run.latest != tag.removeprefix("v"), run.log

    def test_the_newest_3_3_release_wins_over_a_newer_line(self, tmp_path: Path) -> None:
        releases = [_rel("v3.4.1", body=_CVE), _rel("v4.0.0", body=_CVE), _rel("v3.3.7", body=_CVE), *REAL_API_ORDER]
        run = _v3_branch(tmp_path, releases=releases, pin="3.3.5")
        assert run.returncode == 0, run.log
        assert run.latest == "3.3.7", run.log
        assert run.outputs["has-update"] == "true", run.log

    def test_never_a_downgrade_even_for_a_security_release(self, tmp_path: Path) -> None:
        releases = [{**r, "body": _CVE} if r["tag_name"] == "v3.3.4" else r for r in REAL_API_ORDER]
        run = _v3_branch(tmp_path, releases=releases, pin="3.3.5")
        assert run.returncode == 0, run.log
        assert run.outputs["has-update"] == "false", run.log

    def test_on_the_newest_release_is_up_to_date(self, tmp_path: Path) -> None:
        releases = [_rel("v3.3.5", body=_CVE), *REAL_API_ORDER]
        run = _v3_branch(tmp_path, releases=releases, pin="3.3.5")
        assert run.returncode == 0, run.log
        assert run.outputs["has-update"] == "false", run.log

    def test_no_3_3_release_at_all_is_a_no_op(self, tmp_path: Path) -> None:
        releases = [_rel("v4.0.0", body=_CVE), _rel("v3.4.0", body=_CVE), _rel("v2.7.0")]
        run = _v3_branch(tmp_path, releases=releases, pin="3.3.5")
        assert run.returncode == 0, run.log
        assert run.outputs.get("has-update") == "false", run.log
        assert not any(arg.partition("?")[0].endswith("/releases/latest") for call in run.calls for arg in call), run.calls

    def test_every_page_is_read(self, tmp_path: Path) -> None:
        releases = [_rel("v3.2.7"), _rel("v3.2.6"), _rel("v2.7.1"), _rel("v2.7.0"), _rel("v3.3.6", body=_CVE)]
        run = _v3_branch(tmp_path, releases=releases, pin="3.3.5", page_size=2)
        assert run.returncode == 0, run.log
        assert run.latest == "3.3.6", run.log
        assert run.outputs["has-update"] == "true", run.log

    def test_a_mislabelled_prerelease_or_a_draft_is_never_proposed(self, tmp_path: Path) -> None:
        releases = [
            _rel("v3.3.7", draft=True, body=_CVE),
            _rel("v3.3.7-rc.1", body=_CVE),
            _rel("v3.3.7-beta.1", prerelease=True, body=_CVE),
            *REAL_API_ORDER,
        ]
        run = _v3_branch(tmp_path, releases=releases, pin="3.3.5")
        assert run.returncode == 0, run.log
        assert run.outputs["has-update"] == "false", run.log

    def test_release_query_excludes_prereleases_and_drafts(self) -> None:
        text = (WORKFLOW_DIR / V3_BRANCH).read_text(encoding="utf-8")
        query = next(line for line in text.splitlines() if 'startswith("v3.")' in line)
        assert "select(.prerelease == false)" in query
        assert "select(.draft == false)" in query


class TestV3BranchMonitorDispatch:
    def test_a_dispatched_security_release_is_proposed(self, tmp_path: Path) -> None:
        releases = [_rel("v3.3.6", body=_CVE), *REAL_API_ORDER]
        run = _v3_branch(tmp_path, releases=releases, pin="3.3.5", inputs={"surrealdb_version": "3.3.6"})
        assert run.returncode == 0, run.log
        assert run.outputs["has-update"] == "true", run.log

    def test_a_dispatched_version_without_a_release_entry_is_not_proposed(self, tmp_path: Path) -> None:
        """No release entry means no notes, so nothing shows it is a security release."""
        run = _v3_branch(tmp_path, releases=REAL_API_ORDER, pin="3.3.5", inputs={"surrealdb_version": "3.3.6"})
        assert run.returncode == 0, run.log
        assert run.outputs["has-update"] == "false", run.log
        assert run.outputs["is-security-patch"] == "false", run.log

    @pytest.mark.parametrize("version", ["3.4.0", "4.0.0", "2.7.0", "3.3", "3.3.6 ", "3.3.x", "3.3.6\n3.3.7"])
    def test_a_dispatched_version_outside_the_3_3_line_is_refused(self, tmp_path: Path, version: str) -> None:
        releases = [_rel(f"v{version.strip()}", body=_CVE), *REAL_API_ORDER]
        run = _v3_branch(tmp_path, releases=releases, pin="3.3.5", inputs={"surrealdb_version": version})
        assert run.returncode != 0, f"dispatched {version!r} was accepted\n{run.log}"
        assert "has-update" not in run.outputs, run.log

    def test_there_is_no_force_input(self) -> None:
        """A forced run would re-open a PR for the pinned version or bypass the security rule."""
        dispatch = _v3_branch_workflow()[True]["workflow_dispatch"]
        assert set((dispatch.get("inputs") or {}).keys()) == {"surrealdb_version"}


class TestV3BranchMonitorTargetsV3:
    """It reads, tests and writes the `v3` branch, never `main` or `v2`."""

    def test_every_checkout_is_of_v3(self) -> None:
        checkouts = [
            (job_id, step)
            for job_id, job in _v3_branch_workflow()["jobs"].items()
            for step in job.get("steps", [])
            if str(step.get("uses", "")).startswith("actions/checkout@")
        ]
        assert checkouts
        for job_id, step in checkouts:
            assert (step.get("with") or {}).get("ref") == "v3", f"{job_id}: {step.get('name')!r} does not check out v3"

    def test_every_pr_and_fetch_targets_v3(self) -> None:
        text = (WORKFLOW_DIR / V3_BRANCH).read_text(encoding="utf-8")
        bases = re.findall(r"--base\s+(\S+)", text)
        assert bases and set(bases) == {"v3"}, bases
        # No exception since #698: v3 owns its patch series and never reads main.
        for other in ("origin main", "origin v2", "checkout main", "checkout v2", "ref: main", "ref: v2"):
            assert other not in text, f"{V3_BRANCH} mentions {other!r}"

    def test_the_pin_pr_branch_is_refused_by_the_main_branch_guard(self) -> None:
        text = (WORKFLOW_DIR / V3_BRANCH).read_text(encoding="utf-8")
        assert 'BRANCH="chore/surrealdb-v3-${NEW_VERSION}"' in text
        assert 'BUMP_BRANCH="chore/release-v3-${NEW_TAG}"' in text

    def test_the_version_bump_is_its_own_release_pr_on_v3(self) -> None:
        """The squash subject must start with the release gate's phrase, or v3 never tags.

        The v2 monitor commits the bump inside the pin PR, whose squash subject is
        the PR title, so `tag-release.yml` never matches it. The v3 monitor opens
        the bump as its own PR after the pin PR merges, as the 3.x monitor does.
        """
        job = _v3_branch_workflow()["jobs"]["auto-update"]
        step = next(s for s in job["steps"] if s.get("name") == "Create version bump PR")
        assert "steps.wait-merge.outputs.merged == 'true'" in step["if"]
        assert '--title "chore(release): bump version to $NEW_ORM_VERSION"' in step["run"]
        assert "--base v3" in step["run"]
        # The version comes from v3's own pyproject.toml, checked out after the merge.
        names = [s.get("name") for s in job["steps"]]
        assert names.index("Checkout v3 after merge") < names.index("Create version bump PR")

    def test_the_tests_run_with_the_file_storage_flag(self) -> None:
        """v3 carries the 0.39.0 file storage tests, which fail without the flag."""
        job = _v3_branch_workflow()["jobs"]["test-new-version"]
        start = next(s for s in job["steps"] if str(s.get("name", "")).startswith("Start SurrealDB"))
        assert "--allow-experimental=files" in start["run"]
        integ = next(s for s in job["steps"] if s.get("name") == "Run integration tests")
        assert integ["env"]["SORM_REQUIRE_FILES"] == "1"

    def test_it_runs_at_its_own_time(self) -> None:
        crons = {
            name: yaml.safe_load((WORKFLOW_DIR / name).read_text(encoding="utf-8"))[True]["schedule"][0]["cron"]
            for name in MONITORS
        }
        assert len(set(crons.values())) == len(crons), crons


# ---------------------------------------------------------------------------
#
# What `check-release` hands on. Its outputs are single-line `key=value`
# values; nothing read from a release entry is copied into them (the notes and
# title only decide `is-security-patch`). Every job that acts on the selected
# version checks its shape again first, and reads the outputs from `env:`,
# never from a `${{ }}` expanded into its script.

_VALIDATE = "Validate the version output"
_CHECK_RELEASE_OUTPUTS = {"latest-version", "current-version", "has-update", "is-security-patch", "docker-tag"}

# (workflow, a version its consumers accept, versions they refuse)
_VERSION_SHAPES = [
    pytest.param(V3, "3.4.0", ["4.0.0", "2.7.1", "3.3.6-rc.1", "3.3", "v3.3.6", "3.x.0"], id="main-3.x"),
    pytest.param(V2, "2.8.0", ["3.3.6", "4.0.0", "2.7.1-beta.1", "2.7", "v2.7.1"], id="v2-2.x"),
    pytest.param(V3_BRANCH, "3.3.6", ["3.4.0", "4.0.0", "3.2.7", "3.3.6-rc.1", "3.3", "v3.3.6"], id="v3-3.3.x"),
]
_CONSUMERS = ("test-new-version", "auto-update", "create-failure-issue")


def _validate(tmp_path: Path, workflow: str, job: str, latest: str, docker_tag: str | None = None) -> StepRun:
    return _run_step(
        tmp_path,
        workflow,
        job,
        _VALIDATE,
        releases=[],
        pin="0.0.0",
        needs={"latest-version": latest, "docker-tag": f"v{latest}" if docker_tag is None else docker_tag},
    )


def _monitor_workflow(workflow: str) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((WORKFLOW_DIR / workflow).read_text(encoding="utf-8"))
    return data


@pytest.mark.parametrize("workflow", MONITORS)
class TestCheckReleaseOutputs:
    def test_only_the_decision_values_are_outputs(self, workflow: str) -> None:
        outputs = _monitor_workflow(workflow)["jobs"]["check-release"]["outputs"]
        assert set(outputs) == _CHECK_RELEASE_OUTPUTS, (
            f"{workflow}: check-release declares {sorted(set(outputs) - _CHECK_RELEASE_OUTPUTS)} that no job reads"
        )

    def test_release_entry_text_is_not_copied_into_the_outputs(self, tmp_path: Path, workflow: str) -> None:
        pin, tag = {V3: ("3.3.0", "v3.3.4"), V2: ("2.6.5", "v2.7.0"), V3_BRANCH: ("3.3.4", "v3.3.5")}[workflow]
        notes = "Fixes CVE-2026-40002.\n\nSecond paragraph marker-7f3a.\nlast line marker-7f3a"
        releases = [_rel(tag, body=notes), *REAL_API_ORDER]
        run = _monitor(tmp_path, workflow, releases=releases, pin=pin)
        assert run.returncode == 0, run.log
        assert run.latest == tag.removeprefix("v"), run.log
        assert run.outputs["is-security-patch"] == "true", run.log
        assert set(run.outputs) == _CHECK_RELEASE_OUTPUTS, run.outputs
        written = (tmp_path / "github_output").read_text(encoding="utf-8")
        assert "marker-7f3a" not in written, written

    def test_job_outputs_reach_the_scripts_through_env_only(self, workflow: str) -> None:
        for job_name, job in _monitor_workflow(workflow)["jobs"].items():
            for step in job.get("steps", []):
                script = step.get("run") or ""
                assert "${{" not in script, (
                    f"{workflow}: job {job_name!r}, step {step.get('name')!r} expands an expression into its script"
                )

    def test_every_job_acting_on_the_version_checks_it_first(self, workflow: str) -> None:
        jobs = _monitor_workflow(workflow)["jobs"]
        consumers = {
            job_name
            for job_name, job in jobs.items()
            if job_name != "check-release"
            and re.search(r"needs\.check-release\.outputs\.(latest-version|docker-tag)", yaml.safe_dump(job))
        }
        assert consumers == set(_CONSUMERS), consumers
        for job_name in consumers:
            first = jobs[job_name]["steps"][0]
            assert first.get("name") == _VALIDATE, f"{workflow}: {job_name!r} does not start with {_VALIDATE!r}"
            assert "if" not in first, f"{workflow}: {job_name!r}: the check must never be skipped"
            assert set(first["env"]) == {"LATEST_VERSION", "DOCKER_TAG"}, first["env"]


@pytest.mark.parametrize(("workflow", "good", "bad"), _VERSION_SHAPES)
@pytest.mark.parametrize("job", _CONSUMERS)
class TestVersionOutputIsValidated:
    def test_a_stable_version_of_the_line_passes(
        self, tmp_path: Path, workflow: str, good: str, bad: list[str], job: str
    ) -> None:
        run = _validate(tmp_path, workflow, job, good)
        assert run.returncode == 0, run.log
        assert run.outputs == {}, run.outputs

    def test_any_other_version_stops_the_job(self, tmp_path: Path, workflow: str, good: str, bad: list[str], job: str) -> None:
        for index, value in enumerate(bad):
            run_dir = tmp_path / str(index)
            run_dir.mkdir()
            run = _validate(run_dir, workflow, job, value)
            assert run.returncode != 0, f"{workflow}: {job}: {value!r} was accepted\n{run.log}"

    @pytest.mark.parametrize(
        "suffix",
        ["\nextra", " ", "\n", ";true", "$(true)"],
    )
    def test_trailing_text_stops_the_job(
        self, tmp_path: Path, workflow: str, good: str, bad: list[str], job: str, suffix: str
    ) -> None:
        run = _validate(tmp_path, workflow, job, good + suffix)
        assert run.returncode != 0, f"{workflow}: {job}: {good + suffix!r} was accepted\n{run.log}"

    def test_an_empty_version_stops_the_job(self, tmp_path: Path, workflow: str, good: str, bad: list[str], job: str) -> None:
        run = _validate(tmp_path, workflow, job, "", docker_tag="v")
        assert run.returncode != 0, run.log

    def test_the_image_tag_must_be_the_version(
        self, tmp_path: Path, workflow: str, good: str, bad: list[str], job: str
    ) -> None:
        for index, tag in enumerate([good, f"v{good}-rc.1", "latest", f"v{good}\nlatest", ""]):
            run_dir = tmp_path / str(index)
            run_dir.mkdir()
            run = _validate(run_dir, workflow, job, good, docker_tag=tag)
            assert run.returncode != 0, f"{workflow}: {job}: image tag {tag!r} accepted for {good}\n{run.log}"

    def test_the_refused_value_is_not_printed(self, tmp_path: Path, workflow: str, good: str, bad: list[str], job: str) -> None:
        run = _validate(tmp_path, workflow, job, f"{good}\n::notice::marker-91c2")
        assert run.returncode != 0, run.log
        assert "marker-91c2" not in run.log, run.log


# ---------------------------------------------------------------------------
# #698: v3 owns the 0.39.x patch series
# ---------------------------------------------------------------------------
#
# The v3 branch was cut from v0.39.9 and every later 0.39.x release comes from
# it; main releases 0.40.0 next (SurrealDB 4.x). So the v3 monitor selects,
# tests, pins and bumps v3 whatever main's version is: nothing in it reads
# main. On main, the 3.x monitor still opens the pin PR while main is on
# 0.39.x (option (a) of #698: main's CI follows the newest 3.x until the 4.x
# pin, #582) but opens no version-bump PR, so it never releases. Every bump
# step still refuses a computed version that is already tagged.


def _v3_check_release(tmp_path: Path, *, main: str | None, releases: list[dict[str, object]], pin: str) -> JobRun:
    """Run the v3 monitor's whole `check-release` job in a clone of v3 whose origin also has `main`."""
    repo = owner_repo(tmp_path / "git", main=main, target_version=pyproject("0.39.9"), target="v3")
    (repo / ".surrealdb-version").write_text(pin + "\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(FAKE_RELEASES_GH, encoding="utf-8")
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    fixture = tmp_path / "releases.json"
    fixture.write_text(json.dumps(releases), encoding="utf-8")
    gh_log = tmp_path / "gh.log"
    gh_log.write_text("", encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GH_STUB_RELEASES": str(fixture),
        "GH_STUB_PAGE_SIZE": "30",
        "GH_STUB_FAIL": "0",
        "GH_STUB_LOG": str(gh_log),
    }
    context = {"secrets.GITHUB_TOKEN": "stub-token", "inputs.surrealdb_version": ""}
    steps = _v3_branch_workflow()["jobs"]["check-release"]["steps"]
    return run_job(steps, cwd=repo, scratch=tmp_path / "steps", env=env, context=context)


class TestV3BranchMonitorOwnsTheSeries:
    @pytest.mark.parametrize("on_main", ["0.39.9", "0.39.10", "0.39.0", "0.40.0", None, ""])
    def test_a_security_release_is_selected_whatever_main_is_on(self, tmp_path: Path, on_main: str | None) -> None:
        """Before #698 nothing was selected while main was on 0.39.x, and the run failed without main."""
        main = None if on_main is None else (pyproject(on_main) if on_main else "")
        run = _v3_check_release(tmp_path, main=main, releases=[_rel("v3.3.6", body=_CVE), *REAL_API_ORDER], pin="3.3.5")
        assert run.returncode == 0, run.log
        check = run.outputs.get("check", {})
        assert check.get("has-update") == "true", run.log
        assert check.get("latest-version") == "3.3.6", run.log
        assert check.get("is-security-patch") == "true", run.log
        assert "Compare versions" in run.ran, run.ran

    def test_main_on_0_39_does_not_turn_a_routine_release_into_an_update(self, tmp_path: Path) -> None:
        run = _v3_check_release(tmp_path, main=pyproject("0.39.9"), releases=[_rel("v3.3.6"), *REAL_API_ORDER], pin="3.3.5")
        assert run.returncode == 0, run.log
        assert run.outputs.get("check", {}).get("has-update") == "false", run.log

    def test_check_release_has_only_the_checkout_and_the_selection(self) -> None:
        job = _v3_branch_workflow()["jobs"]["check-release"]
        names = [s.get("name") for s in job["steps"]]
        assert names == ["Checkout v3 branch", "Compare versions"], names
        assert all("if" not in s for s in job["steps"]), "no step of check-release may be skipped"

    def test_has_update_is_the_selection_output(self) -> None:
        outputs = _v3_branch_workflow()["jobs"]["check-release"]["outputs"]
        assert outputs["has-update"] == "${{ steps.check.outputs.has-update }}"

    @pytest.mark.parametrize("workflow", MONITORS)
    def test_no_monitor_defers_to_main_version(self, workflow: str) -> None:
        text = (WORKFLOW_DIR / workflow).read_text(encoding="utf-8")
        assert "follows-main" not in text
        assert "Defer to main" not in text


@pytest.mark.parametrize("workflow", MONITORS)
class TestCheckReleaseRunsOnMainOnly:
    """#698 item 5, as on v3's copies since #695: a dispatch on another ref does nothing.

    Dispatched on the v3 ref, a monitor would run v3's copy of its own file
    but check out and act on its target branch with that copy's steps.
    """

    def test_check_release_is_gated_on_the_main_ref(self, workflow: str) -> None:
        jobs = _monitor_workflow(workflow)["jobs"]
        assert jobs["check-release"]["if"] == "github.ref == 'refs/heads/main'"

    def test_every_later_job_needs_an_update_from_check_release(self, workflow: str) -> None:
        for job_id, job in _monitor_workflow(workflow)["jobs"].items():
            if job_id == "check-release":
                continue
            needs = job["needs"] if isinstance(job["needs"], list) else [job["needs"]]
            assert "check-release" in needs, job_id
            assert "needs.check-release.outputs.has-update == 'true'" in " ".join(str(job["if"]).split()), job_id


def test_the_3x_monitor_header_records_the_pin_decision() -> None:
    """#698 item 2: main keeps its pin PRs (option a) but no bump while on 0.39.x."""
    text = (WORKFLOW_DIR / V3).read_text(encoding="utf-8")
    header = text[: text.index("\nname:")]
    assert "#698" in header
    assert "0.39.x" in header and "v3" in header
    assert "#582" in header


FAKE_BUMP_GH = """#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_GH_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1:3] == ["pr", "create"]:
    print("https://github.com/owner/repo/pull/1")
"""

_DEPENDABOT = "dependabot-automerge.yml"
_DEP_ENV = {"DEP_NAMES": "tornado", "UPDATE_TYPE": "security", "PR_NUMBER": "7"}

# (workflow, job, branch the bump starts from, extra env, current version, next
# version, bump branch). main's sites start from 0.40.0: since #698 main
# releases nothing in the 0.39 series, which v3 owns.
_BUMP_SITES = [
    pytest.param(
        V3, "auto-update", "main", {"NEW_VERSION": "3.3.6"}, "0.40.0", "0.40.1", "chore/release-v0.40.1", id="main-3.x-monitor"
    ),
    pytest.param(
        V3_BRANCH,
        "auto-update",
        "v3",
        {"NEW_VERSION": "3.3.6"},
        "0.39.9",
        "0.39.10",
        "chore/release-v3-v0.39.10",
        id="v3-monitor",
    ),
    pytest.param(
        _DEPENDABOT,
        "automerge",
        "main",
        {"BASE_BRANCH": "main", **_DEP_ENV},
        "0.40.0",
        "0.40.1",
        "chore/version-bump-0.40.1",
        id="dependabot-main",
    ),
    pytest.param(
        _DEPENDABOT,
        "automerge",
        "v3",
        {"BASE_BRANCH": "v3", **_DEP_ENV},
        "0.39.9",
        "0.39.10",
        "chore/version-bump-0.39.10",
        id="dependabot-v3",
    ),
]

# The bump sites on main, which stop on 0.39.x.
_MAIN_BUMP_SITES = [
    pytest.param(V3, "auto-update", {"NEW_VERSION": "3.3.6"}, "chore/release-v", id="main-3.x-monitor"),
    pytest.param(_DEPENDABOT, "automerge", {"BASE_BRANCH": "main", **_DEP_ENV}, "chore/version-bump-", id="dependabot-main"),
]


def _run_bump(
    tmp_path: Path,
    workflow: str,
    job: str,
    branch: str,
    extra: dict[str, str],
    *,
    version: str,
    tags: list[str],
    reachable: bool = True,
    pyproject_text: str | None = None,
    main_version: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path, Path, list[list[str]]]:
    repo, origin = bump_repo(
        tmp_path, branch=branch, version=version, tags=tags, pyproject_text=pyproject_text, main_version=main_version
    )
    if not reachable:
        subprocess.run(["git", "-C", str(repo), "remote", "set-url", "origin", str(tmp_path / "gone.git")], check=True)
    data = _monitor_workflow(workflow)
    step = next(s for s in data["jobs"][job]["steps"] if s.get("name") == "Create version bump PR")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh_log = tmp_path / "gh.log"
    gh_log.write_text("", encoding="utf-8")
    for name, body in {
        "gh": FAKE_BUMP_GH,
        "uv": "#!/usr/bin/env bash\nexit 0\n",
    }.items():
        exe = bin_dir / name
        exe.write_text(body, encoding="utf-8")
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    runner_temp = tmp_path / "runner"
    runner_temp.mkdir()
    helper = runner_temp / "merge-when-green.sh"
    helper.write_text(f'#!/usr/bin/env bash\necho merge "$@" >> {gh_log}.merge\n', encoding="utf-8")
    helper.chmod(helper.stat().st_mode | stat.S_IEXEC)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GH_TOKEN": "stub-token",
        "RUNNER_TEMP": str(runner_temp),
        "FAKE_GH_LOG": str(gh_log),
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        **extra,
    }
    script = tmp_path / "bump.sh"
    script.write_text(step["run"], encoding="utf-8")
    result = subprocess.run(["bash", "-e", str(script)], cwd=repo, env=env, capture_output=True, text=True)
    calls = [json.loads(line) for line in gh_log.read_text(encoding="utf-8").splitlines() if line]
    return result, repo, origin, calls


@pytest.mark.parametrize(("workflow", "job", "branch", "extra", "version", "next_version", "bump_branch"), _BUMP_SITES)
class TestBumpRefusesAReleasedVersion:
    def test_a_new_version_is_bumped(
        self,
        tmp_path: Path,
        workflow: str,
        job: str,
        branch: str,
        extra: dict[str, str],
        version: str,
        next_version: str,
        bump_branch: str,
    ) -> None:
        result, _, origin, calls = _run_bump(tmp_path, workflow, job, branch, extra, version=version, tags=[f"v{version}"])
        assert result.returncode == 0, result.stdout + result.stderr
        assert f"refs/heads/{bump_branch}" in remote_refs(origin)
        (create,) = [c for c in calls if c[:2] == ["pr", "create"]]
        assert create[create.index("--base") + 1] == branch
        assert create[create.index("--title") + 1] == f"chore(release): bump version to {next_version}"

    def test_an_existing_tag_stops_before_any_branch_or_pr(
        self,
        tmp_path: Path,
        workflow: str,
        job: str,
        branch: str,
        extra: dict[str, str],
        version: str,
        next_version: str,
        bump_branch: str,
    ) -> None:
        # Another branch already released the next number.
        tags = [f"v{version}", f"v{next_version}"]
        result, repo, origin, calls = _run_bump(tmp_path, workflow, job, branch, extra, version=version, tags=tags)
        assert result.returncode != 0, result.stdout + result.stderr
        assert f"::error::v{next_version} already exists" in result.stdout
        assert remote_refs(origin) == sorted([f"refs/heads/{branch}", *(f"refs/tags/{tag}" for tag in tags)])
        assert calls == []
        assert not (tmp_path / "gh.log.merge").exists()
        head = subprocess.run(["git", "-C", str(repo), "branch", "--show-current"], capture_output=True, text=True)
        assert head.stdout.strip() == branch
        assert f'version = "{version}"' in (repo / "pyproject.toml").read_text()

    def test_an_unreadable_remote_stops_the_bump(
        self,
        tmp_path: Path,
        workflow: str,
        job: str,
        branch: str,
        extra: dict[str, str],
        version: str,
        next_version: str,
        bump_branch: str,
    ) -> None:
        result, _, origin, calls = _run_bump(
            tmp_path, workflow, job, branch, extra, version=version, tags=[f"v{version}"], reachable=False
        )
        assert result.returncode != 0, result.stdout + result.stderr
        assert "::error::Cannot list the remote tags" in result.stdout
        assert calls == []
        assert f"refs/heads/{bump_branch}" not in remote_refs(origin)


@pytest.mark.parametrize(("workflow", "job", "extra", "branch_prefix"), _MAIN_BUMP_SITES)
class TestMainReleasesNothingInThe039Series:
    """#698 items 2 and 3: on main, a 0.39.x version stops the bump before any branch or PR."""

    @pytest.mark.parametrize("version", ["0.39.9", "0.39.0", "0.39.10", "0.39.123"])
    def test_a_0_39_version_is_not_bumped(
        self, tmp_path: Path, workflow: str, job: str, extra: dict[str, str], branch_prefix: str, version: str
    ) -> None:
        result, repo, origin, calls = _run_bump(tmp_path, workflow, job, "main", extra, version=version, tags=[f"v{version}"])
        assert result.returncode == 0, result.stdout + result.stderr
        notices = [line for line in result.stdout.splitlines() if line.startswith("::notice::")]
        assert len(notices) == 1 and "v3" in notices[0], result.stdout
        assert remote_refs(origin) == sorted(["refs/heads/main", f"refs/tags/v{version}"])
        assert calls == []
        assert not (tmp_path / "gh.log.merge").exists()
        head = subprocess.run(["git", "-C", str(repo), "branch", "--show-current"], capture_output=True, text=True)
        assert head.stdout.strip() == "main"
        assert f'version = "{version}"' in (repo / "pyproject.toml").read_text()

    @pytest.mark.parametrize(("version", "next_version"), [("0.40.0", "0.40.1"), ("0.40.9", "0.40.10"), ("0.41.2", "0.41.3")])
    def test_a_later_series_is_bumped(
        self,
        tmp_path: Path,
        workflow: str,
        job: str,
        extra: dict[str, str],
        branch_prefix: str,
        version: str,
        next_version: str,
    ) -> None:
        result, _, origin, calls = _run_bump(tmp_path, workflow, job, "main", extra, version=version, tags=[f"v{version}"])
        assert result.returncode == 0, result.stdout + result.stderr
        assert any(ref.startswith(f"refs/heads/{branch_prefix}") and ref.endswith(next_version) for ref in remote_refs(origin))
        (create,) = [c for c in calls if c[:2] == ["pr", "create"]]
        assert create[create.index("--title") + 1] == f"chore(release): bump version to {next_version}"

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "[project]\nname = 'x'\n",
            pyproject("0.40"),
            pyproject("v0.40.0"),
            pyproject("0.40.0rc1"),
            pyproject("0.40.x"),
        ],
        ids=["empty", "no-version", "two-parts", "prefixed", "pre-release", "not-a-number"],
    )
    def test_an_unreadable_version_fails_closed(
        self, tmp_path: Path, workflow: str, job: str, extra: dict[str, str], branch_prefix: str, text: str
    ) -> None:
        result, _, origin, calls = _run_bump(
            tmp_path, workflow, job, "main", extra, version="0.40.0", tags=[], pyproject_text=text
        )
        assert result.returncode != 0, result.stdout + result.stderr
        assert "::error::" in result.stdout
        assert remote_refs(origin) == ["refs/heads/main"]
        assert calls == []


@pytest.mark.parametrize("on_main", ["0.39.9", "0.39.12", "0.40.0"])
@pytest.mark.parametrize(
    ("workflow", "job", "extra"),
    [
        pytest.param(V3_BRANCH, "auto-update", {"NEW_VERSION": "3.3.6"}, id="v3-monitor"),
        pytest.param(_DEPENDABOT, "automerge", {"BASE_BRANCH": "v3", **_DEP_ENV}, id="dependabot-v3"),
    ],
)
def test_v3_is_bumped_whatever_main_is_on(tmp_path: Path, workflow: str, job: str, extra: dict[str, str], on_main: str) -> None:
    """#698 item 1: the v3 bumps never look at main's version."""
    result, _, origin, calls = _run_bump(
        tmp_path, workflow, job, "v3", extra, version="0.39.9", tags=["v0.39.9"], main_version=on_main
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert any(ref.startswith("refs/heads/chore/") and ref.endswith("0.39.10") for ref in remote_refs(origin))
    (create,) = [c for c in calls if c[:2] == ["pr", "create"]]
    assert create[create.index("--base") + 1] == "v3"
    assert create[create.index("--title") + 1] == "chore(release): bump version to 0.39.10"
