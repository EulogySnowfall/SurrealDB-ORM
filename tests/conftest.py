"""
Pytest configuration for SurrealDB-ORM tests.

This module manages the SurrealDB test container lifecycle:
- Checks if the test container is already running
- Starts it if needed before integration tests
- Stops it after tests only if we started it

Shared connection constants are defined here so every test file can import them
instead of hardcoding URLs, credentials, and ports.
"""

import os
import subprocess
import time
from collections.abc import Generator

import pytest

# ---------------------------------------------------------------------------
# Shared connection constants (import these in test files)
# ---------------------------------------------------------------------------
TEST_PORT = int(os.getenv("SURREALDB_PORT", "8000"))
SURREALDB_URL = os.getenv("SURREALDB_URL", f"http://localhost:{TEST_PORT}")
SURREALDB_WS_URL = os.getenv("SURREALDB_WS_URL", f"ws://localhost:{TEST_PORT}")
SURREALDB_USER = os.getenv("SURREALDB_USER", "root")
SURREALDB_PASS = os.getenv("SURREALDB_PASS", "root")
SURREALDB_NAMESPACE = os.getenv("SURREALDB_NAMESPACE", "test")

# Container configuration
CONTAINER_NAME = "surrealdb-orm"
COMPOSE_FILE = "devops/docker-compose.yml"
HEALTH_CHECK_TIMEOUT = 30  # seconds
SKIP_CONTAINER = os.getenv("SURREALDB_SKIP_CONTAINER", "").lower() in ("1", "true", "yes")


def is_container_running() -> bool:
    """Check if the SurrealDB test container is running."""
    try:
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER_NAME],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def is_port_responding(port: int = TEST_PORT) -> bool:
    """Check if the SurrealDB port is responding (basic TCP check)."""
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(2)
            s.connect(("localhost", port))
            return True
    except (TimeoutError, ConnectionRefusedError, OSError):
        return False


def is_surrealdb_healthy(port: int = TEST_PORT) -> bool:
    """Check if SurrealDB is healthy via /health endpoint."""
    import urllib.error
    import urllib.request

    try:
        url = f"http://localhost:{port}/health"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=2) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def start_container() -> bool:
    """Start the SurrealDB test container."""
    try:
        result = subprocess.run(
            ["docker", "compose", "-f", COMPOSE_FILE, "up", "-d", "surrealdb"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            print(f"Failed to start container: {result.stderr}")
            return False

        # Wait for SurrealDB to be healthy (not just port open)
        start_time = time.time()
        while time.time() - start_time < HEALTH_CHECK_TIMEOUT:
            if is_surrealdb_healthy():
                return True
            time.sleep(0.5)

        print(f"Container did not become healthy within {HEALTH_CHECK_TIMEOUT}s")
        return False
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"Failed to start container: {e}")
        return False


def stop_container() -> None:
    """Stop the SurrealDB test container."""
    try:
        subprocess.run(
            ["docker", "compose", "-f", COMPOSE_FILE, "stop", "surrealdb"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass


# Track if we started the container (so we know whether to stop it)
_container_started_by_tests = False


def pytest_configure(config: pytest.Config) -> None:
    """
    Called after command line options have been parsed and all plugins loaded.

    Start the SurrealDB container if:
    1. We're running integration tests
    2. The container is not already running / SurrealDB is not already healthy
    """
    global _container_started_by_tests

    # Check if we're running integration tests
    markers = config.getoption("-m", default="")
    if markers and "not integration" in markers:
        # Not running integration tests, no need for container
        return

    # Honor SURREALDB_SKIP_CONTAINER (e.g. CI provides its own SurrealDB service)
    if SKIP_CONTAINER:
        print(f"\n[conftest] SURREALDB_SKIP_CONTAINER set — skipping container management (port {TEST_PORT})")
        _container_started_by_tests = False
        return

    # If SurrealDB is already healthy (container or external), skip startup
    if is_surrealdb_healthy():
        print(f"\n[conftest] SurrealDB already healthy on port {TEST_PORT}")
        _container_started_by_tests = False
        return

    # Start the container
    print(f"\n[conftest] Starting SurrealDB container on port {TEST_PORT}...")
    if start_container():
        print("[conftest] SurrealDB container started successfully")
        _container_started_by_tests = True
    else:
        print("[conftest] WARNING: Could not start SurrealDB container. Integration tests may fail.")
        _container_started_by_tests = False


def pytest_unconfigure(config: pytest.Config) -> None:
    """
    Called before test process is exited.

    Stop the SurrealDB container only if we started it.
    """
    global _container_started_by_tests

    if _container_started_by_tests:
        print("\n[conftest] Stopping SurrealDB container (started by tests)...")
        stop_container()
        print("[conftest] SurrealDB container stopped")
        _container_started_by_tests = False
    else:
        # Container was already running before tests, leave it running
        pass


@pytest.fixture(scope="session")
def surrealdb_available() -> Generator[bool, None, None]:
    """
    Session-scoped fixture that indicates if SurrealDB is available.

    Use this fixture in tests that need to conditionally skip if SurrealDB
    is not available:

        def test_something(surrealdb_available):
            if not surrealdb_available:
                pytest.skip("SurrealDB not available")
    """
    yield is_surrealdb_healthy()
