"""Guards for the supported Python range (#235).

The unit-test matrix of ``.github/workflows/ci.yml`` is the one place that says
which CPython minors the project is tested on. These tests keep every other
statement of that range in step with it, and check that the lock file can
actually be installed on each of those Pythons:

* the ``Programming Language :: Python :: 3.X`` classifiers of both
  ``pyproject.toml`` files (the package and the bundled SDK) list exactly the
  tested minors;
* ``uv.lock`` provides a wheel for every package ``uv sync --frozen`` installs.
  A package locked before its maintainers published wheels for a new CPython
  minor is built from source there: a Rust toolchain for ``pydantic-core``, a C
  compiler for ``cbor2`` and ``librt``. The CI job then fails, or tests a
  locally compiled build that no user installs. The fix is a targeted
  ``uv lock --upgrade-package``, never a blanket upgrade;
* the lint targets (ruff ``target-version``, mypy ``python_version``) and the
  oldest tested Python follow the ``requires-python`` floor, not the newest
  release;
* the ``make test-all-python`` helper runs every tested minor.
"""

from __future__ import annotations

import re
import tomllib
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import pytest
from packaging.markers import Marker
from packaging.tags import Tag, compatible_tags, cpython_tags
from packaging.utils import parse_wheel_filename

yaml = pytest.importorskip("yaml", reason="pyyaml required to read the CI workflow")

ROOT = Path(__file__).resolve().parent.parent
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
LOCK_FILE = ROOT / "uv.lock"
MAKEFILE = ROOT / "Makefile"
PYPROJECTS = (ROOT / "pyproject.toml", ROOT / "src" / "surreal_sdk" / "pyproject.toml")

# Packages allowed to build from source on a tested Python. PyYAML (dev group,
# used to read the workflows in tests) publishes no wheel for every new CPython
# minor; its sdist falls back to the pure-Python implementation when the
# libyaml extension cannot be built, so no toolchain is required.
SOURCE_BUILD_ALLOWED = frozenset({"pyyaml"})

# Linux x86_64 as on GitHub's ubuntu runners. A manylinux wheel built for glibc
# 2.35 (Ubuntu 22.04) or older installs there; a newer one may not be picked.
_GLIBC_MAX_MINOR = 35
LINUX_PLATFORMS = [
    *(f"manylinux_2_{minor}_x86_64" for minor in range(_GLIBC_MAX_MINOR, 4, -1)),
    "manylinux2014_x86_64",
    "manylinux2010_x86_64",
    "manylinux1_x86_64",
    "linux_x86_64",
]

# Packages the walk below must reach, or it is not reading the lock correctly.
_EXPECTED_INSTALLED = frozenset({"aiohttp", "cbor2", "httpx", "pydantic", "pydantic-core", "mypy", "pytest"})


def _ci_unit_matrix() -> list[Any]:
    """The ``python-version`` entries of the ``test-unit`` job, as YAML parsed them."""
    workflow: dict[str, Any] = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    return list(workflow["jobs"]["test-unit"]["strategy"]["matrix"]["python-version"])


def _minor(version: str) -> int:
    major, minor = version.split(".")
    assert major == "3", f"unexpected Python version {version!r}"
    return int(minor)


TESTED_PYTHONS = [str(v) for v in _ci_unit_matrix()]


def _pyproject(path: Path) -> dict[str, Any]:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _requires_python_floor() -> str:
    spec = _pyproject(PYPROJECTS[0])["project"]["requires-python"]
    match = re.fullmatch(r">=\s*(3\.\d+)", spec)
    assert match, f"requires-python {spec!r} is not a plain '>=3.X' floor"
    return match.group(1)


# --------------------------------------------------------------------------- uv.lock walk


def _marker_environment(version: str) -> dict[str, str]:
    """PEP 508 environment of CPython *version* on Linux x86_64."""
    return {
        "implementation_name": "cpython",
        "implementation_version": f"{version}.0",
        "os_name": "posix",
        "platform_machine": "x86_64",
        "platform_python_implementation": "CPython",
        "platform_release": "",
        "platform_system": "Linux",
        "platform_version": "",
        "python_full_version": f"{version}.0",
        "python_version": version,
        "sys_platform": "linux",
        "extra": "",
    }


def _applies(marker: str | None, env: dict[str, str]) -> bool:
    return marker is None or Marker(marker).evaluate(env)


def _installed_by_sync(version: str) -> list[dict[str, Any]]:
    """The locked packages ``uv sync --frozen`` installs on CPython *version*.

    That is the project's dependencies plus the default ``dev`` group, followed
    transitively, with every marker evaluated for Linux x86_64. The project
    itself (an editable source) is not included.
    """
    env = _marker_environment(version)
    packages: list[dict[str, Any]] = tomllib.loads(LOCK_FILE.read_text(encoding="utf-8"))["package"]
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for package in packages:
        by_name[package["name"]].append(package)
    root = next(p for p in packages if p.get("source", {}).get("editable") == ".")

    def resolve(dep: dict[str, Any]) -> dict[str, Any]:
        candidates = by_name[dep["name"]]
        if "version" in dep:
            candidates = [p for p in candidates if p["version"] == dep["version"]]
        if len(candidates) > 1:
            candidates = [p for p in candidates if any(_applies(m, env) for m in p.get("resolution-markers", []))]
        assert len(candidates) == 1, f"cannot pick one locked {dep['name']!r} for Python {version}: {candidates}"
        return candidates[0]

    found: dict[tuple[str, str], dict[str, Any]] = {}
    extras_seen: set[tuple[str, str, str]] = set()
    pending: list[dict[str, Any]] = [*root.get("dependencies", []), *root.get("dev-dependencies", {}).get("dev", [])]
    while pending:
        dep = pending.pop()
        if not _applies(dep.get("marker"), env):
            continue
        package = resolve(dep)
        key = (package["name"], package["version"])
        if key not in found:
            found[key] = package
            pending.extend(package.get("dependencies", []))
        for extra in dep.get("extra", []):
            if (*key, extra) not in extras_seen:
                extras_seen.add((*key, extra))
                pending.extend(package.get("optional-dependencies", {}).get(extra, []))
    return list(found.values())


