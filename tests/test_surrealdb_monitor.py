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
RELEASES = "repos/surrealdb/surrealdb/releases"

_EXPR = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")


def _expand(text: str, inputs: dict[str, str]) -> str:
    """Expand the `${{ }}` expressions GitHub substitutes before bash runs.

    A dispatch input that was not given (or a scheduled run, which has none)
    expands to an empty string. Anything other than an input or a secret is
    refused, so the tests never run a script that differs from the real one.
    """

    def sub(match: re.Match[str]) -> str:
        expr = match.group(1)
        if expr.startswith("inputs."):
            return inputs.get(expr.removeprefix("inputs."), "")
        if expr.startswith("secrets."):
            return "stub-token"
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
    env.update({key: _expand(str(value), inputs) for key, value in (step.get("env") or {}).items()})
    script = tmp_path / "step.sh"
    script.write_text(_expand(step["run"], inputs), encoding="utf-8")
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

    @pytest.mark.parametrize("workflow", [V3, V2])
    def test_an_unreachable_api_fails_the_run(self, tmp_path: Path, workflow: str) -> None:
        """An outage must not read as "nothing to do": the run goes red."""
        run = _monitor(tmp_path, workflow, releases=REAL_API_ORDER, pin="3.3.0" if workflow == V3 else "2.6.5", fail=True)
        assert run.returncode != 0, f"{workflow}: an API failure ended green\n{run.log}"
        assert run.outputs.get("has-update") != "true", run.log

    @pytest.mark.parametrize("workflow", [V3, V2])
    def test_the_list_is_read_100_releases_per_page(self, tmp_path: Path, workflow: str) -> None:
        """The API's default page is 30 releases: six requests per run in 2026, and growing."""
        run = _monitor(tmp_path, workflow, releases=REAL_API_ORDER, pin="3.3.4" if workflow == V3 else "2.7.0")
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
        if not title:
            assert notes in run.outputs["release-notes"], run.log

    @pytest.mark.parametrize(("workflow", "version", "pin"), [(V3, "3.3.5", "3.3.4"), (V2, "2.7.1", "2.7.0")])
    def test_a_dispatched_version_without_a_release_entry(self, tmp_path: Path, workflow: str, version: str, pin: str) -> None:
        """Image tags can exist without a GitHub Release (#155): the lookup must not abort the run."""
        run = _monitor(tmp_path, workflow, releases=REAL_API_ORDER, pin=pin, inputs={"surrealdb_version": version})
        assert run.returncode == 0, run.log
        assert run.latest == version, run.log
        assert run.outputs["has-update"] == "true", run.log
        assert run.outputs["is-security-patch"] == "false", run.log
        # gh prints the 404 body on stdout: it must not be taken for the notes.
        assert run.outputs["release-notes"] == "", run.log


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
