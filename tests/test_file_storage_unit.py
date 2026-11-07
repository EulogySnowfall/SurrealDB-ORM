"""Unit tests for file storage (0.39.0) that need no server.

SurrealDB 3.2 and 3.3 keep file storage behind ``--allow-experimental=files``:
without it, ``DEFINE BUCKET`` is a parse error and every ``file::*`` call is a
statement error. The file integration tests probe the capability first and
skip on a flagless server, unless ``SORM_REQUIRE_FILES=1`` asks them to fail
instead. CI, the compose file and the SurrealDB monitors start the server with
the flag and set that variable, so a skip there is impossible.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from tests.files_capability import FILES_SKIP_REASON, files_capability_outcome

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_DIR = ROOT / ".github" / "workflows"
COMPOSE_FILE = ROOT / "devops" / "docker-compose.yml"

FLAG = "--allow-experimental=files"

# The texts the server answers with when the capability is off (3.2.6 and 3.3.5).
FILES_NOT_ENABLED_TEXTS = [
    "There was a problem running the file::exists() function. Experimental capability `files` is not enabled",
    "There was a problem with the database: Experimental feature files is not enabled",
    "Parse error: Unexpected token `BUCKET`, expected the experimental files feature to be enabled\n --> [1:8]",
]


# ---------------------------------------------------------------------------
# U30 — files_capability_outcome()
# ---------------------------------------------------------------------------


class TestFilesCapabilityOutcome:
    """The skip-or-fail decision of the file integration tests' capability probe."""

    @pytest.mark.parametrize("require", [False, True])
    def test_no_error_runs(self, require: bool) -> None:
        assert files_capability_outcome(None, require) == "run"

    @pytest.mark.parametrize("text", FILES_NOT_ENABLED_TEXTS)
    def test_files_not_enabled_skips_when_not_required(self, text: str) -> None:
        assert files_capability_outcome(text, False) == "skip"

    @pytest.mark.parametrize("text", FILES_NOT_ENABLED_TEXTS)
    def test_files_not_enabled_fails_when_required(self, text: str) -> None:
        assert files_capability_outcome(text, True) == "fail"

    @pytest.mark.parametrize("require", [False, True])
    def test_unrelated_error_is_raised_back(self, require: bool) -> None:
        text = "There was a problem with authentication"
        with pytest.raises(RuntimeError, match=re.escape(text)):
            files_capability_outcome(text, require)

    def test_match_is_case_sensitive(self) -> None:
        with pytest.raises(RuntimeError):
            files_capability_outcome("EXPERIMENTAL FEATURE FILES IS NOT ENABLED", False)

    def test_skip_reason_names_the_flag(self) -> None:
        assert FILES_SKIP_REASON == "SurrealDB was started without --allow-experimental=files"


# ---------------------------------------------------------------------------
# The servers the suite runs on carry the flag
# ---------------------------------------------------------------------------


def _load(path: Path) -> Any:
    yaml = pytest.importorskip("yaml", reason="pyyaml required for workflow lint tests")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _steps(workflow: str) -> list[dict[str, Any]]:
    data = _load(WORKFLOW_DIR / workflow)
    steps: list[dict[str, Any]] = []
    for job in data["jobs"].values():
        steps.extend(job.get("steps", []))
    return steps


def _step(workflow: str, name_prefix: str) -> dict[str, Any]:
    matches = [s for s in _steps(workflow) if str(s.get("name", "")).startswith(name_prefix)]
    assert len(matches) == 1, f"{workflow}: expected one step named {name_prefix!r}, found {len(matches)}"
    return matches[0]


class TestServersStartWithFilesCapability:
    """Compose, CI and the 3.x monitors run the suite on a flag-enabled server."""

    def test_compose_surrealdb_service_has_flag(self) -> None:
        command = _load(COMPOSE_FILE)["services"]["surrealdb"]["command"]
        assert command == f"start --log warn --user root --pass root {FLAG} memory"

    def test_ci_integration_step_requires_files(self) -> None:
        step = _step("ci.yml", "Run integration tests")
        assert step["env"]["SORM_REQUIRE_FILES"] == "1"

    def test_ci_starts_surrealdb_from_compose(self) -> None:
        # The flag reaches CI through the compose service, not a docker run line.
        step = _step("ci.yml", "Start SurrealDB")
        assert "docker compose -f devops/docker-compose.yml up -d surrealdb" in step["run"]

    @pytest.mark.parametrize("workflow", ["surrealdb-security.yml", "surrealdb-prerelease-canary.yml"])
    def test_monitor_starts_server_with_flag(self, workflow: str) -> None:
        run = _step(workflow, "Start SurrealDB")["run"]
        assert re.search(rf"start --log error --user root --pass root {re.escape(FLAG)} memory\b", run)

    @pytest.mark.parametrize("workflow", ["surrealdb-security.yml", "surrealdb-prerelease-canary.yml"])
    def test_monitor_integration_step_requires_files(self, workflow: str) -> None:
        step = _step(workflow, "Run integration tests")
        assert step["env"]["SORM_REQUIRE_FILES"] == "1"

    def test_v2_monitor_is_unchanged(self) -> None:
        # SurrealDB 2.x has no files capability: the v2 monitor must not ask for it.
        text = (WORKFLOW_DIR / "surrealdb-v2-security.yml").read_text(encoding="utf-8")
        assert FLAG not in text
        assert "SORM_REQUIRE_FILES" not in text