def _supported_tags(version: str) -> set[Tag]:
    """Wheel tags CPython *version* accepts on Linux x86_64 (including abi3 and pure wheels)."""
    python_version = (3, _minor(version))
    interpreter = f"cp3{python_version[1]}"
    return {
        *cpython_tags(python_version, abis=[interpreter], platforms=LINUX_PLATFORMS),
        *compatible_tags(python_version, interpreter, LINUX_PLATFORMS),
    }


def _has_wheel_for(package: dict[str, Any], supported: set[Tag]) -> bool:
    for wheel in package.get("wheels", []):
        filename = wheel.get("filename") or unquote(wheel["url"].rsplit("/", 1)[-1])
        if parse_wheel_filename(filename)[3] & supported:
            return True
    return False


# --------------------------------------------------------------------------- tests


class TestCIUnitMatrix:
    """The matrix itself: well-formed, starting at the floor, no gap."""

    def test_entries_are_quoted_strings(self) -> None:
        # Unquoted, YAML reads 3.20 as the float 3.2 and setup-python installs the wrong Python.
        entries = _ci_unit_matrix()
        assert all(isinstance(v, str) for v in entries), f"quote every python-version entry: {entries!r}"

    def test_starts_at_requires_python_floor(self) -> None:
        assert min(TESTED_PYTHONS, key=_minor) == _requires_python_floor()

    def test_has_no_gap(self) -> None:
        minors = sorted(_minor(v) for v in TESTED_PYTHONS)
        assert minors == list(range(minors[0], minors[-1] + 1)), f"untested minor inside {TESTED_PYTHONS}"


class TestClassifiers:
    """PyPI classifiers advertise exactly the Pythons CI tests."""

    @pytest.mark.parametrize("pyproject", PYPROJECTS, ids=lambda p: str(p.relative_to(ROOT)))
    def test_python_classifiers_match_ci_matrix(self, pyproject: Path) -> None:
        classifiers = _pyproject(pyproject)["project"]["classifiers"]
        advertised = {m.group(1) for c in classifiers if (m := re.fullmatch(r"Programming Language :: Python :: (3\.\d+)", c))}
        assert advertised == set(TESTED_PYTHONS)

    @pytest.mark.parametrize("pyproject", PYPROJECTS, ids=lambda p: str(p.relative_to(ROOT)))
    def test_requires_python_is_shared(self, pyproject: Path) -> None:
        assert _pyproject(pyproject)["project"]["requires-python"] == f">={_requires_python_floor()}"


class TestLintTargetsFloor:
    """Lint targets the oldest supported Python, so newer syntax is caught."""

    def test_ruff_target_version(self) -> None:
        target = _pyproject(PYPROJECTS[0])["tool"]["ruff"]["target-version"]
        assert target == "py" + _requires_python_floor().replace(".", "")

    def test_mypy_python_version(self) -> None:
        assert _pyproject(PYPROJECTS[0])["tool"]["mypy"]["python_version"] == _requires_python_floor()


class TestLockInstallsFromWheels:
    """``uv sync --frozen`` on each tested Python needs no compiler."""

    @pytest.mark.parametrize("version", TESTED_PYTHONS)
    def test_walk_reaches_the_dependencies(self, version: str) -> None:
        installed = {p["name"] for p in _installed_by_sync(version)}
        assert installed >= _EXPECTED_INSTALLED, f"missing: {sorted(_EXPECTED_INSTALLED - installed)}"

    @pytest.mark.parametrize("version", TESTED_PYTHONS)
    def test_every_package_has_a_wheel(self, version: str) -> None:
        supported = _supported_tags(version)
        source_builds = sorted(
            f"{p['name']}=={p['version']}"
            for p in _installed_by_sync(version)
            if p["name"] not in SOURCE_BUILD_ALLOWED and not _has_wheel_for(p, supported)
        )
        assert not source_builds, (
            f"uv.lock has no CPython {version} wheel for {source_builds}; refresh just these with "
            "`uv lock --upgrade-package <name>` once upstream publishes wheels"
        )


class TestMakefile:
    """``make test-all-python`` runs the unit suite on every tested Python."""

    def test_test_all_python_covers_ci_matrix(self) -> None:
        text = MAKEFILE.read_text(encoding="utf-8")
        recipe = re.search(r"^test-all-python:.*?\n((?:\t.*\n)+)", text, re.MULTILINE)
        assert recipe, "test-all-python target not found"
        assert set(re.findall(r"--python (3\.\d+)", recipe.group(1))) == set(TESTED_PYTHONS)
