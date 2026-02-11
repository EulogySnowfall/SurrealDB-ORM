.DEFAULT_GOAL := all
sources = src
COMPOSE_FILE = devops/docker-compose.yml

.PHONY: .uv  # Check that uv is installed
.uv:
	@uv --version || echo 'Please install uv: https://docs.astral.sh/uv/getting-started/installation/'

.PHONY: install  # Install the package, dependencies, and pre-commit for local development
install: .uv
	uv sync --frozen --group lint
	uv run pre-commit install --install-hooks

.PHONY: format  # Format the code
format:
	uv run ruff format
	uv run ruff check --fix --fix-only

.PHONY: lint  # Lint the code
lint:
	uv run ruff format --check
	uv run ruff check

.PHONY: mypy
mypy:
	uv run python -m mypy $(sources)

.PHONY: typecheck
typecheck:
	uv run pyright

.PHONY: test
test:
	uv run pytest -m "not integration"

.PHONY: test-unit  # Run unit tests only (no SurrealDB required)
test-unit:
	uv run pytest -m "not integration" tests/

.PHONY: test-sdk  # Run SDK tests only
test-sdk:
	uv run pytest -m "not integration" tests/sdk/

.PHONY: test-integration  # Run integration tests (requires SurrealDB)
test-integration: db-up
	uv run pytest -m integration tests/

.PHONY: test-all  # Run all tests including integration
test-all: db-up
	uv run pytest tests/

.PHONY: test-all-python  # Run tests on Python 3.12 to 3.14
test-all-python:
	uv run --python 3.12 coverage run -p -m pytest -m "not integration" --junitxml=junit.xml -o junit_family=legacy
	UV_PROJECT_ENVIRONMENT=.venv313 uv run --python 3.13 coverage run -p -m pytest -m "not integration"
	UV_PROJECT_ENVIRONMENT=.venv314 uv run --python 3.14 coverage run -p -m pytest -m "not integration"
	@uv run coverage xml -o coverage.xml
	@uv run coverage report

.PHONY: html  # Generate HTML coverage report
html: test-all-python
	uv run coverage html -d htmlcov

# =============================================================================
# Docker commands for SurrealDB
# =============================================================================

.PHONY: db-up  # Start SurrealDB container (in-memory, port 8000)
db-up:
	docker compose -f $(COMPOSE_FILE) up -d surrealdb
	@./devops/wait-for-healthy.sh localhost 8000 30
	@echo "SurrealDB ready on port 8000 (in-memory)"

.PHONY: db-down  # Stop SurrealDB container
db-down:
	docker compose -f $(COMPOSE_FILE) down

.PHONY: db-cluster  # Start multi-node cluster for K8s simulation
db-cluster:
	docker compose -f $(COMPOSE_FILE) --profile cluster up -d
	@./devops/wait-for-healthy.sh localhost 8002 30
	@./devops/wait-for-healthy.sh localhost 8003 30
	@./devops/wait-for-healthy.sh localhost 8004 30
	@echo "SurrealDB cluster ready on ports 8002, 8003, 8004"

.PHONY: db-logs  # Show SurrealDB logs
db-logs:
	docker compose -f $(COMPOSE_FILE) logs -f surrealdb

.PHONY: db-shell  # Open SurrealDB SQL shell
db-shell:
	docker compose -f $(COMPOSE_FILE) exec surrealdb /surreal sql --endpoint http://localhost:8000 --username root --password root --namespace test --database test

.PHONY: db-setup  # Setup test database schema
db-setup: db-up
	@./devops/setup-test-db.sh localhost 8000

.PHONY: db-status  # Show status of SurrealDB container
db-status:
	@docker compose -f $(COMPOSE_FILE) ps

.PHONY: db-clean  # Remove SurrealDB container
db-clean:
	docker compose -f $(COMPOSE_FILE) down --remove-orphans

# =============================================================================
# CI targets
# =============================================================================

.PHONY: ci-test  # Run all tests for CI
ci-test: db-up
	uv run pytest tests/ --junitxml=junit.xml -o junit_family=legacy
	uv run coverage xml -o coverage.xml

.PHONY: ci-lint  # Run linting for CI
ci-lint:
	uv run ruff format --check
	uv run ruff check
	uv run python -m mypy $(sources)

.PHONY: ci  # Full CI pipeline
ci: ci-lint ci-test

# =============================================================================
# Utilities
# =============================================================================

.PHONY: clean  # Clean up generated files
clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage coverage.xml junit.xml
	rm -rf dist build *.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

.PHONY: clean-all  # Clean everything including Docker
clean-all: clean db-clean

.PHONY: all
all: format mypy lint typecheck test-all-python

.PHONY: help  # Show this help
help:
	@echo "Available targets:"
	@echo ""
	@echo "  Development:"
	@echo "    install          Install dependencies and pre-commit hooks"
	@echo "    format           Format code with ruff"
	@echo "    lint             Check code with ruff"
	@echo "    mypy             Run mypy type checker"
	@echo "    typecheck        Run pyright type checker"
	@echo ""
	@echo "  Testing:"
	@echo "    test             Run unit tests (no SurrealDB required)"
	@echo "    test-unit        Run unit tests only"
	@echo "    test-sdk         Run SDK tests only"
	@echo "    test-integration Run integration tests (starts SurrealDB)"
	@echo "    test-all         Run all tests including integration"
	@echo "    test-all-python  Run tests on Python 3.12-3.14"
	@echo ""
	@echo "  Docker/SurrealDB:"
	@echo "    db-up            Start SurrealDB (in-memory, port 8000)"
	@echo "    db-down          Stop SurrealDB container"
	@echo "    db-cluster       Start 3-node cluster (ports 8002-8004)"
	@echo "    db-logs          Show SurrealDB logs"
	@echo "    db-shell         Open SurrealDB SQL shell"
	@echo "    db-setup         Setup test database schema"
	@echo "    db-status        Show container status"
	@echo "    db-clean         Remove container"
	@echo ""
	@echo "  CI:"
	@echo "    ci               Full CI pipeline (lint + test)"
	@echo "    ci-test          Run tests for CI"
	@echo "    ci-lint          Run linting for CI"
	@echo ""
	@echo "  Other:"
	@echo "    clean            Clean generated files"
	@echo "    clean-all        Clean everything including Docker"
	@echo "    all              Run format, lint, typecheck, and tests"
	@echo "    help             Show this help"
